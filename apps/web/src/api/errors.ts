// Parse the backend's canonical error envelope out of an openapi-fetch error
// body (`result.error`):
//
//   { "error": { "code": "...", "message": "...", "trace_id": "...", "details"?: {...} } }
//
// then the flat body a Files route answered with before it joined the envelope
// (a current server still sends those keys beside it, for older readers):
//
//   { "code": "...", "message": "...", "detail"?: {...} }
//
// and finally the legacy `{ detail: string | object }` shape so an older
// response (or a test mock) still parses — the nested envelope is read first.

export interface ParsedApiError {
  code: string;
  message: string;
}

/**
 * The error types Pydantic itself emits (`pydantic_core.core_schema.ErrorType`),
 * whose messages ("String should have at least 1 character", "Field required")
 * are written for a developer and name no field. `value_error` is left out: it
 * is how a validator's own `ValueError` sentence arrives. Any type outside this
 * set was named by our own code (`PydanticCustomError("budget_too_large", …)`),
 * and its message was written for the reader.
 */
const PYDANTIC_BUILT_IN_ERROR_TYPES: ReadonlySet<string> = new Set([
  "arguments_type", "assertion_error", "bool_parsing", "bool_type", "bytes_invalid_encoding",
  "bytes_too_long", "bytes_too_short", "bytes_type", "callable_type", "complex_str_parsing",
  "complex_type", "dataclass_exact_type", "dataclass_type", "date_from_datetime_inexact",
  "date_from_datetime_parsing", "date_future", "date_parsing", "date_past", "date_type",
  "datetime_from_date_parsing", "datetime_future", "datetime_object_invalid", "datetime_parsing",
  "datetime_past", "datetime_type", "decimal_max_digits", "decimal_max_places", "decimal_parsing",
  "decimal_type", "decimal_whole_digits", "default_factory_not_called", "dict_type", "enum",
  "extra_forbidden", "finite_number", "float_parsing", "float_type", "frozen_field",
  "frozen_instance", "frozen_set_type", "get_attribute_error", "greater_than",
  "greater_than_equal", "int_from_float", "int_parsing", "int_parsing_size", "int_type",
  "invalid_key", "is_instance_of", "is_subclass_of", "iterable_type", "iteration_error",
  "json_invalid", "json_type", "less_than", "less_than_equal", "list_type", "literal_error",
  "mapping_type", "missing", "missing_argument", "missing_keyword_only_argument",
  "missing_positional_only_argument", "missing_sentinel_error", "model_attributes_type",
  "model_type", "multiple_argument_values", "multiple_of", "needs_python_object",
  "no_such_attribute", "none_required", "recursion_loop", "set_item_not_hashable", "set_type",
  "string_not_ascii", "string_pattern_mismatch", "string_sub_type", "string_too_long",
  "string_too_short", "string_type", "string_unicode", "time_delta_parsing", "time_delta_type",
  "time_parsing", "time_type", "timezone_aware", "timezone_naive", "timezone_offset", "too_long",
  "too_short", "tuple_type", "unexpected_keyword_argument", "unexpected_positional_argument",
  "union_tag_invalid", "union_tag_not_found", "url_parsing", "url_scheme", "url_syntax_violation",
  "url_too_long", "url_type", "uuid_parsing", "uuid_type", "uuid_version",
]);

/**
 * The first field-level message out of a 422's `details.errors[]`.
 *
 * A schema rejection's envelope message is the generic "The request failed
 * validation." — useless to the person who typed the value. The reason they
 * need ("Organization name cannot contain a web address") is one level down, in
 * the per-field list FastAPI emits. Only the message is read: `loc` can name an
 * internal field path, and the input value is never echoed back (it may be a
 * password).
 */
