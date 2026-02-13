"""
DSPy GEPA prompt optimizer for the browser automation agent.

Opus 4.6 as judge: analyzes actual run rollouts, summarizes what went
right and wrong, scores whether the proposed prompt addresses failures.

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
    that solves 30 sequential web challenges.

    CRITICAL RULES:
    - NEVER include hardcoded codes, passwords, XOR keys, session storage keys,
      or any challenge-specific secrets in the prompt.
    - NEVER include specific JavaScript extraction snippets that decode stored data.
    - The prompt must teach GENERALIZABLE strategies (tool usage patterns, error
      recovery, DOM inspection techniques) that work for ANY challenge, not
      solutions to the specific challenge observed in the rollout.
    - Focus on: when to snapshot vs evaluate, how to handle dialogs/overlays,
      efficient step completion patterns, error recovery strategies."""

    rollout_analysis = dspy.InputField(
        desc="Detailed analysis of agent run rollouts including full tool call "
        "inputs/outputs, agent reasoning, errors encountered, and efficiency "
        "metrics. Use this to identify PATTERNS of failure, not specific answers."
    )
    optimized_prompt = dspy.OutputField(
        desc="An optimized system prompt for the browser automation agent. "
        "Must directly address failure PATTERNS identified in the rollout. "
        "Must NOT contain any hardcoded codes, keys, or challenge-specific secrets. "
        "Be concise and actionable -- no filler."
    )


class PromptGenerator(dspy.Module):
    def __init__(self) -> None:
        super().__init__()
        self.generate = dspy.Predict(BrowserAgentPromptSignature)

    def forward(self, rollout_analysis: str) -> dspy.Prediction:
        result = self.generate(rollout_analysis=rollout_analysis)
        return dspy.Prediction(optimized_prompt=result.optimized_prompt.strip())


# ---- rollout analysis ----

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


def extract_rollout_steps(transcript_data: list[dict]) -> list[dict]:
    """extract a per-step summary from a raw transcript."""
    steps = []
    current_step = {"agent_text": [], "tool_calls": [], "tool_results": []}

    for msg in transcript_data:
        content = msg.get("content", "")
        role = msg.get("role", "")

        if role == "assistant" and isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    if block.get("type") == "text":
                        text = block.get("text", "")
                        current_step["agent_text"].append(text)
                    elif block.get("type") == "tool_use":
                        call = {
                            "tool": block.get("name", ""),
                            "input": str(block.get("input", "")),
                        }
                        current_step["tool_calls"].append(call)

        elif role == "user" and isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    result_content = block.get("content", "")
                    if isinstance(result_content, str):
                        current_step["tool_results"].append(result_content)

            # a tool_result batch marks the end of an agent turn
            if current_step["tool_calls"]:
                steps.append(current_step)
                current_step = {"agent_text": [], "tool_calls": [], "tool_results": []}

    # capture last step
    if current_step["tool_calls"]:
        steps.append(current_step)

    return steps


def analyze_rollout(runs_dir: Path) -> str:
    """build a detailed rollout analysis from run data.

    this is what the judge and the generator both see -- it describes
    what actually happened in each step of the agent's run.
    """
    metrics = load_run_metrics(runs_dir)
    transcripts = load_run_transcripts(runs_dir)

    if not metrics and not transcripts:
        return _synthetic_rollout()

    parts = []

    # overall stats
    for m in metrics:
        parts.append(
            f"RUN: {m.get('stepsCompleted', '?')}/30 steps, "
            f"{m.get('agentDurationMs', 0) / 1000:.1f}s, "
            f"{m.get('totalApiCalls', '?')} api calls, "
            f"{m.get('totalToolCalls', '?')} tool calls, "
            f"${m.get('totalCost', '?')}"
        )

    # per-step breakdown from transcripts
    for t in transcripts[:2]:  # analyze up to 2 most recent runs
        steps = extract_rollout_steps(t.get("data", []))
        parts.append(f"\n--- rollout from {t['file']} ({len(steps)} turns) ---")

        for i, step in enumerate(steps):
            # classify this turn
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

    # identify patterns
    all_steps = []
    for t in transcripts[:2]:
        all_steps.extend(extract_rollout_steps(t.get("data", [])))

    if all_steps:
        # wasted calls: same tool called multiple times in sequence with errors
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

        # tool usage distribution
        tool_counts: dict[str, int] = {}
        error_counts: dict[str, int] = {}
        for step in all_steps:
            for call in step["tool_calls"]:
                tool_counts[call["tool"]] = tool_counts.get(call["tool"], 0) + 1
            for r in step["tool_results"]:
                if "error" in r.lower():
                    # attribute to first tool in the turn
                    if step["tool_calls"]:
                        t_name = step["tool_calls"][0]["tool"]
                        error_counts[t_name] = error_counts.get(t_name, 0) + 1

        parts.append("\nTOOL USAGE:")
        for tool, count in sorted(tool_counts.items(), key=lambda x: -x[1]):
            err = error_counts.get(tool, 0)
            parts.append(f"  {tool}: {count} calls, {err} errors")

    return "\n".join(parts)


