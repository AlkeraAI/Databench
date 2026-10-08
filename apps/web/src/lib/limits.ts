/** Every number the web client picks for itself, in one place.
 *
 *  A browser cannot read a deployment setting at import time, so a client-side
 *  cadence, ceiling or page size has to be written down somewhere. This module is
 *  that somewhere: one typed home, each value named with its unit and carrying the
 *  one line that says why it is what it is. A call site imports the name; a bare
 *  number at a call site is a number nobody can find, compare against its sibling,
 *  or change without reading the code around it.
 *
 *  Two rules keep the module honest:
 *
 *  - A limit the SERVER decides does not live here. The largest file an upload may
 *    carry, the page ceiling a listing refuses above, the admission a turn is held
 *    on: those arrive in a response or a header and are read from there, because a
 *    second copy in the client is a copy that drifts. What lives here is what only
 *    the browser can decide — how often a tab re-asks while its stream is down, how
 *    many rows it will sort in memory, how long a toast stays up.
 *  - A value that is a product choice rather than a fact about the machine is
 *    reachable through `LimitsProvider`, so the two hosts of this bundle (the
 *    portal and the VS Code webview) can differ without a second module. The
 *    defaults below are what a host gets when it overrides nothing.
 */

import { createContext, createElement, useContext, type ReactNode } from "react";

import { doublingDelay } from "@alkera/ui/backoff";

/* ---------------------------------------------------------------------------
 * Stream transport — how a tab notices it has lost the server, and how hard it
 * tries to get it back. Every value here is a trade between noticing a dead
 * connection quickly and painting a scary banner over an ordinary blip.
 * ------------------------------------------------------------------------- */

/** First reconnect wait after the event stream drops. Long enough that a page
 *  navigation or a one-frame blip does not cost a round trip. */
export const SSE_BACKOFF_FLOOR_MS = 2_000;

/** Ceiling on that doubling wait. A whole fleet reconnecting at once is what
 *  takes a recovering server back down, so the ladder tops out rather than
 *  hammering — at the cost of a tab sitting stale for up to a minute after the
 *  server is back. */
export const SSE_BACKOFF_CAP_MS = 60_000;

/** How long reconnecting may last before the masthead says live updates are
 *  paused. It spans two failed attempts at the floor, so an ordinary blip is
 *  over before the reader is told anything. */
export const SSE_DOWN_AFTER_MS = 5_000;

/** Silence on an OPEN event stream that means the connection is dead rather than
 *  quiet: three of the server's own keepalives missed in a row. */
export const SSE_STALL_MS = 45_000;

/** Chat socket ping cadence. Under the idle window a proxy or load balancer
 *  typically closes an idle upstream at, so the socket is never reaped for being
 *  quiet. */
export const WS_HEARTBEAT_MS = 20_000;

/** How long the chat socket waits for a pong before calling the socket dead.
 *  Two missed heartbeats plus a beat of slack, so one late pong on a loaded tab
 *  is not a reconnect. */
export const WS_PONG_TIMEOUT_MS = 45_000;

/** How long a welcomed socket must STAY welcomed before the backoff counter
 *  returns to its floor. A socket that is accepted and dropped again is not a
 *  recovery, and treating it as one is how a fleet turns a brief server wobble
 *  into a self-sustaining reconnect storm. */
export const WS_STABLE_AFTER_MS = 60_000;

/* ---------------------------------------------------------------------------
 * Retry ladders — the one shape a client-side backoff has.
 *
 * Three of these were invented independently in this file, each spelling the
 * same three numbers under its own prefix, and a fourth was on the way. They
 * are one idea: where the wait starts, where it stops doubling, and how many
 * asks that buys. It is written down once here, and a caller that needs its own
 * ladder names what it differs on rather than restating the whole shape.
 * ------------------------------------------------------------------------- */

/** Where a doubling wait starts, where it stops doubling, and how many asks that
 *  buys before the caller stops and hands the reader the decision. */
export interface RetryLadder {
  readonly floorMs: number;
  readonly capMs: number;
  readonly attempts: number;
}

