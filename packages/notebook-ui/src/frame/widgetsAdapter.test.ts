import { describe, expect, it } from "vitest";

import { platformModuleLoader } from "./platformModules";
import { WIDGET_VIEW_MIME, type FrameModule } from "./protocol";
import { WIDGETS_MODULE, withWidgetsAdapter } from "./widgetsAdapter";

/** Runs code as the bootstrap does (inline scripts) in a page whose
 *  `__alkRegister` keeps only render modules, as the real bootstrap does. */
function inject(code: string): Array<[string, string, FrameModule]> {
  const run = (text: string): void => {
    const script = document.createElement("script");
    script.textContent = text;
    document.head.appendChild(script);
    script.remove();
  };
  const doc = document as Document & { __registered?: Array<[string, string, FrameModule]> };
  doc.__registered = [];
  // jsdom has no DragEvent, which the widget controls' bundle references.
  run("if (!window.DragEvent) window.DragEvent = window.MouseEvent;");
  run(
    "window.__alkRegister = function (name, version, module) {" +
      " if (!module || typeof module.render !== 'function' || !Array.isArray(module.mimes)) return;" +
      " document.__registered.push([name, version, module]); };",
  );
  run(code);
  run("delete window.__alkRegister; delete window.AlkeraWidgets;");
  return doc.__registered;
}

const FAKE_BUNDLE = (name: string): string =>
  `window.__alkRegister(${JSON.stringify(name)}, "0.1.0", { start: function (o) {` +
  " document.__started = (document.__started || 0) + 1;" +
  " return { handle: function (m) { (document.__handled = document.__handled || []).push(m.type); } }; } });";

describe("the widget manager in the output frame", () => {
  it("the real bundle registers as a render module for widget views", async () => {
    const bundle = await platformModuleLoader()(WIDGETS_MODULE, "*");
    const registered = inject(withWidgetsAdapter(bundle));
    expect(registered.map(([name]) => name)).toEqual([WIDGETS_MODULE]);
    const [, , module] = registered[0];
    expect(module.mimes).toEqual([WIDGET_VIEW_MIME]);
    expect(module.handlesModules).toBe(true);
    // Nothing of the manager is left on the page's global.
    expect((window as unknown as Record<string, unknown>).AlkeraWidgets).toBeUndefined();
  });

  it("starts the manager once and hands it the init and every later message", () => {
    const [[, , module]] = inject(withWidgetsAdapter(FAKE_BUNDLE("alkera-widgets")));
    const listeners: Array<(m: unknown) => void> = [];
    const api = {
      root: document.createElement("div"),
      post: () => undefined,
      onMessage: (listener: (m: unknown) => void) => listeners.push(listener),
    };
    void module.render({ type: "init" } as never, api as never);
    for (const listener of listeners) listener({ type: "comm.msg" });
    const doc = document as Document & { __started?: number; __handled?: string[] };
    expect(doc.__started).toBe(1);
    expect(doc.__handled).toEqual(["init", "comm.msg"]);
  });

  it.each([
    ["a bundle registering under another name", FAKE_BUNDLE("something-else")],
    ["a bundle with no start", `window.__alkRegister("alkera-widgets", "0.1.0", {});`],
  ])("registers nothing for %s", (_label, bundle) => {
    expect(inject(withWidgetsAdapter(bundle))).toEqual([]);
  });

  it("without the prelude the bundle's exports never reach the bootstrap", () => {
    expect(inject(FAKE_BUNDLE("alkera-widgets"))).toEqual([]);
  });
});
