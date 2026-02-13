import OpenAI from "openai";
import { toolDefinitions, executeTool } from "./tools.js";
import { MetricsTracker } from "./metrics.js";
import * as fs from "node:fs";
import * as path from "node:path";

const MODEL = "openai/gpt-oss-120b";
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
  const client = new OpenAI({
    baseURL: "https://openrouter.ai/api/v1",
    apiKey: process.env.OPENROUTER_API_KEY,
  });
  const metrics = new MetricsTracker();
  const transcript: Array<{ role: string; content: unknown }> = [];

  const messages: OpenAI.Chat.Completions.ChatCompletionMessageParam[] = [
    { role: "system", content: SYSTEM_PROMPT },
    {
      role: "user",
      content: `Navigate to ${CHALLENGE_URL} and solve all 30 challenges. Start now.`,
    },
  ];

  let stepsCompleted = 0;
  let prevStepsCompleted = 0;
  let turnCount = 0;
  let interrupted = false;

  const onSigint = () => { interrupted = true; console.log("\n[agent] interrupted, saving..."); };
  process.on("SIGINT", onSigint);

  metrics.startAgent();
  console.log(`starting agent with ${MODEL} (max turns: ${MAX_TURNS}, target steps: ${MAX_STEPS})...`);
  console.log(`challenge: ${CHALLENGE_URL}\n`);

  while (turnCount < MAX_TURNS && !interrupted) {
    turnCount++;

    let response: OpenAI.Chat.Completions.ChatCompletion;
    try {
      response = await client.chat.completions.create({
        model: MODEL,
        max_tokens: MAX_TOKENS,
        tools: toolDefinitions,
        messages,
        // @ts-expect-error OpenRouter-specific: pin to Groq provider
        provider: { order: ["Groq"], allow_fallbacks: false },
      });
    } catch (err) {
      const apiErr = err as { status?: number; error?: unknown; message?: string };
      console.error(`[api error] status=${apiErr.status} message=${apiErr.message}`);
      console.error(`[api error] details:`, JSON.stringify(apiErr.error, null, 2));
      // retry once after 2s
      if (apiErr.status === 400 || apiErr.status === 502 || apiErr.status === 503) {
        console.log("[api] retrying in 2s...");
        await new Promise(r => setTimeout(r, 2000));
        try {
          response = await client.chat.completions.create({
            model: MODEL,
            max_tokens: MAX_TOKENS,
            tools: toolDefinitions,
            messages,
            // @ts-expect-error OpenRouter-specific: pin to Groq provider
            provider: { order: ["Groq"], allow_fallbacks: false },
          });
        } catch (retryErr) {
          console.error("[api] retry also failed, stopping");
          break;
        }
      } else {
        throw err;
      }
    }

    const choice = response.choices[0];
    if (!choice) {
      console.log("\n[agent] no choice in response");
      break;
    }

    const message = choice.message;
    const toolCalls = message.tool_calls ?? [];

    metrics.addApiCall(
      response.usage?.prompt_tokens ?? 0,
      response.usage?.completion_tokens ?? 0,
      toolCalls.length,
    );

    if (message.content?.trim()) {
      console.log(`[agent] ${message.content.trim().slice(0, 200)}`);

      const stepMatch = message.content.match(/step\s+(\d+)/i);
      if (stepMatch) {
        const stepNum = parseInt(stepMatch[1] ?? "0", 10);
        const completed = stepNum - 1;
        if (completed > stepsCompleted && completed <= 30) {
          stepsCompleted = completed;
          console.log(`  >> completed step ${stepsCompleted}/30 (now on step ${stepNum})`);
        }
      }
    }

    // save to transcript
    transcript.push({ role: "assistant", content: message });

    if (choice.finish_reason === "stop" || toolCalls.length === 0) {
      if (choice.finish_reason === "stop") {
        console.log("\n[agent] finished (stop)");
      } else {
        console.log(`\n[agent] stopped: ${choice.finish_reason}`);
      }
      break;
    }

    // append assistant message (with tool_calls) to conversation
    // strip `refusal` — Groq rejects it as unsupported
    const { refusal, ...cleanMessage } = message as typeof message & { refusal?: unknown };
    messages.push(cleanMessage);

    // execute tool calls
    for (const toolCall of toolCalls) {
      if (toolCall.type !== "function") continue;
      const toolName = toolCall.function.name;
      let toolInput: Record<string, unknown>;
      try {
        toolInput = JSON.parse(toolCall.function.arguments);
      } catch {
        toolInput = {};
      }

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

      // append tool result message
      messages.push({
        role: "tool",
        tool_call_id: toolCall.id,
        content: truncated,
      });

      transcript.push({ role: "tool", content: { tool_call_id: toolCall.id, name: toolName, result: truncated } });

      // detect step from tool results
      const resultStepMatch = result.match(/(?:step|challenge)\s+(\d+)/i);
      if (resultStepMatch) {
        const stepNum = parseInt(resultStepMatch[1] ?? "0", 10);
        const completed = stepNum - 1;
        if (completed > stepsCompleted && completed <= 30) {
          stepsCompleted = completed;
        }
      }

      if (stepsCompleted >= MAX_STEPS) {
        break;
      }
    }

    // check if we hit step target
    if (stepsCompleted >= MAX_STEPS) {
      console.log(`\n[agent] reached step target (${stepsCompleted}/${MAX_STEPS}), stopping.`);
      break;
    }

    // clear context on step transition, but keep last tool result so agent knows the new step
    if (stepsCompleted > prevStepsCompleted) {
      console.log(`[context] step ${prevStepsCompleted} → ${stepsCompleted}, clearing context`);
      const nextStep = stepsCompleted + 1;
      // grab the last tool result to carry forward
      const lastToolMsg = [...messages].reverse().find(m => m.role === "tool");
      const lastResult = lastToolMsg && "content" in lastToolMsg ? String(lastToolMsg.content) : "";
      messages.length = 0;
      messages.push(
        { role: "system", content: SYSTEM_PROMPT },
        {
          role: "user",
          content: `You are now on step ${nextStep} of 30. The browser is already open — do NOT use browser_navigate. Solve this step. Here is the current page content:\n\n${lastResult}`,
        },
      );
      prevStepsCompleted = stepsCompleted;
    }
  }

  process.removeListener("SIGINT", onSigint);

  if (turnCount >= MAX_TURNS) {
    console.log(`\n[agent] reached turn limit (${MAX_TURNS}), stopping.`);
  }

  metrics.endAgent();
  const report = metrics.getReport(CHALLENGE_URL, MODEL, stepsCompleted);
  return { stepsCompleted, metrics: report, transcript };
}
