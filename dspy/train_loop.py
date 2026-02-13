"""
Iterative prompt optimization training loop.

Curriculum-based on turn budget (API calls): starts at 10 turns, optimizes
the prompt via GEPA after each run, advances the budget after consecutive
clears (agent completed >= 1 step).

Usage:
    uv run dspy/train_loop.py                          # start fresh
    uv run dspy/train_loop.py --resume                 # resume from saved state
    uv run dspy/train_loop.py --initial-window 20      # start at 20 turns
    uv run dspy/train_loop.py --max-iterations 20      # cap iterations
"""

from __future__ import annotations

import argparse
import json
import os
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

DEFAULT_INITIAL_WINDOW = 30     # starting turn budget
DEFAULT_MAX_ITERATIONS = 100
CONSECUTIVE_CLEARS_TO_ADVANCE = 2
WINDOW_INCREMENT = 10           # add 10 turns per advancement
MAX_WINDOW = 150                # max turn budget
SLIDING_WINDOW_SIZE = 3
SUBPROCESS_TIMEOUT = 600        # 10 minutes
MAX_CONSECUTIVE_FAILURES = 3    # stop after this many agent crashes in a row


# ---- state management ----

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return None


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


def make_fresh_state(initial_window: int) -> dict:
    return {
        "iteration": 0,
        "turn_window": initial_window,
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


# ---- agent runner ----

def run_agent(turn_window: int) -> tuple[int, str]:
    """Run the agent subprocess with MAX_TURNS set.

    Returns (return_code, stdout+stderr output).
    """
    # clean browser data for fresh state
    if BROWSER_DATA_DIR.exists():
        shutil.rmtree(BROWSER_DATA_DIR)

    env = {**os.environ, "MAX_TURNS": str(turn_window)}

    print(f"\n{'='*60}")
    print(f"RUNNING AGENT: MAX_TURNS={turn_window}")
    print(f"{'='*60}\n")

    try:
        result = subprocess.run(
            ["bun", "run", "src/index.ts"],
            cwd=str(PROJECT_DIR),
            env=env,
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT,
        )
        output = result.stdout + "\n" + result.stderr
        print(output[-2000:])  # tail of output
        return result.returncode, output
    except subprocess.TimeoutExpired:
        print(f"[train_loop] agent timed out after {SUBPROCESS_TIMEOUT}s")
        return -1, "TIMEOUT"
    except Exception as e:
        print(f"[train_loop] agent failed: {e}")
        return -1, str(e)


# ---- metrics parsing ----

def read_metrics(run_file: Path) -> dict:
    """Read and return run metrics."""
    return json.loads(run_file.read_text())


def extract_cost(metrics: dict) -> float:
    """Extract cost from run metrics."""
    return float(metrics.get("totalCost", 0))


# ---- curriculum ----

def check_curriculum(
    steps_completed: int,
    turn_window: int,
    consecutive_clears: int,
) -> tuple[int, int, bool]:
    """Check if we should advance the turn budget.

    A "clear" = agent completed >= 1 step within the turn budget.
    After CONSECUTIVE_CLEARS_TO_ADVANCE clears, increase turn budget.

    Returns (new_window, new_consecutive_clears, advanced).
    """
    cleared = steps_completed >= 1

    if cleared:
        consecutive_clears += 1
    else:
        consecutive_clears = 0

    advanced = False
    new_window = turn_window

    if consecutive_clears >= CONSECUTIVE_CLEARS_TO_ADVANCE:
        new_window = min(turn_window + WINDOW_INCREMENT, MAX_WINDOW)
        if new_window > turn_window:
            advanced = True
            consecutive_clears = 0  # reset after advancing

    return new_window, consecutive_clears, advanced


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

def run_iteration(state: dict) -> dict:
    """Run one full training iteration.

    Returns the updated state dict.
    """
    iteration = state["iteration"]
    turn_window = state["turn_window"]

    print(f"\n{'#'*60}")
    print(f"# ITERATION {iteration} | budget={turn_window} turns")
    print(f"# consecutive_clears={state['consecutive_clears']} | total_cost=${state['total_cost']:.2f}")
    print(f"{'#'*60}")

    # 1. read current prompt (before optimization)
    previous_prompt = ""
    if SYSTEM_PROMPT_PATH.exists():
        previous_prompt = SYSTEM_PROMPT_PATH.read_text()

    # 2. run the agent
    files_before = get_run_files_before(RUNS_DIR)
    return_code, agent_output = run_agent(turn_window)

    # 3. find new files
    run_file, transcript_file = find_new_files(RUNS_DIR, files_before)

    if not run_file:
        print("[train_loop] WARNING: no run file found, agent may have crashed")
        steps_completed = 0
        run_metrics = {"stepsCompleted": 0, "totalCost": 0, "totalApiCalls": 0, "totalToolCalls": 0}
        state["consecutive_failures"] = state.get("consecutive_failures", 0) + 1
    else:
        run_metrics = read_metrics(run_file)
        steps_completed = run_metrics.get("stepsCompleted", 0)
        state["consecutive_failures"] = 0  # reset on successful run

    agent_cost = extract_cost(run_metrics)
    turns_used = run_metrics.get("totalApiCalls", 0)
    tool_calls = run_metrics.get("totalToolCalls", 0)
    print(f"\n[train_loop] steps={steps_completed}, turns={turns_used}/{turn_window}, "
          f"tools={tool_calls}, cost=${agent_cost:.2f}")

    # 4. record in history
    history_entry = {
        "iteration": iteration,
        "run_file": str(run_file) if run_file else None,
        "transcript_file": str(transcript_file) if transcript_file else None,
        "steps_completed": steps_completed,
        "turn_window": turn_window,
        "turns_used": turns_used,
        "tool_calls": tool_calls,
    }
    state["trajectory_history"].append(history_entry)

    # 5. curriculum check
    new_window, new_consecutive, advanced = check_curriculum(
        steps_completed, turn_window, state["consecutive_clears"]
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

    # 8. backup and write new prompt (always save, even if unchanged)
    backup_prompt(SYSTEM_PROMPT_PATH, iteration)
    if optimized_prompt:
        SYSTEM_PROMPT_PATH.write_text(optimized_prompt)
        print(f"[train_loop] wrote new SYSTEM.md ({len(optimized_prompt)} chars)")
    else:
        print("[train_loop] no optimized prompt, keeping existing SYSTEM.md")

    # 9. save iteration JSON
    ITERATIONS_DIR.mkdir(parents=True, exist_ok=True)
    total_iter_cost = agent_cost + optimization_cost

    # read transcript data for the iteration JSON
    transcript_data = []
    if transcript_file and Path(transcript_file).exists():
        try:
            transcript_data = json.loads(Path(transcript_file).read_text())
        except Exception:
            pass

    iteration_data = {
        "iteration": iteration,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "turn_window": turn_window,
        "turns_used": turns_used,
        "tool_calls": tool_calls,
        "steps_completed": steps_completed,
        "run_metrics": run_metrics,
        "transcript": transcript_data,
        "rollout_analysis": rollout_analysis,
        "judge_feedback": judge_feedback,
        "previous_prompt": previous_prompt,
        "optimized_prompt": optimized_prompt,
        "curriculum": {
            "consecutive_clears": new_consecutive,
            "advanced": advanced,
            "next_window": new_window,
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
    state["turn_window"] = new_window
    state["consecutive_clears"] = new_consecutive
    state["total_cost"] = round(state["total_cost"] + total_iter_cost, 4)

    save_state(state)

    # summary
    print(f"\n--- iteration {iteration} summary ---")
    print(f"  steps: {steps_completed} | turns: {turns_used}/{turn_window} | tools: {tool_calls}")
    print(f"  cost: ${total_iter_cost:.2f} (agent=${agent_cost:.2f} + opt=${optimization_cost:.2f})")
    print(f"  curriculum: clears={new_consecutive}, advanced={advanced}, next_window={new_window}")
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
        "--initial-window", type=int, default=DEFAULT_INITIAL_WINDOW,
        help=f"Initial turn budget (default: {DEFAULT_INITIAL_WINDOW})",
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
            state = make_fresh_state(args.initial_window)
        else:
            print(f"[train_loop] resuming from iteration {state['iteration']}, "
                  f"window={state['turn_window']} turns, cost=${state['total_cost']:.2f}")
    else:
        state = make_fresh_state(args.initial_window)

    # ensure directories
    ITERATIONS_DIR.mkdir(parents=True, exist_ok=True)
    PROMPT_HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    RUNS_DIR.mkdir(parents=True, exist_ok=True)

    print(f"[train_loop] starting: window={state['turn_window']} turns, "
          f"max_iter={args.max_iterations}")

    while state["iteration"] < args.max_iterations:
        # window cap
        if state["turn_window"] > MAX_WINDOW:
            print(f"\n[train_loop] MAX WINDOW reached: {state['turn_window']} turns")
            break

        # consecutive failure guard
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
    print(f"  final window: {state['turn_window']} turns")
    print(f"  total cost: ${state['total_cost']:.2f}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
