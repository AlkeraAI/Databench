// @alkera/widgets: the bundle the output frame's bootstrap injects when it
// shows a widget. The bootstrap validates every message (envelope, source,
// nonce) and calls `start` once; after that it passes each widget message to
// `handle` and posts whatever the manager emits.
//
//   window.__alkRegister = (name, version, exports) => { if (name === "alkera-widgets") api = exports; };
//   // ...inject the bundle...
//   const widgets = api.start({ post, root: document.getElementById("out") });
//   widgets.handle(message); // init, module, comm.open, comm.msg, comm.close, comm.status, theme
import WIDGETS_CSS from "virtual:widgets-css";

import { FrameManager, type ManagerOptions } from "./manager";
import { ipydatagridVegaExpr } from "./patches/ipydatagrid";
import { PatchRegistry } from "./patches/registry";
import type { ParentToFrame, Post } from "./protocol";
import { UI_CSS } from "./ui/elements";

export * from "./protocol";
export { FrameManager, platformModules } from "./manager";
export { PatchRegistry, type ModulePatch } from "./patches/registry";

export const THEME_CSS = `
[data-theme=dark]{color-scheme:dark;--jp-widgets-color:#ddd;--jp-widgets-label-color:#ddd;--jp-widgets-readout-color:#ddd;--jp-widgets-input-color:#ddd;--jp-widgets-input-background-color:#1e1e1e;--jp-widgets-input-border-color:#555;--jp-layout-color1:#1e1e1e;--jp-layout-color2:#2a2a2a;--jp-layout-color3:#444;--jp-border-color1:#555;--jp-ui-font-color1:#ddd;--jp-content-font-color1:#ddd}
.alk-readonly input,.alk-readonly select,.alk-readonly textarea,.alk-readonly button{cursor:not-allowed}
.alk-output pre{margin:0;white-space:pre-wrap}
.alk-error{color:#c33}
`;

/** The patches every frame applies. */
export function defaultPatches(): PatchRegistry {
  const registry = new PatchRegistry();
  registry.register(ipydatagridVegaExpr);
  return registry;
}

export interface FrameWidgets {
  handle(message: ParentToFrame): void;
  manager: FrameManager;
}

function injectStyle(css: string, id: string): void {
  if (document.getElementById(id)) return;
  const style = document.createElement("style");
  style.id = id;
  style.textContent = css;
  document.head.appendChild(style);
}

export const RUNTIME_CODE_ERROR =
  "This widget generates code while it runs, which notebook outputs do not allow, so it cannot be shown here.";

export function start(options: { post: Post; root?: HTMLElement } & Partial<ManagerOptions>): FrameWidgets {
  injectStyle(WIDGETS_CSS, "alk-widgets-controls-css");
  injectStyle(UI_CSS, "alk-widgets-ui-css");
  injectStyle(THEME_CSS, "alk-widgets-theme-css");
  const manager = new FrameManager({ patches: defaultPatches(), ...options });
  // Runtime code generation (Function, eval, blob: workers) is refused by the
  // frame's CSP. Say so once, plainly, instead of leaving an empty output.
  let reported = false;
  const report = () => {
    if (reported) return;
    reported = true;
    manager.reportError(RUNTIME_CODE_ERROR);
  };
  addEventListener("securitypolicyviolation", (event: SecurityPolicyViolationEvent) => {
    const blocked = event.blockedURI;
    if (blocked === "eval" || blocked === "wasm-eval" || blocked.startsWith("blob")) report();
  });
  addEventListener("error", (event: ErrorEvent) => {
    if (event.error instanceof EvalError) report();
  });
  addEventListener("unhandledrejection", (event: PromiseRejectionEvent) => {
    if (event.reason instanceof EvalError) report();
  });
  return { handle: (message) => manager.handle(message), manager };
}
