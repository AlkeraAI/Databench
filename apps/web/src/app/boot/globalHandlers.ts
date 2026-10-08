// Global browser error handlers → reportClientError. Idempotent (guards against
// double-install in StrictMode / HMR).

import { reportClientError } from "./reportClientError";

let installed = false;

export function installGlobalErrorHandlers(): void {
  if (installed || typeof window === "undefined") return;
  installed = true;

  window.addEventListener("error", (event: ErrorEvent) => {
    void reportClientError(event.error ?? event.message, { kind: "window.onerror" });
  });
  window.addEventListener("unhandledrejection", (event: PromiseRejectionEvent) => {
    void reportClientError(event.reason, { kind: "unhandledrejection" });
  });
}