function firstFieldMessage(body: unknown): string | null {
  if (!body || typeof body !== "object" || !("error" in body)) return null;
  const details = (body as { error?: { details?: unknown } }).error?.details;
  if (!details || typeof details !== "object" || !("errors" in details)) return null;
  const errors = (details as { errors: unknown }).errors;
  if (!Array.isArray(errors)) return null;
  for (const entry of errors) {
    if (!entry || typeof entry !== "object") continue;
    const { msg, type } = entry as { msg?: unknown; type?: unknown };
    if (typeof msg !== "string" || !msg) continue;
    // Only a sentence written for the reader: a validator's own ValueError
    // (filed under `value_error`, prefixed "Value error, ") or a custom error
    // type our code named. Pydantic's built-in constraint messages are not
    // shown; the caller's own sentence stands in for them.
    const builtIn = typeof type === "string" && PYDANTIC_BUILT_IN_ERROR_TYPES.has(type);
    if (builtIn && !msg.startsWith("Value error,")) continue;
    return msg.replace(/^Value error,\s*/, "");
  }
  return null;
}

function parseApiError(body: unknown): ParsedApiError | null {
  if (!body || typeof body !== "object") return null;

  if ("error" in body) {
    const env = (body as { error: unknown }).error;
    if (env && typeof env === "object" && "code" in env && "message" in env) {
      const e = env as { code: unknown; message: unknown };
      const code = String(e.code);
      // A validation envelope's own message is a placeholder; prefer the
      // field-level reason when the server sent one.
      const message =
        (code === "validation_error" ? firstFieldMessage(body) : null) ?? String(e.message);
      return { code, message };
    }
  }

  // Every Files route answers a FLAT envelope — `{code, message, detail?}` at the
  // top level, written by `backend/api/deps/files_errors.py` rather than by the
  // platform handler. Read after the nested shape (a body carrying both came from
  // the platform, whose `error.code` is the authoritative one) and before `detail`,
  // whose Files spelling is a bag of ids and never a code. Without this the whole
  // `files.*` vocabulary arrives as a null code and the server's sentence is lost.
  if ("code" in body) {
    const flat = body as { code: unknown; message?: unknown };
    if (typeof flat.code === "string" && flat.code) {
      return {
        code: flat.code,
        // The opaque "not yours" body is `{code:"not_found"}` and nothing else;
        // an empty message lets ApiError fall back to its status sentence.
        message: typeof flat.message === "string" ? flat.message : "",
      };
    }
  }

  if ("detail" in body) {
    const detail = (body as { detail: unknown }).detail;
    if (typeof detail === "string") return { code: "error", message: detail };
    if (detail && typeof detail === "object") {
      const d = detail as { code?: unknown; message?: unknown };
      return {
        code: typeof d.code === "string" ? d.code : "error",
        message: typeof d.message === "string" ? d.message : "",
      };
    }
  }
  return null;
}

/** The bag of fields a refusal explains itself with: the envelope's
 *  `error.details`, else the `detail` object a Files route answered with before
 *  it joined the envelope (never the legacy sentence). */
function detailBag(body: unknown): Readonly<Record<string, unknown>> | null {
  if (!body || typeof body !== "object") return null;
  if ("error" in body) {
    const env = (body as { error: unknown }).error;
    if (env && typeof env === "object" && "code" in env) return envelopeDetails(body);
  }
  if (!("detail" in body)) return null;
  const detail = (body as { detail: unknown }).detail;
  if (!detail || typeof detail !== "object" || Array.isArray(detail)) return null;
  return detail as Record<string, unknown>;
}

/**
 * The single error type thrown by {@link request} for any failed API call.
 * Carries the HTTP `status` and the canonical envelope `code`, plus a user-safe
 * `message` (the backend's `error.message`, else a generic fallback). The
 * dashboard's error state renders `.message`.
 */
export class ApiError extends Error {
  readonly status: number;
  readonly code: string | null;
  /** The `Retry-After` the response carried, verbatim, or null. Kept as the
   *  wire string because the header has two legal spellings (a delta in seconds
   *  and an HTTP date) and only the backoff needs to care which. */
  readonly retryAfter: string | null;
  /** The bag of ids and enums a Files refusal explains itself with (the flat
   *  envelope's `detail`), or null when the body carried none. Untyped on
   *  purpose: each code has its own keys, and the caller that knows the code
   *  narrows it. */
  readonly detail: Readonly<Record<string, unknown>> | null;
  /** The sentence the SERVER wrote, or null when it wrote none and `message`
   *  is this client's own fallback. */
  readonly serverMessage: string | null;
  /** The platform envelope's `error.details` (for example the `login_url` an org's
   *  sign-in step-up names), or null when the body carried none. */
  readonly details: Readonly<Record<string, unknown>> | null;