/** Build a ladder. `attempts` is DERIVED by default, so a cap always documents a
 *  wait the ladder actually reaches: the rungs that climb to it, plus one spent
 *  there and one more before the reader is asked. A count picked on its own is
 *  how a cap ends up describing a wait nothing ever arms — pass one only when
 *  the number of asks is itself the point. */
export function retryLadder(floorMs: number, capMs: number, attempts?: number): RetryLadder {
  return { floorMs, capMs, attempts: attempts ?? Math.ceil(Math.log2(capMs / floorMs)) + 2 };
}

/** The wait a ladder arms on `rung`: its floor doubled `rung` times, held at
 *  its cap. Rung 0 is the floor. Jitter, a server-named wait and which attempt
 *  counts as the first stay the caller's, because those are where the ladders
 *  genuinely differ; the climb is not. */
export function ladderDelay(rung: number, ladder: Pick<RetryLadder, "floorMs" | "capMs">): number {
  return doublingDelay(rung, ladder.floorMs, ladder.capMs);
}

/** The portal's own ladder, and the one every other is measured against.
 *
 *  Its floor is the wait under every refused read INCLUDING one the server
 *  named: long enough that a single loaded second on the server is over before
 *  anybody re-asks, and that a `Retry-After` of `0`, of nothing, or of a moment
 *  this browser's clock has already passed cannot turn a refusal into an
 *  immediate re-send of the same burst. Its cap is where doubling stops — a tab
 *  refused for half a minute is not going to be let in by asking faster, and a
 *  whole fleet re-asking at once is what keeps a recovering server down. */
export const RETRY_BACKOFF = retryLadder(1_000, 30_000);

export const RETRY_BACKOFF_FLOOR_MS = RETRY_BACKOFF.floorMs;

/** A live document's waits after the server answers busy (a hello, an edit, an
 *  earlier page's edits sent again): short at first, because a keystroke is
 *  waiting on it, and doubling while the server stays busy, so every tab that
 *  reconnected after a restart does not come back in the same half second. */
export const LIVE_BUSY_RETRY = retryLadder(250, 10_000);

/** A live document waiting on the answer to its subscribe or its hello: the
 *  deadline before it asks again, doubling from twelve seconds. A document
 *  still opening stops after the ladder's attempts (a little over two minutes
 *  in all) and offers the reader a retry instead of opening forever. */
export const LIVE_OPEN_DEADLINE = retryLadder(12_000, 48_000);
export const RETRY_BACKOFF_CAP_MS = RETRY_BACKOFF.capMs;

/** Re-asking for a page of earlier messages: the portal's ladder, unmodified.
 *  Its derived attempt count is what stops the tape — nobody is watching a
 *  transcript that will not come, and a tab left open overnight must not keep
 *  asking, so past it the reader is offered the retry instead. */
export const HISTORY_RETRY = RETRY_BACKOFF;

export const HISTORY_RETRY_FLOOR_MS = HISTORY_RETRY.floorMs;
export const HISTORY_RETRY_CAP_MS = HISTORY_RETRY.capMs;
export const HISTORY_RETRY_ATTEMPTS = HISTORY_RETRY.attempts;

/** Re-reading a chat's durable record after the server refused it: the portal's
 *  ladder, with a ceiling of its own.
 *
 *  It climbs from the same floor but stops doubling twice as late, because what
 *  it is riding out is a rate limiter rather than a restart, and nobody is
 *  waiting on this read the way they wait on a page they asked for — it runs
 *  behind a chat that already has a tape on screen. Its derived attempt count is
 *  what stops it: past the last rung the source stops claiming the chat is
 *  current and re-derives the tape, rather than asking a refusing server for
 *  ever. */
export const CATCH_UP_RETRY = retryLadder(RETRY_BACKOFF_FLOOR_MS, 60_000);

export const CATCH_UP_RETRY_FLOOR_MS = CATCH_UP_RETRY.floorMs;
export const CATCH_UP_RETRY_CAP_MS = CATCH_UP_RETRY.capMs;
export const CATCH_UP_RETRY_ATTEMPTS = CATCH_UP_RETRY.attempts;

