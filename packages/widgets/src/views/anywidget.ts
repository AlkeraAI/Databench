// anywidget models and views: the widget's `_esm` (and `_css`) arrive as
// model state, or as an asset reference when the engine moved a large value
// into the asset store. The front-end module API (AFM) is anywidget's.
import { DOMWidgetModel, DOMWidgetView } from "@jupyter-widgets/base";

import { loadEsm } from "../loaders/esm";
import { isAssetRef } from "../protocol";

export const ANYWIDGET_MODULE = "anywidget";

/** What anywidget models need from the manager. */
export interface AnywidgetHost {
  /** The text behind an asset reference, asked of the parent once. */
  resolveAsset(ref: string): Promise<string>;
  /** Reports a failure as the output's error. */
  reportError(message: string): void;
}

type Callback = (...args: unknown[]) => void;

interface AnyWidgetDefinition {
  initialize?(props: { model: Afm; experimental: Experimental }): unknown;
  render?(props: { model: Afm; el: HTMLElement; experimental: Experimental }): unknown;
}

interface Afm {
  get(key: string): unknown;
  set(key: string, value: unknown): void;
  save_changes(): void;
  send(content: unknown, callbacks?: unknown, buffers?: ArrayBuffer[] | ArrayBufferView[]): void;
  on(event: string, callback: Callback): void;
  off(event?: string | null, callback?: Callback | null): void;
  widget_manager: unknown;
}

interface Experimental {
  invoke(name: string, msg?: unknown, options?: { buffers?: ArrayBuffer[]; signal?: AbortSignal }): Promise<[unknown, DataView[]]>;
}

function afm(model: AnyModel): Afm {
  return {
    get: (key) => model.get(key),
    set: (key, value) => {
      model.set(key, value);
    },
    save_changes: () => model.save_changes(),
    send: (content, callbacks, buffers) =>
      model.send(content as never, callbacks as never, buffers as ArrayBuffer[] | undefined),
    on: (event, callback) => model.on(event, callback),
    off: (event, callback) => model.off(event ?? undefined, callback ?? undefined),
    widget_manager: model.widget_manager,
  };
}

let invokeSeq = 0;

/** anywidget's request/response over custom messages. */
function experimental(model: AnyModel): Experimental {
  return {
    invoke(name, msg, options = {}) {
      const id = `invoke-${++invokeSeq}`;
      return new Promise((resolve, reject) => {
        const onMsg = (content: unknown, buffers: DataView[]) => {
          const reply = content as { id?: string; kind?: string; response?: unknown };
          if (reply?.kind !== "anywidget-command-response" || reply.id !== id) return;
          model.off("msg:custom", onMsg);
          resolve([reply.response, buffers ?? []]);
        };
        options.signal?.addEventListener("abort", () => {
          model.off("msg:custom", onMsg);
          reject(new Error("aborted"));
        });
        model.on("msg:custom", onMsg);
        model.send({ id, kind: "anywidget-command", name, msg } as never, undefined, options.buffers);
      });
    },
  };
}

function injectStyle(css: string, key: string): void {
  const id = `anywidget-css-${key}`;
  let style = document.getElementById(id);
  if (!style) {
    style = document.createElement("style");
    style.id = id;
    document.head.appendChild(style);
  }
  style.textContent = css;
}

export class AnyModel extends DOMWidgetModel {
  widgetDefinition!: Promise<AnyWidgetDefinition>;

  defaults(): ReturnType<DOMWidgetModel["defaults"]> {
    return {
      ...super.defaults(),
      _model_name: "AnyModel",
      _view_name: "AnyView",
      _model_module: ANYWIDGET_MODULE,
      _view_module: ANYWIDGET_MODULE,
    };
  }

  initialize(attributes: never, options: never): void {
    super.initialize(attributes, options);
    const host = this.widget_manager as unknown as AnywidgetHost;
    const text = async (value: unknown): Promise<string> =>
      isAssetRef(value) ? host.resolveAsset(value) : typeof value === "string" ? value : "";
    const key = String(this.get("_anywidget_id") ?? this.model_id);
    const css = this.get("_css");
    if (css) {
      text(css)
        .then((t) => injectStyle(t, key))
        .catch((err: Error) => host.reportError(`widget styles: ${err.message}`));
    }
    this.widgetDefinition = text(this.get("_esm"))
      .then((code) => loadEsm(code))
      .then(async (mod) => {
        let def = (mod.default ?? mod) as AnyWidgetDefinition | (() => Promise<AnyWidgetDefinition>);
        if (typeof def === "function") def = await def();
        await def.initialize?.({ model: afm(this), experimental: experimental(this) });
        return def;
      });
    this.widgetDefinition.catch((err: Error) => host.reportError(err.message));
  }
}

export class AnyView extends DOMWidgetView {
  private cleanup: unknown;

  async render(): Promise<void> {
    const model = this.model as AnyModel;
    const def = await model.widgetDefinition;
    this.cleanup = await def.render?.({ model: afm(model), el: this.el, experimental: experimental(model) });
  }

  remove(): void {
    if (typeof this.cleanup === "function") (this.cleanup as () => void)();
    super.remove();
  }
}
