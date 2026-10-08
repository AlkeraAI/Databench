// The widget manager that runs inside the output frame. It holds no network
// access and no code of its own beyond this bundle: every other module (a
// library's nbextension, a large anywidget ESM) is asked of the parent with
// `need_module` and arrives as text.
import * as base from "@jupyter-widgets/base";
import { ManagerBase } from "@jupyter-widgets/base-manager";
import * as controls from "@jupyter-widgets/controls";
import { Widget } from "@lumino/widgets";

import { FrameComm, toDataView, type CommRouter } from "./comm";
import { evaluateAmd, externalDeps, injectClassicScript, instantiateAmd, type ScriptInjector } from "./loaders/amd";
import { PatchRegistry } from "./patches/registry";
import { onRegister, register, type Registration } from "./registry";
import {
  ASSET_VERSION,
  WIDGET_STATE_MIME,
  isAssetRef,
  type CommOpenMessage,
  type InitMessage,
  type JSONObject,
  type ModuleMessage,
  type ParentToFrame,
  type Post,
  type Theme,
} from "./protocol";
import { UI_MODULE, uiWidgetsModule } from "./ui/elements";
import { ANYWIDGET_MODULE, AnyModel, AnyView, type AnywidgetHost } from "./views/anywidget";
import { DataUrlAudioView, DataUrlImageView, DataUrlVideoView } from "./views/media";
import { OUTPUT_MODULE, OutputModel, OutputView } from "./views/output";

type Exports = Record<string, unknown>;
type ModuleKind = "amd" | "asset";

interface PendingModule {
  kind: ModuleKind;
  promise: Promise<unknown>;
  resolve(value: unknown): void;
  reject(err: Error): void;
}

export interface ManagerOptions {
  post: Post;
  /** Where displayed views attach. Default: `document.body`. */
  root?: HTMLElement;
  /** How long a `need_module` may go unanswered. */
  moduleTimeoutMs?: number;
  patches?: PatchRegistry;
  /** How classic module text is run (an inline script in the frame). */
  inject?: ScriptInjector;
}

/** The modules this bundle carries. An environment asset may never replace one. */
export function platformModules(): Map<string, Exports> {
  return new Map<string, Exports>([
    ["@jupyter-widgets/base", base as unknown as Exports],
    [
      "@jupyter-widgets/controls",
      { ...(controls as unknown as Exports), ImageView: DataUrlImageView, VideoView: DataUrlVideoView, AudioView: DataUrlAudioView },
    ],
    [OUTPUT_MODULE, { OutputModel, OutputView }],
    [ANYWIDGET_MODULE, { AnyModel, AnyView }],
    [UI_MODULE, uiWidgetsModule],
  ]);
}

async function sha256Hex(text: string): Promise<string | null> {
  const subtle = globalThis.crypto?.subtle;
  if (!subtle) return null;
  const digest = await subtle.digest("SHA-256", new TextEncoder().encode(text));
  return Array.from(new Uint8Array(digest), (b) => b.toString(16).padStart(2, "0")).join("");
}

const FORM_CONTROLS = "input, select, textarea, button";

export class FrameManager extends ManagerBase implements CommRouter, AnywidgetHost {
  readonly post: Post;
  viewOnly = false;
  private readonly root: HTMLElement;
  private readonly modules: Map<string, Exports>;
  private readonly platform: ReadonlySet<string>;
  private readonly pending = new Map<string, PendingModule>();
  private readonly comms = new Map<string, FrameComm>();
  private readonly sent = new Map<string, FrameComm>();
  private readonly patches: PatchRegistry;
  private readonly moduleTimeoutMs: number;
  private readonly inject: ScriptInjector;
  private readonlyObserver: MutationObserver | null = null;
  private readonly unsubscribe: () => void;

