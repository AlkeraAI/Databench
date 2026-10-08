// The output frame's wire contract, shared by the host (the notebook tab) and
// the modules that run inside the frame.
//
// Every message in either direction carries `{alk: 1, frame: <nonce>}` and a
// `type`. The bootstrap page served at `/c/nb-output/<build hash>` owns the
// frame's side of the envelope; the modules below it see only the payloads.

import type { CommOpen, OutputTheme } from "../outputs/types";

/** The envelope version every message carries. */
export const FRAME_PROTOCOL = 1;
/** The tallest a frame may ask to be. */
export const MAX_FRAME_HEIGHT = 20_000;
/** The largest `comm.send` a frame may make (serialized content plus buffers). */
export const MAX_COMM_SEND_BYTES = 1 << 20;
/** How many `comm.send` messages a frame may make in any one second. */
export const COMM_SENDS_PER_SECOND = 60;

export const WIDGET_VIEW_MIME = "application/vnd.jupyter.widget-view+json";

/** The MIME types drawn in the frame, and the module that draws each. */
export const FRAME_MODULE_BY_MIME: Readonly<Record<string, FrameModuleRef>> = {
  "text/html": { name: "nb-html", version: "1" },
  "image/svg+xml": { name: "nb-svg", version: "1" },
  "application/vnd.vegalite.v5+json": { name: "nb-vega", version: "1" },
  "application/vnd.vegalite.v6+json": { name: "nb-vega", version: "1" },
  "application/vnd.plotly.v1+json": { name: "nb-plotly", version: "1" },
  [WIDGET_VIEW_MIME]: { name: "@alkera/widgets", version: "1" },
};

/** The platform's own frame modules, built from `src/frame/modules/`. */
export const PLATFORM_FRAME_MODULES = ["nb-html", "nb-svg", "nb-vega", "nb-plotly"] as const;
export type PlatformFrameModule = (typeof PLATFORM_FRAME_MODULES)[number];

export interface FrameModuleRef {
  name: string;
  version: string;
}

/** Parent to frame. */
export type ParentMessage =
  | {
      type: "init";
      theme: OutputTheme;
      output_id: string;
      mime: string;
      data: unknown;
      opens: CommOpen[];
      readonly: boolean;
    }
  | { type: "module"; name: string; version: string; code: string }
  | ({ type: "comm.open" } & CommOpen)
  | {
      type: "comm.msg";
      comm_id: string;
      content: Record<string, unknown>;
      buffers: ArrayBuffer[];
      parent_msg_id: string | null;
    }
  | { type: "comm.close"; comm_id: string }
  | { type: "comm.status"; msg_id: string; execution_state: "idle" }
  | { type: "theme"; theme: OutputTheme };

/** Frame to parent. */
export type FrameMessage =
  | { type: "ready" }
  | { type: "size"; height: number }
  | {
      type: "comm.send";
      comm_id: string;
      msg_id: string;
      content: Record<string, unknown>;
      buffers: ArrayBuffer[];
    }
  | { type: "need_module"; name: string; version: string }
  | { type: "link"; href: string }
  | { type: "error"; message: string };

export type FrameMessageType = FrameMessage["type"];

/** The `init` payload a module renders. */
export type FrameInit = Extract<ParentMessage, { type: "init" }>;

/** What the bootstrap hands a module when it renders. */
export interface FrameApi {
  /** The element the output draws into. */
  root: HTMLElement;
  /** Posts a frame-to-parent message (the bootstrap stamps the envelope). */
  post(type: FrameMessageType, payload?: Record<string, unknown>, transfer?: Transferable[]): void;
  /** The current theme. */
  theme(): OutputTheme;
  /** Parent messages after `init`: comm traffic and theme changes. */
  onMessage(listener: (message: ParentMessage) => void): void;
  /** Asks the parent for a module and resolves once it has been injected. */
  loadModule(name: string, version: string): Promise<void>;
  /** Reports an error the reader should see. */
  reportError(message: unknown): void;
}

/** A module that draws one or more MIME types inside the frame. It registers
 *  itself with `window.__alkRegister(name, version, exports)` when its code
 *  runs; the exports are this module. */
export interface FrameModule {
  mimes: readonly string[];
  /** Whether later `module` messages go to this module's `onMessage` listeners
   *  instead of being injected by the bootstrap. The widget manager loads (and
   *  verifies) its own dependencies, some of which are not scripts at all. */
  handlesModules?: boolean;
  render(init: FrameInit, api: FrameApi): void | Promise<void>;
}

declare global {
  interface Window {
    __alkRegister?: (name: string, version: string, exports: FrameModule) => void;
  }
}
