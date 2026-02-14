from __future__ import annotations

import json
import os
import re
from pathlib import Path

import dspy

SCRIPT_DIR = Path(__file__).resolve().parent
OPTIMIZED_MODULE_DIR = SCRIPT_DIR / "dspy_prompts"
PROMPT_HISTORY_DIR = SCRIPT_DIR / "prompt_history"


def extract_rollout_steps(transcript_data: list[dict]) -> list[dict]:
    """Extract a per-step summary from a raw transcript."""
    steps = []
    current_step = {"agent_text": [], "tool_calls": [], "tool_results": []}

    for msg in transcript_data:
        content = msg.get("content", "")
        role = msg.get("role", "")

        if role == "assistant":
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
    """Build rollout analysis from transcript and metric files."""
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


# ---- DSPy Signatures ----

class PromptImprover(dspy.Signature):
    """Improve a browser automation agent's system prompt based on trajectory analysis.

    The agent solves a 30-step web challenge using browser_evaluate (JavaScript execution).
    Each step reveals a 6-character code that must be submitted to advance.

    Analyze the trajectory to identify:
    1. Which challenge types the agent failed on and why
    2. Which patterns worked successfully
    3. Where the agent wasted turns (wrong approaches, repeated errors)

    Then produce an improved prompt that:
    - Completes MORE steps (most important metric)
    - Fixes failure patterns with concrete JavaScript solutions
    - Preserves and reinforces successful patterns
    - Reduces wasted turns: no repeated errors, no unnecessary diagnostics, no wrong approaches
    - Minimizes turns per step (target 2-3 calls per step, combine solve+submit)
    - Be as detailed and comprehensive as needed — length is not a concern
    """

    trajectory_analysis = dspy.InputField(
        desc="Analysis of the agent's recent run: per-step tool calls, errors, successes, and metrics"
    )
    prompt_history = dspy.InputField(
        desc="History of previous prompts and their trajectory results (most recent first). Shows what was tried before and how it performed."
    )
    current_prompt = dspy.InputField(
        desc="The current system prompt the agent uses"
    )
    improved_prompt = dspy.OutputField(
        desc="Complete improved system prompt. Must be self-contained (not a diff). Output ONLY the prompt text."
    )


class PromptJudge(dspy.Signature):
    """Evaluate an improved system prompt for a browser automation agent.

    Score each criterion 0.0-1.0 with specific reasoning.
    """

    evaluation_context = dspy.InputField(
        desc="Context: trajectory analysis, original prompt, improved prompt"
    )
    scores_and_feedback = dspy.OutputField(
        desc=(
            "Structured evaluation. Format EXACTLY as:\n"
            "FAILURE_COVERAGE: <float> - <reasoning>\n"
            "PATTERN_PRESERVATION: <float> - <reasoning>\n"
            "EFFICIENCY: <float> - <reasoning>\n"
            "SUGGESTIONS: <specific improvements>"
        )
    )


# ---- Module ----

class PromptOptimizerModule(dspy.Module):
    """Generates an improved system prompt from trajectory analysis."""

    def __init__(self):
        self.improve = dspy.Predict(PromptImprover)

    def forward(self, trajectory_analysis: str, prompt_history: str, current_prompt: str) -> dspy.Prediction:
        result = self.improve(
            trajectory_analysis=trajectory_analysis,
            prompt_history=prompt_history,
            current_prompt=current_prompt,
        )
        return dspy.Prediction(improved_prompt=result.improved_prompt.strip())


# ---- Metric with LLM Judge Feedback ----

