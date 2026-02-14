"""
Direct LLM prompt optimizer for the browser automation agent.

Replaces DSPy GEPA with a simple approach: analyze trajectory → LLM improves
prompt → return optimized prompt.

Usage:
    from optimize import run_optimization
    result = run_optimization(transcript_file, current_prompt)
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from openai import OpenAI


# ---- paths ----

SCRIPT_DIR = Path(__file__).resolve().parent
SYSTEM_PROMPT_PATH = SCRIPT_DIR.parent / "src" / "prompts" / "SYSTEM.md"


# ---- rollout analysis (kept from original) ----

def load_run_transcripts(transcript_files: list[str | Path]) -> list[dict]:
    """Load transcripts from explicit file paths."""
    transcripts = []
    for f in transcript_files:
        try:
            data = json.loads(Path(f).read_text())
            if isinstance(data, dict) and "transcript" in data:
                data = data["transcript"]
            transcripts.append({"file": Path(f).name, "data": data})
        except Exception:
            continue
    return transcripts


def extract_rollout_steps(transcript_data: list[dict]) -> list[dict]:
    """Extract a per-step summary from a raw transcript."""
    steps = []
    current_step = {"agent_text": [], "tool_calls": [], "tool_results": []}

    for msg in transcript_data:
        content = msg.get("content", "")
        role = msg.get("role", "")

        if role == "assistant":
            # Handle both formats:
            # Format A (Anthropic): content is list of {type: "text"/"tool_use"} blocks
            # Format B (OpenAI/agent.ts): content is dict with {content, tool_calls}
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        if block.get("type") == "text":
                            current_step["agent_text"].append(block.get("text", ""))
                        elif block.get("type") == "tool_use":
                            raw_input = str(block.get("input", ""))
                            current_step["tool_calls"].append({
                                "tool": block.get("name", ""),
                                "input": raw_input[:500],
                            })
            elif isinstance(content, dict):
                text = content.get("content", "")
                if text and isinstance(text, str):
                    current_step["agent_text"].append(text)
                for tc in content.get("tool_calls", []):
                    fn = tc.get("function", {})
                    raw_input = fn.get("arguments", "")[:500]
                    current_step["tool_calls"].append({
                        "tool": fn.get("name", ""),
                        "input": raw_input,
                    })
            elif isinstance(content, str) and content.strip():
                current_step["agent_text"].append(content)

        elif role == "tool":
            # Format A: content is list with {type: "tool_result"} blocks
            # Format B: content is dict with {result: "..."}
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        result_content = block.get("content", "")
                        if isinstance(result_content, str):
                            current_step["tool_results"].append(result_content[:1000])
            elif isinstance(content, dict):
                result = content.get("result", "")
                if isinstance(result, str):
                    current_step["tool_results"].append(result[:1000])
            elif isinstance(content, str):
                current_step["tool_results"].append(content[:1000])

            if current_step["tool_calls"]:
                steps.append(current_step)
                current_step = {"agent_text": [], "tool_calls": [], "tool_results": []}

        elif role == "user" and isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    result_content = block.get("content", "")
                    if isinstance(result_content, str):
                        current_step["tool_results"].append(result_content[:1000])
            if current_step["tool_calls"]:
                steps.append(current_step)
                current_step = {"agent_text": [], "tool_calls": [], "tool_results": []}

    if current_step["tool_calls"]:
        steps.append(current_step)

    return steps


def analyze_trajectories(
    transcript_files: list[str | Path],
    metric_files: list[str | Path],
) -> str:
    """Build rollout analysis from explicit file paths."""
    transcripts = []
    for f in transcript_files:
        try:
            data = json.loads(Path(f).read_text())
            if isinstance(data, dict) and "transcript" in data:
                data = data["transcript"]
            transcripts.append({"file": Path(f).name, "data": data})
        except Exception:
            continue

    metrics = []
    for f in metric_files:
        try:
            data = json.loads(Path(f).read_text())
            metrics.append(data)
        except Exception:
            continue

    if not metrics and not transcripts:
        return "No trajectory data available."

    parts = []

    for m in metrics:
        parts.append(
            f"RUN: {m.get('stepsCompleted', '?')}/30 steps, "
            f"{m.get('agentDurationMs', 0) / 1000:.1f}s, "
            f"{m.get('totalApiCalls', '?')} api calls, "
            f"{m.get('totalToolCalls', '?')} tool calls, "
            f"${m.get('totalCost', '?')}"
        )

    for t in transcripts:
        steps = extract_rollout_steps(t.get("data", []))
        parts.append(f"\n--- rollout from {t['file']} ({len(steps)} turns) ---")

        for i, step in enumerate(steps):
            errors = [r for r in step["tool_results"] if "error" in r.lower()]
            successes = [r for r in step["tool_results"] if "error" not in r.lower()]
            agent_reasoning = " ".join(step["agent_text"])

            status = "FAIL" if errors else "OK"
            parts.append(f"\nturn {i + 1} [{status}]:")

            if agent_reasoning.strip():
                parts.append(f"  reasoning: {agent_reasoning}")

            for c in step["tool_calls"]:
                parts.append(f"  call: {c['tool']}({c['input']})")

            if errors:
                for e in errors:
                    parts.append(f"  ERROR: {e}")

            if successes:
                for s in successes:
                    parts.append(f"  result: {s}")

    all_steps = []
    for t in transcripts:
        all_steps.extend(extract_rollout_steps(t.get("data", [])))

    if all_steps:
        wasted = []
        for step in all_steps:
            errors = [r for r in step["tool_results"] if "error" in r.lower()]
            if len(errors) > 1:
                wasted.append(
                    f"  - {len(errors)} errors in one turn: "
                    f"{[c['tool'] for c in step['tool_calls']]}"
                )
        if wasted:
            parts.append("\nWASTED CALLS (multiple errors per turn):")
            parts.extend(wasted[:5])

        tool_counts: dict[str, int] = {}
        error_counts: dict[str, int] = {}
        for step in all_steps:
            for call in step["tool_calls"]:
                tool_counts[call["tool"]] = tool_counts.get(call["tool"], 0) + 1
            for r in step["tool_results"]:
                if "error" in r.lower():
                    if step["tool_calls"]:
                        t_name = step["tool_calls"][0]["tool"]
                        error_counts[t_name] = error_counts.get(t_name, 0) + 1

        parts.append("\nTOOL USAGE:")
        for tool, count in sorted(tool_counts.items(), key=lambda x: -x[1]):
            err = error_counts.get(tool, 0)
            parts.append(f"  {tool}: {count} calls, {err} errors")

    return "\n".join(parts)


# ---- LLM prompt optimization ----

OPTIMIZER_SYSTEM = """You are a prompt engineer improving a browser automation agent's system prompt.

