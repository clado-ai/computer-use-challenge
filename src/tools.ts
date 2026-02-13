import { chromium, type Browser, type Page, type ElementHandle } from "playwright";
import type Anthropic from "@anthropic-ai/sdk";
import * as path from "node:path";

// ---- dev-browser client (inline to avoid import path issues) ----

interface PageInfo {
  wsEndpoint: string;
  name: string;
  targetId: string;
}

let browser: Browser | null = null;
let currentPage: Page | null = null;
const serverUrl = "http://localhost:9222";

async function ensureBrowser(): Promise<Browser> {
  if (browser && browser.isConnected()) return browser;
  const res = await fetch(serverUrl);
  const info = (await res.json()) as { wsEndpoint: string };
  browser = await chromium.connectOverCDP(info.wsEndpoint);
  return browser;
}

async function getOrCreatePage(name: string): Promise<Page> {
  if (currentPage && !currentPage.isClosed()) return currentPage;

  const res = await fetch(`${serverUrl}/pages`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, viewport: { width: 1280, height: 720 } }),
  });
  const pageInfo = (await res.json()) as PageInfo;
  const b = await ensureBrowser();

  // find page by targetId
  for (const ctx of b.contexts()) {
    for (const p of ctx.pages()) {
      let session;
      try {
        session = await ctx.newCDPSession(p);
        const resp = (await session.send("Target.getTargetInfo")) as any;
        if (resp.targetInfo.targetId === pageInfo.targetId) {
          currentPage = p;
          return p;
        }
      } catch {
        // ignore
      } finally {
        try {
          await session?.detach();
        } catch {
          /* */
        }
      }
    }
  }
  throw new Error(`page "${name}" not found in browser`);
}

// load snapshot script from dev-browser
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

// ---- page name for this session ----
const PAGE_NAME = "challenge";

// ---- tool definitions ----

export const toolDefinitions: Anthropic.Messages.Tool[] = [
  {
    name: "browser_navigate",
    description:
      "Navigate to a URL. Automatically suppresses all JavaScript dialogs (alert, confirm, prompt). Returns the page title and URL after loading.",
    input_schema: {
      type: "object" as const,
      properties: {
        url: { type: "string", description: "The URL to navigate to" },
      },
      required: ["url"],
    },
  },
  {
    name: "browser_snapshot",
    description:
      "Get an ARIA accessibility snapshot of the current page. Returns a YAML tree with interactive elements labeled [ref=eN]. Use these refs with browser_action.",
    input_schema: {
      type: "object" as const,
      properties: {},
    },
  },
  {
    name: "browser_action",
    description:
      'Perform an action on the page. Actions: "click" (click element by ref), "type" (type text into element by ref), "select" (select option by ref), "press" (press a key like Enter, Tab, Escape), "scroll" (scroll by ref or page). Use refs from browser_snapshot.',
    input_schema: {
      type: "object" as const,
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
  {
    name: "browser_evaluate",
    description:
      "Execute JavaScript in the browser page context. Returns the JSON-serialized result. Use for: reading hidden DOM content, manipulating elements, dismissing popups, extracting data, dispatching events.",
    input_schema: {
      type: "object" as const,
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
];

// ---- tool execution ----

export async function executeTool(
  name: string,
  input: Record<string, unknown>,
): Promise<string> {
  try {
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
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    return `error: ${msg}`;
  }
}

async function toolNavigate(url: string): Promise<string> {
  const page = await getOrCreatePage(PAGE_NAME);

  // auto-dismiss dialogs
  page.on("dialog", async (dialog) => {
    try {
      await dialog.dismiss();
    } catch {
      /* */
    }
  });

  await page.goto(url, { waitUntil: "domcontentloaded", timeout: 15000 });

  // suppress JS dialog functions
  await page.evaluate(`
    window.alert = () => {};
    window.confirm = () => true;
    window.prompt = () => "";
    window.onbeforeunload = null;
  `);

  // brief wait for dynamic content
  await page.waitForTimeout(500);

  const title = await page.title();
  const currentUrl = page.url();
  return `navigated to: ${currentUrl}\ntitle: ${title}`;
}

async function toolSnapshot(): Promise<string> {
  const page = await getOrCreatePage(PAGE_NAME);

  const script = await getSnapshotScript();
  const snapshot = await page.evaluate((s: string) => {
    const w = globalThis as any;
    if (!w.__devBrowser_getAISnapshot) {
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
  const page = await getOrCreatePage(PAGE_NAME);

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
      await el.click({ timeout: 5000 });
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
  const page = await getOrCreatePage(PAGE_NAME);
  try {
    const result = await page.evaluate(script);
    if (result === undefined || result === null) return "null";
    return typeof result === "string" ? result : JSON.stringify(result, null, 2);
  } catch (err) {
    return `error: ${err instanceof Error ? err.message : String(err)}`;
  }
}

async function resolveRef(
  page: Page,
  ref: string,
): Promise<ElementHandle | null> {
  const handle = await page.evaluateHandle((r: string) => {
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
  if (browser) {
    try {
      await browser.close();
    } catch {
      /* */
    }
    browser = null;
  }
  currentPage = null;
}
