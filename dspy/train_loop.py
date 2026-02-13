"""
Iterative prompt optimization training loop.

Simple loop: run agent → analyze transcript → LLM improves prompt → repeat.

Usage:
    uv run dspy/train_loop.py                          # default 10 iterations
    uv run dspy/train_loop.py --max-iterations 5       # cap iterations
"""

from __future__ import annotations

import glob as globmod
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
SYSTEM_PROMPT_PATH = PROJECT_DIR / "src" / "prompts" / "SYSTEM.md"
PROMPT_HISTORY_DIR = SCRIPT_DIR / "prompt_history"
BROWSER_DATA_DIR = PROJECT_DIR / ".browser-data"

SUBPROCESS_TIMEOUT = 3600  # 60 minutes


# ---- prompt backup ----

def backup_prompt(iteration: int) -> Path:
    """Backup the current SYSTEM.md before overwriting."""
    PROMPT_HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = PROMPT_HISTORY_DIR / f"SYSTEM_iter{iteration}_{ts}.md"
    if SYSTEM_PROMPT_PATH.exists():
        shutil.copy2(SYSTEM_PROMPT_PATH, backup_path)
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

def run_agent() -> tuple[int, str]:
    """Run the agent subprocess with clean browser state."""
    _kill_orphaned_browsers()
    _clean_all_browser_dirs()

    print(f"\n{'='*60}")
    print(f"RUNNING AGENT")
    print(f"{'='*60}\n")

    proc = None
    try:
        proc = subprocess.Popen(
            ["bun", "run", "src/index.ts"],
            cwd=str(PROJECT_DIR),
            env=os.environ.copy(),
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


# ---- transcript discovery ----

def find_latest_transcript() -> Path | None:
    """Find the most recent transcript file in runs/."""
    if not RUNS_DIR.exists():
        return None
    transcripts = sorted(RUNS_DIR.glob("transcript_*.json"), reverse=True)
    if not transcripts:
        # Also check for trajectory files
        transcripts = sorted(RUNS_DIR.glob("trajectory_*.json"), reverse=True)
    return transcripts[0] if transcripts else None


# ---- main loop ----

def main(max_iterations: int = 50) -> None:
    PROMPT_HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    RUNS_DIR.mkdir(parents=True, exist_ok=True)

    for i in range(max_iterations):
        print(f"\n{'#'*60}")
        print(f"# ITERATION {i}")
        print(f"{'#'*60}")

        # 1. Backup current SYSTEM.md
        backup_prompt(i)

        # 2. Run the agent
        rc, output = run_agent()

        # 3. Find latest transcript
        transcript = find_latest_transcript()
        if not transcript:
            print("[train_loop] no transcript found, skipping optimization")
            continue

        print(f"[train_loop] using transcript: {transcript.name}")

        # 4. LLM improves prompt
        current_prompt = SYSTEM_PROMPT_PATH.read_text() if SYSTEM_PROMPT_PATH.exists() else ""

        try:
            from optimize import run_optimization
            result = run_optimization(transcript, current_prompt)
            optimized_prompt = result["optimized_prompt"]
        except Exception as e:
            print(f"[train_loop] optimization failed: {e}")
            import traceback
            traceback.print_exc()
            continue

        # 5. Write new SYSTEM.md
        if optimized_prompt and optimized_prompt != current_prompt:
            SYSTEM_PROMPT_PATH.write_text(optimized_prompt)
            print(f"[train_loop] wrote new SYSTEM.md ({len(optimized_prompt)} chars)")
        else:
            print("[train_loop] no change to SYSTEM.md")

    print(f"\n{'='*60}")
    print(f"TRAINING COMPLETE ({max_iterations} iterations)")
    print(f"{'='*60}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Iterative prompt optimization loop")
    parser.add_argument(
        "--max-iterations", type=int, default=50,
        help="Maximum number of iterations (default: 50)",
    )
    args = parser.parse_args()
    main(max_iterations=args.max_iterations)
