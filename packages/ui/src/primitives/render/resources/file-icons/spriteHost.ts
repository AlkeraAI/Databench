// The file-icon sheet is a <defs> spritesheet. A <use> that points at another
// document is a network fetch, which a strict host CSP (the VS Code webview's
// default-src 'none') denies and an unrewritten asset URL 404s. Mounting the
// sheet once into the document keeps every icon a same-document `#Symbol`
// reference, so the icons render wherever the bundle does.

let mounted = false;

/** Idempotently mount the sprite into the document so `#Symbol` hrefs resolve.
 *  The sheet is ~1MB of markup, so it loads as its own async chunk instead of
 *  riding the shared render barrel; `<use>` references resolve live, so icons
 *  appear the moment the sheet lands. */
export function ensureFileIconSprite(): void {
  if (mounted || typeof document === "undefined") return;
  mounted = true;
  void import("./sprite.svg?raw").then(({ default: sheet }) => {
    // The sheet is a separate chunk, so it can land after the document went away — a test
    // environment torn down while the import was in flight, or a component unmounted during
    // navigation. Re-check rather than throwing into an unhandled rejection.
    if (typeof document === "undefined") return;
    const host = document.createElement("div");
    host.dataset.alkFileIconSprite = "";
    host.style.display = "none";
    host.setAttribute("aria-hidden", "true");
    host.innerHTML = sheet;
    const attach = (): void => {
      document.body.appendChild(host);
    };
    // A module evaluated from a <head> script runs before <body> exists.
    if (document.body) attach();
    else document.addEventListener("DOMContentLoaded", attach, { once: true });
  });
}
