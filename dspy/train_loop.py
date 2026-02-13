"""
Iterative prompt optimization training loop.

Step-based curriculum: starts with a target of 2 steps, optimizes the prompt
via GEPA after each batch of parallel agent runs. After 2 consecutive clears
(best agent hits the step target), the target increases by 2. Turn budget
scales with the step target (10 turns per target step, min 30).

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
MAX_TURNS = 300                 # safety cap, agent stops on step target not turns
SLIDING_WINDOW_SIZE = 5         # history for GEPA
SUBPROCESS_TIMEOUT = 3600       # 60 minutes
MAX_CONSECUTIVE_FAILURES = 3    # stop after this many agent crashes in a row
DEFAULT_PARALLEL_AGENTS = 3     # run N agents concurrently per iteration


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

def _run_single_agent(step_target: int, agent_id: int) -> tuple[int, str]:
    """Run one agent subprocess with its own browser data dir.

    Uses process groups so we can kill Chromium grandchild processes on
    timeout or error.
    """
    browser_dir = PROJECT_DIR / f".browser-data-{agent_id}"
    if browser_dir.exists():
        shutil.rmtree(browser_dir, ignore_errors=True)

    env = {
        **os.environ,
        "MAX_TURNS": str(MAX_TURNS),
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


def run_agents_parallel(step_target: int, n_agents: int) -> list[tuple[int, str]]:
    """Run N agents concurrently, each with isolated browser state."""
    import concurrent.futures

    # clean slate: kill orphans and remove all browser data dirs
    _kill_orphaned_browsers()
    _clean_all_browser_dirs()

    print(f"\n{'='*60}")
    print(f"RUNNING {n_agents} AGENTS: target={step_target} steps, max_turns={MAX_TURNS}")
    print(f"{'='*60}\n")

    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=n_agents) as executor:
        futures = {
            executor.submit(_run_single_agent, step_target, i): i
            for i in range(n_agents)
        }
        for future in concurrent.futures.as_completed(futures):
            agent_id = futures[future]
            rc, output = future.result()
            print(f"[agent-{agent_id}] done (rc={rc})")
            print(output[-1000:])
            results.append((rc, output))

    # post-run cleanup: kill any lingering browsers, remove dirs
    _kill_orphaned_browsers()
    _clean_all_browser_dirs()

    return results


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


# ---- iteration ----

def run_iteration(state: dict, n_agents: int = DEFAULT_PARALLEL_AGENTS) -> dict:
    """Run one full training iteration with N parallel agents.

    Returns the updated state dict.
    """
    iteration = state["iteration"]
    step_target = state["step_target"]

    print(f"\n{'#'*60}")
    print(f"# ITERATION {iteration} | target={step_target} steps | {n_agents} agents")
    print(f"# consecutive_clears={state['consecutive_clears']} | total_cost=${state['total_cost']:.2f}")
    print(f"{'#'*60}")

    # 1. read current prompt (before optimization)
    previous_prompt = ""
    if SYSTEM_PROMPT_PATH.exists():
        previous_prompt = SYSTEM_PROMPT_PATH.read_text()

    # 2. run N agents in parallel
    files_before = get_run_files_before(RUNS_DIR)
    agent_results = run_agents_parallel(step_target, n_agents)

    # 3. find ALL new files (multiple run/transcript pairs)
    new_run_files = []
    new_transcript_files = []
    if RUNS_DIR.exists():
        for f in sorted(RUNS_DIR.iterdir(), reverse=True):
            if f.name not in files_before:
                if f.name.startswith("run_") and f.suffix == ".json":
                    new_run_files.append(f)
                elif f.name.startswith("transcript_") and f.suffix == ".json":
                    new_transcript_files.append(f)

    if not new_run_files:
        print("[train_loop] WARNING: no run files found, agents may have crashed")
        state["consecutive_failures"] = state.get("consecutive_failures", 0) + 1
    else:
        state["consecutive_failures"] = 0

    # aggregate metrics across all agents
    total_agent_cost = 0.0
    total_turns = 0
    total_tools = 0
    best_steps = 0
    all_run_metrics = []

    for rf in new_run_files:
        m = read_metrics(rf)
        all_run_metrics.append(m)
        total_agent_cost += extract_cost(m)
        total_turns += m.get("totalApiCalls", 0)
        total_tools += m.get("totalToolCalls", 0)
        best_steps = max(best_steps, m.get("stepsCompleted", 0))

    print(f"\n[train_loop] {len(new_run_files)} agents completed:")
    for i, m in enumerate(all_run_metrics):
        print(f"  agent-{i}: steps={m.get('stepsCompleted', 0)}, "
              f"turns={m.get('totalApiCalls', 0)}, cost=${extract_cost(m):.2f}")
    print(f"  best_steps={best_steps}, total_cost=${total_agent_cost:.2f}")

    # 4. record ALL runs in history
    for rf, tf in zip(new_run_files, new_transcript_files):
        m = read_metrics(rf)
        history_entry = {
            "iteration": iteration,
            "run_file": str(rf),
            "transcript_file": str(tf),
            "steps_completed": m.get("stepsCompleted", 0),
            "step_target": step_target,
            "turns_used": m.get("totalApiCalls", 0),
            "tool_calls": m.get("totalToolCalls", 0),
        }
        state["trajectory_history"].append(history_entry)

    # handle case where no files were produced
    if not new_run_files:
        state["trajectory_history"].append({
            "iteration": iteration,
            "run_file": None,
            "transcript_file": None,
            "steps_completed": 0,
            "step_target": step_target,
            "turns_used": 0,
            "tool_calls": 0,
        })

    # 5. curriculum check (based on best agent)
    new_target, new_consecutive, advanced = check_curriculum(
        best_steps, step_target, state["consecutive_clears"]
    )

    # 6. get sliding window of trajectories for optimization
    transcript_files, metric_files = get_sliding_window(state["trajectory_history"])

    # 7. run GEPA optimization
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
            optimization_cost = 2.0  # conservative estimate
        except Exception as e:
            print(f"[train_loop] optimization failed: {e}")
            import traceback
            traceback.print_exc()
    else:
        print("[train_loop] no transcripts available, skipping optimization")

    # 8. backup and write new prompt (with safety check)
    backup_prompt(SYSTEM_PROMPT_PATH, iteration)
    # reject meta-prompts that tell the agent to "generate a prompt" instead of act
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

    # 9. save iteration JSON
    ITERATIONS_DIR.mkdir(parents=True, exist_ok=True)
    total_iter_cost = total_agent_cost + optimization_cost

    iteration_data = {
        "iteration": iteration,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "step_target": step_target,
        "n_agents": len(new_run_files),
        "best_steps": best_steps,
        "all_run_metrics": all_run_metrics,
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
            "agent_cost": round(total_agent_cost, 4),
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
    print(f"  target: {step_target} steps | best: {best_steps}/{step_target} | turns: {total_turns} | tools: {total_tools}")
    print(f"  cost: ${total_iter_cost:.2f} (agent=${total_agent_cost:.2f} + opt=${optimization_cost:.2f})")
    print(f"  curriculum: clears={new_consecutive}, advanced={advanced}, next_target={new_target}")
    print(f"  total_cost: ${state['total_cost']:.2f}")

    return state


# ---- main loop ----

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
    parser.add_argument(
        "--parallel-agents", type=int, default=DEFAULT_PARALLEL_AGENTS,
        help=f"Number of parallel agents per iteration (default: {DEFAULT_PARALLEL_AGENTS})",
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

    # ensure directories
    ITERATIONS_DIR.mkdir(parents=True, exist_ok=True)
    PROMPT_HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    RUNS_DIR.mkdir(parents=True, exist_ok=True)

    print(f"[train_loop] starting: target={state['step_target']} steps, "
          f"max_iter={args.max_iterations}")

    while state["iteration"] < args.max_iterations:
        # done if target reached 30
        if state["step_target"] > MAX_STEPS:
            print(f"\n[train_loop] ALL 30 STEPS mastered!")
            break

        # consecutive failure guard
        if state.get("consecutive_failures", 0) >= MAX_CONSECUTIVE_FAILURES:
            print(f"\n[train_loop] STOPPING: {MAX_CONSECUTIVE_FAILURES} consecutive agent failures")
            break

        try:
            state = run_iteration(state, n_agents=args.parallel_agents)
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