/** How long the one shared socket stays open after the last surface releases it,
 *  so flipping between two chats costs no handshake. */
export const REALTIME_LINGER_MS = 30_000;

/** How long a hole in the op sequence stays open before the client gives up on
 *  the missing frame and refetches the whole document. An ordered socket makes a
 *  gap a genuinely lost frame, so the wait only has to cover reordering in the
 *  browser's own delivery. */
export const DOC_SYNC_GAP_TIMEOUT_MS = 2_000;

/** Out-of-order ops held while that hole is open. Nothing is lost past it — the
 *  client refetches instead — so this is a memory guard, not a correctness one. */
export const DOC_SYNC_MAX_BUFFERED_OPS = 64;

/** How often a tab tells the server it is still watching a chat. Two beats fit
 *  inside the server's presence TTL, so one dropped frame never drops a face. */
export const PRESENCE_HEARTBEAT_MS = 20_000;

/** How many times per server presence TTL a roster looks for a peer it has
 *  stopped hearing from: a lapsed peer goes within a third of the TTL of it. */
export const PRESENCE_EXPIRY_CHECKS_PER_TTL = 3;

/** Floor between caret frames in a shared composer draft: half the socket's
 *  per-connection frame budget, so a fast typist cannot be throttled by it. */
export const PRESENCE_CURSOR_THROTTLE_MS = 100;

/* ---------------------------------------------------------------------------
 * Degraded polling — the floor under everything the event stream would have
 * delivered. These only run while the stream is known to be down.
 * ------------------------------------------------------------------------- */

/** How often a read re-asks while the event stream is down. The stream is the
 *  real path; this only bounds how stale a screen can get without it. */
export const OFFLINE_POLL_MS = 15_000;

/** How often the chat's machine state is re-read even when no event says to. The
 *  frame that would announce a dead box travels over the connection that box may
 *  have taken down with it, so a poll is the floor under the banner. */
export const MACHINE_POLL_MS = 15_000;

/** How long the open chat waits for its transcript to settle before it tells
 *  the server how far the reader has seen. */
export const MARK_READ_DEBOUNCE_MS = 1_500;

/** The shortest gap between two re-reads of one folder listing or one item
 *  while a machine is saving into it: a frame per save, the first change read
 *  at once, the rest of a burst in one read per window. */
export const MACHINE_REFRESH_MS = 12_000;

/* ---------------------------------------------------------------------------
 * Query cache — the portal-wide defaults every read inherits.
 * ------------------------------------------------------------------------- */

/** How long a cached read is served without re-asking. Short enough that a dense
 *  screen is never visibly behind, long enough that tabbing between two views
 *  does not refetch the same rows twice. */
export const QUERY_STALE_MS = 5_000;

/** Attempts a failed read makes before it surfaces. Settled answers (a 401, a
 *  404) are carved out at the call site — retrying an answer only delays it. */
export const QUERY_RETRY_ATTEMPTS = 3;

/** How much of a wait may be added on top of it, at random. Every tab in an org
 *  is refused by the same limiter in the same second, so a wait with no jitter
 *  brings all of them back in the same second too. It is added and never
 *  subtracted: a wait the server asked for is a floor, not an average. */
export const RETRY_JITTER_RATIO = 0.3;

/** The longest a `Retry-After` is honoured before it is treated as a mistake.
 *  A header the server got wrong must not park a screen for an hour. */
export const RETRY_AFTER_CAP_MS = 120_000;

/** The longest a refused SEND waits before it is handed back to the reader.
 *
 *  Shorter than {@link RETRY_AFTER_CAP_MS} on purpose: a read that re-asks
 *  itself can afford to wait, and a person watching the field they just typed
 *  in cannot. Past this the limiter is asking for longer than anyone will watch
 *  a composer for, so the send stops being automatic and becomes theirs. */
export const SEND_THROTTLE_CAP_MS = 60_000;

/** How long every read waits after one of them is refused and the server named
 *  no wait of its own. It is deliberately longer than the retry ladder's floor:
 *  the ladder re-asks ONE read, this holds the page's whole herd, and a page
 *  refused once was refused as a burst. A limiter that says only "no" still
 *  means "not yet", and taking that as "ask again on the next tick" is what
 *  turned five idle tabs into nineteen hundred refusals in half an hour. */