def _synthetic_rollout() -> str:
    """synthetic rollout for when no real data exists yet."""
    return """RUN: 15/30 steps, 180.0s, 45 api calls, 60 tool calls, $3.50
RUN: 22/30 steps, 240.0s, 55 api calls, 80 tool calls, $4.20

--- synthetic rollout ---
turn 1 [OK]: tools=['browser_navigate'] | results: navigated to challenge url
turn 2 [OK]: tools=['browser_evaluate'] | results: suppressed dialogs
turn 3 [OK]: tools=['browser_snapshot'] | results: aria tree with step 1
turn 4 [OK]: tools=['browser_action'] | results: clicked start button
turn 5 [FAIL]: tools=['browser_snapshot', 'browser_action'] | errors: ref e5 not found. take a new snapshot.
turn 6 [OK]: tools=['browser_snapshot', 'browser_action'] | results: clicked correct button after re-snapshot
turn 7 [FAIL]: tools=['browser_action', 'browser_action', 'browser_action'] | errors: dialog blocked interaction; dialog blocked interaction; dialog blocked interaction
turn 8 [OK]: tools=['browser_evaluate'] | results: dismissed dialog via js, suppressed future dialogs
turn 9 [FAIL]: tools=['browser_snapshot', 'browser_action'] | errors: element not interactable (hidden behind overlay)
turn 10 [OK]: tools=['browser_evaluate'] | results: read hidden text via getComputedStyle, found answer code
turn 11 [FAIL]: tools=['browser_snapshot', 'browser_snapshot', 'browser_snapshot'] | errors: agent took 3 snapshots without acting (wasted calls)
turn 12 [FAIL]: tools=['browser_action'] | errors: drag and drop failed with simple click

WASTED CALLS:
  - 3 errors in one turn: ['browser_action', 'browser_action', 'browser_action'] (dialog blocking)
  - 3 snapshots without action (indecisive)
  - drag/drop attempted with click instead of JS events

TOOL USAGE:
  browser_snapshot: 25 calls, 3 errors
  browser_action: 20 calls, 6 errors
  browser_evaluate: 10 calls, 0 errors
  browser_navigate: 5 calls, 0 errors

KEY FAILURES:
- stale refs after page transitions (need re-snapshot before action)
- dialogs blocking actions (need to suppress earlier and more aggressively)
- hidden elements not found via snapshot alone (need evaluate for CSS tricks)
- drag-and-drop not working via click (need JS dispatchEvent)
- agent being indecisive (multiple snapshots without acting)"""


# ---- metric (judge analyzes rollout) ----

