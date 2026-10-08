// A stubbed VS Code host for the chat-ui surfaces: one authenticated
// account, one chat with a pinned model, and a model catalog. Enough for the
// webview to render its real chrome and composer with no daemon behind it.

import type { Page } from "@playwright/test";

export const CHAT_ID = "chat-ui-harness";
export const CHAT_TITLE = "Chat harness";

export const AUTHED = {
  authenticated: true,
  email: "e2e@alkera.local",
  api_url: null,
  expires_at: null,
  reason: null,
  frontend_url: "https://app.alkera.test",
};

/** Fixture input, not a copy of anything: a model name longer than the model
 *  chip's cap, so a width sweep proves the cap holds rather than that short
 *  names fit. */
export const LONG_MODEL = "Claude Opus 4.5 (thinking, 1M context window)";

export const MODELS = [
  {
    id: "anthropic/claude-opus-4-5-1m",
    display_name: LONG_MODEL,
    wire: "anthropic",
    efforts: ["low", "medium", "high"],
    default_effort: "medium",
  },
];

/** The chat's pinned model. Without it the composer has no effort ladder to
 *  offer and the effort chip never renders. */
export const PINNED = { model_id: MODELS[0].id, efforts: MODELS[0].efforts, effort: "medium" };

const RECENT_EVENTS = [
  { event_type: "message.created", message_id: "u1", role: "user" },
  {
    event_type: "part.created",
    message_id: "u1",
    part: { part_id: "u1-text", message_id: "u1", type: "text", text: "Widen the rail." },
  },
  { event_type: "turn.finished", event_id: "sum-1", stop_reason: "complete", summary: "Done." },
];

/** Optional overrides for a spec that needs a different account (e.g. no
 *  `frontend_url`, so the web-app key must not render) or a richer transcript
 *  than the default single-turn replay. */
export interface FixtureOverrides {
  authed?: typeof AUTHED;
  engine?: Record<string, unknown>;
  events?: unknown[];
  contextTotal?: number;
  lineageTotal?: number;
}

export async function installFixture(page: Page, overrides: FixtureOverrides = {}): Promise<void> {
  await page.addInitScript(({ authed, chatId, chatTitle, contextTotal, engine, events, lineageTotal, models, pinned }) => {
    let persisted: unknown = { auth: authed, workspaceName: "alkera-ide-frontend" };
    // Every envelope the webview posts, kept for assertions on host-bound
    // side effects (openBrowser and friends) that produce nothing in the DOM.
    const posted: unknown[] = [];
    (window as unknown as { __posted: unknown[] }).__posted = posted;
    const emit = (data: unknown): void => {
      window.dispatchEvent(new MessageEvent("message", { data }));
    };
    window.acquireVsCodeApi = () => ({
      postMessage(message: unknown) {
        if (typeof message !== "object" || message === null) return;
        posted.push(message);
        const envelope = message as { type?: string; id?: number; op?: string; method?: string };
        if (envelope.type === "ready") {
          queueMicrotask(() =>
            emit({ type: "init", payload: { auth: authed, workspaceName: "alkera-ide-frontend" } }),
          );
          return;
        }
        if (envelope.type === "rpc" && typeof envelope.id === "number") {
          let data: unknown = {};
          if (envelope.method === "harness.list_models") data = { models };
          else if (envelope.method === "preferences.resolve_chat_defaults") {
            data = { model: models[0].id, effort: "high" };
          } else if (envelope.method === "preferences.get") {
            // Past the first-run tour: these tests are about the chat, not the gate.
            data = { preferences: { onboarded_extension: true } };
          }
          queueMicrotask(() => emit({ type: "rpcResult", id: envelope.id, ok: true, data }));
          return;
        }
        if (envelope.type !== "engineRequest" || typeof envelope.id !== "number") return;
        let data: unknown = {};
        const manifest = { session_id: chatId, title: chatTitle, model: pinned, updated_at: "2026-07-01T12:00:00.000Z" };
        if (Object.prototype.hasOwnProperty.call(engine, envelope.op ?? "")) {
          data = engine[envelope.op ?? ""];
        } else if (envelope.op === "chat.list") {
          data = { chats: [manifest] };
        } else if (envelope.op === "chat.open") {
          data = { session_id: chatId, manifest, recent_events: events };
        } else if (envelope.op === "chat.listCommands") {
          data = {
            commands: [
              { name: "usage", aliases: [], summary: "Show credits and usage", usage: "/usage", hidden: false, ui: true },
              { name: "preferences", aliases: [], summary: "Open preferences", usage: "/preferences", hidden: false, ui: true },
            ],
          };
        } else if (envelope.op === "context.list") {
          data = { items: [], total: contextTotal };
        } else if (envelope.op === "org.sync_status") {
          data = {
            org_sync_enabled: true,
            org_sync_known: true,
            project_setting: null,
            effective: true,
            repo: "alkera-ide-frontend",
          };
        } else if (envelope.op === "lineage.roots") {
          data = { roots: [], total_nodes: lineageTotal };
        } else {
          data = { ok: true };
        }
        queueMicrotask(() => emit({ type: "engineResult", id: envelope.id, ok: true, data }));
      },
      getState() {
        return persisted;
      },
      setState(state: unknown) {
        persisted = state;
      },
    });
  }, {
    authed: overrides.authed ?? AUTHED,
    chatId: CHAT_ID,
    chatTitle: CHAT_TITLE,
    contextTotal: overrides.contextTotal ?? 12,
    engine: overrides.engine ?? {},
    events: overrides.events ?? RECENT_EVENTS,
    lineageTotal: overrides.lineageTotal ?? 47,
    models: MODELS,
    pinned: PINNED,
  });
}

