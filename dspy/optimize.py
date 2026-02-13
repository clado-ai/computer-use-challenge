"""
DSPy GEPA prompt optimizer for the browser automation agent.

Analyzes run transcripts and optimizes the system prompt using
Opus 4.6 as a judge. Follows the pattern from dspy-clay.

Usage:
    uv run optimize.py --optimize          # run GEPA optimization
    uv run optimize.py                     # test current optimized prompt
"""

from __future__ import annotations

import argparse
import json
import os
import warnings
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

warnings.filterwarnings("ignore", category=UserWarning, module="pydantic")

import dspy


# ---- paths ----

SCRIPT_DIR = Path(__file__).resolve().parent
RUNS_DIR = SCRIPT_DIR.parent / "runs"
PROMPTS_DIR = SCRIPT_DIR / "prompts"
DATA_DIR = SCRIPT_DIR / "data"
SYSTEM_PROMPT_PATH = SCRIPT_DIR.parent / "src" / "prompts" / "SYSTEM.md"
OPTIMIZED_MODULE_PATH = PROMPTS_DIR / "optimized_prompt_generator.json"


# ---- dspy setup ----

def build_lm(api_key: str, model: str) -> dspy.LM:
    return dspy.LM(
        model=f"openrouter/{model}",
        api_key=api_key,
    )


# ---- signature and module ----

class BrowserAgentPromptSignature(dspy.Signature):
    """Generate an optimized system prompt for a browser automation agent
    that solves 30 sequential web challenges. The prompt should instruct
    the agent to be efficient (minimize tool calls), handle JS dialogs,
    inspect DOM for hidden information, and act decisively."""

    run_analysis = dspy.InputField(
        desc="Analysis of agent run transcripts including: steps completed, "
        "common failure patterns, wasted tool calls, and successful strategies."
    )
    optimized_prompt = dspy.OutputField(
        desc="An optimized system prompt for the browser automation agent. "
        "Should be concise, actionable, and cover dialog handling, DOM inspection, "
        "common challenge patterns, and efficiency rules."
    )


class PromptGenerator(dspy.Module):
    def __init__(self) -> None:
        super().__init__()
        self.generate = dspy.Predict(BrowserAgentPromptSignature)

    def forward(self, run_analysis: str) -> dspy.Prediction:
        result = self.generate(run_analysis=run_analysis)
        return dspy.Prediction(optimized_prompt=result.optimized_prompt.strip())


# ---- transcript analysis ----

def load_run_transcripts(runs_dir: Path, limit: int = 5) -> list[dict]:
    """load the most recent run transcripts."""
    transcript_files = sorted(runs_dir.glob("transcript_*.json"), reverse=True)
    transcripts = []
    for f in transcript_files[:limit]:
        try:
            data = json.loads(f.read_text())
            transcripts.append({"file": f.name, "data": data})
        except Exception:
            continue
    return transcripts


def load_run_metrics(runs_dir: Path, limit: int = 5) -> list[dict]:
    """load the most recent run metrics."""
    metric_files = sorted(runs_dir.glob("run_*.json"), reverse=True)
    metrics = []
    for f in metric_files[:limit]:
        try:
            data = json.loads(f.read_text())
            metrics.append(data)
        except Exception:
            continue
    return metrics