def make_metric(judge_lm: dspy.LM, rollout: str) -> Callable:
    """create a metric where opus 4.6 judges the prompt against the actual rollout.

    the judge sees:
    1. the rollout (what happened)
    2. the proposed prompt (what the agent would be told)
    and evaluates whether the prompt would fix the observed failures.
    """

    def metric(
        gold: dspy.Example,
        pred: dspy.Prediction,
        trace: Optional[Any] = None,
        pred_name: Optional[str] = None,
        pred_trace: Optional[Any] = None,
    ) -> dspy.Prediction:
        prompt = pred.optimized_prompt

        judge_prompt = f"""You are evaluating a system prompt for a browser automation agent.

IMPORTANT: Do NOT reference specific step numbers, codes, passwords, or answers from the rollout.
Your suggestions must be GENERALIZABLE strategies that help with ANY step, not solutions to specific steps.
Focus on: tool usage patterns, error recovery strategies, efficiency improvements, DOM inspection techniques.

ACTUAL ROLLOUT (what happened when the agent ran):
---
{rollout[:10000]}
---

PROPOSED SYSTEM PROMPT (what the agent would be told next time):
---
{prompt[:5000]}
---

Analyze:
1. WHAT WENT WRONG in the rollout? List the top 3-5 failures (describe patterns, not specific step solutions).
2. WHAT WENT RIGHT? List the top 3 successes.
3. Does the proposed prompt DIRECTLY ADDRESS each failure? For each failure, say YES/NO and why.
4. Does the prompt introduce any BAD ADVICE that could cause new failures?
5. Is the prompt CONCISE enough? (verbose prompts waste tokens and confuse the agent)

Score 0.0-1.0 where:
- 1.0 = prompt fixes all observed failures without introducing new problems
- 0.7 = prompt fixes most failures
- 0.5 = prompt is generic and doesn't specifically address rollout failures
- 0.3 = prompt misses critical failures or adds bad advice
- 0.0 = prompt would make things worse

Respond with JSON:
{{
  "what_went_wrong": ["failure1", "failure2", ...],
  "what_went_right": ["success1", "success2", ...],
  "failure_coverage": {{"failure1": "YES/NO - reason", ...}},
  "bad_advice": ["any problematic instructions"],
  "score": 0.0-1.0,
  "feedback": "concrete summary of what to improve in the prompt"
}}"""

        with dspy.context(lm=judge_lm):
            judge_response = dspy.Predict("prompt -> evaluation")(prompt=judge_prompt)

        try:
            eval_text = judge_response.evaluation
            if "{" in eval_text:
                json_str = eval_text[eval_text.index("{") : eval_text.rindex("}") + 1]
                result = json.loads(json_str)
                score = float(result.get("score", 0.5))

                # build rich feedback from the analysis
                feedback_parts = []

                wrong = result.get("what_went_wrong", [])
                if wrong:
                    feedback_parts.append("FAILURES: " + "; ".join(wrong[:5]))

                right = result.get("what_went_right", [])
                if right:
                    feedback_parts.append("SUCCESSES: " + "; ".join(right[:3]))

                coverage = result.get("failure_coverage", {})
                uncovered = [
                    k for k, v in coverage.items()
                    if isinstance(v, str) and v.upper().startswith("NO")
                ]
                if uncovered:
                    feedback_parts.append("UNCOVERED FAILURES: " + "; ".join(uncovered))

                bad = result.get("bad_advice", [])
                if bad:
                    feedback_parts.append("BAD ADVICE TO REMOVE: " + "; ".join(bad))

                summary = result.get("feedback", "")
                if summary:
                    feedback_parts.append("SUMMARY: " + summary)

                feedback = "\n".join(feedback_parts) if feedback_parts else summary
            else:
                score = 0.5
                feedback = eval_text
        except Exception as e:
            score = 0.5
            feedback = f"failed to parse judge response: {e}"

        return dspy.Prediction(score=score, feedback=feedback)

    return metric


# ---- training data ----

def build_trainset(rollout: str) -> list[dspy.Example]:
    """build training examples from the rollout analysis."""
    examples = []
    for i in range(5):
        example = dspy.Example(
            rollout_analysis=rollout,
        ).with_inputs("rollout_analysis")
        examples.append(example)
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


# ---- programmatic API for train_loop ----

def analyze_trajectories(
    transcript_files: list[str | Path],
    metric_files: list[str | Path],
) -> str:
    """Build rollout analysis from explicit file paths (not scanning runs/).

    Like analyze_rollout() but takes specific files, useful for the training
    loop's sliding window of recent trajectories.
    """
    transcripts = []
    for f in transcript_files:
        try:
            data = json.loads(Path(f).read_text())
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
        return _synthetic_rollout()

    parts = []

    # overall stats
    for m in metrics:
        parts.append(
            f"RUN: {m.get('stepsCompleted', '?')}/30 steps, "
            f"{m.get('agentDurationMs', 0) / 1000:.1f}s, "
            f"{m.get('totalApiCalls', '?')} api calls, "
            f"{m.get('totalToolCalls', '?')} tool calls, "
            f"${m.get('totalCost', '?')}"
        )

    # per-step breakdown from ALL provided transcripts (not just 2)
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

    # identify patterns across all transcripts
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


