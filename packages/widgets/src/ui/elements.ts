// `@alkera/ui-widgets`: the views for `alkera.ui` elements. One model class
// carries every element (its state is `value`, `label`, `disabled` and the
// kind's own options); the view name picks the control. Option lists travel
// as labels, and the kernel maps a label back to the Python value.
import { DOMWidgetModel, DOMWidgetView } from "@jupyter-widgets/base";

export const UI_MODULE = "@alkera/ui-widgets";
export const UI_MODULE_VERSION = "1.0.0";

export const UI_CSS = `
.alk-ui{display:flex;align-items:center;gap:8px;font:inherit;margin:2px 0}
.alk-ui label.alk-ui-label{min-width:0;color:var(--alk-ui-muted,#555)}
.alk-ui input[type=range]{flex:1;min-width:120px}
.alk-ui .alk-ui-readout{min-width:3ch;font-variant-numeric:tabular-nums}
.alk-ui textarea{width:100%;font:inherit}
.alk-ui-radio,.alk-ui-multiselect{flex-direction:column;align-items:flex-start}
.alk-ui button{font:inherit;padding:4px 12px;border-radius:6px;border:1px solid var(--alk-ui-border,#bbb);background:var(--alk-ui-bg,#f6f6f6);color:inherit;cursor:pointer}
.alk-ui-switch input{appearance:none;width:32px;height:18px;border-radius:9px;background:#bbb;position:relative;cursor:pointer}
.alk-ui-switch input:checked{background:#2f6fde}
.alk-ui-switch input::after{content:"";position:absolute;top:2px;left:2px;width:14px;height:14px;border-radius:50%;background:#fff;transition:left .1s}
.alk-ui-switch input:checked::after{left:16px}
[data-theme=dark] .alk-ui{--alk-ui-muted:#aaa;--alk-ui-border:#555;--alk-ui-bg:#2a2a2a}
`;

export class ElementModel extends DOMWidgetModel {
  defaults(): ReturnType<DOMWidgetModel["defaults"]> {
    return {
      ...super.defaults(),
      _model_name: "ElementModel",
      _model_module: UI_MODULE,
      _view_module: UI_MODULE,
      _model_module_version: UI_MODULE_VERSION,
      _view_module_version: UI_MODULE_VERSION,
      value: null,
      label: "",
      disabled: false,
    };
  }
}

/** Shared plumbing: a labelled row, the disabled trait, value sync. */
abstract class ElementView extends DOMWidgetView {
  protected abstract kind: string;
  protected abstract build(row: HTMLElement): void;
  protected abstract show(value: unknown): void;
  protected abstract controls(): HTMLElement[];

  render(): void {
    this.el.classList.add("alk-ui", `alk-ui-${this.kind}`);
    const label = String(this.model.get("label") ?? "");
    if (label) {
      const el = document.createElement("label");
      el.className = "alk-ui-label";
      el.textContent = label;
      this.el.appendChild(el);
    }
    this.build(this.el);
    this.listenTo(this.model, "change:value", () => this.show(this.model.get("value")));
    this.listenTo(this.model, "change:disabled", () => this.applyDisabled());
    this.show(this.model.get("value"));
    this.applyDisabled();
  }

  protected commit(value: unknown): void {
    this.model.set("value", value, { updated_view: this });
    this.touch();
  }

  private applyDisabled(): void {
    const disabled = Boolean(this.model.get("disabled"));
    for (const control of this.controls()) (control as HTMLInputElement).disabled = disabled;
  }
}

function options(model: DOMWidgetModel): string[] {
  const raw = model.get("options");
  return Array.isArray(raw) ? raw.map(String) : [];
}