  constructor(options: ManagerOptions) {
    super();
    this.post = options.post;
    this.root = options.root ?? document.body;
    this.modules = platformModules();
    this.platform = new Set(this.modules.keys());
    this.patches = options.patches ?? new PatchRegistry();
    this.moduleTimeoutMs = options.moduleTimeoutMs ?? 30_000;
    this.inject = options.inject ?? injectClassicScript;
    this.root.addEventListener("click", (event) => this.interceptLink(event), true);
    this.unsubscribe = onRegister((r) => this.registered(r));
  }

  /** Stops listening for registrations (a frame that is torn down). */
  dispose(): void {
    this.unsubscribe();
  }

  // ---------------------------------------------------------------- messages

  /** Handles one message from the parent (already validated by the bootstrap). */
  handle(message: ParentToFrame): void {
    switch (message.type) {
      case "init":
        void this.init(message);
        break;
      case "module":
        void this.receiveModule(message);
        break;
      case "comm.open":
        void this.openComm(message);
        break;
      case "comm.msg":
        this.comms.get(message.comm_id)?.deliver(message.content, message.buffers ?? [], message.parent_msg_id ?? null);
        break;
      case "comm.status": {
        const comm = this.sent.get(message.msg_id);
        this.sent.delete(message.msg_id);
        comm?.idle(message.msg_id);
        break;
      }
      case "comm.close": {
        const comm = this.comms.get(message.comm_id);
        this.comms.delete(message.comm_id);
        comm?.closed();
        break;
      }
      case "theme":
        this.applyTheme(message.theme);
        break;
    }
  }

  track(msgId: string, comm: FrameComm): void {
    this.sent.set(msgId, comm);
  }

  reportError(message: string): void {
    this.post({ type: "error", message });
  }

  private async init(message: InitMessage): Promise<void> {
    this.applyTheme(message.theme);
    if (message.readonly) this.enterReadonly();
    try {
      for (const open of message.opens) await this.openComm(open);
      if (message.mime === WIDGET_STATE_MIME && message.opens.length === 0) {
        await this.set_state(message.data as never);
      }
      const modelId = String(message.data.model_id ?? "");
      if (!modelId) throw new Error("the output names no widget");
      await this.display(modelId);
    } catch (err) {
      this.reportError(`widget: ${(err as Error).message ?? String(err)}`);
    }
  }

  private async openComm(message: CommOpenMessage): Promise<void> {
    const comm = new FrameComm(this, message.comm_id, message.target_name);
    this.comms.set(message.comm_id, comm);
    try {
      await this.handle_comm_open(comm as never, {
        content: { comm_id: message.comm_id, target_name: message.target_name, data: message.data },
        metadata: message.metadata ?? {},
        buffers: (message.buffers ?? []).map(toDataView),
      } as never);
    } catch (err) {
      this.reportError(`widget ${message.comm_id}: ${(err as Error).message ?? String(err)}`);
    }
  }

  async display(modelId: string): Promise<base.DOMWidgetView> {
    const model = (await this.get_model(modelId)) as base.DOMWidgetModel;
    const view = await this.create_view<base.DOMWidgetView>(model, {});
    Widget.attach(view.luminoWidget, this.root);
    return view;
  }

  // ---------------------------------------------------------------- modules

  protected async loadClass(className: string, moduleName: string, moduleVersion: string): Promise<typeof base.WidgetModel | typeof base.WidgetView> {
    const mod = (this.modules.get(moduleName) ?? (await this.requestModule(moduleName, moduleVersion, "amd"))) as Exports;
    const cls = mod[className];
    if (!cls) throw new Error(`${moduleName} has no ${className}`);
    return cls as typeof base.WidgetModel;
  }

  /** Asks the parent for a module once; later asks share the answer. */
  requestModule(name: string, version: string, kind: ModuleKind): Promise<unknown> {
    const known = this.pending.get(name);
    if (known) return known.promise;
    let resolve!: (value: unknown) => void;
    let reject!: (err: Error) => void;
    const promise = new Promise<unknown>((res, rej) => {
      resolve = res;
      reject = rej;
    });
    const entry: PendingModule = { kind, promise, resolve, reject };
    this.pending.set(name, entry);
    const timer = setTimeout(() => reject(new Error(`${name} was not supplied`)), this.moduleTimeoutMs);
    promise.then(
      () => clearTimeout(timer),
      () => clearTimeout(timer),
    );
    this.post({ type: "need_module", name, version });
    return promise;
  }