def make_prompt_metric(judge_lm):
    """Create a metric that uses an LLM judge with rich feedback for GEPA."""

    judge = dspy.Predict(PromptJudge)

    def metric(gold, pred, trace=None, pred_name=None, pred_trace=None):
        improved = pred.improved_prompt
        trajectory = gold.trajectory_analysis
        current = gold.current_prompt
        history = gold.prompt_history

        eval_context = (
            f"PROMPT HISTORY (previous attempts and results):\n"
            f"{history[:12000]}\n\n"
            f"TRAJECTORY ANALYSIS (what happened during the agent's run):\n"
            f"{trajectory[:8000]}\n\n"
            f"CURRENT PROMPT:\n"
            f"{current}\n\n"
            f"IMPROVED PROMPT:\n"
            f"{improved}\n\n"
            f"Criteria to evaluate:\n"
            f"1. FAILURE_COVERAGE: Does the improved prompt address failures from the trajectory? Will it help the agent complete MORE steps? Does it reduce wasted turns (repeated errors, unnecessary diagnostics, wrong approaches)?\n"
            f"2. PATTERN_PRESERVATION: Are successful patterns preserved?\n"
            f"3. EFFICIENCY: Does the prompt minimize turns per step? Combines solve+submit in one call? Eliminates wasteful calls that don't advance progress?"
        )

        with dspy.context(lm=judge_lm):
            eval_result = judge(evaluation_context=eval_context)

        eval_text = eval_result.scores_and_feedback

        criteria = ["FAILURE_COVERAGE", "PATTERN_PRESERVATION", "EFFICIENCY"]
        scores = {}
        for c in criteria:
            match = re.search(rf"{c}:\s*([\d.]+)", eval_text)
            if match:
                scores[c] = min(1.0, max(0.0, float(match.group(1))))
            else:
                scores[c] = 0.5

        weights = {
            "FAILURE_COVERAGE": 0.50,
            "PATTERN_PRESERVATION": 0.20,
            "EFFICIENCY": 0.30,
        }
        combined = sum(scores[k] * weights[k] for k in weights)

        feedback_parts = [f"Overall score: {combined:.2f}"]
        for c in criteria:
            feedback_parts.append(f"  {c}: {scores[c]:.2f} (weight {weights[c]:.2f})")
        feedback_parts.append(f"\nJudge evaluation:\n{eval_text}")
        feedback = "\n".join(feedback_parts)

        return dspy.Prediction(score=combined, feedback=feedback)

    return metric


# ---- Prompt History ----

def build_prompt_history(runs_dir: Path, num_entries: int = 3) -> str:
    """Build a history of previous prompts and their trajectory results.

    Pairs prompt backups with their closest trajectory by timestamp.
    """
    if not PROMPT_HISTORY_DIR.exists():
        return "No prompt history available yet."

    prompt_files = sorted(
        PROMPT_HISTORY_DIR.glob("SYSTEM_iter*.md"),
        key=lambda f: f.stat().st_mtime,
        reverse=True,
    )[:num_entries]

    if not prompt_files:
        return "No prompt history available yet."

    trajectory_files = sorted(
        runs_dir.glob("trajectory_*.json"),
        key=lambda f: f.stat().st_mtime,
        reverse=True,
    )

    run_files = sorted(
        runs_dir.glob("run_*.json"),
        key=lambda f: f.stat().st_mtime,
        reverse=True,
    )

    parts = []
    for i, pf in enumerate(prompt_files):
        prompt_text = pf.read_text()
        pf_time = pf.stat().st_mtime

        matched_trajectory = None
        for tf in trajectory_files:
            if tf.stat().st_mtime >= pf_time:
                matched_trajectory = tf
            else:
                break

        matched_run = None
        for rf in run_files:
            if rf.stat().st_mtime >= pf_time:
                matched_run = rf
            else:
                break

        parts.append(f"=== PROMPT VERSION {i+1} (from {pf.name}) ===")

        if matched_run:
            try:
                metrics = json.loads(matched_run.read_text())
                steps = metrics.get("stepsCompleted", "?")
                calls = metrics.get("totalApiCalls", "?")
                cost = metrics.get("totalCost", "?")
                parts.append(f"RESULT: {steps}/30 steps, {calls} api calls, ${cost}")
            except Exception:
                pass

        if matched_trajectory:
            try:
                analysis = analyze_trajectories([matched_trajectory], [])
                if len(analysis) > 8000:
                    analysis = analysis[:8000] + "\n... (truncated)"
                parts.append(f"TRAJECTORY:\n{analysis}")
            except Exception:
                pass

        parts.append(f"PROMPT:\n{prompt_text}")
        parts.append("")

    return "\n".join(parts)


# ---- Training Data ----

def build_trainset(
    runs_dir: Path,
    current_prompt: str,
    prompt_history: str,
    max_examples: int = 8,
) -> list[dspy.Example]:
    """Build DSPy training set from trajectory files in runs/."""
    transcript_files = sorted(
        runs_dir.glob("trajectory_*.json"),
        key=lambda f: f.stat().st_mtime,
        reverse=True,
    )[:max_examples]

    trainset = []
    for tf in transcript_files:
        try:
            analysis = analyze_trajectories([tf], [])
            if len(analysis) < 200:
                continue

            if len(analysis) > 50000:
                analysis = analysis[:50000] + "\n... (truncated)"

            example = dspy.Example(
                trajectory_analysis=analysis,
                prompt_history=prompt_history,
                current_prompt=current_prompt,
            ).with_inputs("trajectory_analysis", "prompt_history", "current_prompt")
            trainset.append(example)
        except Exception as e:
            print(f"[optimize] skipping {tf.name}: {e}")
            continue

    print(f"[optimize] built trainset with {len(trainset)} examples from {len(transcript_files)} files")
    return trainset


