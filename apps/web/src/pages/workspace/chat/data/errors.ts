// Failure shapes and polling cadences the chat surfaces read, independent of
// which source produced the failure.
//
// These moved out of the webview's daemon source when the chat host was ported
// into the browser tree: both a JSON-RPC rejection and an HTTP one reach the
// surfaces as an object with a `code` and a `message`, so the predicates below
// are the same either way. The webview's data barrel re-exports them, which is
// why nothing that imported them from there had to change.

import { readerSentence } from "@/api/client";
import { ApiError } from "@/api/errors";

/** A JSON-RPC SESSION_NOT_OPEN (-32002) rejection, serialized by the host
 *  bridge: the daemon no longer holds this chat's live session (it restarted or
 *  reaped it). The chat still exists on disk, so re-opening it recovers. */
export function isSessionNotOpen(err: unknown): boolean {
  return typeof err === "object" && err !== null && (err as { code?: unknown }).code === -32002;
}

const UNREACHABLE = "Couldn't reach the server.";
const ABORTED = "The request was cancelled before an answer came back.";
const SERVER_FAILED = "The server couldn't finish the request.";

/** The sentence for a failure of the trip rather than a refusal with a reason:
 *  the network never carried the request, it was aborted, or the server fell
 *  over with nothing to say (a 5xx without a code of its own). The browser's own words for these ("Failed to fetch", "The
 *  user aborted a request") are addressed to a developer, so they never reach
 *  a reader. Null for anything else, which carries its own sentence. */
export function transportFailure(err: unknown): string | null {
  if (typeof err !== "object" || err === null) return null;
  const { name, message, status, code } = err as {
    name?: unknown;
    message?: unknown;
    status?: unknown;
    code?: unknown;
  };
  if (name === "AbortError") return ABORTED;
  if (
    err instanceof TypeError &&
    typeof message === "string" &&
    /failed to fetch|networkerror|load failed|network request failed/i.test(message)
  ) {
    return UNREACHABLE;
  }
  // A 5xx that names its own code carries a sentence the server wrote for a
  // reader ("The chat's box is unreachable."); a bare one, or the framework's
  // default `detail` ("Internal Server Error", read as code "error"), is replaced.
  const bare = code === null || code === undefined || code === "error";
  if (typeof status === "number" && status >= 500 && status <= 599 && bare) {
    return SERVER_FAILED;
  }
  return null;
}

/** A failure as a sentence a reader can take in: a transport failure in the
 *  words above; an API refusal in the server's own sentence, or, when it wrote
 *  none, in the portal's sentence for that kind of failure (the message such an
 *  error carries is this client's fallback, which can name a path or a status);
 *  anything else in the message it carries. */
export function errorText(err: unknown): string {
  const transport = transportFailure(err);
  if (transport) return transport;
  if (err instanceof ApiError) {
    return err.serverMessage ?? readerSentence(err);
  }
  if (typeof err === "object" && err !== null) {
    const message = (err as { message?: unknown }).message;
    if (typeof message === "string" && message) return message;
  }
  return String(err);
}

/** React Query `refetchInterval` for queries whose failure renders an error
 *  banner: poll only while errored, so the banner disappears on its own once
 *  the daemon/backend recovers instead of sticking until a manual reload. */
export function refetchWhileErrored(query: { state: { status: string } }): number | false {
  return query.state.status === "error" ? 15_000 : false;
}

/** `refetchInterval` for gateway-backed catalog queries that resolve to `[]` on
 *  a TRANSIENT failure the data source swallows — e.g. a mid-rotation gateway
 *  401 makes `listModels()` return `[]`, which React Query stores as a SUCCESS,
 *  so `refetchWhileErrored` alone never re-polls and the empty catalog freezes
 *  until a manual reload. Poll while errored OR empty so a transient-empty
 *  catalog self-heals within one interval. A genuinely-empty catalog keeps
 *  polling slowly — acceptable: the login panel owns the screen when logged out
 *  (these queries aren't even mounted then), and the poll is the recovery path
 *  the moment auth returns. The `Array.isArray` guard keeps a still-pending
 *  query (`data === undefined`) from being poked. */
export function refetchWhileErroredOrEmpty(query: {
  state: { status: string; data?: unknown };
}): number | false {
  if (query.state.status === "error") return 15_000;
  if (Array.isArray(query.state.data) && query.state.data.length === 0) return 30_000;
  return false;
}

/** `refetchInterval` for the resolved chat-defaults query: poll while errored OR
 *  while no model is seeded yet. A swallowed transient failure (gateway 401 mid
 *  rotation, signed out) resolves to `{model:null}` — stored as a SUCCESS — so a
 *  plain error-poll never re-resolves and the composer is stuck seeding `models[0]`
 *  until a reload. Polling while `model` is null self-heals the moment the gateway
 *  recovers (the same self-heal `refetchWhileErroredOrEmpty` gives the catalog);
 *  a non-null model (even with no effort variants) stops the poll. */
export function refetchWhileNoChatDefault(query: {
  state: { status: string; data?: unknown };
}): number | false {
  if (query.state.status === "error") return 15_000;
  const data = query.state.data as { model?: string | null } | undefined;
  return data && data.model == null ? 30_000 : false;
}

