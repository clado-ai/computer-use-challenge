"""
Iterative prompt optimization training loop.

Step-based curriculum: starts with a target of 2 steps, optimizes the prompt
via GEPA after each agent run. After 2 consecutive clears (agent hits the
step target), the target increases by 2.

Usage:
    uv run dspy/train_loop.py                          # start fresh
    uv run dspy/train_loop.py --resume                 # resume from saved state
    uv run dspy/train_loop.py --initial-target 2       # start at 2 steps
    uv run dspy/train_loop.py --max-iterations 20      # cap iterations
"""

from __future__ import annotations

import argparse
import glob as globmod
import json
import os
import signal
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# ---- paths ----

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent  # computer-use-challenge/

# ensure optimize.py is importable
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

RUNS_DIR = PROJECT_DIR / "runs"
SYSTEM_PROMPT_PATH = PROJECT_DIR / "src" / "prompts" / "SYSTEM.md"
ITERATIONS_DIR = SCRIPT_DIR / "iterations"
PROMPT_HISTORY_DIR = SCRIPT_DIR / "prompt_history"
STATE_FILE = SCRIPT_DIR / "training_state.json"
BROWSER_DATA_DIR = PROJECT_DIR / ".browser-data"

# ---- defaults ----

DEFAULT_INITIAL_TARGET = 2      # start by trying to clear 2 steps
DEFAULT_MAX_ITERATIONS = 100
CONSECUTIVE_CLEARS_TO_ADVANCE = 2
STEP_INCREMENT = 2              # add 2 steps per advancement
MAX_STEPS = 30                  # final goal
TURNS_PER_STEP = 25             # budget: 25 turns per target step
MIN_TURNS = 30                  # minimum turn budget
MAX_TURNS_CAP = 300             # absolute cap
SLIDING_WINDOW_SIZE = 5         # history for GEPA
SUBPROCESS_TIMEOUT = 3600       # 60 minutes
MAX_CONSECUTIVE_FAILURES = 3    # stop after this many agent crashes in a row


# ---- state management ----

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return None


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


def make_fresh_state(initial_target: int) -> dict:
    return {
        "iteration": 0,
        "step_target": initial_target,
        "consecutive_clears": 0,
        "consecutive_failures": 0,
        "trajectory_history": [],
        "total_cost": 0.0,
    }


# ---- file discovery ----

def get_run_files_before(runs_dir: Path) -> set[str]:
    """Get set of filenames in runs/ before an agent run."""
    if not runs_dir.exists():
        return set()
    return set(f.name for f in runs_dir.iterdir())


def find_new_files(runs_dir: Path, before: set[str]) -> tuple[Path | None, Path | None]:
    """Find new run_*.json and transcript_*.json after an agent run."""
    if not runs_dir.exists():
        return None, None

    new_files = [f for f in runs_dir.iterdir() if f.name not in before]
    run_file = None
    transcript_file = None

    for f in sorted(new_files, reverse=True):
        if f.name.startswith("run_") and f.suffix == ".json" and run_file is None:
            run_file = f
        elif f.name.startswith("transcript_") and f.suffix == ".json" and transcript_file is None:
            transcript_file = f

    return run_file, transcript_file


# ---- prompt backup ----

def backup_prompt(prompt_path: Path, iteration: int) -> Path:
    """Backup the current SYSTEM.md before overwriting."""
    PROMPT_HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = PROMPT_HISTORY_DIR / f"SYSTEM_iter{iteration}_{ts}.md"
    if prompt_path.exists():
        shutil.copy2(prompt_path, backup_path)
    return backup_path


# ---- cleanup helpers ----

def _kill_process_tree(pid: int) -> None:
    """Kill a process and all its children via process group."""
    try:
        pgid = os.getpgid(pid)
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    # give processes a moment to exit gracefully
    import time
    time.sleep(2)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _kill_orphaned_browsers() -> None:
    """Kill any orphaned Chromium/chromium processes from previous agent runs."""
    for pattern in ["chromium.*browser-data", "chrome.*browser-data"]:
        try:
            subprocess.run(
                ["pkill", "-f", pattern],
                capture_output=True, timeout=5,
            )
        except Exception:
            pass


