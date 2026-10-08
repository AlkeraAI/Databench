// The notebook settings schema's shape, as alkera_notebook.format.settings
// exports it (generated/notebookSettings.ts holds the values). A settings form
// renders any list of these; none of a setting's names or values live here.

export type SettingControl = "enum" | "bool" | "int-range" | "env-ref" | "connection-ref" | "identifier" | "none";
export type SettingEffect = "none" | "restart_kernel" | "next_run";
/** Where an effective value came from. */
export type SettingSource = "notebook" | "workspace" | "detected" | "default";

export interface SettingSpec {
  name: string;
  type: "enum" | "bool" | "int" | "env" | "text";
  default: string | number | boolean | null;
  label: string;
  help: string;
  control: SettingControl;
  effect: SettingEffect;
  choices?: readonly { value: string | number | boolean; label: string }[];
  minimum?: number;
  maximum?: number;
  reason?: string;
  /** For `env` and `text`: the whole value must match it. */
  pattern?: string;
}

/** Whether `value` is one `spec` admits: the same rule the server applies,
 *  read from the exported schema (`null` takes a setting out of the file). */
export function settingValid(spec: SettingSpec, value: unknown): boolean {
  if (value === null) return true;
  switch (spec.type) {
    case "enum":
      return (spec.choices ?? []).some((c) => c.value === value);
    case "bool":
      return typeof value === "boolean";
    case "int":
      return Number.isInteger(value) && (spec.minimum === undefined || (value as number) >= spec.minimum) && (spec.maximum === undefined || (value as number) <= spec.maximum);
    default:
      return typeof value === "string" && (spec.pattern === undefined || new RegExp(`^(?:${spec.pattern})$`, "u").test(value));
  }
}

export interface NotebookSettingsSchema {
  notebook: readonly SettingSpec[];
  cells: Readonly<Record<string, readonly SettingSpec[]>>;
}
