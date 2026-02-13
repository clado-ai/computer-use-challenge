import {
  chromium,
  type BrowserContext,
  type Page,
  type ElementHandle,
} from "playwright";
import type OpenAI from "openai";
import * as path from "node:path";
import * as fs from "node:fs";

// ---- browser management (direct launch, no server needed) ----

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
      "--disable-blink-features=AutomationControlled",
      "--disable-dev-shm-usage",     // prevent /dev/shm OOM crashes
      "--disable-gpu",                // reduce memory usage
      "--no-sandbox",                 // avoid sandbox-related crashes
      "--disable-extensions",
      "--js-flags=--max-old-space-size=512",  // cap V8 heap
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

const SNAPSHOT_SCRIPT_PATH = path.resolve(
  import.meta.dir,
  "../../dev-browser/skills/dev-browser/src/snapshot/browser-script.ts",
);

let snapshotScriptCache: string | null = null;
async function getSnapshotScript(): Promise<string> {
  if (snapshotScriptCache) return snapshotScriptCache;
  const mod = await import(SNAPSHOT_SCRIPT_PATH);
  snapshotScriptCache = mod.getSnapshotScript() as string;
  return snapshotScriptCache;
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
      name: "browser_snapshot",
      description:
        "Get an ARIA accessibility snapshot of the current page. Returns a YAML tree with interactive elements labeled [ref=eN]. Use these refs with browser_action.",
      parameters: {
        type: "object",
        properties: {},
      },
    },
  },
  {
    type: "function",
    function: {
      name: "browser_action",
      description:
        'Perform an action on the page. Actions: "click" (click element by ref), "type" (type text into element by ref), "select" (select option by ref), "press" (press a key like Enter, Tab, Escape), "scroll" (scroll by ref or page). Use refs from browser_snapshot.',
      parameters: {
        type: "object",
        properties: {
          action: {
            type: "string",
            enum: ["click", "type", "select", "press", "scroll"],
            description: "The action to perform",
          },
          ref: {
            type: "string",
            description:
              "Element ref from snapshot (e.g. 'e5'). Required for click, type, select.",
          },
          value: {
            type: "string",
            description:
              "Text to type, option to select, key to press, or scroll direction (up/down).",
          },
        },
        required: ["action"],
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

// ---- tool execution ----

async function executeToolInner(
  name: string,
  input: Record<string, unknown>,
): Promise<string> {
  switch (name) {
    case "browser_navigate":
      return await toolNavigate(input.url as string);
    case "browser_snapshot":
      return await toolSnapshot();
    case "browser_action":
      return await toolAction(
        input.action as string,
        input.ref as string | undefined,
        input.value as string | undefined,
      );
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
    // if the browser context died, reset state but DON'T silently relaunch
    // — the agent needs to know so it can re-navigate
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

  // suppress JS dialog functions
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

async function toolSnapshot(): Promise<string> {
  const page = await ensurePage();

  const script = await getSnapshotScript();
  const snapshot = await page.evaluate((s: string) => {
    // biome-ignore lint/suspicious/noExplicitAny: browser globalThis has dynamic properties
    const w = globalThis as any;
    if (!w.__devBrowser_getAISnapshot) {
      // biome-ignore lint/security/noGlobalEval: injecting snapshot script into browser context
      eval(s);
    }
    return w.__devBrowser_getAISnapshot();
  }, script);

  return snapshot as string;
}

async function toolAction(
  action: string,
  ref?: string,
  value?: string,
): Promise<string> {
  const page = await ensurePage();

  if (action === "press") {
    await page.keyboard.press(value || "Enter");
    return `pressed: ${value || "Enter"}`;
  }

  if (action === "scroll") {
    if (ref) {
      const el = await resolveRef(page, ref);
      if (el) {
        await el.scrollIntoViewIfNeeded();
        return `scrolled to ref ${ref}`;
      }
    }
    const direction = value === "up" ? -500 : 500;
    await page.mouse.wheel(0, direction);
    return `scrolled ${value || "down"}`;
  }

  if (!ref) return "error: ref is required for click/type/select actions";

  const el = await resolveRef(page, ref);
  if (!el) return `error: ref ${ref} not found. take a new snapshot.`;

  switch (action) {
    case "click":
      await el.click({ force: true });
      return `clicked ref ${ref}`;
    case "type":
      await el.fill(value || "");
      return `typed "${value}" into ref ${ref}`;
    case "select":
      await el.selectOption(value || "");
      return `selected "${value}" in ref ${ref}`;
    default:
      return `unknown action: ${action}`;
  }
}

async function toolEvaluate(script: string): Promise<string> {
  const page = await ensurePage();
  try {
    // 30s timeout to prevent hanging evaluate calls from crashing the browser
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

async function resolveRef(
  page: Page,
  ref: string,
): Promise<ElementHandle | null> {
  const handle = await page.evaluateHandle((r: string) => {
    // biome-ignore lint/suspicious/noExplicitAny: browser globalThis has dynamic properties
    const w = globalThis as any;
    const refs = w.__devBrowserRefs;
    if (!refs) return null;
    return refs[r] || null;
  }, ref);

  const el = handle.asElement();
  if (!el) {
    await handle.dispose();
    return null;
  }
  return el;
}

// cleanup
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