def _clean_all_browser_dirs() -> None:
    """Remove all .browser-data* directories for a clean slate."""
    # default dir
    if BROWSER_DATA_DIR.exists():
        shutil.rmtree(BROWSER_DATA_DIR, ignore_errors=True)
    # numbered dirs from parallel agents
    for d in globmod.glob(str(PROJECT_DIR / ".browser-data-*")):
        shutil.rmtree(d, ignore_errors=True)


# ---- agent runner ----

def _run_single_agent(step_target: int, agent_id: int, turn_budget: int = 50) -> tuple[int, str]:
    """Run one agent subprocess with its own browser data dir.

    Uses process groups so we can kill Chromium grandchild processes on
    timeout or error.
    """
    browser_dir = PROJECT_DIR / f".browser-data-{agent_id}"
    if browser_dir.exists():
        shutil.rmtree(browser_dir, ignore_errors=True)

    env = {
        **os.environ,
        "MAX_TURNS": str(turn_budget),
        "MAX_STEPS": str(step_target),
        "BROWSER_DATA_DIR": str(browser_dir),
    }

    proc = None
    try:
        proc = subprocess.Popen(
            ["bun", "run", "src/index.ts"],
            cwd=str(PROJECT_DIR),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,  # new process group for clean kill
        )
        stdout, stderr = proc.communicate(timeout=SUBPROCESS_TIMEOUT)
        return proc.returncode, stdout + "\n" + stderr
    except subprocess.TimeoutExpired:
        if proc:
            _kill_process_tree(proc.pid)
            # drain any buffered output
            try:
                stdout, stderr = proc.communicate(timeout=5)
                return -1, f"TIMEOUT\n{stdout}\n{stderr}"
            except Exception:
                pass
        return -1, "TIMEOUT"
    except Exception as e:
        return -1, str(e)
    finally:
        # ensure process is dead
        if proc and proc.poll() is None:
            _kill_process_tree(proc.pid)
            try:
                proc.wait(timeout=5)
            except Exception:
                pass
        # cleanup browser data
        if browser_dir.exists():
            shutil.rmtree(browser_dir, ignore_errors=True)


def _compute_turn_budget(step_target: int) -> int:
    """Scale turn budget with step target."""
    return max(MIN_TURNS, min(step_target * TURNS_PER_STEP, MAX_TURNS_CAP))


def run_agent(step_target: int) -> tuple[int, str]:
    """Run a single agent with clean browser state."""
    _kill_orphaned_browsers()
    _clean_all_browser_dirs()

    turn_budget = _compute_turn_budget(step_target)
    print(f"\n{'='*60}")
    print(f"RUNNING AGENT: target={step_target} steps, max_turns={turn_budget}")
    print(f"{'='*60}\n")

    rc, output = _run_single_agent(step_target, 0, turn_budget=turn_budget)
    print(f"[agent] done (rc={rc})")
    print(output[-1000:])

    _kill_orphaned_browsers()
    _clean_all_browser_dirs()

    return rc, output


# ---- metrics parsing ----

def read_metrics(run_file: Path) -> dict:
    """Read and return run metrics."""
    return json.loads(run_file.read_text())


def extract_cost(metrics: dict) -> float:
    """Extract cost from run metrics."""
    return float(metrics.get("totalCost", 0))


# ---- curriculum ----

def check_curriculum(
    best_steps: int,
    step_target: int,
    consecutive_clears: int,
) -> tuple[int, int, bool]:
    """Check if we should advance the step target.

    A "clear" = best agent completed >= step_target steps.
    After CONSECUTIVE_CLEARS_TO_ADVANCE clears, increase step target.

    Returns (new_step_target, new_consecutive_clears, advanced).
    """
    cleared = best_steps >= step_target

    if cleared:
        consecutive_clears += 1
    else:
        consecutive_clears = 0

    advanced = False
    new_target = step_target

    if consecutive_clears >= CONSECUTIVE_CLEARS_TO_ADVANCE:
        new_target = min(step_target + STEP_INCREMENT, MAX_STEPS)
        if new_target > step_target:
            advanced = True
            consecutive_clears = 0  # reset after advancing

    return new_target, consecutive_clears, advanced


# ---- sliding window ----

