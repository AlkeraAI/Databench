// A frame stand-in for manager tests: the manager posts into `sent`, the test
// plays the parent. Classic module text runs through a test injector, since
// jsdom here does not execute inline scripts (the Playwright walkthrough
// covers real injection under the real CSP).
import { FrameManager } from "../manager";
import type { CommOpenMessage, FrameToParent, JSONObject } from "../protocol";
import { defaultPatches } from "../index";

export interface Harness {
  manager: FrameManager;
  root: HTMLElement;
  sent: FrameToParent[];
  sends(): Extract<FrameToParent, { type: "comm.send" }>[];
  errors(): string[];
}

export function harness(): Harness {
  const root = document.createElement("div");
  document.body.appendChild(root);
  const sent: FrameToParent[] = [];
  const manager = new FrameManager({
    post: (m) => sent.push(m),
    root,
    patches: defaultPatches(),
    moduleTimeoutMs: 2_000,
    inject: (code) => new Function(code)(),
  });
  return {
    manager,
    root,
    sent,
    sends: () => sent.filter((m): m is Extract<FrameToParent, { type: "comm.send" }> => m.type === "comm.send"),
    errors: () => sent.filter((m): m is Extract<FrameToParent, { type: "error" }> => m.type === "error").map((m) => m.message),
  };
}

let seq = 0;

export function open(modelName: string, module: string, viewName: string | null, state: JSONObject, extra: Partial<CommOpenMessage> = {}): CommOpenMessage {
  const version = module === "@jupyter-widgets/controls" ? "2.0.0" : module === "@jupyter-widgets/base" ? "2.0.0" : "1.0.0";
  return {
    type: "comm.open",
    comm_id: `model-${++seq}`,
    target_name: "jupyter.widget",
    data: {
      state: {
        _model_name: modelName,
        _model_module: module,
        _model_module_version: version,
        _view_name: viewName,
        _view_module: viewName ? (viewName === "StyleView" ? "@jupyter-widgets/base" : module) : null,
        _view_module_version: viewName ? (viewName === "StyleView" ? "2.0.0" : version) : "",
        ...state,
      },
      buffer_paths: [],
    },
    metadata: { version: "2.1.0" },
    ...extra,
  };
}

export function layout(): CommOpenMessage {
  return open("LayoutModel", "@jupyter-widgets/base", "LayoutView", {});
}

export async function settle(ms = 30): Promise<void> {
  await new Promise((r) => setTimeout(r, ms));
}

export async function until(check: () => boolean, ms = 2_000): Promise<void> {
  const start = Date.now();
  while (!check()) {
    if (Date.now() - start > ms) throw new Error("condition not met");
    await settle(5);
  }
}
