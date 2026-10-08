// What may leave the browser in an error report.
//
// `main.tsx` goes to real trouble to keep a credential-bearing URL out of
// third-party telemetry: a session that opens `/reset-password/<token>` or
// `/signup?invite=<token>` never arms Sentry or analytics at all. It says so in
// as many words — those errors "never reach Sentry, which is the point" — and
// records the half it did not build: "a shared href sanitizer applied inside
// each sink". This is that sanitizer, and the sink it was meant to cover is our
// own `/api/v1/errors/events`, which was posting `window.location.href`
// verbatim. A live password-reset token written into our own error store is the
// same credential in a different database.
//
// Scrubbing errs toward redaction. A report is for finding a bug; a parameter
// name that merely looks like a credential is not worth reading, and the cost
// of guessing wrong in the other direction is a live token in a log.

/** What replaces anything that might be a credential. Recognisable on sight in
 *  a report, and not a plausible value of anything. */
export const REDACTED = "[redacted]";

/** A query parameter whose NAME contains one of these carries a value nobody
 *  reading an error report needs. `main.tsx`'s own list (`invite`,
 *  `oauth_ticket`, `user_code`) is covered by `invite`, `oauth` and `code`. */
const CREDENTIAL_PARAM_PARTS = [
  "auth",
  "code",
  "credential",
  "invite",
  "oauth",
  "passwd",
  "password",
  "redirect_uri",
  "refresh",
  "return_to",
  "secret",
  "session",
  "sig",
  "state",
  "ticket",
  "token",
];

/** A path that carries its credential as a segment. The prefix survives — it is
 *  the page the error happened on, which is the useful half — and everything
 *  after it does not. Mirrors `main.tsx`'s `CREDENTIAL_PATH_PREFIXES`. */
const CREDENTIAL_PATH_PREFIXES = ["/reset-password/", "/verify-email/", "/accept-invite/"];

/** A stand-in origin so a relative href parses; it is stripped again on the way
 *  out. `.invalid` is reserved by RFC 2606 and can never resolve. */
const SCRUB_BASE = "http://scrubbed.invalid";

function isCredentialParam(name: string): boolean {
  const lower = name.toLowerCase();
  return CREDENTIAL_PARAM_PARTS.some((part) => lower.includes(part));
}

function scrubParams(params: URLSearchParams): void {
  for (const name of [...new Set(params.keys())]) {
    if (isCredentialParam(name)) params.set(name, REDACTED);
  }
}

/** An `http(s)` URL, which carries its own origin. */
const ABSOLUTE_HTTP = /^https?:\/\//i;

/** Any scheme at all — `alpha *( alpha / digit / "+" / "-" / "." ) ":"`. */
const HAS_SCHEME = /^[a-z][a-z0-9+.-]*:/i;

/**
 * Whether this href is a place in THIS document's origin — `/files`, `files/x`,
 * `?q=1`, `#frag` — and so can be resolved against a stand-in origin and have
 * that origin sliced back off.
 *
 * A protocol-relative `//host/x` is not one: it is an authority, and resolving
 * it against the stand-in would name a different host than the string does.
 */
function isSameOriginPath(href: string): boolean {
  return !href.startsWith("//") && !HAS_SCHEME.test(href);
}

/**
 * A URL with everything credential-shaped taken out of it: userinfo, the values
 * of credential-named query parameters, the tail of a credential-bearing path,
 * and the whole fragment.
 *
 * The fragment goes wholesale. It never reaches a server, so nothing sends one
 * except in-page JavaScript — which is exactly what this is — and an OAuth
 * implicit response puts the access token there. Keeping the part before a `?`
 * inside it would still leak the rest.
 *
 * An `http(s)` URL and a path in this origin are the two shapes this understands.
 * Anything else — `about:blank`, `blob:`, `vscode-webview:`, a protocol-relative
 * authority — has no page structure here to read, and resolving it against a
 * stand-in origin rewrites it into something that is not the href at all. Those
 * are swept for token shapes and handed back with their shape intact.
 *
 * A string that will not parse as a URL is redacted whole: an unparsable href is
 * not something a report can be sure it has read correctly.
 */
export function scrubUrl(href: string): string {
  const absolute = ABSOLUTE_HTTP.test(href);
  if (!absolute && !isSameOriginPath(href)) return scrubText(href);

  let url: URL;
  try {
    url = new URL(href, SCRUB_BASE);
  } catch {
    return REDACTED;
  }

  // A credential in the authority is not a query parameter and no parser will
  // treat it as one: https://user:token@host/.
  if (url.username || url.password) {
    url.username = "";
    url.password = "";
  }

  for (const prefix of CREDENTIAL_PATH_PREFIXES) {
    if (url.pathname.startsWith(prefix) && url.pathname.length > prefix.length) {
      url.pathname = `${prefix}${REDACTED}`;
      break;
    }
  }

  scrubParams(url.searchParams);
  if (url.hash) url.hash = REDACTED;

  // `URLSearchParams` percent-encodes the brackets on the way out; a report is
  // read by a person, so the marker goes back to the shape it is recognised in.
  const scrubbed = url.toString().replaceAll(encodeURIComponent(REDACTED), REDACTED);
  // `new URL(path, base)` invents an origin for a relative href; give the caller
  // back the shape it handed in.
  return absolute ? scrubbed : scrubbed.slice(SCRUB_BASE.length);
}

/** Anything in free text that is shaped like a secret rather than like prose.
 *  Order matters: the URL rule runs first so a credential inside a query string
 *  is handled as a URL, and the catch-all opaque run runs last. */
const SECRET_RULES: readonly { pattern: RegExp; replace: (...parts: string[]) => string }[] = [
  {
    // A URL inside a message or a stack frame, scrubbed as a URL — the module a
    // frame names is most of what makes it useful.
    pattern: /\bhttps?:\/\/[^\s"'<>)\]]+/gi,
    replace: (match) => scrubUrl(match),
  },
  {
    // `Bearer <token>`, `Authorization: <token>`.
    pattern: /\b(bearer|authorization)([\s:]+)[\w.~+/=-]{8,}/gi,
    replace: (_match, label, gap) => `${label}${gap}${REDACTED}`,
  },
  {
    // A JSON Web Token, wherever it appears — the shape of our own session.
    pattern: /\bey[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]*/g,
    replace: () => REDACTED,
  },
  {
    // The prefixed keys this fleet and its providers issue.
    pattern: /\b(?:alk|sk|pk|rk|ghp|gho|xox[abps])[_-][A-Za-z0-9_-]{8,}/gi,
    replace: () => REDACTED,
  },
  {
    // `token=…`, `api_key: …`, `password = …` in text that is not a URL.
    pattern:
      /\b([\w-]*(?:token|secret|password|passwd|api[_-]?key|credential)[\w-]*)(\s*[=:]\s*)["']?[^\s"',;)&]+["']?/gi,
    replace: (_match, name, gap) => `${name}${gap}${REDACTED}`,
  },
  {
    // A long opaque run — a hex digest, a base64url blob. Deliberately longer
    // than the build hashes that ride in asset names in a stack frame.
    pattern: /\b[A-Za-z0-9_-]{40,}\b/g,
    replace: () => REDACTED,
  },
];

/** A message or a stack frame with the credential-shaped parts taken out. */
export function scrubText(text: string): string {
  let out = text;
  for (const rule of SECRET_RULES) {
    out = out.replace(rule.pattern, (...parts: unknown[]) => {
      const groups = parts.slice(0, -2).map((part) => (typeof part === "string" ? part : ""));
      return rule.replace(...groups);
    });
  }
  return out;
}
