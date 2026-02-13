# Start Challenge

Runs the browser automation agent to solve all 30 steps of the Computer Use Challenge.

## Prerequisites

1. dev-browser server must be running:
   ```bash
   cd ../dev-browser/skills/dev-browser && bun run scripts/start-server.ts
   ```

2. `ANTHROPIC_API_KEY` must be set in `.env` or environment.

## Run

```bash
cd computer-use-challenge && bun run src/index.ts
```

## What it does

1. Launches the agent (Claude Opus 4.6) with 4 browser tools
2. Agent navigates to the challenge URL
3. Suppresses all JS dialogs (alert/confirm/prompt)
4. Solves each of 30 steps by observing DOM via ARIA snapshots and acting
5. Outputs metrics: time, tokens, cost, steps completed
6. Saves run data to `runs/` directory