export const READ_PAUSE_DEFAULT_MS = 5_000;

/** The longest a refused read holds the others. Past it a header is no longer
 *  protecting the server, it is parking a tab — and a reader who waited a
 *  minute has already reloaded. */
export const READ_PAUSE_CAP_MS = 60_000;

/** The SESSION read's ladder — the portal's own, differing on all three counts
 *  and saying why for each.
 *
 *  It starts faster than the portal's floor (nothing at all is on screen until
 *  the shell knows who is reading, so the first re-ask should not cost a whole
 *  second), stops doubling much earlier (past it the shell stops waiting
 *  silently and hands the reader the decision — that ceiling also bounds a
 *  `Retry-After` the server sends with a rate limit), and takes a fixed number
 *  of asks rather than the derived one, because what it is buying is a rolling
 *  restart rather than a full climb to the ceiling. */
export const SESSION_RETRY = retryLadder(400, 10_000, 3);

/* ---------------------------------------------------------------------------
 * Page sizes — how much one request asks for. None of these bound what is
 * REACHABLE: every listing behind them pages to exhaustion or offers a cursor.
 * The server's own ceiling is the one that refuses, and it is read from the
 * refusal rather than restated here.
 * ------------------------------------------------------------------------- */

/** Rows per request while walking a folder's children. Sized so one screen of a
 *  large folder arrives in a single round trip. */
export const FOLDER_CHILDREN_PAGE = 500;

/* ---------------------------------------------------------------------------
 * Files surface — the drive's own client-side choices.
 * ------------------------------------------------------------------------- */

/** Files from one drop uploading at once. Each lane holds an upload session and
 *  a hash worker, so the figure trades a fast drop against the fan-out one tab
 *  puts on the session API. */
export const FILES_UPLOAD_CONCURRENCY = 3;

/** How long the folder listing waits for files landing beside one another before
 *  it refetches once for all of them. It bounds how stale the rows behind a tray
 *  can be, so it is a cadence rather than a batch: the listing catches up while
 *  the drop is still running, not when the last file settles. */
export const FILES_UPLOAD_REFRESH_MS = 250;

/** Files that may land inside one of those windows before the listing refetches
 *  anyway. A drop whose lanes finish faster than the window kept re-arming it,
 *  and a person watching a folder fill up saw nothing until the drop was over. */
export const FILES_UPLOAD_REFRESH_EVERY = 4;

/** Children loaded into one folder view before scroll prefetch stops and Load
 *  more is offered. It is also what bounds client-side sort and select-all, so
 *  it is the point past which the browser would be doing the server's work. */
export const FILES_SOFT_THRESHOLD_ROWS = 20_000;

/** Rows a selection may carry before a write over it is sent as batches rather
 *  than one request per row. It mirrors the server's own inline ceiling
 *  (`alkera_core.files.bulk`): at or below it the batch route would answer
 *  inline anyway, and the per-row path is kept there because it gives every row
 *  its own operation — so undoing one does not drag the rest back. */
export const FILES_BULK_INLINE_ITEMS = 100;

/** Items one batch request carries. It mirrors the server's own ceiling, which
 *  refuses a longer body outright (`files.batch_too_large`), so a longer
 *  selection is sent as several batches. */
export const FILES_BULK_BATCH_ITEMS = 1_000;

/** How long typing must stop before a Files search request goes out. */
export const FILES_SEARCH_DEBOUNCE_MS = 150;

/** Characters typed before Files search asks the server anything. Below it a
 *  query matches most of a drive, and the answer is no use to the reader. */
export const FILES_SEARCH_MIN_LENGTH = 2;

/** Destructive Files writes the undo stack walks back. Each entry is a handful
 *  of strings, so the bound is about a session that never ends, not memory. */
export const FILES_UNDO_DEPTH = 32;

/** How long the undo prompt stays on screen. The keyboard undo keeps working
 *  afterwards, so this is how long the offer is visible, not how long it lasts. */