def analyze_runs(runs_dir: Path) -> str:
    """analyze run transcripts and metrics into a summary for the optimizer."""
    metrics = load_run_metrics(runs_dir)
    transcripts = load_run_transcripts(runs_dir)

    if not metrics:
        return "no run data available. use default optimization based on challenge type."

    analyses = []
    for m in metrics:
        analysis = (
            f"run: steps={m.get('stepsCompleted', '?')}/30, "
            f"time={m.get('agentDurationMs', 0) / 1000:.1f}s, "
            f"api_calls={m.get('totalApiCalls', '?')}, "
            f"tool_calls={m.get('totalToolCalls', '?')}, "
            f"cost=${m.get('totalCost', '?')}"
        )
        analyses.append(analysis)

    # extract common patterns from transcripts
    failure_patterns = []
    success_patterns = []

    for t in transcripts:
        for msg in t.get("data", []):
            content = msg.get("content", "")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        text = block.get("text", "") or block.get("content", "")
                        if isinstance(text, str):
                            if "error" in text.lower() or "not found" in text.lower():
                                failure_patterns.append(text[:200])
                            elif "clicked" in text.lower() or "typed" in text.lower():
                                success_patterns.append(text[:200])
            elif isinstance(content, str):
                if "error" in content.lower():
                    failure_patterns.append(content[:200])

    summary = "RUN SUMMARIES:\n" + "\n".join(analyses)

    if failure_patterns:
        unique_failures = list(set(failure_patterns))[:10]
        summary += "\n\nCOMMON FAILURES:\n" + "\n".join(f"- {f}" for f in unique_failures)

    if success_patterns:
        unique_successes = list(set(success_patterns))[:10]
        summary += "\n\nSUCCESSFUL PATTERNS:\n" + "\n".join(
            f"- {s}" for s in unique_successes
        )

    return summary


# ---- metric (judge) ----

def make_metric(judge_lm: dspy.LM) -> Callable:
    """create a metric that uses opus 4.6 to judge prompt quality."""

    def metric(
        gold: dspy.Example,
        pred: dspy.Prediction,
        trace: Optional[Any] = None,
        **kwargs: Any,
    ) -> dspy.Prediction:
        prompt = pred.optimized_prompt

        # score the prompt on key criteria
        judge_prompt = f"""Rate this browser automation system prompt on a scale of 0-1.
Score based on these criteria:
1. EFFICIENCY (0.3 weight): Does it minimize tool calls? Does it encourage 1-shot solutions?
2. COMPLETENESS (0.3 weight): Does it cover dialog handling, DOM inspection, hidden elements, common patterns?
3. CONCISENESS (0.2 weight): Is it focused and not verbose? Does it avoid unnecessary detail?
4. ACTIONABILITY (0.2 weight): Are instructions clear and specific? Can the agent follow them without ambiguity?

System prompt to evaluate:
---
{prompt}
---

Respond with JSON: {{"score": 0.0-1.0, "feedback": "detailed feedback on strengths and weaknesses"}}"""

        with dspy.context(lm=judge_lm):
            judge_response = dspy.Predict("prompt -> evaluation")(prompt=judge_prompt)

        try:
            # parse json from response
            eval_text = judge_response.evaluation
            # try to extract json
            if "{" in eval_text:
                json_str = eval_text[eval_text.index("{") : eval_text.rindex("}") + 1]
                result = json.loads(json_str)
                score = float(result.get("score", 0.5))
                feedback = result.get("feedback", "no feedback")
            else:
                score = 0.5
                feedback = eval_text
        except Exception:
            score = 0.5
            feedback = "failed to parse judge response"

        return dspy.Prediction(score=score, feedback=feedback)

    return metric


# ---- training data ----

def build_trainset(runs_dir: Path) -> list[dspy.Example]:
    """build training examples from run analyses."""
    analysis = analyze_runs(runs_dir)

    # create variations of the analysis for training diversity
    examples = []
    for i in range(5):
        example = dspy.Example(
            run_analysis=f"[variation {i + 1}]\n{analysis}",
        ).with_inputs("run_analysis")
        examples.append(example)

    # if no real data, use a synthetic example
    if "no run data" in analysis:
        synthetic = (
            "RUN SUMMARIES:\n"
            "run: steps=15/30, time=180s, api_calls=45, tool_calls=60, cost=$3.50\n"
            "run: steps=22/30, time=240s, api_calls=55, tool_calls=80, cost=$4.20\n\n"
            "COMMON FAILURES:\n"
            "- error: ref e5 not found (stale refs after page transition)\n"
            "- error: dialog blocked page interaction\n"
            "- agent took 5 snapshots on same step without acting\n\n"
            "SUCCESSFUL PATTERNS:\n"
            "- using evaluate to read innerHTML for hidden codes\n"
            "- suppressing dialogs with window.alert override\n"
            "- clicking refs immediately after snapshot"
        )
        examples = [
            dspy.Example(run_analysis=synthetic).with_inputs("run_analysis")
            for _ in range(5)
        ]

    return examples