The agent solves a 30-step web challenge where each step reveals a 6-character code to submit.
It uses browser_evaluate (JavaScript execution) as its primary tool.

Your job: given the current prompt and a trajectory analysis showing what happened during the
agent's run, produce an IMPROVED version of the system prompt.

Rules:
- NEVER include hardcoded codes, passwords, XOR keys
- Only add GENERALIZABLE patterns that work for ANY challenge instance
- Keep patterns that worked well in the trajectory
- Remove or fix patterns that caused failures
- Be concise — the prompt is injected every API turn, so verbosity costs tokens
- Output ONLY the improved prompt text, nothing else (no explanation, no markdown wrapper)"""


def run_optimization(
    transcript_file: str | Path,
    current_prompt: str,
    model: str = "anthropic/claude-sonnet-4.5",
) -> dict:
    """Run one optimization step: analyze trajectory → LLM improves prompt.

    Args:
        transcript_file: Path to a single transcript JSON file.
        current_prompt: The current SYSTEM.md content.
        model: OpenRouter model ID for the optimizer LLM.

    Returns:
        {"optimized_prompt": str, "analysis": str}
    """
    try:
        import dotenv
        dotenv.load_dotenv(SCRIPT_DIR.parent / ".env")
    except Exception:
        pass

    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        raise ValueError("OPENROUTER_API_KEY required in env or .env")

    analysis = analyze_trajectories([transcript_file], [])

    MAX_CHARS = 50000
    if len(analysis) > MAX_CHARS:
        analysis = analysis[:MAX_CHARS] + "\n... (truncated)"
        print(f"[optimize] analysis truncated: {len(analysis)} -> {MAX_CHARS} chars")

    client = OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=api_key,
    )

    user_message = f"""Here is the current system prompt for the agent:

<current_prompt>
{current_prompt}
</current_prompt>

Here is the trajectory analysis from the agent's most recent run:

<trajectory_analysis>
{analysis}
</trajectory_analysis>

Produce an improved version of the system prompt that addresses the failures and reinforces the successes observed in the trajectory. Output ONLY the improved prompt text."""

    print(f"[optimize] calling {model} to improve prompt...")
    import time
    response = None
    for attempt in range(3):
        try:
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": OPTIMIZER_SYSTEM},
                    {"role": "user", "content": user_message},
                ],
                max_tokens=16384,
                temperature=0.7,
            )
            break
        except Exception as e:
            print(f"[optimize] attempt {attempt + 1}/3 failed: {e}")
            if attempt < 2:
                time.sleep(5 * (attempt + 1))

    if not response:
        raise RuntimeError("All 3 optimization API attempts failed")

    optimized_prompt = response.choices[0].message.content.strip()

    return {
        "optimized_prompt": optimized_prompt,
        "analysis": analysis,
    }
