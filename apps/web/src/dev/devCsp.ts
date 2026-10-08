/**
 * The Content-Security-Policy the Vite dev server sends.
 *
 * A file preview opens in an <iframe> pointed at the content origin — a
 * different host from the SPA, so its bytes can never execute in the app's
 * origin. Production enforces that with `frame-src <content origin>` (the CDN
 * policy, and ALKERA_CSP in the self-hosted image). Dev sent no policy at all,
 * so a frame src that production blocks worked on a developer's machine and
 * failed only after a deploy. This sends the same `frame-src`, and only that
 * directive: with no `default-src` beside it nothing else on the page is
 * restricted, so module loading, the HMR socket and eval-backed tooling are
 * untouched while the one rule worth catching early is enforced.
 */

/** The dev content origin, as the workspace env spells it. */
export const DEV_CONTENT_ORIGIN_FALLBACK = "http://files.localhost:8000";

/**
 * `frame-src` for the dev server: the content origin and nothing else.
 *
 * `contentBaseUrl` is `FILES_CONTENT_BASE_URL` from the per-worktree env — the
 * same value the API mints grant URLs against. An unset or unparseable value
 * falls back to the classic dev origin rather than widening the directive.
 */
export function devServerHeaders(contentBaseUrl: string | undefined): Record<string, string> {
  return { "Content-Security-Policy": `frame-src ${contentOrigin(contentBaseUrl)}` };
}

function contentOrigin(contentBaseUrl: string | undefined): string {
  const raw = contentBaseUrl?.trim();
  if (!raw) return DEV_CONTENT_ORIGIN_FALLBACK;
  try {
    const url = new URL(raw);
    // `new URL("files.localhost:8123")` parses — as the scheme `files.localhost:`,
    // whose origin is the string "null". Only a real http(s) origin is a source.
    if (url.protocol !== "http:" && url.protocol !== "https:") return DEV_CONTENT_ORIGIN_FALLBACK;
    return url.origin;
  } catch {
    return DEV_CONTENT_ORIGIN_FALLBACK;
  }
}