  resolveAsset(ref: string): Promise<string> {
    if (!isAssetRef(ref)) return Promise.reject(new Error("not an asset reference"));
    return this.requestModule(ref, ASSET_VERSION, "asset") as Promise<string>;
  }

  /** A module the frame asked for. Anything it did not ask for, or any name
   *  this bundle already carries, is refused. */
  async receiveModule(message: ModuleMessage): Promise<void> {
    const entry = this.pending.get(message.name);
    if (this.platform.has(message.name) || !entry || this.modules.has(message.name)) {
      this.reportError(`refused module ${message.name}: not requested by this frame`);
      return;
    }
    try {
      if (entry.kind === "asset") {
        const expected = message.name.slice(message.name.lastIndexOf(":") + 1);
        const actual = await sha256Hex(message.code);
        if (actual !== null && actual !== expected) throw new Error("content does not match its hash");
        entry.resolve(message.code);
        return;
      }
      let definition;
      try {
        definition = evaluateAmd(message.code, message.name, this.inject);
      } catch (err) {
        // A script that registered itself instead of calling `define` has
        // already resolved the wait (see `registered`).
        if (this.modules.has(message.name)) return;
        throw err;
      }
      const resolved = new Map<string, unknown>();
      for (const dep of externalDeps(definition)) {
        resolved.set(dep, this.modules.get(dep) ?? (await this.requestModule(dep, "*", "amd")));
      }
      const exportsObject = instantiateAmd(definition, resolved);
      this.patches.apply(message.name, message.version, exportsObject);
      // Registered like every injected script; `registered` resolves the wait.
      register(message.name, message.version, exportsObject);
    } catch (err) {
      const error = err instanceof Error ? err : new Error(String(err));
      this.reportError(`module ${message.name}: ${error.message}`);
      entry.reject(error);
    }
  }

  /** Resolves a `need_module` wait when its module registers. A platform
   *  name, a module already loaded, or one this frame never asked for
   *  changes nothing. */
  private registered(r: Registration): void {
    const entry = this.pending.get(r.name);
    if (!entry || entry.kind !== "amd" || this.platform.has(r.name) || this.modules.has(r.name)) return;
    this.modules.set(r.name, r.exports);
    entry.resolve(r.exports);
  }

  // ---------------------------------------------------------------- comms

  protected async _create_comm(targetName: string, modelId?: string): Promise<never> {
    // A model the frontend creates without a kernel comm stays local.
    const id = modelId ?? base.uuid();
    const comm = new FrameComm(this, id, targetName, true);
    return comm as never;
  }

  protected async _get_comm_info(): Promise<object> {
    return {};
  }

  // ---------------------------------------------------------------- display state

  private applyTheme(theme: Theme): void {
    document.documentElement.dataset.theme = theme;
  }

  /** Inputs disabled and no `comm.send`, for readers who may not run. */
  private enterReadonly(): void {
    this.viewOnly = true;
    this.root.classList.add("alk-readonly");
    const disable = (scope: ParentNode) => {
      for (const el of Array.from(scope.querySelectorAll<HTMLInputElement>(FORM_CONTROLS))) {
        // Only when needed: setting it again is itself a mutation.
        if (!el.disabled) el.disabled = true;
      }
    };
    disable(this.root);
    this.readonlyObserver = new MutationObserver(() => disable(this.root));
    this.readonlyObserver.observe(this.root, { subtree: true, childList: true, attributes: true, attributeFilter: ["disabled"] });
  }

  private interceptLink(event: MouseEvent): void {
    const anchor = (event.target as Element | null)?.closest?.("a[href]") as HTMLAnchorElement | null;
    if (!anchor) return;
    event.preventDefault();
    const href = anchor.getAttribute("href") ?? "";
    if (/^https?:/i.test(href)) this.post({ type: "link", href });
  }
}

export type { JSONObject };