  constructor(status: number, body: unknown, fallback = "Request failed", headers?: Headers) {
    const parsed = parseApiError(body);
    super(parsed?.message || `${fallback} (${status})`);
    this.name = "ApiError";
    this.status = status;
    this.code = parsed?.code ?? null;
    this.serverMessage = parsed?.message || null;
    this.retryAfter = headers?.get("retry-after") ?? null;
    this.detail = detailBag(body);
    this.details = envelopeDetails(body);
  }
}

function envelopeDetails(body: unknown): Readonly<Record<string, unknown>> | null {
  if (!body || typeof body !== "object") return null;
  const details = (body as { error?: { details?: unknown } }).error?.details;
  if (!details || typeof details !== "object" || Array.isArray(details)) return null;
  return details as Record<string, unknown>;
}

/** The URL an org's sign-in step-up names (its single sign-on), when the error carries one. */
export function stepUpLoginUrl(error: unknown): string | null {
  if (!(error instanceof ApiError)) return null;
  const url = error.details?.login_url;
  return typeof url === "string" && url ? url : null;
}

/** The sentence for a failure nothing more precise can be said about. */
export const GENERIC_FAILURE = "Something went wrong on our end. Try again.";

/** How a caller words the refusals it knows better than the server does. */
export interface RefusalWording {
  /** By envelope code, else by HTTP status: said instead of anything the server wrote. */
  readonly known?: Readonly<Record<string | number, string | undefined>>;
  /** By HTTP status: said only when the server wrote no sentence of its own. */
  readonly unexplained?: Readonly<Record<number, string | undefined>>;
  /** Said when nothing more precise can be (a failed transport, a 5xx, an unexplained
   *  refusal). {@link GENERIC_FAILURE} when omitted. */
  readonly fallback?: string;
}

/**
 * The one sentence a person is shown for a failed request.
 *
 * The caller's copy for the envelope code, then for the status, comes first; then the
 * sentence the server wrote for a refusal it explained. Everything else (a transport
 * failure, a 5xx, a refusal with no sentence) reads as the fallback. The client's own
 * diagnostic (`could not … (500)`) is never shown.
 */
export function refusalSentence(error: unknown, wording: RefusalWording = {}): string {
  const fallback = wording.fallback ?? GENERIC_FAILURE;
  if (!(error instanceof ApiError)) return fallback;
  const known = wording.known ?? {};
  const byCode = error.code !== null ? known[error.code] : undefined;
  const chosen = byCode ?? known[error.status];
  if (chosen) return chosen;
  if (error.status === 0 || error.status >= 500) return fallback;
  if (error.serverMessage) return error.serverMessage;
  return wording.unexplained?.[error.status] ?? fallback;
}

/** What a refused write tells the person who asked for it: `conflict` when the
 *  write named a version that moved on, the server's own sentence where it
 *  wrote one, `forbidden` for a refusal it explained no further, else
 *  `fallback`. Never a status code. */
export function refusalCopy(
  error: unknown,
  copy: { conflict: string; fallback: string; forbidden?: string },
): string {
  return refusalSentence(error, {
    known: { version_conflict: copy.conflict, 409: copy.conflict },
    unexplained: { 403: copy.forbidden },
    fallback: copy.fallback,
  });
}

/**
 * The one sentence a failed read is described by, or `undefined` when nothing more precise than
 * "the request never got there" can be said (the transport failed, so there is no status to read).
 *
 * It exists because the plate's default sentence is about the request not reaching the server, and
 * for a 5xx that is simply false — the request arrived and the server answered badly. Saying the
 * wrong thing confidently is worse than saying the general thing.
 */
export function failureSentence(error: unknown): string | undefined {
  if (!(error instanceof ApiError)) return undefined;
  if (error.status === 429) return "The server is limiting how often it will answer.";
  if (error.status >= 500) return "The server could not answer.";
  return error.message || undefined;
}
