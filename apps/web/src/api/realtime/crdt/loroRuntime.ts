// The browser build of Loro and its WebAssembly, as one lazily loaded chunk.
// Imported only by `loro.ts`'s `loadLoro()`.

import { Cursor, EphemeralStore, LoroDoc, LoroMap, LoroMovableList, LoroText, UndoManager, VersionVector } from "loro-crdt/web";
// The web entry re-exports everything but the initialiser; it lives on the
// module the entry re-exports, so this is the same instance the classes use.
import init from "loro-crdt/web/loro_wasm.js";
import wasmUrl from "loro-crdt/web/loro_wasm_bg.wasm?url";

import type { LoroApi } from "./loro";

export async function start(): Promise<LoroApi> {
  await init({ module_or_path: wasmUrl });
  return { Cursor, EphemeralStore, LoroDoc, LoroMap, LoroMovableList, LoroText, UndoManager, VersionVector };
}
