// The part hasher, off the main thread.
//
// BLAKE3 in JS costs ~1 s per 32 MiB part (`blake3.ts`), so hashing a 1 GB drop
// on the main thread freezes the tab for half a minute and a 5,000-file drop
// stutters the whole way through. This module is the other side of the seam:
// the upload client posts a part's bytes here and awaits the hex, so the
// scrolling, the tray and the cancel button all stay live while a part hashes.
//
// It is loaded as a module worker from a same-origin URL
// (`new Worker(new URL("./blake3.worker.ts", import.meta.url), {type:"module"})`)
// because the SPA's CSP allows `worker-src 'self'` and deliberately NOT `blob:`.

import { blake3HexOfBlob } from "./blake3";

/** One part, as the pool posts it. A Blob rather than its bytes: structured
 *  clone carries the reference, so a 128 MiB part costs nothing to hand over
 *  and the worker reads it one window at a time — neither side is ever holding
 *  the whole part. */
export interface HashRequest {
  part: Blob;
}

/** The answer, or the reason there is none. A worker that cannot hash sends
 *  `error` so the pool can finish the part in-thread instead of hanging. */
export type HashReply = { hex: string } | { error: string };

/** The whole of the worker's logic, callable without a worker around it — which
 *  is how the tests drive this exact code path. */
export async function answer(request: HashRequest): Promise<HashReply> {
  try {
    return { hex: await blake3HexOfBlob(request.part) };
  } catch (error) {
    return { error: error instanceof Error ? error.message : String(error) };
  }
}

/** A dedicated worker's global has `postMessage` and no `window`; a test that
 *  imports this module for {@link answer} has a `window` and must not have a
 *  message listener installed on it. */
interface WorkerScope {
  window?: unknown;
  postMessage?: (message: HashReply) => void;
  addEventListener?: (type: "message", listener: (event: { data: HashRequest }) => void) => void;
}

const scope = globalThis as WorkerScope;
if (scope.window === undefined && typeof scope.postMessage === "function") {
  scope.addEventListener?.("message", (event) => {
    void answer(event.data).then((reply) => scope.postMessage?.(reply));
  });
}
