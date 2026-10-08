// The widget part of the output frame's message contract. The frame's
// bootstrap page owns the envelope ({alk: 1, frame: <nonce>}), the
// `event.source` check and the parent origin; it hands the manager the
// messages below already validated, and posts what the manager emits.

/** A binary buffer as it crosses postMessage (structured clone keeps it). */
export type Buffer = ArrayBuffer | ArrayBufferView;

export type JSONValue = null | boolean | number | string | JSONValue[] | { [key: string]: JSONValue };
export type JSONObject = { [key: string]: JSONValue };

/** A comm-open replay or a live comm open. */
export interface CommOpenMessage {
  type: "comm.open";
  comm_id: string;
  target_name: string;
  /** Jupyter widget comm data: `{state, buffer_paths}`. */
  data: JSONObject;
  metadata?: JSONObject;
  buffers?: Buffer[];
}

export interface InitMessage {
  type: "init";
  theme: Theme;
  output_id: string;
  mime: string;
  /** For `application/vnd.jupyter.widget-view+json`: `{model_id, version_major}`.
   *  For `application/vnd.jupyter.widget-state+json` (a kernel-less snapshot):
   *  `{version_major, state: {model_id: {model_name, model_module, model_module_version, state}}}`
   *  plus `model_id`, the view to show. */
  data: JSONObject;
  /** Comm-open replays of the displayed model's closure, in creation order. */
  opens: CommOpenMessage[];
  readonly: boolean;
}

export interface ModuleMessage {
  type: "module";
  name: string;
  version: string;
  code: string;
}

export interface CommMsgMessage {
  type: "comm.msg";
  comm_id: string;
  content: JSONObject;
  buffers?: Buffer[];
  parent_msg_id?: string | null;
}

export interface CommCloseMessage {
  type: "comm.close";
  comm_id: string;
}

export interface CommStatusMessage {
  type: "comm.status";
  msg_id: string;
  execution_state: "idle";
}

export interface ThemeMessage {
  type: "theme";
  theme: Theme;
}

export type Theme = "light" | "dark";

export type ParentToFrame =
  | InitMessage
  | ModuleMessage
  | CommOpenMessage
  | CommMsgMessage
  | CommCloseMessage
  | CommStatusMessage
  | ThemeMessage;

export type FrameToParent =
  | { type: "comm.send"; comm_id: string; msg_id: string; content: JSONObject; buffers: ArrayBuffer[] }
  | { type: "need_module"; name: string; version: string }
  | { type: "link"; href: string }
  | { type: "error"; message: string };

export type Post = (message: FrameToParent, transfer?: Transferable[]) => void;

export const WIDGET_VIEW_MIME = "application/vnd.jupyter.widget-view+json";
export const WIDGET_STATE_MIME = "application/vnd.jupyter.widget-state+json";

/** Large anywidget `_esm` and `_css` values the engine moved into the asset
 *  store reach the frame as this prefix plus the content's SHA-256; the frame
 *  asks for them with `need_module {name: <the reference>, version: "asset"}`. */
export const ASSET_REF_PREFIX = "alkera-asset:sha256:";
export const ASSET_VERSION = "asset";

export function isAssetRef(value: unknown): value is string {
  return typeof value === "string" && /^alkera-asset:sha256:[0-9a-f]{64}$/.test(value);
}