# ---- prompt extraction ----

def extract_instructions(program: Any) -> Optional[str]:
    """extract optimized instructions from compiled program."""
    for attr in ("generate", "predict"):
        component = getattr(program, attr, None)
        if component is None:
            continue
        sig = getattr(component, "signature", None)
        if sig is None:
            continue
        instructions = getattr(sig, "instructions", None)
        if instructions:
            return instructions
    return None


def save_optimized_prompt(program: Any, output_dir: Path) -> Optional[Path]:
    """save the optimized prompt to a timestamped file."""
    instructions = extract_instructions(program)
    if not instructions:
        print("warning: could not extract instructions from compiled program")
        return None

    output_dir.mkdir(parents=True, exist_ok=True)
    run_id = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    prompt_path = output_dir / f"gepa_optimized_prompt_{run_id}.txt"
    prompt_path.write_text(instructions, encoding="utf-8")
    print(f"saved optimized prompt to: {prompt_path}")
    return prompt_path


# ---- main ----

def main() -> None:
    parser = argparse.ArgumentParser(description="DSPy prompt optimizer for browser agent")
    parser.add_argument(
        "--optimize", action="store_true", help="run GEPA optimization"
    )
    parser.add_argument(
        "--generator-model",
        default="anthropic/claude-opus-4.5",
        help="model for prompt generation",
    )
    parser.add_argument(
        "--judge-model",
        default="anthropic/claude-opus-4.5",
        help="model for judging prompts",
    )
    parser.add_argument(
        "--apply", action="store_true", help="apply optimized prompt to SYSTEM.md"
    )
    args = parser.parse_args()

    try:
        import dotenv
        dotenv.load_dotenv(SCRIPT_DIR.parent / ".env")
    except Exception:
        pass

    openrouter_key = os.getenv("OPENROUTER_API_KEY")
    if not openrouter_key:
        raise ValueError("OPENROUTER_API_KEY required in env or .env")

    # configure dspy
    dspy.configure(lm=build_lm(openrouter_key, args.generator_model))

    judge_lm = dspy.LM(
        model=f"openrouter/{args.judge_model}",
        api_key=openrouter_key,
        temperature=0.2,
    )
    reflection_lm = dspy.LM(
        model=f"openrouter/{args.generator_model}",
        api_key=openrouter_key,
        temperature=1.0,
        max_tokens=4096,
    )

    program = PromptGenerator()
    metric = make_metric(judge_lm)

    if args.optimize:
        print("=" * 60)
        print("GEPA optimization: optimizing browser agent system prompt")
        print("=" * 60)

        trainset = build_trainset(RUNS_DIR)
        print(f"training examples: {len(trainset)}")

        optimizer = dspy.GEPA(
            metric=metric,
            auto="light",
            track_stats=True,
            reflection_minibatch_size=3,
            reflection_lm=reflection_lm,
        )
        program = optimizer.compile(program, trainset=trainset)

        # save module and extracted prompt
        PROMPTS_DIR.mkdir(parents=True, exist_ok=True)
        program.save(str(OPTIMIZED_MODULE_PATH))
        print(f"saved optimized module to: {OPTIMIZED_MODULE_PATH}")

        prompt_path = save_optimized_prompt(program, PROMPTS_DIR)

        if args.apply and prompt_path:
            # overwrite the system prompt
            optimized = prompt_path.read_text()
            SYSTEM_PROMPT_PATH.write_text(optimized)
            print(f"applied optimized prompt to: {SYSTEM_PROMPT_PATH}")

    # test
    print("\n" + "=" * 60)
    print("TEST: generating optimized prompt")
    print("=" * 60)

    analysis = analyze_runs(RUNS_DIR)
    prediction = program(run_analysis=analysis)
    print("\ngenerated prompt:")
    print("-" * 40)
    print(prediction.optimized_prompt[:2000])
    print("-" * 40)


if __name__ == "__main__":
    main()
