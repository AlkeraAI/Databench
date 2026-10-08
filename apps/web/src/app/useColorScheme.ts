import { useColorScheme as useColorSchemeBase, type ColorScheme } from "@alkera/ui";

export type { ColorScheme };

/**
 * The portal's light/dark control, signed in or not. Until a person picks one, the portal follows
 * the machine — live, so switching the OS while a tab is open re-themes it. An explicit pick wins
 * and persists under the portal's key; picking Auto stores `system` and hands the choice back to
 * the machine. The mechanics — writing the `data-alkera-color-scheme` attribute and tracking the
 * OS preference for `system` — come from ui.
 *
 * This runs at MOUNT, which is far too late to decide an unconfigured visitor's first paint: the
 * token layer's base is the dark palette, so a light machine would see dark until the bundle
 * boots. public/theme-boot.js settles the attribute before anything paints and this takes it over
 * unchanged — the two must agree on the key and the default or the handover is a visible flash.
 */
export function useColorScheme(): {
  scheme: ColorScheme;
  resolved: "light" | "dark";
  setScheme: (s: ColorScheme) => void;
} {
  return useColorSchemeBase({ defaultScheme: "system", storageKey: "alk-color-scheme" });
}