export const FILES_UNDO_TOAST_MS = 8_000;

/** How long a refused write's sentence stays on screen. It is dismissable, so
 *  this is the ceiling, not the wait.
 *
 *  It is deliberately SHORTER than {@link FILES_UNDO_TOAST_MS}: the refusal is
 *  drawn over the page and the undo offer is drawn under it, so a refusal that
 *  outlived the offer would be the last thing covering a button whose whole
 *  point is to be pressed within a few seconds. The layout keeps them out of
 *  each other's way; this keeps them out of each other's way if it ever does
 *  not. */
export const FILES_REFUSAL_MS = 6_000;

/** Pages a preview walks to resolve a folder-relative reference inside a drawn
 *  document. Past it the reference is left unresolved rather than turning one
 *  image into an unbounded crawl of a folder. */
export const PREVIEW_FOLDER_WALK_PAGES = 20;

/** Depth a relative path inside a previewed document may reach. Past it the path
 *  is not a path into this folder, it is an attempt to leave it. */
export const PREVIEW_PATH_MAX_SEGMENTS = 32;

/** How close to expiry a page grant is re-minted rather than spent, so a grant
 *  cannot lapse between the check and the request it authorises. */
export const PREVIEW_GRANT_SPENT_MARGIN_MS = 30_000;

/** How much of a text file one read of a preview brings. A file under it is read
 *  whole in one request; a larger one lands its first window and the next ones
 *  as the reader asks, so its size never decides whether it can be read. */
export const PREVIEW_TEXT_WINDOW_BYTES = 1024 * 1024;

/* ---------------------------------------------------------------------------
 * Host overrides.
 * ------------------------------------------------------------------------- */

/** The limits a host may differ on: the ones a component or hook reads, so the
 *  answer can depend on where it is drawn. Everything above is a default; a host
 *  that overrides nothing gets exactly these. The VS Code webview is the reason
 *  the seam exists — it draws into a panel a fraction of a portal window's width
 *  and reaches its data over a JSON-RPC bridge rather than the network, so its
 *  sensible list size and typing window are not the portal's.
 *
 *  A constant that is read outside React (a reducer, a module-level pool) stays a
 *  plain export above: a value nothing can read through a provider does not
 *  belong in one, where it would read as overridable and quietly not be.
 *
 *  A new entry is one line here plus its default below; nothing else changes. */
export interface WebLimits {
  filesSoftThresholdRows: number;
  filesSearchDebounceMs: number;
  filesSearchMinLength: number;
  filesUndoToastMs: number;
  filesRefusalMs: number;
  sessionRetryAttempts: number;
  sessionRetryFloorMs: number;
  sessionRetryCapMs: number;
}

export const DEFAULT_WEB_LIMITS: WebLimits = {
  sessionRetryAttempts: SESSION_RETRY.attempts,
  sessionRetryFloorMs: SESSION_RETRY.floorMs,
  sessionRetryCapMs: SESSION_RETRY.capMs,
  filesSoftThresholdRows: FILES_SOFT_THRESHOLD_ROWS,
  filesSearchDebounceMs: FILES_SEARCH_DEBOUNCE_MS,
  filesSearchMinLength: FILES_SEARCH_MIN_LENGTH,
  filesUndoToastMs: FILES_UNDO_TOAST_MS,
  filesRefusalMs: FILES_REFUSAL_MS,
};

const LimitsContext = createContext<WebLimits>(DEFAULT_WEB_LIMITS);

/** The limits in force. Outside a provider, the defaults — so a component is
 *  never obliged to be wrapped, and a test that does not care can render bare. */
export function useLimits(): WebLimits {
  return useContext(LimitsContext);
}

/** Narrow one or more limits for everything below. Nesting composes: an inner
 *  provider starts from what it is inside, not from the defaults. */
export function LimitsProvider({
  overrides,
  children,
}: {
  overrides?: Partial<WebLimits>;
  children: ReactNode;
}) {
  const inherited = useContext(LimitsContext);
  const value = overrides ? { ...inherited, ...overrides } : inherited;
  return createElement(LimitsContext.Provider, { value }, children);
}
