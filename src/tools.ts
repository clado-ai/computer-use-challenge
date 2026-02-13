import {
  chromium,
  type BrowserContext,
  type Page,
  type ElementHandle,
} from "playwright";
import type Anthropic from "@anthropic-ai/sdk";
import * as path from "node:path";
import * as fs from "node:fs";

// ---- browser management (direct launch, no server needed) ----

let context: BrowserContext | null = null;
let currentPage: Page | null = null;

const PROFILE_DIR = path.resolve(import.meta.dir, "..", ".browser-data");

async function ensurePage(): Promise<Page> {
  if (currentPage && !currentPage.isClosed()) return currentPage;

  if (!context) {
    fs.mkdirSync(PROFILE_DIR, { recursive: true });
    console.log("[browser] launching chromium...");
    const headless = process.env.HEADLESS !== "false";
    context = await chromium.launchPersistentContext(PROFILE_DIR, {
      headless,
      viewport: { width: 1280, height: 720 },
      args: ["--disable-blink-features=AutomationControlled"],
    });
    console.log("[browser] ready");
  }

  // use existing page or create new one
  const pages = context.pages();
  currentPage = pages[0] ?? (await context.newPage());

  // set up dialog auto-dismiss
  currentPage.on("dialog", async (dialog) => {
    try {
      await dialog.dismiss();
    } catch {
      /* */
    }
  });

  return currentPage;
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
  const page = await ensurePage();

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
  const page = await ensurePage();

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
