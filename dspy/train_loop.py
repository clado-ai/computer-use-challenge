"""
Iterative prompt optimization training loop with step + model curriculum.

Step curriculum: starts at target=2 steps, advances by 2 after clearing.
Model curriculum: cycles through models, resetting step target for each.
Turn budget scales from 30 to 300 with diminishing increases (power curve).

Usage:
    uv run dspy/train_loop.py                          # default 50 iterations
    uv run dspy/train_loop.py --max-iterations 20      # cap iterations
    uv run dspy/train_loop.py --initial-target 6       # start at 6 steps
"""

from __future__ import annotations

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

if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

RUNS_DIR = PROJECT_DIR / "runs"
SYSTEM_BASE_PATH = PROJECT_DIR / "src" / "prompts" / "SYSTEM_BASE.md"
PROMPT_HISTORY_DIR = SCRIPT_DIR / "prompt_history"
BROWSER_DATA_DIR = PROJECT_DIR / ".browser-data"

SUBPROCESS_TIMEOUT = 3600  # 60 minutes
MAX_STEPS = 30
STEP_INCREMENT = 3

# Model curriculum: each model runs through the full step curriculum (2→30),
# then we move to the next model and reset step target.
MODEL_CURRICULUM = [
    "anthropic/claude-opus-4.6",
    "openai/gpt-oss-120b",
]


# ---- turn budget ----

def compute_turn_budget(step_target: int) -> int:
    """Scale turn budget: 30 at target=2, 150 at target=30, diminishing increases.

    Uses power curve (exponent 0.85) so early levels get proportionally
    more turns per new step than later levels.
    """
    if step_target <= 2:
        return 30
    ratio = (step_target - 2) / (MAX_STEPS - 2)  # 0..1
    return int(30 + 120 * ratio ** 0.85)


# ---- prompt backup ----

def backup_prompt(iteration: int, model: str) -> Path:
    """Backup the current SYSTEM_BASE.md before overwriting."""
    PROMPT_HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    model_short = model.split("/")[-1]
    backup_path = PROMPT_HISTORY_DIR / f"SYSTEM_iter{iteration}_{model_short}_{ts}.md"
    if SYSTEM_BASE_PATH.exists():
        shutil.copy2(SYSTEM_BASE_PATH, backup_path)
        print(f"[train_loop] backed up prompt to {backup_path.name}")
    return backup_path


# ---- cleanup helpers ----

def _kill_process_tree(pid: int) -> None:
    """Kill a process and all its children via process group."""
    try:
        pgid = os.getpgid(pid)
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
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
    if BROWSER_DATA_DIR.exists():
        shutil.rmtree(BROWSER_DATA_DIR, ignore_errors=True)
    for d in globmod.glob(str(PROJECT_DIR / ".browser-data-*")):
        shutil.rmtree(d, ignore_errors=True)


# ---- agent runner ----

def run_agent(step_target: int, turn_budget: int, model: str) -> tuple[int, str]:
    """Run the agent subprocess with clean browser state."""
    _kill_orphaned_browsers()
    _clean_all_browser_dirs()

    print(f"\n{'='*60}")
    print(f"RUNNING AGENT: model={model}, target={step_target} steps, max_turns={turn_budget}")
    print(f"{'='*60}\n")

    env = {
        **os.environ,
        "MAX_TURNS": str(turn_budget),
        "MAX_STEPS": str(step_target),
        "HEADLESS": os.environ.get("HEADLESS", "true"),
        "SYSTEM_PROMPT_PATH": str(SYSTEM_BASE_PATH),
        "AGENT_MODEL": model,
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
            start_new_session=True,
        )
        stdout, stderr = proc.communicate(timeout=SUBPROCESS_TIMEOUT)
        rc = proc.returncode
        output = stdout + "\n" + stderr
    except subprocess.TimeoutExpired:
        if proc:
            _kill_process_tree(proc.pid)
            try:
                stdout, stderr = proc.communicate(timeout=5)
                output = f"TIMEOUT\n{stdout}\n{stderr}"
            except Exception:
                output = "TIMEOUT"
        rc = -1
    except Exception as e:
        rc = -1
        output = str(e)
    finally:
        if proc and proc.poll() is None:
            _kill_process_tree(proc.pid)
            try:
                proc.wait(timeout=5)
            except Exception:
                pass

    _kill_orphaned_browsers()
    _clean_all_browser_dirs()

    print(f"[agent] done (rc={rc})")
    print(output[-1000:])
    return rc, output


# ---- file discovery ----