# ---- Prompt Extraction ----

def _extract_instructions(program: PromptOptimizerModule) -> str | None:
    """Extract optimized instructions from a compiled program."""
    try:
        sig = program.improve.signature
        if hasattr(sig, "instructions"):
            return sig.instructions
    except Exception:
        pass
    return None


def save_optimized_prompt(prompt: str, tag: str = "") -> Path:
    """Save an optimized prompt to the dspy_prompts directory."""
    from datetime import datetime, timezone

    OPTIMIZED_MODULE_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    suffix = f"_{tag}" if tag else ""
    path = OPTIMIZED_MODULE_DIR / f"gepa_prompt{suffix}_{ts}.txt"
    path.write_text(prompt)
    print(f"[optimize] saved optimized prompt to {path.name}")
    return path


# ---- Main Optimization ----

def run_optimization(
    runs_dir: Path,
    current_prompt: str,
    model: str = "openrouter/anthropic/claude-haiku-4.5",
    reflection_model: str = "openrouter/anthropic/claude-sonnet-4.5",
    max_examples: int = 2,
) -> dict:
    """Run DSPy GEPA optimization on the system prompt.

    Builds a training set from trajectory files, defines an LLM-judge metric
    with rich feedback, and runs GEPA to optimize the prompt improver's
    instructions. Then uses the compiled module to produce an improved prompt.

    Args:
        runs_dir: Directory containing trajectory_*.json files.
        current_prompt: Current SYSTEM_BASE.md content.
        model: DSPy LM model for generation and judging.
        reflection_model: DSPy LM model for GEPA reflection.
        max_examples: Max trajectory files to use.

    Returns:
        {"optimized_prompt": str, "stats": dict}
    """
    try:
        import dotenv
        dotenv.load_dotenv(SCRIPT_DIR.parent / ".env")
    except Exception:
        pass

    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        raise ValueError("OPENROUTER_API_KEY required in env or .env")

    main_lm = dspy.LM(
        model=model,
        api_key=api_key,
        temperature=0.7,
        max_tokens=65536,
    )
    judge_lm = dspy.LM(
        model=model,
        api_key=api_key,
        temperature=0.3,
        max_tokens=4096,
    )
    reflection_lm = dspy.LM(
        model=reflection_model,
        api_key=api_key,
        temperature=1.0,
        max_tokens=4096,
    )

    dspy.configure(lm=main_lm)

    prompt_history = build_prompt_history(runs_dir, num_entries=3)
    print(f"[optimize] prompt history: {len(prompt_history)} chars")

    trainset = build_trainset(runs_dir, current_prompt, prompt_history, max_examples)
    if not trainset:
        print("[optimize] no training data available, skipping GEPA")
        return {"optimized_prompt": current_prompt, "stats": {"error": "no training data"}}

    program = PromptOptimizerModule()
    metric_fn = make_prompt_metric(judge_lm)

    module_path = OPTIMIZED_MODULE_DIR / "optimized_prompt_improver.json"
    if module_path.exists():
        try:
            program.load(str(module_path))
            print(f"[optimize] loaded saved module from {module_path.name}")
        except Exception as e:
            print(f"[optimize] failed to load saved module: {e}")

    print(f"[optimize] running GEPA with {len(trainset)} examples, reflection model: {reflection_model}")
    optimizer = dspy.GEPA(
        metric=metric_fn,
        max_full_evals=5,
        track_stats=True,
        reflection_minibatch_size=min(2, len(trainset)),
        reflection_lm=reflection_lm,
    )

    compiled = optimizer.compile(program, trainset=trainset)

    OPTIMIZED_MODULE_DIR.mkdir(parents=True, exist_ok=True)
    compiled.save(str(module_path))
    print(f"[optimize] saved compiled module to {module_path.name}")

    instructions = _extract_instructions(compiled)
    if instructions:
        save_optimized_prompt(instructions, tag="instructions")

    result = compiled(
        trajectory_analysis=trainset[0].trajectory_analysis,
        prompt_history=prompt_history,
        current_prompt=current_prompt,
    )

    save_optimized_prompt(result.improved_prompt, tag="output")

    return {
        "optimized_prompt": result.improved_prompt,
        "stats": {
            "trainset_size": len(trainset),
            "instructions_length": len(instructions) if instructions else 0,
        },
    }
