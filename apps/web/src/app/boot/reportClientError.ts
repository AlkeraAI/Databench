// Client-error reporting: dedupe + throttle, POST to /api/v1/errors/events
// (logged server-side), and hand to every telemetry sink the product registers.
// Never throws into the caller.

import { api } from "../../api/client";

import { PORTAL_TELEMETRY } from "../extensions/portal";
import { scrubText, scrubUrl } from "./scrub";

interface Normalized {
  message: string;
  errorType: string;
  stack?: string;
}

function normalize(error: unknown): Normalized {
  if (error instanceof Error) {
    return {
      message: error.message || error.name,
      errorType: error.name || "Error",
      stack: error.stack,
    };
  }
  return { message: String(error), errorType: "Error" };
}

// Dedupe identical errors within a window + cap total per session so a render
// loop or a refetch storm can't flood the endpoint.
const lastSentAt = new Map<string, number>();
const DEDUPE_WINDOW_MS = 60_000;
const MAX_PER_SESSION = 50;
let sentThisSession = 0;

/** The id-shaped parts of a message, which make one failure look like many.
 *  `ApiError` spells the path it failed on — "the request to
 *  /api/v1/chats/<uuid>/messages failed (404)" — so a reader walking between
 *  chats mints a fresh signature per chat: the 60-second dedupe never matches
 *  and the map grows one entry per id touched. */
const ID_SHAPED =
  /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|\b[0-9a-f]{16,}\b|\b\d{4,}\b/gi;

function signatureOf(errorType: string, message: string): string {
  return `${errorType}:${message.replace(ID_SHAPED, "<id>")}`;
}

export async function reportClientError(
  error: unknown,
  context?: Record<string, unknown>,
): Promise<void> {
  const { message, errorType, stack } = normalize(error);
  const signature = signatureOf(errorType, message);
  const now = Date.now();
  const last = lastSentAt.get(signature);
  if (last !== undefined && now - last < DEDUPE_WINDOW_MS) return;
  // Recorded only for a report that is actually sent, which is what bounds the
  // map: past the session cap nothing is deduped, so remembering a signature
  // there would grow it for as long as the tab is open and dedupe nothing. One
  // entry per report sent, and never more than MAX_PER_SESSION of those — no
  // separate eviction is needed, and one written to look needed would be a
  // branch nothing could ever reach.
  if (sentThisSession >= MAX_PER_SESSION) return;
  lastSentAt.set(signature, now);
  sentThisSession += 1;

  for (const sink of PORTAL_TELEMETRY.items()) sink.captureException?.(error);
  try {
    // `main.tsx` keeps a credential-bearing URL away from Sentry by disarming it
    // for the whole session, and routes those errors here instead — so this is
    // the sink a live reset token or invite actually reaches. Everything that
    // leaves is scrubbed: the page's own href, the message, and the frames.
    const href = typeof window !== "undefined" ? window.location.href : null;
    await api.POST("/api/v1/errors/events", {
      body: {
        component: "web",
        message: scrubText(message),
        error_type: errorType,
        stack: stack ? scrubText(stack) : null,
        // Both sweeps, not one. `scrubUrl` knows the page's structure — the
        // credential-bearing routes, the credential-named parameters, the
        // fragment — and `scrubText` knows the token SHAPES. A route neither
        // list has heard of yet still carries a JWT or an opaque blob in a path
        // segment, and that is redacted in a message today; the href is the
        // same string and gets the same treatment.
        url: href === null ? null : scrubText(scrubUrl(href)),
        context: context ?? null,
      },
    });
  } catch {
    // Reporting must never throw — swallow transport/auth failures.
  }
}

export interface CrashReportInput {
  message: string;
  stacktrace?: string;
  comment?: string;
}

/**
 * Submit a user-initiated crash report (the error-boundary "Send report" path).
 * Returns the report id, or null on failure.
 *
 * Scrubbed like every other report, and for a stronger reason than the rest:
 * this endpoint PERSISTS a row, and the server re-sends the message to Sentry
 * itself — where the boot-time disarm that keeps a credential-bearing session
 * out of third-party telemetry has no reach, because it is a client-side
 * control. The button offering this promises the reader "no secrets"; the
 * error it sends is the same object the boundary already reported.
 *
 * The comment is the reader's own sentence, typed into a box that asks what
 * they were doing, and is sent as written.
 */
export async function submitCrashReport(input: CrashReportInput): Promise<string | null> {
  try {
    const { data } = await api.POST("/api/v1/errors/reports", {
      body: {
        component: "web",
        message: scrubText(input.message),
        stacktrace: input.stacktrace ? scrubText(input.stacktrace) : null,
        comment: input.comment ?? null,
      },
    });
    return data?.id ?? null;
  } catch {
    return null;
  }
}

// Test-only: reset the dedupe/throttle state between cases.
/** How many signatures the dedupe is currently holding. The map is the one
 *  thing here that grows, so a test can watch it rather than infer it. */
export function dedupeSignatureCount(): number {
  return lastSentAt.size;
}

export function __resetReportingState(): void {
  lastSentAt.clear();
  sentThisSession = 0;
}
