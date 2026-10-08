// The envelope every failed alkera tool call is delivered in, and the one
// reading of it. `tool_error_result` in the CLI (`plugins/plugin_base/wire.py`)
// builds `{error, tool, classification, ...extra}` for a failure; the harness
// carries it to the transcript as the call's error text, as JSON. A reader
// wants the message, not the envelope.

/** A failed call's envelope, as the CLI wrote it. */
export interface ToolErrorEnvelope {
  /** The readable message the tool raised. */
  message: string;
  /** The tool's registered name. */
  tool: string;
  /** The CLI's classification of the failure (`error`, a probe class, …). */
  classification: string;
}

/** The envelope in a call's error text, or null when the text is not one. The
 *  shape is checked in full — a JSON object carrying string `error`, `tool`
 *  and `classification` — so anything else the harness reports (its own abort
 *  sentence, a provider's message, a tool's plain text) passes through as it
 *  came. Extra keys are the CLI's own additions and are allowed. */
export function parseToolError(text: string | null | undefined): ToolErrorEnvelope | null {
  if (!text) return null;
  const trimmed = text.trim();
  if (!trimmed.startsWith("{") || !trimmed.endsWith("}")) return null;
  let parsed: unknown;
  try {
    parsed = JSON.parse(trimmed);
  } catch {
    return null;
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return null;
  const { error, tool, classification } = parsed as Record<string, unknown>;
  if (typeof error !== "string" || typeof tool !== "string" || typeof classification !== "string") {
    return null;
  }
  return { message: error, tool, classification };
}

/** What a reader sees for a call's failure: the envelope's message when the
 *  text is an envelope, else the text itself. Null stays null. */
export function readableToolError(text: string | null | undefined): string | null {
  if (!text) return null;
  return asSentence(parseToolError(text)?.message ?? text);
}

/** A tool's error read as a sentence: the first letter raised, and only the
 *  first. Tools write their reasons in the register they think in
 *  (`query failed: relation … does not exist`), and the line a reader sees
 *  starts a sentence. A message that opens with an identifier is left alone,
 *  because `web.fetch failed for …` or `files.lease_mismatch: …` names a tool
 *  or a code, not a word, and raising it would misspell it. */
export function asSentence(text: string): string {
  const first = text.search(/\S/);
  if (first < 0) return text;
  const word = text.slice(first).split(/\s/, 1)[0] ?? "";
  if (/[._/:@]/.test(word)) return text;
  const letter = text.charAt(first);
  if (letter === letter.toUpperCase()) return text;
  return text.slice(0, first) + letter.toUpperCase() + text.slice(first + 1);
}

/** The openings a refusal is written with, lowercased: the in-tool gate's
 *  `permission denied: …`, a mode's or a rule's `Refused: …`, the fence's
 *  `The workspace policy refused …`, a person's `Permission was not granted …`,
 *  a read-only connection's sentence, and the vendor's older wording a
 *  persisted transcript still carries. */
const REFUSAL_OPENINGS = [
  // The in-tool gate's form, colon included: a database's own "permission
  // denied for relation orders" is a run that failed, not a refusal.
  "permission denied:",
  "refused",
  "the workspace policy",
  "permission was not granted",
  "this connection is read-only",
  "the user rejected permission",
  "the user has specified a rule",
];

/** Whether a failed call's error text is a refusal — the call never ran —
 *  rather than a run that failed. Read through the envelope, so an alkera
 *  tool's `{error: "permission denied: …"}` counts. */
export function isRefusal(text: string | null | undefined): boolean {
  if (!text) return false;
  const message = (parseToolError(text)?.message ?? text).trim().toLowerCase();
  return REFUSAL_OPENINGS.some((opening) => message.startsWith(opening));
}
