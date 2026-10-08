/* The portal's colour scheme, decided before the first paint.
 *
 * The token layer's `:root` base is the DARK palette and light keys on
 * `data-alkera-color-scheme="light"` (packages/ui/src/theme/tokens.css), so the attribute on
 * <html> is what every token reads. React writes it too — but only once the bundle has booted,
 * which is long after the browser has painted the body ground. A machine set to light would
 * therefore flash the dark page on every load. This settles it first.
 *
 * It is a FILE rather than an inline <script> because the shipped image's default
 * Content-Security-Policy is `script-src 'self'` with no 'unsafe-inline' (apps/web/
 * docker-entrypoint.sh) — an inline block would be refused in production and the flash would
 * come back only there. It is loaded render-blocking (no defer/async) from <head>, ahead of the
 * stylesheet, so nothing is painted until it has run.
 *
 * Kept in step with src/app/useColorScheme.ts: same storage key, same default, same attribute.
 * A test runs this exact file and the hook over every stored value and asserts they agree —
 * drift between them IS the flash this file exists to prevent.
 */
(function () {
  var stored = null;
  try {
    stored = window.localStorage.getItem("alk-color-scheme");
  } catch (e) {
    /* A browser refusing site storage has no stored pick — follow the machine. */
  }
  // Anything that is not an explicit pick — absent, "system", or a value from some other
  // version of the app — means auto.
  var dark = stored === "dark" || (stored !== "light" && window.matchMedia("(prefers-color-scheme: dark)").matches);
  if (dark) document.documentElement.removeAttribute("data-alkera-color-scheme");
  else document.documentElement.setAttribute("data-alkera-color-scheme", "light");
})();
