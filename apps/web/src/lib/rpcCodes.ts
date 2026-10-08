/** The daemon's JSON-RPC error codes the webview reads off a rejection.
 *
 *  The editor's chat reaches its data through the extension's bridge to the
 *  daemon, and a rejection arrives here as the daemon raised it: `{ code,
 *  message }`. The codes are the daemon's (`alkera_cli/daemon/server.py`) and
 *  the extension spells the same ones in `src/daemon/errors.ts`; a test holds
 *  all three to one value. */

/** A method needed a signed-in user and there was none. The extension has
 *  already flipped its view to the sign-in panel when this arrives. */
export const AUTH_REQUIRED_CODE = -32001;
