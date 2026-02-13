import { runAgent } from "./agent.js";
import { cleanup } from "./tools.js";
import * as fs from "node:fs";
import * as path from "node:path";

async function main() {
  console.log("=== Computer Use Challenge Agent ===\n");

  // ensure runs directory exists
  const runsDir = path.join(import.meta.dir, "..", "runs");
  fs.mkdirSync(runsDir, { recursive: true });

  try {
    const result = await runAgent();

    // save metrics
    const timestamp = new Date().toISOString().replace(/[:.]/g, "-");
    const metricsPath = path.join(runsDir, `run_${timestamp}.json`);
    fs.writeFileSync(metricsPath, JSON.stringify(result.metrics, null, 2));
    console.log(`\nmetrics saved to: ${metricsPath}`);

    // save transcript
    const transcriptPath = path.join(runsDir, `transcript_${timestamp}.json`);
    fs.writeFileSync(transcriptPath, JSON.stringify(result.transcript, null, 2));
    console.log(`transcript saved to: ${transcriptPath}`);

    // print summary
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