def get_sliding_window(history: list[dict], size: int = SLIDING_WINDOW_SIZE) -> tuple[list[str], list[str]]:
    """Get the last N trajectory/metric file paths from history.

    Returns (transcript_files, metric_files).
    """
    recent = history[-size:] if len(history) > size else history

    transcript_files = []
    metric_files = []

    for entry in recent:
        tf = entry.get("transcript_file")
        rf = entry.get("run_file")
        if tf and Path(tf).exists():
            transcript_files.append(tf)
        if rf and Path(rf).exists():
            metric_files.append(rf)

    return transcript_files, metric_files

def run_iteration(state: dict) -> dict:
    """Run one full training iteration with a single agent.

    Returns the updated state dict.
    """
    iteration = state["iteration"]
    step_target = state["step_target"]

    print(f"\n{'#'*60}")
    print(f"# ITERATION {iteration} | target={step_target} steps")
    print(f"# consecutive_clears={state['consecutive_clears']} | total_cost=${state['total_cost']:.2f}")
    print(f"{'#'*60}")

    previous_prompt = ""
    if SYSTEM_PROMPT_PATH.exists():
        previous_prompt = SYSTEM_PROMPT_PATH.read_text()

    files_before = get_run_files_before(RUNS_DIR)
    rc, output = run_agent(step_target)

    run_file, transcript_file = find_new_files(RUNS_DIR, files_before)

    if not run_file:
        print("[train_loop] WARNING: no run file found, agent may have crashed")
        state["consecutive_failures"] = state.get("consecutive_failures", 0) + 1
    else:
        state["consecutive_failures"] = 0

    agent_cost = 0.0
    agent_turns = 0
    agent_tools = 0
    steps_completed = 0
    run_metrics = {}

    if run_file:
        run_metrics = read_metrics(run_file)
        agent_cost = extract_cost(run_metrics)
        agent_turns = run_metrics.get("totalApiCalls", 0)
        agent_tools = run_metrics.get("totalToolCalls", 0)
        steps_completed = run_metrics.get("stepsCompleted", 0)

    print(f"\n[train_loop] agent completed: steps={steps_completed}, "
          f"turns={agent_turns}, cost=${agent_cost:.2f}")

    history_entry = {
        "iteration": iteration,
        "run_file": str(run_file) if run_file else None,
        "transcript_file": str(transcript_file) if transcript_file else None,
        "steps_completed": steps_completed,
        "step_target": step_target,
        "turns_used": agent_turns,
        "tool_calls": agent_tools,
    }
    state["trajectory_history"].append(history_entry)

    new_target, new_consecutive, advanced = check_curriculum(
        steps_completed, step_target, state["consecutive_clears"]
    )

    transcript_files, metric_files = get_sliding_window(state["trajectory_history"])

    optimization_cost = 0.0
    optimized_prompt = previous_prompt
    judge_feedback = []
    rollout_analysis = ""

    if transcript_files:
        print(f"\n[train_loop] optimizing with {len(transcript_files)} transcripts...")
        try:
            from optimize import run_optimization
            opt_result = run_optimization(
                transcript_files=transcript_files,
                metric_files=metric_files,
            )
            optimized_prompt = opt_result["optimized_prompt"]
            judge_feedback = opt_result["judge_feedback"]
            rollout_analysis = opt_result["rollout_analysis"]
            optimization_cost = 2.0
        except Exception as e:
            print(f"[train_loop] optimization failed: {e}")
            import traceback
            traceback.print_exc()
    else:
        print("[train_loop] no transcripts available, skipping optimization")

    backup_prompt(SYSTEM_PROMPT_PATH, iteration)

    bad_patterns = ["generate an optimized", "output only the optimized", "format: output"]
    is_meta_prompt = any(p in optimized_prompt.lower()[:200] for p in bad_patterns)
    if is_meta_prompt:
        print(f"[train_loop] WARNING: GEPA returned a meta-prompt, not an agent prompt. Keeping previous SYSTEM.md.")
        optimized_prompt = previous_prompt
    if optimized_prompt and optimized_prompt != previous_prompt:
        SYSTEM_PROMPT_PATH.write_text(optimized_prompt)
        print(f"[train_loop] wrote new SYSTEM.md ({len(optimized_prompt)} chars)")
    else:
        print("[train_loop] keeping existing SYSTEM.md")

    ITERATIONS_DIR.mkdir(parents=True, exist_ok=True)
    total_iter_cost = agent_cost + optimization_cost

    iteration_data = {
        "iteration": iteration,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "step_target": step_target,
        "steps_completed": steps_completed,
        "run_metrics": run_metrics,
        "rollout_analysis": rollout_analysis,
        "judge_feedback": judge_feedback,
        "previous_prompt": previous_prompt,
        "optimized_prompt": optimized_prompt,
        "curriculum": {
            "consecutive_clears": new_consecutive,
            "advanced": advanced,
            "next_target": new_target,
        },
        "cost": {
            "agent_cost": round(agent_cost, 4),
            "optimization_cost": round(optimization_cost, 4),
            "total": round(total_iter_cost, 4),
        },
    }

    iter_path = ITERATIONS_DIR / f"iteration_{iteration}.json"
    iter_path.write_text(json.dumps(iteration_data, indent=2, default=str))
    print(f"[train_loop] saved {iter_path}")

    # 10. update state
    state["iteration"] = iteration + 1
    state["step_target"] = new_target
    state["consecutive_clears"] = new_consecutive
    state["total_cost"] = round(state["total_cost"] + total_iter_cost, 4)

    save_state(state)

    # summary
    print(f"\n--- iteration {iteration} summary ---")
    print(f"  target: {step_target} steps | completed: {steps_completed}/{step_target} | turns: {agent_turns} | tools: {agent_tools}")
    print(f"  cost: ${total_iter_cost:.2f} (agent=${agent_cost:.2f} + opt=${optimization_cost:.2f})")
    print(f"  curriculum: clears={new_consecutive}, advanced={advanced}, next_target={new_target}")
    print(f"  total_cost: ${state['total_cost']:.2f}")

    return state

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Iterative prompt optimization training loop"
    )
    parser.add_argument(
        "--resume", action="store_true",
        help="Resume from saved training state",
    )
    parser.add_argument(
        "--initial-target", type=int, default=DEFAULT_INITIAL_TARGET,
        help=f"Initial step target (default: {DEFAULT_INITIAL_TARGET})",
    )
    parser.add_argument(
        "--max-iterations", type=int, default=DEFAULT_MAX_ITERATIONS,
        help=f"Maximum number of iterations (default: {DEFAULT_MAX_ITERATIONS})",
    )
    args = parser.parse_args()

    # load or create state
    if args.resume:
        state = load_state()
        if state is None:
            print("[train_loop] no saved state found, starting fresh")
            state = make_fresh_state(args.initial_target)
        else:
            print(f"[train_loop] resuming from iteration {state['iteration']}, "
                  f"target={state['step_target']} steps, cost=${state['total_cost']:.2f}")
    else:
        state = make_fresh_state(args.initial_target)

    ITERATIONS_DIR.mkdir(parents=True, exist_ok=True)
    PROMPT_HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    RUNS_DIR.mkdir(parents=True, exist_ok=True)

    print(f"[train_loop] starting: target={state['step_target']} steps, "
          f"max_iter={args.max_iterations}")

    while state["iteration"] < args.max_iterations:
        if state["step_target"] > MAX_STEPS:
            print(f"\n[train_loop] ALL 30 STEPS mastered!")
            break

        if state.get("consecutive_failures", 0) >= MAX_CONSECUTIVE_FAILURES:
            print(f"\n[train_loop] STOPPING: {MAX_CONSECUTIVE_FAILURES} consecutive agent failures")
            break

        try:
            state = run_iteration(state)
        except KeyboardInterrupt:
            print("\n[train_loop] interrupted, saving state...")
            save_state(state)
            sys.exit(1)
        except Exception as e:
            print(f"\n[train_loop] iteration failed: {e}")
            import traceback
            traceback.print_exc()
            save_state(state)
            state["iteration"] += 1
            state["consecutive_failures"] = state.get("consecutive_failures", 0) + 1

    print(f"\n{'='*60}")
    print(f"TRAINING COMPLETE")
    print(f"  iterations: {state['iteration']}")
    print(f"  final target: {state['step_target']} steps")
    print(f"  total cost: ${state['total_cost']:.2f}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
