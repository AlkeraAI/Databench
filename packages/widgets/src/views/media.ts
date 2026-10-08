// controls' Image, Video and Audio views build `blob:` URLs; the frame's CSP
// allows `data:` only for images and media. These subclasses write data URLs.
import { DOMWidgetView } from "@jupyter-widgets/base";
import { AudioView, ImageView, VideoView } from "@jupyter-widgets/controls";

export function bytesToBase64(value: ArrayBuffer | ArrayBufferView): string {
  const bytes =
    value instanceof ArrayBuffer ? new Uint8Array(value) : new Uint8Array(value.buffer, value.byteOffset, value.byteLength);
  let binary = "";
  for (let i = 0; i < bytes.length; i += 0x8000) {
    binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  }
  return btoa(binary);
}

export function base64ToBytes(text: string): ArrayBuffer {
  const binary = atob(text);
  const out = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) out[i] = binary.charCodeAt(i);
  return out.buffer;
}

const FORMAT = /^[a-z0-9.+-]{1,32}$/i;

/** The `src` for a media model's value: a data URL, or for `format: "url"`
 *  the URL the value names (the CSP still decides whether it loads). */
export function mediaSource(kind: "image" | "video" | "audio", format: string, value: unknown): string {
  const bytes = value instanceof DataView || ArrayBuffer.isView(value) || value instanceof ArrayBuffer ? value : new ArrayBuffer(0);
  if (format === "url") {
    const view = bytes instanceof ArrayBuffer ? new Uint8Array(bytes) : new Uint8Array(bytes.buffer, bytes.byteOffset, bytes.byteLength);
    return new TextDecoder("utf-8").decode(view);
  }
  const subtype = FORMAT.test(format) ? format.toLowerCase() : "octet-stream";
  const mime = kind === "image" && subtype === "svg" ? "image/svg+xml" : `${kind}/${subtype}`;
  return `data:${mime};base64,${bytesToBase64(bytes)}`;
}

type MediaElement = HTMLImageElement & HTMLMediaElement;

function applySize(el: Element, width: unknown, height: unknown): void {
  for (const [key, value] of [
    ["width", width],
    ["height", height],
  ] as const) {
    if (typeof value === "string" && value.length > 0) el.setAttribute(key, value);
    else el.removeAttribute(key);
  }
}

function updateMedia(view: DOMWidgetView, kind: "image" | "video" | "audio"): void {
  const el = view.el as MediaElement;
  const model = view.model;
  el.src = mediaSource(kind, String(model.get("format")), model.get("value"));
  applySize(el, model.get("width"), model.get("height"));
  if (kind !== "image") {
    el.loop = Boolean(model.get("loop"));
    el.autoplay = Boolean(model.get("autoplay"));
    el.controls = Boolean(model.get("controls"));
  }
}

export class DataUrlImageView extends ImageView {
  update(): void {
    updateMedia(this, "image");
    DOMWidgetView.prototype.update.call(this);
  }
  remove(): void {
    DOMWidgetView.prototype.remove.call(this);
  }
}

export class DataUrlVideoView extends VideoView {
  update(): void {
    updateMedia(this, "video");
    DOMWidgetView.prototype.update.call(this);
  }
  remove(): void {
    DOMWidgetView.prototype.remove.call(this);
  }
}

export class DataUrlAudioView extends AudioView {
  update(): void {
    updateMedia(this, "audio");
    DOMWidgetView.prototype.update.call(this);
  }
  remove(): void {
    DOMWidgetView.prototype.remove.call(this);
  }
}
