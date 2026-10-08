// The one floor a URL crosses before it becomes a navigation or an href.
//
// These strings come from chat cards, permission asks, web-search hits, tool
// results and the contents of files — model- and third-party text, every one of
// them attacker-influenced under prompt injection. A `javascript:` href executes
// in this origin on click with no browser hardening in the way, and a `data:`
// document runs as markup of its own. The rule was already written down in the
// webview's host and nowhere else; it lives here so every sink inherits it.

/** The URL as a navigation may see it, or null if it may not become one.
 *
 *  `http`/`https` only, and no credentials: a URL carrying a userinfo pair
 *  sends a secret to whatever host follows the `@`, and reads as the host
 *  before it. The answer is the PARSED href rather than the string handed in,
 *  so a spelling that reads as one target and resolves as another cannot
 *  survive the check — the parser has already normalised away the whitespace,
 *  control characters and case a scheme can be hidden behind. */
export function externalHref(url: string): string | null {
  let parsed: URL;
  try {
    parsed = new URL(url.trim());
  } catch {
    return null;
  }
  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") return null;
  if (parsed.username !== "" || parsed.password !== "") return null;
  return parsed.href;
}

/** Open an admitted URL in a new tab, or nothing at all.
 *
 *  The new tab gets neither an opener nor a referrer, so a page reached this way
 *  can neither navigate the tab that opened it nor learn where it came from. A
 *  refused URL is dropped silently: the page cannot tell a drop from a blocked
 *  popup, so a poisoned link cannot be probed from inside it. */
export function openExternalUrl(url: string): boolean {
  const href = externalHref(url);
  if (href === null) return false;
  window.open(href, "_blank", "noopener,noreferrer");
  return true;
}
