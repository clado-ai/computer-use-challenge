import { runAgent } from "./agent.js";
import { cleanup } from "./tools.js";

async function main() {
  console.log("=== Computer Use Challenge Agent ===\n");

  try {
    const result = await runAgent();

    const m = result.metrics;
    console.log("\n=== Run Summary ===");
    console.log(`steps completed: ${m.stepsCompleted}/${m.totalSteps}`);
    console.log(`total time: ${(m.totalDurationMs / 1000).toFixed(1)}s`);
    console.log(`agent time: ${(m.agentDurationMs / 1000).toFixed(1)}s`);
    console.log(`api calls: ${m.totalApiCalls}`);
    console.log(`tool calls: ${m.totalToolCalls}`);
    console.log(`tokens: ${m.totalInputTokens.toLocaleString()} in / ${m.totalOutputTokens.toLocaleString()} out`);
    console.log(`cost: $${m.totalCost}`);
  } catch (err) {
    console.error("agent failed:", err);
    process.exit(1);
  } finally {
    await cleanup();
  }
}

main();