def find_new_files(before: set[str]) -> tuple[Path | None, Path | None]:
    """Find new run_*.json and transcript_*.json created after `before` snapshot."""
    if not RUNS_DIR.exists():
        return None, None

    new_files = [f for f in RUNS_DIR.iterdir() if f.name not in before]
    run_file = None
    transcript_file = None

    for f in sorted(new_files, reverse=True):
        if f.name.startswith("run_") and f.suffix == ".json" and run_file is None:
            run_file = f
        elif f.name.startswith("transcript_") and f.suffix == ".json" and transcript_file is None:
            transcript_file = f

    return run_file, transcript_file


# ---- single model phase ----

def run_model_phase(
    model: str,
    max_iterations: int,
    initial_target: int,
    iteration_offset: int,
) -> int:
    """Run the step curriculum for a single model.

    Returns the number of iterations actually used.
    """
    step_target = initial_target
    iterations_used = 0

    print(f"\n{'*'*60}")
    print(f"* MODEL PHASE: {model}")
    print(f"* Starting at step target={initial_target}, max_iterations={max_iterations}")
    print(f"{'*'*60}")

    for i in range(max_iterations):
        global_iter = iteration_offset + i
        iterations_used = i + 1
        turn_budget = compute_turn_budget(step_target)

        print(f"\n{'#'*60}")
        print(f"# ITERATION {global_iter} | model={model} | target={step_target} steps | turns={turn_budget}")
        print(f"{'#'*60}")

        # 1. Backup current SYSTEM_BASE.md
        backup_prompt(global_iter, model)

        # 2. Snapshot runs/ before agent run
        files_before = set(f.name for f in RUNS_DIR.iterdir()) if RUNS_DIR.exists() else set()

        # 3. Run the agent
        rc, output = run_agent(step_target, turn_budget, model)

        # 4. Find new files from this run
        run_file, transcript_file = find_new_files(files_before)

        # 5. Read steps completed from run metrics
        steps_completed = 0
        if run_file:
            try:
                metrics = json.loads(run_file.read_text())
                steps_completed = metrics.get("stepsCompleted", 0)
            except Exception:
                pass

        print(f"[train_loop] completed {steps_completed}/{step_target} steps")

        # 6. Advance curriculum if target met
        if steps_completed >= step_target and step_target < MAX_STEPS:
            old_target = step_target
            step_target = min(step_target + STEP_INCREMENT, MAX_STEPS)
            print(f"[train_loop] ADVANCING: {old_target} -> {step_target} steps")

        # Check if model completed all 30 steps
        if step_target > MAX_STEPS:
            print(f"[train_loop] model {model} completed all {MAX_STEPS} steps!")
            break

        # 7. Optimize prompt if we have a transcript
        if not transcript_file:
            print("[train_loop] no transcript found, skipping optimization")
            continue

        print(f"[train_loop] using transcript: {transcript_file.name}")

        current_prompt = SYSTEM_BASE_PATH.read_text() if SYSTEM_BASE_PATH.exists() else ""

        try:
            from optimize import run_optimization
            result = run_optimization(transcript_file, current_prompt)
            optimized_prompt = result["optimized_prompt"]
        except Exception as e:
            print(f"[train_loop] optimization failed: {e}")
            import traceback
            traceback.print_exc()
            continue

        # 8. Write updated SYSTEM_BASE.md
        if optimized_prompt and optimized_prompt != current_prompt:
            SYSTEM_BASE_PATH.write_text(optimized_prompt)
            print(f"[train_loop] wrote new SYSTEM_BASE.md ({len(optimized_prompt)} chars)")
        else:
            print("[train_loop] no change to SYSTEM_BASE.md")

    return iterations_used


# ---- main ----

def main(max_iterations: int = 50, initial_target: int = 2) -> None:
    PROMPT_HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    RUNS_DIR.mkdir(parents=True, exist_ok=True)

    # Split iterations evenly across models
    iters_per_model = max_iterations // len(MODEL_CURRICULUM)
    remainder = max_iterations % len(MODEL_CURRICULUM)

    iteration_offset = 0

    for idx, model in enumerate(MODEL_CURRICULUM):
        # Give remainder iterations to the first model
        model_iters = iters_per_model + (remainder if idx == 0 else 0)
        if model_iters <= 0:
            continue

        used = run_model_phase(
            model=model,
            max_iterations=model_iters,
            initial_target=initial_target,
            iteration_offset=iteration_offset,
        )
        iteration_offset += used

    print(f"\n{'='*60}")
    print(f"TRAINING COMPLETE ({iteration_offset} total iterations across {len(MODEL_CURRICULUM)} models)")
    print(f"{'='*60}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Iterative prompt optimization loop")
    parser.add_argument(
        "--max-iterations", type=int, default=50,
        help="Total iterations across all models (default: 50)",
    )
    parser.add_argument(
        "--initial-target", type=int, default=2,
        help="Initial step target per model (default: 2)",
    )
    args = parser.parse_args()
    main(max_iterations=args.max_iterations, initial_target=args.initial_target)
