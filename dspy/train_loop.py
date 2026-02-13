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
SLIDING_WINDOW_SIZE = 5         # more history for GEPA
SUBPROCESS_TIMEOUT = 600        # 10 minutes
MAX_CONSECUTIVE_FAILURES = 3    # stop after this many agent crashes in a row
DEFAULT_PARALLEL_AGENTS = 3     # run N agents concurrently per iteration


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

def _run_single_agent(turn_window: int, agent_id: int) -> tuple[int, str]:
    """Run one agent subprocess with its own browser data dir."""
    browser_dir = PROJECT_DIR / f".browser-data-{agent_id}"
    if browser_dir.exists():
        shutil.rmtree(browser_dir)

    env = {
        **os.environ,
        "MAX_TURNS": str(turn_window),
        "BROWSER_DATA_DIR": str(browser_dir),
    }

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
        return result.returncode, output
    except subprocess.TimeoutExpired:
        return -1, "TIMEOUT"
    except Exception as e:
        return -1, str(e)
    finally:
        # cleanup browser data
        if browser_dir.exists():
            shutil.rmtree(browser_dir, ignore_errors=True)


def run_agents_parallel(turn_window: int, n_agents: int) -> list[tuple[int, str]]:
    """Run N agents concurrently, each with isolated browser state."""
    import concurrent.futures

    # also clean the default browser data dir
    if BROWSER_DATA_DIR.exists():
        shutil.rmtree(BROWSER_DATA_DIR)

    print(f"\n{'='*60}")
    print(f"RUNNING {n_agents} AGENTS: MAX_TURNS={turn_window}")
    print(f"{'='*60}\n")

    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=n_agents) as executor:
        futures = {
            executor.submit(_run_single_agent, turn_window, i): i
            for i in range(n_agents)
        }
        for future in concurrent.futures.as_completed(futures):
            agent_id = futures[future]
            rc, output = future.result()
            print(f"[agent-{agent_id}] done (rc={rc})")
            print(output[-1000:])
            results.append((rc, output))

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

def run_iteration(state: dict, n_agents: int = DEFAULT_PARALLEL_AGENTS) -> dict:
    """Run one full training iteration with N parallel agents.

    Returns the updated state dict.
    """
    iteration = state["iteration"]
    turn_window = state["turn_window"]

    print(f"\n{'#'*60}")
    print(f"# ITERATION {iteration} | budget={turn_window} turns | {n_agents} agents")
    print(f"# consecutive_clears={state['consecutive_clears']} | total_cost=${state['total_cost']:.2f}")
    print(f"{'#'*60}")

    # 1. read current prompt (before optimization)
    previous_prompt = ""
    if SYSTEM_PROMPT_PATH.exists():
        previous_prompt = SYSTEM_PROMPT_PATH.read_text()

    # 2. run N agents in parallel
    files_before = get_run_files_before(RUNS_DIR)
    agent_results = run_agents_parallel(turn_window, n_agents)

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
            "turn_window": turn_window,
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
            "turn_window": turn_window,
            "turns_used": 0,
            "tool_calls": 0,
        })

    # 5. curriculum check (based on best agent)
    new_window, new_consecutive, advanced = check_curriculum(
        best_steps, turn_window, state["consecutive_clears"]
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
    total_iter_cost = total_agent_cost + optimization_cost

    iteration_data = {
        "iteration": iteration,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "turn_window": turn_window,
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
            "next_window": new_window,
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
    state["turn_window"] = new_window
    state["consecutive_clears"] = new_consecutive
    state["total_cost"] = round(state["total_cost"] + total_iter_cost, 4)

    save_state(state)

    # summary
    print(f"\n--- iteration {iteration} summary ---")
    print(f"  agents: {len(new_run_files)} | best_steps: {best_steps} | turns: {total_turns}/{turn_window} | tools: {total_tools}")
    print(f"  cost: ${total_iter_cost:.2f} (agent=${total_agent_cost:.2f} + opt=${optimization_cost:.2f})")
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
    print(f"  final window: {state['turn_window']} turns")
    print(f"  total cost: ${state['total_cost']:.2f}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
