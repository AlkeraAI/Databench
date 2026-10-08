// The runtime convergence detector — a dev-only tripwire over the live fold.
//
// While a chat is open it periodically, and on every reconnect, re-reads the
// chat's whole durable log over REST, folds it fresh through the oracle
// (`convergence.ts`) and diffs the result against what the live data source is
// rendering. A divergence is logged as one structured line and the log window
// is kept in `localStorage` so it can be lifted out as a corpus fixture.
//
// Never in a production build: every entry point is behind `import.meta.env.DEV`,
// which Vite folds to `false` and tree-shakes on a production build, so neither
// the flag nor the localStorage key can switch it on there.
//
// Switching it on (dev server only):
//   * `VITE_CHAT_CONVERGENCE_CHECK=true pnpm --filter @alkera/web dev`, or
//   * in the console: `localStorage.setItem("alkera.chatConvergenceCheck", "1")`
//     and reload.
// Exporting a window: `localStorage.getItem("alkera.chatConvergence.<chatId>")`
// is a `{chat_id, rows}` document in the shape of
// `apps/web/src/tests/fixtures/chat-convergence/<chat>/log.json`.

import { safeLocalStorage } from "@alkera/ui/storage";

import * as cloud from "../../../../api/cloudChat/transport";

import type { ChatDataSource } from "./ChatDataSource";
import { CloudDataSource } from "./CloudDataSource";
import { diffViews, durableTurns, openAt, type Divergence, type FoldView, type LogRow } from "./convergence";
import { conversationAwaitsResponse, pendingAskKind } from "./harnessEventFold";

export const CONVERGENCE_CHECK_STORAGE_KEY = "alkera.chatConvergenceCheck";
export const CONVERGENCE_WINDOW_STORAGE_PREFIX = "alkera.chatConvergence.";
/** How often the live fold is re-checked while the chat is open. */
export const CONVERGENCE_CHECK_INTERVAL_MS = 30_000;
/** The most rows a stored window keeps; a longer log keeps its tail. */
const STORED_WINDOW_MAX_ROWS = 600;

/** Whether the detector may run: a dev build, and the flag or the key set. */
export function convergenceCheckEnabled(): boolean {
  if (!import.meta.env.DEV) return false;
  if (import.meta.env.VITE_CHAT_CONVERGENCE_CHECK === "true") return true;
  return safeLocalStorage().get(CONVERGENCE_CHECK_STORAGE_KEY) === "1";
}

/** What one check reads and what it found. */
export interface ConvergenceReport {
  chatId: string;
  /** The lowest and highest sequence the fresh read held. */
  n0: number;
  n: number;
  divergences: Divergence[];
  /** The rows the fresh fold was made from. */
  log: readonly LogRow[];
}

/** The whole durable log, forward from zero, as the portal pages it. */
async function readLog(chatId: string, rest: typeof cloud): Promise<LogRow[]> {
  const rows: LogRow[] = [];
  let after = 0;
  for (;;) {
    const page = await rest.listMessages(chatId, { afterSeq: after });
    rows.push(...page.items);
    if (page.items.length === 0 || page.next_after_seq <= after) break;
    after = page.next_after_seq;
  }
  return rows;
}

/** Diff the live source's fold of `chatId` against a fresh fold of the durable
 *  log. The live source's own turn word is handed to the fresh fold, so the
 *  comparison is of the transcript fold alone. */
export async function checkConvergence(
  chatId: string,
  live: ChatDataSource,
  rest: typeof cloud = cloud,
): Promise<ConvergenceReport | null> {
  const log = await readLog(chatId, rest);
  if (log.length === 0) return null;
  const n = log[log.length - 1].seq;
  const turnState = live.turnState?.(chatId) ?? null;
  const fresh = await openAt(log, n, { pageRows: n + 1, turnState });
  let snapshot: FoldView;
  try {
    snapshot = await fresh.view();
  } finally {
    fresh.close();
  }
  const turns = durableTurns(await live.getChatTurns(chatId));
  const liveView: FoldView = {
    turns,
    pendingAsk: pendingAskKind(turns),
    awaitsResponse: conversationAwaitsResponse(turns),
    turnState,
    hasOlder: live.transcriptHistory?.(chatId).hasOlder ?? false,
  };
  return { chatId, n0: log[0].seq, n, divergences: diffViews(liveView, snapshot), log };
}

/** Keep the log window beside the report so it can be lifted into a fixture. */
function storeWindow(report: ConvergenceReport, log: readonly LogRow[]): void {
  const rows = log.slice(-STORED_WINDOW_MAX_ROWS);
  // A full or blocked store is the safe store's to swallow: the console line
  // still says what diverged.
  safeLocalStorage().set(
    `${CONVERGENCE_WINDOW_STORAGE_PREFIX}${report.chatId}`,
    JSON.stringify({
      chat_id: report.chatId,
      at: new Date().toISOString(),
      n0: report.n0,
      n: report.n,
      divergences: report.divergences,
      rows,
    }),
  );
}

/** Run one check and report it. Exposed so a test can drive it without timers. */
export async function runConvergenceCheck(
  chatId: string,
  live: ChatDataSource,
  rest: typeof cloud = cloud,
  log: (line: string, detail: Record<string, unknown>) => void = (line, detail) =>
    console.warn(line, detail),
): Promise<ConvergenceReport | null> {
  const report = await checkConvergence(chatId, live, rest);
  if (!report || report.divergences.length === 0) return report;
  log("chat.convergence.diverged", {
    chat_id: report.chatId,
    n0: report.n0,
    n: report.n,
    paths: report.divergences.map((d) => d.path),
  });
  // The rows the check folded, not a second read: a chat that moved in
  // between would store a window its own `n` does not describe.
  storeWindow(report, report.log);
  return report;
}

/** Install the detector on an open chat: a check now, one every
 *  `intervalMs`, and one on every replay the source announces (a reconnect's
 *  snapshot). Returns the teardown. Only a cloud source is checked — the
 *  daemon source has no REST log to fold against. */
export function installConvergenceDetector(
  chatId: string,
  live: ChatDataSource,
  opts: { intervalMs?: number; rest?: typeof cloud } = {},
): () => void {
  if (!(live instanceof CloudDataSource)) return () => undefined;
  let running = false;
  const check = (): void => {
    if (running) return;
    running = true;
    void runConvergenceCheck(chatId, live, opts.rest).finally(() => {
      running = false;
    });
  };
  const timer = setInterval(check, opts.intervalMs ?? CONVERGENCE_CHECK_INTERVAL_MS);
  const off = live.subscribeChat(chatId, (event) => {
    if (event.replay) check();
  });
  check();
  return () => {
    clearInterval(timer);
    off();
  };
}
