import { useCallback, useState } from "react";

import type { PreviewProps } from "./types";

// A renderer's own view setting — soft wrap, a zoom — held so that either side
// can own the control.
//
// The default is the renderer's: it keeps the value, draws the bar, and tells
// the host so the choice survives a remount. A host that has no room for a
// second bar takes the control instead (`viewControls="host"`), and then the
// value must come from the host on EVERY render — a renderer that kept its own
// copy would answer the file with one value while the toggle the person just
// pressed shows another.

type Host = Pick<PreviewProps, "viewControls" | "viewState" | "onViewState">;

/**
 * The renderer's setting and the one way to change it.
 *
 * `read` turns whatever the host is holding into this renderer's shape; it is
 * given `undefined` for an absent or foreign state and must answer with a
 * sensible default rather than throw, because the host's bag is opaque and may
 * hold a sort column some other renderer left behind.
 */
export function useRendererViewState<T>(
  host: Host,
  read: (viewState: unknown) => T,
): [T, (next: T | ((current: T) => T)) => void] {
  const { viewControls, viewState, onViewState } = host;
  const driven = viewControls === "host";
  const [local, setLocal] = useState<T>(() => read(viewState));
  const value = driven ? read(viewState) : local;

  const set = useCallback(
    (next: T | ((current: T) => T)) => {
      const resolve = (current: T): T =>
        typeof next === "function" ? (next as (current: T) => T)(current) : next;
      if (driven) {
        // The host is the only copy. Resolving against what it is holding right
        // now keeps a wheel notch — which asks for "one step from wherever we
        // are" — correct without a second source of truth to drift from.
        onViewState?.(resolve(read(viewState)));
        return;
      }
      setLocal((current) => {
        const resolved = resolve(current);
        onViewState?.(resolved);
        return resolved;
      });
    },
    [driven, onViewState, read, viewState],
  );

  return [value, set];
}
