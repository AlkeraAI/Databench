// What an output renderer is, and what it is handed.
//
// An output is a MIME bundle; the registry picks the richest type a renderer
// is registered for and draws it either here, in the app (text, images,
// sanitized Markdown, JSON, tables, Alkera charts, layouts), or in the
// content-origin frame (HTML, SVG, Vega-Lite, Plotly, widgets), where code
// the notebook produced can run without reaching the app. A renderer is a
// registration, so a new type is one call, never a branch in the cell.

import type { ComponentType, ReactNode } from "react";

import type { FrameQuery, MimeBundle, TablePageWire } from "../model/types";

export type OutputTheme = "light" | "dark";

/** Where a renderer draws. */
export type OutputPlace = "app" | "frame";

/** What the frame host needs from the page to run a framed output. The host
 *  app supplies it; without one a framed type falls back to the next
 *  renderer the bundle offers (usually `text/plain`). */
export interface FrameServices {
  /** The content origin's bootstrap page, `/c/nb-output/<build hash>` on the
   *  content host, absolute. */
  bootstrapUrl: string;
  /** The code of a renderer or widget module, by name and version: the
   *  platform's own bundles from the app's assets, widget assets from the
   *  notebook's asset store. Rejects for a module it cannot vouch for. */
  loadModule(name: string, version: string): Promise<string>;
  /** Comm traffic for widgets. Absent: widgets render read-only. */
  comms?: CommBridge;
  /** Asks before a link a framed output wants to open is followed. */
  confirmLink?(href: string): void;
}

/** The page's side of widget comms, per notebook. */
export interface CommBridge {
  /** Comm-open replays for the closure of `modelId`, in creation order. */
  opensFor(modelId: string): CommOpen[];
  /** Kernel-to-frontend traffic, as it arrives. */
  subscribe(listener: (message: CommInbound) => void): () => void;
  /** A frontend message to the kernel; `msg_id` names the delivery, and
   *  `frame_id` the frame it came from when the bridge attaches frames. */
  send(message: { comm_id: string; msg_id: string; content: Record<string, unknown>; buffers: ArrayBuffer[]; frame_id?: string }): void;
  /** Where the engine keeps the frames (its widget hub routes each frame only
   *  the models it shows): attach one, get its comm-open replays, and receive
   *  its own traffic. Resolves `null` when the person may not attach (a
   *  reader): the frame then shows the widget read-only, as it does without
   *  this method, from `opensFor` and the broadcast. A read-only frame is
   *  never attached. */
  attach?(frame: FrameAttach, listener: (message: CommInbound) => void): Promise<AttachedFrame | null>;
}

export interface FrameAttach {
  /** The host's own name for the frame: what `send` names it by. The bridge
   *  maps it to whatever id the hub gives the frame. */
  frame_id: string;
  /** The output the frame shows. */
  output_id: string;
  /** The widget model it displays. */
  model_id: string;
}

export interface AttachedFrame {
  opens: CommOpen[];
  /** Traffic the hub routed the frame before the attach answered: delivered
   *  after the replays, in order. */
  pending?: CommInbound[];
  detach(): void;
}

export interface CommOpen {
  comm_id: string;
  target_name: string;
  data: Record<string, unknown>;
  buffers?: ArrayBuffer[];
  metadata?: Record<string, unknown>;
}

export type CommInbound =
  | ({ type: "comm.open" } & CommOpen)
  | { type: "comm.msg"; comm_id: string; content: Record<string, unknown>; buffers?: ArrayBuffer[]; parent_msg_id?: string | null }
  | { type: "comm.close"; comm_id: string }
  | { type: "comm.status"; msg_id: string; execution_state: "idle" };

/** Everything a renderer may need beyond its own data. */
export interface OutputContext {
  theme: OutputTheme;
  /** A reader who may not run: widgets render without sending. */
  readonly: boolean;
  cellId: string;
  outputId: string;
  /** Pages, sorts and filters a table through `notebook.inspect frame`. */
  inspectFrame?: (query: FrameQuery) => Promise<TablePageWire>;
  /** The content frame, when the host can serve one. */
  frame?: FrameServices;
  /** Where an output too large to travel inline is read from, by its hash
   *  (the notebook's saved-output store). Absent: such an output is named,
   *  not shown. */
  blobUrl?: (sha256: string) => string;
  /** Draws a nested bundle (a layout's children) with the same registry. */
  renderBundle(bundle: MimeBundle, key: string): ReactNode;
}

export interface OutputRendererProps<T = unknown> {
  mime: string;
  data: T;
  bundle: MimeBundle;
  metadata?: Record<string, unknown>;
  context: OutputContext;
}

export interface OutputRenderer {
  id: string;
  /** The MIME types it draws. */
  mimes: readonly string[];
  /** Higher wins among renderers for the same bundle. */
  rank: number;
  place: OutputPlace;
  /** Whether it can draw in this context (a framed renderer needs `frame`). */
  available?(context: OutputContext): boolean;
  Component: ComponentType<OutputRendererProps>;
}

/** What a host hands an output list: the context minus what each output
 *  fills in for itself (its id and the nested-bundle renderer). */
export type OutputAreaContext = Omit<OutputContext, "outputId" | "renderBundle">;
