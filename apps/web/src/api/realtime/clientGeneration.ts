// The realtime client generation this build speaks, and what a tab does when the
// server names a newer one.
//
// The server's `welcome` frame carries `min_client_generation`: the oldest client
// generation it still serves. A tab whose compiled generation is below it was built
// before a protocol change it cannot follow, so it reloads into the current build.
//
// The reload never interrupts typing: it waits until the tab is hidden, or until the
// person has stopped typing for a moment (so a debounced draft write has gone out).
// It happens at most once per server generation per tab session; if the reload
// lands on a build that is still older (a stale cache), the tab keeps running
// instead of reloading in a loop.

import { type SafeStorage, safeSessionStorage } from "@alkera/ui/storage";

/** Must equal `REALTIME_CLIENT_GENERATION` in
 *  `packages/api-core/alkera_core/schemas/realtime/frames.py` (a test pins it). */
export const REALTIME_CLIENT_GENERATION = 2;

/** How long the person must have stopped typing before an outdated tab reloads. */
export const RELOAD_IDLE_MS = 3_000;

const RELOADED_FOR_KEY = "alk.realtime.reloadedForGeneration";

/** Activity that means "someone is typing; do not reload yet". */
const ACTIVITY_EVENTS = ["keydown", "input", "compositionstart", "compositionupdate"] as const;

export interface ClientGenerationDeps {
  readonly generation: number;
  storage: Pick<SafeStorage, "get" | "set">;
  reload: () => void;
  /** The document whose visibility and typing activity gate the reload. */
  doc: Pick<Document, "visibilityState" | "addEventListener" | "removeEventListener">;
  setTimeout: (fn: () => void, ms: number) => unknown;
  clearTimeout: (handle: unknown) => void;
}

export type GenerationOutcome = "current" | "reloading" | "stale";

export function defaultClientGenerationDeps(): ClientGenerationDeps {
  return {
    generation: REALTIME_CLIENT_GENERATION,
    storage: safeSessionStorage(),
    reload: () => window.location.reload(),
    doc: document,
    setTimeout: (fn, ms) => globalThis.setTimeout(fn, ms),
    clearTimeout: (handle) => globalThis.clearTimeout(handle as ReturnType<typeof setTimeout>),
  };
}

/**
 * React to the server's minimum client generation.
 *
 * - `current`: this build is new enough; nothing happens.
 * - `reloading`: this build is older; a reload is scheduled for the next quiet moment.
 * - `stale`: this build is older but already reloaded once for that generation in this
 *   tab session, so it keeps running rather than looping.
 */
export function onMinClientGeneration(
  min: number,
  deps: ClientGenerationDeps = defaultClientGenerationDeps(),
): GenerationOutcome {
  if (!Number.isInteger(min) || min <= deps.generation) return "current";
  const marker = String(min);
  if (deps.storage.get(RELOADED_FOR_KEY) === marker) return "stale";
  // A browser that will not keep the marker could reload forever: stay put instead.
  if (!deps.storage.set(RELOADED_FOR_KEY, marker)) return "stale";
  scheduleQuietReload(deps);
  return "reloading";
}

function scheduleQuietReload(deps: ClientGenerationDeps): void {
  let timer: unknown = null;
  let done = false;
  const fire = (): void => {
    if (done) return;
    done = true;
    if (timer !== null) deps.clearTimeout(timer);
    for (const name of ACTIVITY_EVENTS) deps.doc.removeEventListener(name, onActivity, true);
    deps.doc.removeEventListener("visibilitychange", onVisibility);
    deps.reload();
  };
  const arm = (): void => {
    if (timer !== null) deps.clearTimeout(timer);
    timer = deps.setTimeout(fire, RELOAD_IDLE_MS);
  };
  function onActivity(): void {
    if (!done) arm();
  }
  function onVisibility(): void {
    if (deps.doc.visibilityState === "hidden") fire();
  }
  if (deps.doc.visibilityState === "hidden") {
    fire();
    return;
  }
  for (const name of ACTIVITY_EVENTS) deps.doc.addEventListener(name, onActivity, true);
  deps.doc.addEventListener("visibilitychange", onVisibility);
  arm();
}