def run_optimization(
    transcript_files: list[str | Path],
    metric_files: list[str | Path],
    generator_model: str = "anthropic/claude-opus-4.5",
    judge_model: str = "anthropic/claude-opus-4.5",
) -> dict:
    """Programmatic entry point for GEPA optimization.

    Returns dict with:
        optimized_prompt: str
        judge_feedback: list[dict]  - raw judge evaluations
        rollout_analysis: str       - the analysis text fed to GEPA
    """
    try:
        import dotenv
        dotenv.load_dotenv(SCRIPT_DIR.parent / ".env")
    except Exception:
        pass

    openrouter_key = os.getenv("OPENROUTER_API_KEY")
    if not openrouter_key:
        raise ValueError("OPENROUTER_API_KEY required in env or .env")

    # build rollout analysis from provided files
    rollout = analyze_trajectories(transcript_files, metric_files)

    # configure dspy
    dspy.configure(lm=build_lm(openrouter_key, generator_model))

    judge_lm = dspy.LM(
        model=f"openrouter/{judge_model}",
        api_key=openrouter_key,
        temperature=0.2,
    )
    reflection_lm = dspy.LM(
        model=f"openrouter/{generator_model}",
        api_key=openrouter_key,
        temperature=1.0,
        max_tokens=4096,
    )

    program = PromptGenerator()
    metric = make_metric(judge_lm, rollout)

    trainset = build_trainset(rollout)

    optimizer = dspy.GEPA(
        metric=metric,
        max_metric_calls=75,
        track_stats=True,
        reflection_minibatch_size=3,
        reflection_lm=reflection_lm,
    )
    compiled = optimizer.compile(program, trainset=trainset)

    # extract optimized prompt
    optimized_prompt = extract_instructions(compiled)
    if not optimized_prompt:
        # fallback: run the compiled program to get output
        prediction = compiled(rollout_analysis=rollout)
        optimized_prompt = prediction.optimized_prompt

    # collect judge feedback from optimizer stats
    judge_feedback = []
    if hasattr(optimizer, "stats"):
        stats = optimizer.stats
        if isinstance(stats, dict):
            judge_feedback = stats.get("evaluations", [])
        elif isinstance(stats, list):
            judge_feedback = stats

    # if no stats available, run one evaluation to get feedback
    if not judge_feedback:
        eval_result = metric(
            dspy.Example(rollout_analysis=rollout).with_inputs("rollout_analysis"),
            dspy.Prediction(optimized_prompt=optimized_prompt),
        )
        judge_feedback = [{
            "score": eval_result.score,
            "feedback": eval_result.feedback,
        }]

    return {
        "optimized_prompt": optimized_prompt,
        "judge_feedback": judge_feedback,
        "rollout_analysis": rollout,
    }


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

    # analyze rollouts
    rollout = analyze_rollout(RUNS_DIR)
    print("rollout analysis:")
    print(rollout[:1000])
    print("...")

    program = PromptGenerator()
    metric = make_metric(judge_lm, rollout)

    if args.optimize:
        print("\n" + "=" * 60)
        print("GEPA: optimizing prompt based on rollout analysis")
        print("=" * 60)

        trainset = build_trainset(rollout)
        print(f"training examples: {len(trainset)}")

        optimizer = dspy.GEPA(
            metric=metric,
            max_metric_calls=75,
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
            optimized = prompt_path.read_text()
            SYSTEM_PROMPT_PATH.write_text(optimized)
            print(f"applied optimized prompt to: {SYSTEM_PROMPT_PATH}")

    # test
    print("\n" + "=" * 60)
    print("TEST: generating prompt from rollout")
    print("=" * 60)

    prediction = program(rollout_analysis=rollout)
    print("\ngenerated prompt:")
    print("-" * 40)
    print(prediction.optimized_prompt[:2000])
    print("-" * 40)


if __name__ == "__main__":
    main()
