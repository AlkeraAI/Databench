/** Which accelerator a keyboard shortcut reads: Cmd on a Mac, Ctrl elsewhere. */

export type Platform = "mac" | "other";

/**
 * The platform whose accelerator table this browser reads.
 *
 * `navigator.userAgentData.platform` is the modern signal and is not frozen the
 * way `navigator.platform` is; `navigator.platform` is the fallback every
 * shipping browser still fills in, and the user-agent string is the last resort.
 * All three are consulted, and any one of them naming a Mac settles it: taking
 * only the first one present meant a browser that filled the modern hint with a
 * value the test does not recognise shadowed the two that said `MacIntel`, and
 * the whole page then bound Cmd+A, Cmd+Z and Cmd+click to nothing on a Mac.
 * Resolved once at the route so a Mac gets Cmd and everyone else gets Ctrl —
 * a default of `other` silently binds Cmd+Z to nothing on macOS.
 */
export function detectPlatform(): Platform {
  if (typeof navigator === "undefined") return "other";
  const withData = navigator as Navigator & { userAgentData?: { platform?: string } };
  const signals = [withData.userAgentData?.platform, navigator.platform, navigator.userAgent];
  return signals.some((signal) => typeof signal === "string" && /mac/i.test(signal))
    ? "mac"
    : "other";
}
