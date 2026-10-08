// Which source and shell the chat composition is running against, right now.
//
// The composition is shared by two shells that resolve their data completely
// differently, so it cannot import either one: it asks here, at call time. Each
// shell installs its pair once at boot (`webview/data` for the extension, the
// `/chat` route for the browser) and a test installs a fixture pair in a
// `beforeEach`.
//
// Read through the accessors, never by destructuring at module scope: a module
// that captured `chatData()` at import time would pin whichever source happened
// to be installed first, which in a test file is none.
//
// OWNERSHIP. One installation at a time, stamped with whoever installed it, and
// only that owner can retire it — a shell cannot pull the pair out from under a
// shell that came after it. The browser's owner is a React provider, and React
// is allowed to re-attach a subtree's effects WITHOUT re-rendering the parent
// that owns them: StrictMode's dev remount does it on every mount, and a
// Suspense re-reveal, an error-boundary reset and a keyed remount do it in
// production. In that sequence the owner's cleanup runs before its own
// re-install, and a child effect in between would read a slot the owner is
// about to fill again. So a release is not applied where it is asked for: it is
// held until the current task has run out, and an install arriving first
// cancels it. Retiring a runtime nobody has left is invisible; retiring one a
// mounted child still reads is the crash this exists to prevent.

import type { ChatCapabilities, ChatDataSource, ChatHost } from "./ChatDataSource";

export interface ChatRuntime {
  source: ChatDataSource;
  host: ChatHost;
}

/** Whoever installed the runtime. A React provider passes its own instance, so
 *  its teardown can only ever retire the installation it made. */
export type ChatRuntimeOwner = object;

/** The owner a shell that installs once at boot gets — the extension webview's
 *  data barrel, and a test's fixture pair. Neither ever releases. */
const SHELL_OWNER: ChatRuntimeOwner = { shell: true };

let installed: { owner: ChatRuntimeOwner; runtime: ChatRuntime } | null = null;
/** The owner whose release has been asked for but not yet applied. */
let retiring: ChatRuntimeOwner | null = null;

/** Install the pair the chat composition runs against. Idempotent per shell —
 *  calling it again replaces the pair (which is how a test swaps fixtures). */
export function installChatRuntime(next: ChatRuntime, owner: ChatRuntimeOwner = SHELL_OWNER): void {
  retiring = null;
  installed = { owner, runtime: next };
}

/** Retire this owner's installation, once it is clear the owner is really gone.
 *
 *  Deferred by one task, and cancelled by any install that lands first, because
 *  React unmounts a subtree it is about to re-attach exactly the same way it
 *  unmounts one the reader has left. Only the difference in what happens NEXT
 *  tells the two apart. */
export function releaseChatRuntime(owner: ChatRuntimeOwner): void {
  if (installed?.owner !== owner) return;
  retiring = owner;
  queueMicrotask(() => {
    if (retiring !== owner) return;
    retiring = null;
    installed = null;
  });
}

/** Forget the installed pair, so a test that never installs one fails loudly
 *  rather than inheriting the previous file's fixture. */
export function resetChatRuntime(): void {
  retiring = null;
  installed = null;
}

export function chatRuntime(): ChatRuntime {
  if (installed === null) {
    throw new Error(
      "no chat runtime installed — the shell must call installChatRuntime({ source, host }) before mounting the chat",
    );
  }
  return installed.runtime;
}

export function chatData(): ChatDataSource {
  return chatRuntime().source;
}

export function chatHost(): ChatHost {
  return chatRuntime().host;
}

/** What the installed source can do. Surfaces gate on this rather than on a
 *  constant, so a source that does not drive an OpenCode harness stops asking
 *  the engine-shaped questions. */
export function chatCaps(): ChatCapabilities {
  return chatRuntime().source.caps;
}
