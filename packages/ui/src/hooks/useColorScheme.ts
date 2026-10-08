import { useCallback, useEffect, useState } from "react";

import { safeLocalStorage } from "../storage";

/**
 * Color-scheme infrastructure for @alkera/ui. The dark scheme is the `:root` base; the light
 * scheme keys on `:root[data-alkera-color-scheme="light"]`. This owns writing that attribute,
 * tracking the OS preference for `system`, and (optionally) persisting the choice. The APP supplies
 * its own default + storage key — those are product choices, not library ones.
 */

export type ColorScheme = "light" | "dark" | "system";

/** Write ui's color-scheme attribute: set it for the light scheme, remove it for dark (the
 *  `:root` base). `system` resolves against the supplied OS-dark flag. */
export function applyColorScheme(scheme: ColorScheme, systemDark: boolean): void {
  const resolved = scheme === "system" ? (systemDark ? "dark" : "light") : scheme;
  const root = document.documentElement;
  if (resolved === "light") root.setAttribute("data-alkera-color-scheme", "light");
  else root.removeAttribute("data-alkera-color-scheme");
}

export interface UseColorSchemeOptions {
  /** Initial scheme when nothing is persisted (default `system`). */
  defaultScheme?: ColorScheme;
  /** localStorage key to persist the choice under; omit to not persist. */
  storageKey?: string;
}

function isScheme(v: unknown): v is ColorScheme {
  return v === "light" || v === "dark" || v === "system";
}

/** Storage throws outright in a private window, in an embedded context, and
 *  wherever site data is blocked — and reaching for it is what throws, not only
 *  using it. A remembered theme is worth nothing beside the page it is painted
 *  on, so both sides of it go through the shared guarded store: one that will
 *  not answer costs the reader their choice and never the render. */
function readSaved(key: string | undefined): string | null {
  return key ? safeLocalStorage().get(key) : null;
}

function writeSaved(key: string | undefined, value: ColorScheme): void {
  // The choice holds for this page even when the browser will not keep it.
  if (key) safeLocalStorage().set(key, value);
}

/**
 * Own the `data-alkera-color-scheme` attribute + the system-preference tracking + persistence. Returns
 * the current scheme and a setter. An app wraps this with its own `defaultScheme` + `storageKey`.
 */
export function useColorScheme(options: UseColorSchemeOptions = {}): {
  scheme: ColorScheme;
  /** The scheme actually painted — `system` collapsed against the live OS preference. A control
   *  that styles or labels itself by the current theme reads this instead of calling matchMedia
   *  at render time, which would not update when the machine switches. */
  resolved: "light" | "dark";
  setScheme: (s: ColorScheme) => void;
} {
  const { defaultScheme = "system", storageKey } = options;

  const [scheme, setSchemeState] = useState<ColorScheme>(() => {
    const saved = readSaved(storageKey);
    return isScheme(saved) ? saved : defaultScheme;
  });
  const [systemDark, setSystemDark] = useState(
    () => typeof window !== "undefined" && window.matchMedia("(prefers-color-scheme: dark)").matches,
  );

  useEffect(() => {
    const mq = window.matchMedia("(prefers-color-scheme: dark)");
    const onChange = () => setSystemDark(mq.matches);
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, []);

  useEffect(() => {
    applyColorScheme(scheme, systemDark);
  }, [scheme, systemDark]);

  const setScheme = useCallback(
    (s: ColorScheme) => {
      setSchemeState(s);
      writeSaved(storageKey, s);
    },
    [storageKey],
  );

  const resolved = scheme === "system" ? (systemDark ? "dark" : "light") : scheme;

  return { scheme, resolved, setScheme };
}

/** The attribute the scheme is written to, and the class a VS Code host flips. */
const SCHEME_ATTRIBUTES = ["data-alkera-color-scheme", "class"];

/**
 * Call `onChange` whenever the painted scheme may have changed: the portal's own attribute on the
 * root, a VS Code host's theme class on the body, or the OS preference. Anything that captures
 * resolved colors once (a chart, a canvas) re-reads them on it. Returns the unsubscribe.
 */
export function watchColorScheme(onChange: () => void): () => void {
  if (typeof document === "undefined") return () => undefined;
  const observer =
    typeof MutationObserver === "undefined" ? null : new MutationObserver(() => onChange());
  for (const el of [document.documentElement, document.body]) {
    if (el) observer?.observe(el, { attributes: true, attributeFilter: SCHEME_ATTRIBUTES });
  }
  const mq = typeof window.matchMedia === "function" ? window.matchMedia("(prefers-color-scheme: dark)") : null;
  mq?.addEventListener("change", onChange);
  return () => {
    observer?.disconnect();
    mq?.removeEventListener("change", onChange);
  };
}

/** A number that moves whenever the painted scheme may have changed ({@link watchColorScheme}),
 *  for an effect that must run again when it does. */
export function useColorSchemeChanges(): number {
  const [changes, setChanges] = useState(0);
  useEffect(() => watchColorScheme(() => setChanges((n) => n + 1)), []);
  return changes;
}
