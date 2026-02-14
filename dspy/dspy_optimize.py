"""
DSPy GEPA-based prompt optimizer for the browser automation agent.

Uses DSPy's GEPA (Grounded Explanation-based Prompt Adaptation) optimizer
with LLM judge feedback to iteratively improve the system prompt based
on trajectory analysis.

Pattern follows the cookbook at:
  rl/training/cookbook/main-sql-clay/dspy-clay/

Usage:
    from dspy_optimize import run_dspy_optimization
    result = run_dspy_optimization(runs_dir, current_prompt)
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import dspy

SCRIPT_DIR = Path(__file__).resolve().parent
OPTIMIZED_MODULE_DIR = SCRIPT_DIR / "dspy_prompts"


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
    - Fixes failure patterns with concrete JavaScript solutions
    - Preserves and reinforces successful patterns
    - Maximizes speed: combine solve+submit in single calls, avoid diagnostic waste
    - Maximizes efficiency: minimize turns per step (target 2-3 calls per step)
    - Stays concise (each extra token costs money every API call)
    - NEVER includes hardcoded codes, passwords, or instance-specific data
    """

    trajectory_analysis = dspy.InputField(
        desc="Analysis of the agent's recent run: per-step tool calls, errors, successes, and metrics"
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
            "NO_HARDCODED: <float> - <reasoning>\n"
            "EFFICIENCY: <float> - <reasoning>\n"
            "SPEED: <float> - <reasoning>\n"
            "SUGGESTIONS: <specific improvements>"
        )
    )


# ---- Module ----

class PromptOptimizerModule(dspy.Module):
    """Generates an improved system prompt from trajectory analysis."""

    def __init__(self):
        self.improve = dspy.Predict(PromptImprover)

    def forward(self, trajectory_analysis: str, current_prompt: str) -> dspy.Prediction:
        result = self.improve(
            trajectory_analysis=trajectory_analysis,
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

        eval_context = (
            f"TRAJECTORY ANALYSIS (what happened during the agent's run):\n"
            f"{trajectory[:8000]}\n\n"
            f"ORIGINAL PROMPT (first 3000 chars):\n"
            f"{current[:3000]}\n\n"
            f"IMPROVED PROMPT (first 8000 chars):\n"
            f"{improved[:8000]}\n\n"
            f"Criteria to evaluate:\n"
            f"1. FAILURE_COVERAGE: Does the improved prompt address failures from the trajectory?\n"
            f"2. PATTERN_PRESERVATION: Are successful patterns preserved?\n"
            f"3. NO_HARDCODED: No hardcoded 6-char codes or instance-specific data?\n"
            f"4. EFFICIENCY: Does the prompt minimize turns per step? Combines solve+submit in one call? Avoids wasteful diagnostic calls?\n"
            f"5. SPEED: Is the prompt concise (not bloated)? Does it guide the agent to act immediately rather than deliberate?"
        )

        with dspy.context(lm=judge_lm):
            eval_result = judge(evaluation_context=eval_context)

        eval_text = eval_result.scores_and_feedback

        # Parse scores from judge response
        criteria = [
            "FAILURE_COVERAGE", "PATTERN_PRESERVATION", "NO_HARDCODED",
            "EFFICIENCY", "SPEED",
        ]
        scores = {}
        for c in criteria:
            match = re.search(rf"{c}:\s*([\d.]+)", eval_text)
            if match:
                scores[c] = min(1.0, max(0.0, float(match.group(1))))
            else:
                scores[c] = 0.5

        # Weighted combination
        weights = {
            "FAILURE_COVERAGE": 0.30,
            "PATTERN_PRESERVATION": 0.20,
            "NO_HARDCODED": 0.10,
            "EFFICIENCY": 0.25,
            "SPEED": 0.15,
        }
        combined = sum(scores[k] * weights[k] for k in weights)

        # Build rich feedback string for GEPA reflection
        feedback_parts = [f"Overall score: {combined:.2f}"]
        for c in criteria:
            feedback_parts.append(f"  {c}: {scores[c]:.2f} (weight {weights[c]:.2f})")
        feedback_parts.append(f"\nJudge evaluation:\n{eval_text}")
        feedback = "\n".join(feedback_parts)

        return dspy.Prediction(score=combined, feedback=feedback)

    return metric


# ---- Training Data ----

def build_trainset(
    runs_dir: Path,
    current_prompt: str,
    max_examples: int = 8,
) -> list[dspy.Example]:
    """Build DSPy training set from trajectory files in runs/."""
    from optimize import analyze_trajectories

    # Get most recent trajectory files
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

            # Cap analysis to stay within token limits
            if len(analysis) > 50000:
                analysis = analysis[:50000] + "\n... (truncated)"

            example = dspy.Example(
                trajectory_analysis=analysis,
                current_prompt=current_prompt,
            ).with_inputs("trajectory_analysis", "current_prompt")
            trainset.append(example)
        except Exception as e:
            print(f"[dspy] skipping {tf.name}: {e}")
            continue

    print(f"[dspy] built trainset with {len(trainset)} examples from {len(transcript_files)} files")
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
    print(f"[dspy] saved optimized prompt to {path.name}")
    return path


# ---- Main Optimization ----

def run_dspy_optimization(
    runs_dir: Path,
    current_prompt: str,
    model: str = "openrouter/anthropic/claude-haiku-4.5",
    reflection_model: str = "openrouter/anthropic/claude-sonnet-4.5",
    max_examples: int = 4,
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

    # Configure LMs (following cookbook pattern: separate main/judge/reflection)
    main_lm = dspy.LM(
        model=model,
        api_key=api_key,
        temperature=0.7,
        max_tokens=16384,
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

    # Build training data from trajectories
    trainset = build_trainset(runs_dir, current_prompt, max_examples)
    if not trainset:
        print("[dspy] no training data available, skipping GEPA")
        return {"optimized_prompt": current_prompt, "stats": {"error": "no training data"}}

    # Create module and metric
    program = PromptOptimizerModule()
    metric_fn = make_prompt_metric(judge_lm)

    # Load previously saved module if exists
    module_path = OPTIMIZED_MODULE_DIR / "optimized_prompt_improver.json"
    if module_path.exists():
        try:
            program.load(str(module_path))
            print(f"[dspy] loaded saved module from {module_path.name}")
        except Exception as e:
            print(f"[dspy] failed to load saved module: {e}")

    # Run GEPA optimization
    print(f"[dspy] running GEPA with {len(trainset)} examples, reflection model: {reflection_model}")
    optimizer = dspy.GEPA(
        metric=metric_fn,
        auto="quick",
        track_stats=True,
        reflection_minibatch_size=min(3, len(trainset)),
        reflection_lm=reflection_lm,
    )

    compiled = optimizer.compile(program, trainset=trainset)

    # Save compiled module for next time
    OPTIMIZED_MODULE_DIR.mkdir(parents=True, exist_ok=True)
    compiled.save(str(module_path))
    print(f"[dspy] saved compiled module to {module_path.name}")

    # Extract and save the optimized instructions (meta-level)
    instructions = _extract_instructions(compiled)
    if instructions:
        save_optimized_prompt(instructions, tag="instructions")

    # Generate improved prompt using compiled module on latest trajectory
    result = compiled(
        trajectory_analysis=trainset[0].trajectory_analysis,
        current_prompt=current_prompt,
    )

    # Save the output prompt
    save_optimized_prompt(result.improved_prompt, tag="output")

    return {
        "optimized_prompt": result.improved_prompt,
        "stats": {
            "trainset_size": len(trainset),
            "instructions_length": len(instructions) if instructions else 0,
        },
    }