/** The envelopes the webview has posted to the stubbed host so far. */
export async function postedMessages(page: Page): Promise<unknown[]> {
  return page.evaluate(() => (window as unknown as { __posted?: unknown[] }).__posted ?? []);
}

/** Deliver a live harness event through the real host channel and fold,
 *  exactly as the extension host relays daemon streams. */
export async function pushHarnessEvent(page: Page, chatId: string, event: unknown): Promise<void> {
  await page.evaluate(({ chatId, event }) => {
    window.dispatchEvent(new MessageEvent("message", {
      data: { type: "engine.harnessEvent", args: { chatId, event } },
    }));
  }, { chatId, event });
}

/** The worktree's vite config pins `hmr.clientPort` to the worktree dev port
 *  while this harness serves from its own, so vite's client would ping, fail,
 *  and reload the page mid-test. Abort the ping. */
export async function stopViteReloadLoop(page: Page): Promise<void> {
  await page.route(/^https?:\/\/(localhost|127\.0\.0\.1):\d+\/$/, (route) => {
    if (route.request().headers()["accept"] === "text/x-vite-ping") return route.abort();
    return route.fallback();
  });
}

/** VS Code injects these into a real webview document. This harness stubs the
 *  editor's API but not its palette, and the webview's token layer maps onto
 *  them, so without a stand-in every host-mapped paint is guaranteed-invalid and
 *  menus and cards render transparent. The app solves the same gap for its
 *  browser preview in src/webview/previewHostTokens.ts, which is skipped here
 *  precisely because this host claims to be VS Code. Values are the editor's own
 *  Dark Modern. */
const VSCODE_DARK = `:root {
  --vscode-editor-background: #1f1f1f;
  --vscode-activityBar-background: #181818;
  --vscode-sideBar-background: #181818;
  --vscode-editorWidget-background: #202020;
  --vscode-input-background: #313131;
  --vscode-foreground: #cccccc;
  --vscode-descriptionForeground: #9d9d9d;
  --vscode-input-placeholderForeground: #989898;
  --vscode-disabledForeground: #7f7f7f;
  --vscode-panel-border: #2b2b2b;
  --vscode-widget-border: #313131;
  --vscode-scrollbarSlider-background: rgb(121 121 121 / 40%);
  --vscode-errorForeground: #f85149;
  --vscode-editorWarning-foreground: #cca700;
  --vscode-list-hoverBackground: #2a2d2e;
  --vscode-list-activeSelectionBackground: #04395e;
  --vscode-list-inactiveSelectionBackground: #37373d;
}`;

/** Open one webview surface and wait for the marker it is done painting. */
export async function gotoSurface(page: Page, entry: string, ready: string): Promise<void> {
  await stopViteReloadLoop(page);
  await page.goto(`/vscode.html?entry=${encodeURIComponent(entry)}&theme=dark`, { waitUntil: "networkidle" });
  await page.addStyleTag({ content: VSCODE_DARK });
  await page.waitForSelector(ready);
  await page.waitForTimeout(250);
}
