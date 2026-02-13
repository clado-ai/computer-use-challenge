import Anthropic from "@anthropic-ai/sdk";
import { toolDefinitions, executeTool } from "./tools.js";
import { MetricsTracker } from "./metrics.js";
import * as fs from "node:fs";
import * as path from "node:path";

const MODEL = "claude-haiku-4-5-20251001";
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

    // count tool calls in this response
    const toolUseBlocks = response.content.filter(
      (b): b is Anthropic.Messages.ToolUseBlock => b.type === "tool_use",
    );
    metrics.addApiCall(
      response.usage.input_tokens,
      response.usage.output_tokens,
      toolUseBlocks.length,
    );

    // log text blocks
    for (const block of response.content) {
      if (block.type === "text" && block.text.trim()) {
        console.log(`[agent] ${block.text.trim().slice(0, 200)}`);

        // detect step completion from agent reasoning
        // "Step N" means we're viewing step N, so we completed N-1
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
    }

    // check if we hit step target
    if (stepsCompleted >= MAX_STEPS) {
      console.log(`\n[agent] reached step target (${stepsCompleted}/${MAX_STEPS}), stopping.`);
      break;
    }

    messages.push({ role: "assistant", content: response.content });
    messages.push({ role: "user", content: toolResults });
    transcript.push({ role: "user", content: toolResults });

    // context management: if messages are getting long, trim old tool results
    if (messages.length > 60) {
      trimContext(messages);
    }
  }

  if (turnCount >= MAX_TURNS) {
    console.log(`\n[agent] reached turn limit (${MAX_TURNS}), stopping.`);
  }

  metrics.endAgent();
  const report = metrics.getReport(CHALLENGE_URL, MODEL, stepsCompleted);
  return { stepsCompleted, metrics: report, transcript };
}

function trimContext(messages: Anthropic.Messages.MessageParam[]) {
  // keep first message (initial prompt) + last 40 messages
  // replace middle tool results with summaries
  if (messages.length <= 42) return;

  const keep = 40;
  const toTrim = messages.length - keep - 1; // -1 for first message
  for (let i = 1; i < toTrim + 1 && i < messages.length - keep; i++) {
    const msg = messages[i];
    if (!msg) continue;
    if (msg.role === "user" && Array.isArray(msg.content)) {
      // replace tool results with short summaries
      msg.content = (msg.content as Anthropic.Messages.ToolResultBlockParam[]).map((block) => {
        if (block.type === "tool_result" && typeof block.content === "string" && block.content.length > 200) {
          return { ...block, content: block.content.slice(0, 200) + "... (trimmed)" };
        }
        return block;
      });
    }
  }
}
