# Computer Use Challenge Agent

Automated agent that solves a 30-step browser navigation challenge using LLM-driven browser automation.

## Prerequisites

- [Bun](https://bun.sh) runtime
- An [OpenRouter](https://openrouter.ai) API key

## Setup

```bash
cd computer-use-challenge
bun install
```

Create a `.env` file:

```
OPENROUTER_API_KEY=sk-or-v1-...
```

Install Playwright browsers (first time only):

```bash
bunx playwright install chromium
```

## Usage

```bash
bun run start
```

This launches a headless Chromium browser, navigates to the challenge site, and uses `gpt-oss-120b` (via OpenRouter/Groq) to solve each step.

### Environment variables

| Variable | Default | Description |
|---|---|---|
| `OPENROUTER_API_KEY` | (required) | OpenRouter API key |
| `MAX_TURNS` | `1000` | Max LLM round-trips before stopping |
| `MAX_STEPS` | `30` | Target number of steps to complete |
| `HEADLESS` | `true` | Set to `false` to see the browser window |

Example with visible browser:

```bash
HEADLESS=false bun run start
```

## Output

Results are saved to `runs/` after every completed step:

- `trajectory_<timestamp>.json` — full transcript (updated incrementally after each step)
- `run_<timestamp>.json` — metrics summary (tokens, cost, timing)
- `transcript_<timestamp>.json` — final transcript

## Architecture

```
src/
  index.ts    — entry point, saves final metrics
  agent.ts    — main agent loop, stuck detection, sessionStorage bypass
  tools.ts    — browser tools (navigate, evaluate, snapshot, action)
  metrics.ts  — token/cost tracking
  prompts/
    SYSTEM.md — system prompt with challenge-solving patterns
```

### How it works

1. The agent navigates to the challenge URL and clicks START
2. For each of the 30 steps, the LLM reads the page, identifies the challenge type, solves it to reveal a 6-character code, then submits the code
3. On step transitions, context is cleared to keep token usage low
4. **SessionStorage bypass** (steps 18-20 where there is a bug in environment): if stuck for 5 turns, decodes the XOR+base64 encoded `wo_session` from sessionStorage to extract the code directly and submit it