class RangeView extends ElementView {
  protected kind = "slider";
  private input!: HTMLInputElement;
  private readout!: HTMLSpanElement;
  protected build(row: HTMLElement): void {
    this.input = document.createElement("input");
    this.input.type = "range";
    for (const key of ["min", "max", "step"]) {
      const v = this.model.get(key);
      if (v !== null && v !== undefined) this.input.setAttribute(key, String(v));
    }
    this.readout = document.createElement("span");
    this.readout.className = "alk-ui-readout";
    this.input.addEventListener("input", () => {
      this.readout.textContent = this.input.value;
      this.commit(Number(this.input.value));
    });
    row.append(this.input, this.readout);
  }
  protected show(value: unknown): void {
    this.input.value = String(value ?? "");
    this.readout.textContent = String(value ?? "");
  }
  protected controls(): HTMLElement[] {
    return [this.input];
  }
}

class NumberView extends ElementView {
  protected kind = "number";
  private input!: HTMLInputElement;
  protected build(row: HTMLElement): void {
    this.input = document.createElement("input");
    this.input.type = "number";
    for (const key of ["min", "max", "step"]) {
      const v = this.model.get(key);
      if (v !== null && v !== undefined) this.input.setAttribute(key, String(v));
    }
    this.input.addEventListener("change", () => {
      if (this.input.value === "") return;
      this.commit(Number(this.input.value));
    });
    row.appendChild(this.input);
  }
  protected show(value: unknown): void {
    this.input.value = value === null || value === undefined ? "" : String(value);
  }
  protected controls(): HTMLElement[] {
    return [this.input];
  }
}

class TextView extends ElementView {
  protected kind = "text";
  private input!: HTMLInputElement;
  protected build(row: HTMLElement): void {
    this.input = document.createElement("input");
    this.input.type = this.model.get("kind") === "password" ? "password" : "text";
    this.input.placeholder = String(this.model.get("placeholder") ?? "");
    this.input.autocomplete = "off";
    this.input.addEventListener("input", () => this.commit(this.input.value));
    row.appendChild(this.input);
  }
  protected show(value: unknown): void {
    const text = typeof value === "string" ? value : "";
    if (this.input.value !== text) this.input.value = text;
  }
  protected controls(): HTMLElement[] {
    return [this.input];
  }
}

class TextAreaView extends ElementView {
  protected kind = "text-area";
  private input!: HTMLTextAreaElement;
  protected build(row: HTMLElement): void {
    this.input = document.createElement("textarea");
    this.input.rows = Number(this.model.get("rows") ?? 4);
    this.input.placeholder = String(this.model.get("placeholder") ?? "");
    this.input.addEventListener("input", () => this.commit(this.input.value));
    row.appendChild(this.input);
  }
  protected show(value: unknown): void {
    const text = typeof value === "string" ? value : "";
    if (this.input.value !== text) this.input.value = text;
  }
  protected controls(): HTMLElement[] {
    return [this.input];
  }
}

class CheckboxView extends ElementView {
  protected kind = "checkbox";
  protected input!: HTMLInputElement;
  protected build(row: HTMLElement): void {
    this.input = document.createElement("input");
    this.input.type = "checkbox";
    this.input.addEventListener("change", () => this.commit(this.input.checked));
    row.prepend(this.input);
  }
  protected show(value: unknown): void {
    this.input.checked = Boolean(value);
  }
  protected controls(): HTMLElement[] {
    return [this.input];
  }
}

class SwitchView extends CheckboxView {
  protected kind = "switch";
  protected build(row: HTMLElement): void {
    super.build(row);
    this.input.setAttribute("role", "switch");
  }
}

class DropdownView extends ElementView {
  protected kind = "dropdown";
  private select!: HTMLSelectElement;
  protected build(row: HTMLElement): void {
    this.select = document.createElement("select");
    if (this.model.get("allow_select_none")) this.select.appendChild(new Option("", ""));
    for (const label of options(this.model)) this.select.appendChild(new Option(label, label));
    this.select.addEventListener("change", () => this.commit(this.select.value === "" ? null : this.select.value));
    row.appendChild(this.select);
  }
  protected show(value: unknown): void {
    this.select.value = typeof value === "string" ? value : "";
  }
  protected controls(): HTMLElement[] {
    return [this.select];
  }
}

