// Loro's runtime as the editor's webview sees it: absent. The webview has no
// live draft, so nothing there loads it; a call that somehow did falls back
// like any browser that cannot run WebAssembly.

import type { LoroApi } from "./loro";

export function start(): Promise<LoroApi> {
  return Promise.reject(new Error("the live draft is not available in the editor"));
}
