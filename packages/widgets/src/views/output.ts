// The Output widget. The kernel routes output produced under `with out:` into
// the model's `outputs` trait (the frame never sees IOPub), so the view only
// draws that list.
import { DOMWidgetModel, DOMWidgetView } from "@jupyter-widgets/base";

export const OUTPUT_MODULE = "@jupyter-widgets/output";
export const OUTPUT_MODULE_VERSION = "1.0.0";

interface StreamOutput {
  output_type: "stream";
  name: string;
  text: string | string[];
}
interface ErrorOutput {
  output_type: "error";
  ename: string;
  evalue: string;
  traceback?: string[];
}
interface DataOutput {
  output_type: "display_data" | "execute_result";
  data: Record<string, unknown>;
}
type OutputItem = StreamOutput | ErrorOutput | DataOutput;

// eslint-disable-next-line no-control-regex -- matches ANSI escape sequences
const ANSI = /\u001b\[[0-9;?]*[A-Za-z]/g;
const joinText = (t: string | string[]): string => (Array.isArray(t) ? t.join("") : t);

export class OutputModel extends DOMWidgetModel {
  defaults(): ReturnType<DOMWidgetModel["defaults"]> {
    return {
      ...super.defaults(),
      _model_name: "OutputModel",
      _view_name: "OutputView",
      _model_module: OUTPUT_MODULE,
      _view_module: OUTPUT_MODULE,
      _model_module_version: OUTPUT_MODULE_VERSION,
      _view_module_version: OUTPUT_MODULE_VERSION,
      outputs: [],
      msg_id: "",
    };
  }
}

function pre(className: string, text: string): HTMLPreElement {
  const el = document.createElement("pre");
  el.className = className;
  el.textContent = text;
  return el;
}

function image(mime: string, data: string): HTMLImageElement {
  const img = document.createElement("img");
  img.src = mime === "image/svg+xml" ? `data:image/svg+xml;charset=utf-8,${encodeURIComponent(data)}` : `data:${mime};base64,${data}`;
  return img;
}

export function renderOutputItem(item: OutputItem): HTMLElement {
  if (item.output_type === "stream") return pre(`alk-stream alk-stream-${item.name}`, joinText(item.text).replace(ANSI, ""));
  if (item.output_type === "error") {
    const trace = item.traceback?.length ? item.traceback.join("\n") : `${item.ename}: ${item.evalue}`;
    return pre("alk-error", trace.replace(ANSI, ""));
  }
  const data = item.data ?? {};
  const div = document.createElement("div");
  div.className = "alk-display";
  for (const mime of ["image/png", "image/jpeg", "image/gif", "image/webp", "image/svg+xml"]) {
    if (typeof data[mime] === "string") {
      div.appendChild(image(mime, data[mime] as string));
      return div;
    }
  }
  if (typeof data["text/html"] === "string" || Array.isArray(data["text/html"])) {
    // The frame is sandboxed with no same-origin; markup inserted this way
    // does not run its scripts.
    div.innerHTML = joinText(data["text/html"] as string | string[]);
    return div;
  }
  const plain = data["text/plain"];
  div.appendChild(pre("alk-plain", typeof plain === "string" || Array.isArray(plain) ? joinText(plain) : ""));
  return div;
}

export class OutputView extends DOMWidgetView {
  render(): void {
    this.el.classList.add("jupyter-widgets-output-area", "alk-output");
    this.listenTo(this.model, "change:outputs", () => this.paint());
    this.paint();
  }

  private paint(): void {
    this.el.textContent = "";
    for (const item of (this.model.get("outputs") as OutputItem[] | undefined) ?? []) {
      this.el.appendChild(renderOutputItem(item));
    }
  }
}