class MultiselectView extends ElementView {
  protected kind = "multiselect";
  private boxes: HTMLInputElement[] = [];
  protected build(row: HTMLElement): void {
    for (const label of options(this.model)) {
      const item = document.createElement("label");
      const box = document.createElement("input");
      box.type = "checkbox";
      box.value = label;
      box.addEventListener("change", () => this.commit(this.boxes.filter((b) => b.checked).map((b) => b.value)));
      item.append(box, document.createTextNode(` ${label}`));
      this.boxes.push(box);
      row.appendChild(item);
    }
  }
  protected show(value: unknown): void {
    const chosen = new Set(Array.isArray(value) ? value.map(String) : []);
    for (const box of this.boxes) box.checked = chosen.has(box.value);
  }
  protected controls(): HTMLElement[] {
    return this.boxes;
  }
}

let radioGroup = 0;

class RadioView extends ElementView {
  protected kind = "radio";
  private radios: HTMLInputElement[] = [];
  protected build(row: HTMLElement): void {
    const group = `alk-radio-${++radioGroup}`;
    for (const label of options(this.model)) {
      const item = document.createElement("label");
      const radio = document.createElement("input");
      radio.type = "radio";
      radio.name = group;
      radio.value = label;
      radio.addEventListener("change", () => {
        if (radio.checked) this.commit(radio.value);
      });
      item.append(radio, document.createTextNode(` ${label}`));
      this.radios.push(radio);
      row.appendChild(item);
    }
  }
  protected show(value: unknown): void {
    for (const radio of this.radios) radio.checked = radio.value === value;
  }
  protected controls(): HTMLElement[] {
    return this.radios;
  }
}

class DateView extends ElementView {
  protected kind = "date";
  private input!: HTMLInputElement;
  protected build(row: HTMLElement): void {
    this.input = document.createElement("input");
    this.input.type = "date";
    for (const key of ["min", "max"]) {
      const v = this.model.get(key);
      if (typeof v === "string") this.input.setAttribute(key, v);
    }
    this.input.addEventListener("change", () => this.commit(this.input.value || null));
    row.appendChild(this.input);
  }
  protected show(value: unknown): void {
    this.input.value = typeof value === "string" ? value : "";
  }
  protected controls(): HTMLElement[] {
    return [this.input];
  }
}

/** A button's value counts clicks; the kernel runs `on_click` and, for a run
 *  button, the cells that read it. */
class ButtonView extends ElementView {
  protected kind = "button";
  private button!: HTMLButtonElement;
  render(): void {
    // The label is the button's text, not a separate caption.
    this.el.classList.add("alk-ui", `alk-ui-${this.kind}`);
    this.button = document.createElement("button");
    this.button.type = "button";
    this.button.textContent = String(this.model.get("label") || "Run");
    this.button.addEventListener("click", () => this.commit(Number(this.model.get("value") ?? 0) + 1));
    this.el.appendChild(this.button);
    this.listenTo(this.model, "change:label", () => {
      this.button.textContent = String(this.model.get("label") || "Run");
    });
    this.listenTo(this.model, "change:disabled", () => {
      this.button.disabled = Boolean(this.model.get("disabled"));
    });
    this.button.disabled = Boolean(this.model.get("disabled"));
  }
  protected build(): void {}
  protected show(): void {}
  protected controls(): HTMLElement[] {
    return [this.button];
  }
}

class RunButtonView extends ButtonView {
  protected kind = "run-button";
}

export const uiWidgetsModule: Record<string, unknown> = {
  ElementModel,
  SliderView: RangeView,
  NumberView,
  TextView,
  TextAreaView,
  CheckboxView,
  SwitchView,
  DropdownView,
  MultiselectView,
  RadioView,
  DateView,
  ButtonView,
  RunButtonView,
};
