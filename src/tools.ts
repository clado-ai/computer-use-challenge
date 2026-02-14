import {
  chromium,
  type BrowserContext,
  type Page,
} from "playwright";
import type OpenAI from "openai";
import * as path from "node:path";
import * as fs from "node:fs";
let context: BrowserContext | null = null;
let currentPage: Page | null = null;

const PROFILE_DIR = process.env.BROWSER_DATA_DIR
  ? path.resolve(process.env.BROWSER_DATA_DIR)
  : path.resolve(import.meta.dir, "..", ".browser-data");

async function launchBrowser(): Promise<BrowserContext> {
  fs.mkdirSync(PROFILE_DIR, { recursive: true });
  console.log("[browser] launching chromium...");
  const headless = process.env.HEADLESS !== "false";
  const ctx = await chromium.launchPersistentContext(PROFILE_DIR, {
    headless,
    viewport: { width: 1280, height: 720 },
    args: [
      "--disable-blink-features=AutomationControlled", // a bunch of stuff here to just save compute
      "--disable-dev-shm-usage",
      "--disable-gpu",
      "--no-sandbox",
      "--disable-extensions",
      "--js-flags=--max-old-space-size=512"
    ],
    timeout: 30000,
  });
  console.log("[browser] ready");
  return ctx;
}

function isContextAlive(ctx: BrowserContext): boolean {
  try {
    ctx.pages();
    return true;
  } catch {
    return false;
  }
}

async function ensurePage(): Promise<Page> {
  if (currentPage && !currentPage.isClosed() && context && isContextAlive(context)) {
    return currentPage;
  }

  if (context) {
    if (!isContextAlive(context)) {
      console.log("[browser] context died, re-launching...");
    }
    try { await context.close(); } catch { /* already dead */ }
    context = null;
    currentPage = null;
  }

  context = await launchBrowser();

  const pages = context.pages();
  currentPage = pages[0] ?? (await context.newPage());

  currentPage.on("dialog", async (dialog) => {
    try {
      await dialog.dismiss();
    } catch {
      /* */
    }
  });

  return currentPage;
}

export const toolDefinitions: OpenAI.Chat.Completions.ChatCompletionTool[] = [
  {
    type: "function",
    function: {
      name: "browser_navigate",
      description:
        "Navigate to a URL. Automatically suppresses all JavaScript dialogs (alert, confirm, prompt). Returns the page title and URL after loading.",
      parameters: {
        type: "object",
        properties: {
          url: { type: "string", description: "The URL to navigate to" },
        },
        required: ["url"],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "browser_evaluate",
      description:
        "Execute JavaScript in the browser page context. Returns the JSON-serialized result. Use for: reading hidden DOM content, manipulating elements, dismissing popups, extracting data, dispatching events.",
      parameters: {
        type: "object",
        properties: {
          script: {
            type: "string",
            description:
              "JavaScript code to execute. Must be an expression or IIFE that returns a value.",
          },
        },
        required: ["script"],
      },
    },
  },
];

async function executeToolInner(
  name: string,
  input: Record<string, unknown>,
): Promise<string> {
  switch (name) {
    case "browser_navigate":
      return await toolNavigate(input.url as string);
    case "browser_evaluate":
      return await toolEvaluate(input.script as string);
    default:
      return `unknown tool: ${name}`;
  }
}

export async function executeTool(
  name: string,
  input: Record<string, unknown>,
): Promise<string> {
  try {
    return await executeToolInner(name, input);
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    if (msg.includes("has been closed") || msg.includes("Target closed")) {
      console.log("[browser] context lost — browser crashed");
      context = null;
      currentPage = null;
      return `error: browser crashed. Use browser_navigate to reload the challenge URL and continue from where you left off.`;
    }
    return `error: ${msg}`;
  }
}

async function toolNavigate(url: string): Promise<string> {
  const page = await ensurePage();

  await page.goto(url, { waitUntil: "domcontentloaded", timeout: 15000 });

  await page.evaluate(`
    window.alert = () => {};
    window.confirm = () => true;
    window.prompt = () => "";
    window.onbeforeunload = null;
    // Prevent form submissions from navigating (destroys Playwright execution context)
    document.addEventListener('submit', e => e.preventDefault(), true);
  `);

  // brief wait for dynamic content
  await page.waitForTimeout(500);

  const title = await page.title();
  const currentUrl = page.url();
  return `navigated to: ${currentUrl}\ntitle: ${title}`;
}

async function toolEvaluate(script: string): Promise<string> {
  const page = await ensurePage();
  try {
    const result = await Promise.race([
      page.evaluate(script),
      new Promise((_, reject) =>
        setTimeout(() => reject(new Error("evaluate timed out after 30s")), 30000),
      ),
    ]);
    if (result === undefined || result === null) return "null";
    return typeof result === "string" ? result : JSON.stringify(result, null, 2);
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);

    if (msg.includes("Execution context was destroyed")) {
      await new Promise(r => setTimeout(r, 800));
      try {
        const pg = await ensurePage();
        const settled = await pg.evaluate(
          "document.querySelector('h1')?.parentElement?.innerText?.substring(0, 800)",
        );
        return `[page reloaded after submit] ${settled || "(empty page)"}`;
      } catch {
        return `error: evaluate: ${msg}`;
      }
    }

    return `error: evaluate: ${msg}`;
  }
}

export async function cleanup() {
  if (context) {
    try {
      await context.close();
    } catch {
      /* */
    }
    context = null;
  }
  currentPage = null;
}
