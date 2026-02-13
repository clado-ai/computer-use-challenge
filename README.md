# Computer Use Challenge Agent

Solves all 30 steps of the [Browser Navigation Challenge](https://serene-frangipane-7fd25b.netlify.app) using Claude Opus 4.6 with browser automation.

## Architecture

- **Agent**: Claude Opus 4.6 via Anthropic SDK (tool-use loop)
- **Browser**: [dev-browser](../dev-browser/) for persistent Chromium with ARIA snapshots
- **Tools**: 4 browser tools (navigate, snapshot, action, evaluate)
- **Metrics**: Time, token, cost tracking per step and overall

## Setup

```bash
# install dependencies
bun install

# copy env and add your Anthropic API key
cp .env.example .env

# install playwright chromium (if not already installed)
bunx playwright install chromium
```

## Run

```bash
# terminal 1: start the dev-browser server
bun run start-server

# terminal 2: run the agent
bun run start
```

## Output

Results are saved to `runs/`:
- `run_<timestamp>.json` — metrics (time, tokens, cost, steps)
- `transcript_<timestamp>.json` — full agent conversation transcript

## Prompt Optimization (DSPy)

After collecting run transcripts, optimize the system prompt:

```bash
cd dspy && uv run optimize.py
```

## Tools

| Tool | Description |
|------|-------------|
| `browser_navigate` | Navigate to URL, auto-dismiss JS dialogs |
| `browser_snapshot` | ARIA accessibility tree with element refs |
| `browser_action` | Click, type, select, press, scroll by ref |
| `browser_evaluate` | Execute arbitrary JavaScript on the page |
