// Loro, loaded only when a live document is opened.
//
// The library is WebAssembly and several hundred kilobytes; a reader who never
// opens a shared draft must not pay for it. So nothing here imports it at the
// top: `loadLoro()` pulls in the runtime chunk the first time it is called, and
// every caller after shares the one load. A load that fails — a CSP that
// refuses WebAssembly, a browser without it, a network that drops the chunk —
// rejects, and the caller keeps the draft to the tab.

import type * as LoroModule from "loro-crdt/web";

/** The parts of Loro the live lane uses. A test hands in the Node build; the
 *  portal loads the web build. */
export type LoroApi = Pick<
  typeof LoroModule,
  "LoroDoc" | "VersionVector" | "UndoManager" | "EphemeralStore" | "Cursor" | "LoroMap" | "LoroText" | "LoroMovableList"
>;

/** The `loro-crdt` version this build ships, sent in every hello for diagnosing skew. */
export const LORO_JS_VERSION = "1.16.4";

let loading: Promise<LoroApi> | null = null;

export function loadLoro(): Promise<LoroApi> {
  loading ??= import("./loroRuntime").then((runtime) => runtime.start()).catch((error: unknown) => {
    // Allow a later attempt (a flaky network) by the next composer that opens.
    loading = null;
    throw error;
  });
  return loading;
}
