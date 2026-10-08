// The notebook tab's side of the output frame, reduced to what the browser
// check needs: one FrameHost per mounted output, module code fetched from this
// page's own server, a stand-in comm bridge, and everything each host reports
// kept for the spec.

import { FrameHost, type DropReason } from "../../../packages/notebook-ui/src/frame/host";
import type { CommBridge, CommInbound, CommOpen } from "../../../packages/notebook-ui/src/outputs/types";

interface Mounted {
  ready: boolean;
  sizes: number[];
  errors: string[];
  links: string[];
  drops: Record<DropReason, number>;
}

interface Sent {
  comm_id: string;
  msg_id: string;
  content: Record<string, unknown>;
}

declare global {
  interface Window {
    mountOutput(mime: string, data: unknown, bootstrapUrl: string, options?: { readonly?: boolean; height?: number }): number;
    outputs: Mounted[];
    commSent: Sent[];
  }
}

window.outputs = [];
window.commSent = [];

/** A kernel that holds one model per id and answers every send with its idle. */
function standInBridge(): CommBridge {
  const listeners = new Set<(message: CommInbound) => void>();
  return {
    opensFor: (modelId): CommOpen[] => [{ comm_id: modelId, target_name: "jupyter.widget", data: { state: { value: 1 } } }],
    subscribe: (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    send: (message) => {
      window.commSent.push({ comm_id: message.comm_id, msg_id: message.msg_id, content: message.content });
      setTimeout(() => {
        for (const listener of listeners) listener({ type: "comm.status", msg_id: message.msg_id, execution_state: "idle" });
      }, 10);
    },
  };
}

window.mountOutput = (mime, data, bootstrapUrl, options = {}) => {
  const id = window.outputs.length;
  const iframe = document.createElement("iframe");
  iframe.setAttribute("sandbox", "allow-scripts");
  iframe.referrerPolicy = "no-referrer";
  iframe.id = `out-${id}`;
  iframe.style.cssText = `width: 640px; height: ${options.height ?? 40}px; border: 0; display: block`;
  const record: Partial<Mounted> = { ready: false, sizes: [], errors: [], links: [] };
  const host = new FrameHost({
    services: {
      bootstrapUrl,
      loadModule: async (name) => {
        const response = await fetch(`/modules/${encodeURIComponent(name)}.js`);
        if (!response.ok) throw new Error(`no module ${name}`);
        return response.text();
      },
      comms: standInBridge(),
      confirmLink: (href) => record.links?.push(href),
    },
    outputId: `o${id}`,
    mime,
    data,
    theme: "light",
    readonly: options.readonly ?? true,
    events: {
      onReady: () => {
        record.ready = true;
      },
      onSize: (height) => {
        record.sizes?.push(height);
        iframe.style.height = `${height}px`;
      },
      onError: (message) => record.errors?.push(message),
    },
  });
  record.drops = host.drops;
  window.outputs.push(record as Mounted);
  host.attach(() => iframe.contentWindow);
  window.addEventListener("message", (event) => host.handleMessage(event));
  iframe.addEventListener("load", () => host.handleLoad());
  iframe.src = host.src;
  document.body.append(iframe);
  return id;
};
