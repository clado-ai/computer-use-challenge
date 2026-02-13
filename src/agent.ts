import Anthropic from "@anthropic-ai/sdk";
import { toolDefinitions, executeTool } from "./tools.js";
import { MetricsTracker } from "./metrics.js";
import * as fs from "node:fs";
import * as path from "node:path";

const MODEL = "claude-opus-4-6";
const MAX_TOKENS = 8192;
const CHALLENGE_URL = "https://serene-frangipane-7fd25b.netlify.app";
const MAX_TURNS = parseInt(process.env.MAX_TURNS || "300", 10);
const MAX_STEPS = parseInt(process.env.MAX_STEPS || "30", 10);

// load system prompt
const systemPromptPath = path.join(import.meta.dir, "prompts", "SYSTEM.md");
const SYSTEM_PROMPT = fs.readFileSync(systemPromptPath, "utf-8");

export interface AgentResult {
  stepsCompleted: number;
  metrics: ReturnType<MetricsTracker["getReport"]>;
  transcript: Array<{ role: string; content: unknown }>;
}

export async function runAgent(): Promise<AgentResult> {
  const client = new Anthropic();
  const metrics = new MetricsTracker();
  const transcript: Array<{ role: string; content: unknown }> = [];

  const messages: Anthropic.Messages.MessageParam[] = [
    {
      role: "user",
      content: `Navigate to ${CHALLENGE_URL} and solve all 30 challenges. Start now.`,
    },
  ];

  let stepsCompleted = 0;
  let prevStepsCompleted = 0;
  let turnCount = 0;

  metrics.startAgent();
  console.log(`starting agent with ${MODEL} (max turns: ${MAX_TURNS}, target steps: ${MAX_STEPS})...`);
  console.log(`challenge: ${CHALLENGE_URL}\n`);

  while (turnCount < MAX_TURNS) {
    turnCount++;

    const response = await client.messages.create({
      model: MODEL,
      max_tokens: MAX_TOKENS,
      system: SYSTEM_PROMPT,
      tools: toolDefinitions,
      messages,
    });

    const toolUseBlocks = response.content.filter(
      (b): b is Anthropic.Messages.ToolUseBlock => b.type === "tool_use",
    );
    metrics.addApiCall(
      response.usage.input_tokens,
      response.usage.output_tokens,
      toolUseBlocks.length,
    );

    for (const block of response.content) {
      if (block.type === "text" && block.text.trim()) {
        console.log(`[agent] ${block.text.trim().slice(0, 200)}`);

        const stepMatch = block.text.match(/step\s+(\d+)/i);
        if (stepMatch) {
          const stepNum = parseInt(stepMatch[1]!, 10);
          const completed = stepNum - 1;
          if (completed > stepsCompleted && completed <= 30) {
            stepsCompleted = completed;
            console.log(`  >> completed step ${stepsCompleted}/30 (now on step ${stepNum})`);
          }
        }
      }
    }

    // save to transcript
    transcript.push({ role: "assistant", content: response.content });

    if (response.stop_reason === "end_turn") {
      console.log("\n[agent] finished (end_turn)");
      break;
    }

    if (response.stop_reason !== "tool_use" || toolUseBlocks.length === 0) {
      console.log(`\n[agent] stopped: ${response.stop_reason}`);
      break;
    }

    // execute tool calls
    const toolResults: Anthropic.Messages.ToolResultBlockParam[] = [];
    for (const toolUse of toolUseBlocks) {
      const toolName = toolUse.name;
      const toolInput = toolUse.input as Record<string, unknown>;

      const shortInput =
        toolName === "browser_evaluate"
          ? (toolInput.script as string)?.slice(0, 80) + "..."
          : toolName === "browser_navigate"
            ? toolInput.url
            : toolName === "browser_action"
              ? `${toolInput.action} ${toolInput.ref || ""} ${toolInput.value || ""}`.trim()
              : "";
      console.log(`  [tool] ${toolName}(${shortInput})`);

      const result = await executeTool(toolName, toolInput);

      // truncate large results to save context
      const truncated =
        result.length > 4000
          ? result.slice(0, 4000) + "\n... (truncated)"
          : result;

      console.log(`  [result] ${truncated.slice(0, 120).replace(/\n/g, " ")}...`);

      toolResults.push({
        type: "tool_result",
        tool_use_id: toolUse.id,
        content: truncated,
      });

      // detect step from tool results (e.g. "Step 5" visible in snapshot)
      // "Step N" in results means we're on step N, so completed N-1
      const resultStepMatch = result.match(/(?:step|challenge)\s+(\d+)/i);
      if (resultStepMatch) {
        const stepNum = parseInt(resultStepMatch[1]!, 10);
        const completed = stepNum - 1;
        if (completed > stepsCompleted && completed <= 30) {
          stepsCompleted = completed;
        }
      }

      // early exit: stop processing more tool results once we hit target
      if (stepsCompleted >= MAX_STEPS) {
        break;
      }
    }

    // check if we hit step target
    if (stepsCompleted >= MAX_STEPS) {
      console.log(`\n[agent] reached step target (${stepsCompleted}/${MAX_STEPS}), stopping.`);
      break;
    }

    messages.push({ role: "assistant", content: response.content });
    messages.push({ role: "user", content: toolResults });
    transcript.push({ role: "user", content: toolResults });

    // clear context on step transition — replace with step-aware prompt
    if (stepsCompleted > prevStepsCompleted) {
      console.log(`[context] step ${prevStepsCompleted} → ${stepsCompleted}, clearing context`);
      const nextStep = stepsCompleted + 1;
      messages.length = 0;
      messages.push({
        role: "user",
        content: `You are on step ${nextStep} of 30. The browser is already open on the challenge page — do NOT use browser_navigate. Take a snapshot and solve this step.`,
      });
      prevStepsCompleted = stepsCompleted;
    }
  }

  if (turnCount >= MAX_TURNS) {
    console.log(`\n[agent] reached turn limit (${MAX_TURNS}), stopping.`);
  }

  metrics.endAgent();
  const report = metrics.getReport(CHALLENGE_URL, MODEL, stepsCompleted);
  return { stepsCompleted, metrics: report, transcript };
}
