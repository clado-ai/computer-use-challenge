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

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent

if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

RUNS_DIR = PROJECT_DIR / "runs"
SYSTEM_PATH = PROJECT_DIR / "src" / "prompts" / "SYSTEM_BASE.md"
PROMPT_HISTORY_DIR = SCRIPT_DIR / "prompt_history"
BROWSER_DATA_DIR = PROJECT_DIR / ".browser-data"

SUBPROCESS_TIMEOUT = 3600
MAX_STEPS = 30
TURN_BUDGET = 150
AGENT_MODEL = "openai/gpt-oss-120b"

def backup_prompt(iteration: int) -> Path:
    """Backup the current SYSTEM_BASE.md before overwriting."""
    PROMPT_HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = PROMPT_HISTORY_DIR / f"SYSTEM_iter{iteration}_{ts}.md"
    if SYSTEM_PATH.exists():
        shutil.copy2(SYSTEM_PATH, backup_path)
        print(f"[train_loop] backed up prompt to {backup_path.name}")
    return backup_path

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


def _cleanup_browser() -> None:
    """Kill orphaned browser processes and remove browser data directories."""
    for pattern in ["chromium.*browser-data", "chrome.*browser-data"]:
        try:
            subprocess.run(["pkill", "-f", pattern], capture_output=True, timeout=5)
        except Exception:
            pass
    if BROWSER_DATA_DIR.exists():
        shutil.rmtree(BROWSER_DATA_DIR, ignore_errors=True)
    for d in globmod.glob(str(PROJECT_DIR / ".browser-data-*")):
        shutil.rmtree(d, ignore_errors=True)

def run_agent() -> tuple[int, str]:
    """Run the agent subprocess with clean browser state."""
    _cleanup_browser()

    print(f"\n{'='*60}")
    print(f"RUNNING AGENT: model={AGENT_MODEL}, target={MAX_STEPS} steps, max_turns={TURN_BUDGET}")
    print(f"{'='*60}\n")

    env = {
        **os.environ,
        "MAX_TURNS": str(TURN_BUDGET),
        "MAX_STEPS": str(MAX_STEPS),
        "HEADLESS": os.environ.get("HEADLESS", "true"),
        "SYSTEM_PROMPT_PATH": str(SYSTEM_PATH),
        "AGENT_MODEL": AGENT_MODEL,
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

    _cleanup_browser()

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


# ---- main ----

def main(max_iterations: int = 100) -> None:
    PROMPT_HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    RUNS_DIR.mkdir(parents=True, exist_ok=True)

    for i in range(max_iterations):
        print(f"\n{'#'*60}")
        print(f"# ITERATION {i} | model={AGENT_MODEL} | target={MAX_STEPS} steps | turns={TURN_BUDGET}")
        print(f"{'#'*60}")

        # 1. Backup current SYSTEM_BASE.md
        backup_prompt(i)

        # 2. Snapshot runs/ before agent run
        files_before = set(f.name for f in RUNS_DIR.iterdir()) if RUNS_DIR.exists() else set()

        # 3. Run the agent
        rc, output = run_agent()

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

        print(f"[train_loop] completed {steps_completed}/{MAX_STEPS} steps")

        # 6. Optimize prompt if we have a transcript
        if not transcript_file:
            print("[train_loop] no transcript found, skipping optimization")
            continue

        print(f"[train_loop] using transcript: {transcript_file.name}")

        current_prompt = SYSTEM_PATH.read_text() if SYSTEM_PATH.exists() else ""
        optimized_prompt = None

        trajectory_count = len(list(RUNS_DIR.glob("trajectory_*.json")))
        try:
            from optimize import run_optimization
            print(f"[train_loop] running DSPy GEPA optimization ({trajectory_count} trajectories)...")
            result = run_optimization(RUNS_DIR, current_prompt)
            optimized_prompt = result["optimized_prompt"]
            print(f"[train_loop] DSPy GEPA done (stats: {result.get('stats', {})})")
        except Exception as e:
            print(f"[train_loop] DSPy optimization failed: {e}")
            import traceback
            traceback.print_exc()
            continue

        # 7. Write updated SYSTEM_BASE.md
        if optimized_prompt and optimized_prompt != current_prompt:
            SYSTEM_PATH.write_text(optimized_prompt)
            print(f"[train_loop] wrote new SYSTEM_BASE.md ({len(optimized_prompt)} chars)")
        else:
            print("[train_loop] no change to SYSTEM_BASE.md")

    print(f"\n{'='*60}")
    print(f"TRAINING COMPLETE ({max_iterations} iterations)")
    print(f"{'='*60}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Iterative prompt optimization loop")
    parser.add_argument(
        "--max-iterations", type=int, default=100,
        help="Total iterations (default: 100)",
    )
    args = parser.parse_args()
    main(max_iterations=args.max_iterations)
