// One form for any list of settings the schema describes: the notebook's own
// (the Settings panel), a cell kind's, and later a workspace's. Each control
// comes from the setting's `control`; a new setting is a schema entry, never
// a change here. A value shows where it came from when the notebook does not
// set it itself, and a value the notebook sets can be handed back to what it
// inherits.

import { useEffect, useId, useState } from "react";
import type { SettingSource, SettingSpec } from "../../model/settings";
import "./panels.css";

type Value = string | number | boolean | null;

export interface SettingOption {
  value: string;
  label: string;
}

export interface SettingsFormProps {
  specs: readonly SettingSpec[];
  /** What the file itself sets (absent: it sets nothing for that name). */
  stored: Readonly<Record<string, unknown>>;
  /** Each setting's value in effect, as the server resolved it. */
  effective?: Readonly<Record<string, unknown>>;
  /** Where each value in effect came from, as the server said. */
  sources?: Readonly<Record<string, SettingSource>>;
  /** The options of an `env-ref` or `connection-ref` control, by name. */
  options?: Readonly<Record<string, readonly SettingOption[]>>;
  canEdit: boolean;
  /** `null` hands the setting back to what it inherits. */
  onChange(name: string, value: Value): void;
}

export const SOURCE_TEXT: Record<Exclude<SettingSource, "notebook">, string> = {
  workspace: "From the workspace",
  detected: "Detected",
  default: "Default",
};

export const EFFECT_TEXT: Record<SettingSpec["effect"], string | null> = {
  none: null,
  restart_kernel: "Changing it restarts the kernel.",
  next_run: "Takes effect on the next run.",
};

function sourceOf(spec: SettingSpec, stored: SettingsFormProps["stored"], sources: SettingsFormProps["sources"]): SettingSource {
  if (stored[spec.name] !== undefined && stored[spec.name] !== null) return "notebook";
  return sources?.[spec.name] ?? (spec.type === "env" ? "detected" : "default");
}

function valueOf(spec: SettingSpec, props: SettingsFormProps): Value {
  const own = props.stored[spec.name];
  if (own !== undefined && own !== null) return own as Value;
  const effective = props.effective?.[spec.name];
  if (effective !== undefined) return effective as Value;
  return spec.default;
}

function IntField({ spec, value, disabled, labelId, onCommit }: { spec: SettingSpec; value: Value; disabled: boolean; labelId: string; onCommit(v: number | null): void }) {
  const [draft, setDraft] = useState(value === null ? "" : String(value));
  useEffect(() => setDraft(value === null ? "" : String(value)), [value]);
  const parsed = draft.trim() === "" ? null : Number(draft);
  const valid =
    parsed === null ||
    (Number.isInteger(parsed) && (spec.minimum === undefined || parsed >= spec.minimum) && (spec.maximum === undefined || parsed <= spec.maximum));
  const commit = () => {
    if (!valid) return;
    if (parsed !== (value === null ? null : Number(value))) onCommit(parsed);
  };
  return (
    <>
      <input
        aria-labelledby={labelId}
        className="nb-input nb-mono"
        inputMode="numeric"
        value={draft}
        disabled={disabled}
        aria-invalid={!valid}
        onChange={(event) => setDraft(event.target.value)}
        onBlur={commit}
        onKeyDown={(event) => {
          if (event.key === "Enter") commit();
        }}
      />
      {!valid ? (
        <p className="nb-install nb-install--error" role="alert">
          {`Enter a whole number from ${(spec.minimum ?? 0).toLocaleString("en-US")} to ${(spec.maximum ?? 0).toLocaleString("en-US")}.`}
        </p>
      ) : null}
    </>
  );
}

export function SettingsForm(props: SettingsFormProps) {
  const base = useId();
  return (
    <div className="nb-settings">
      {props.specs
        .filter((spec) => spec.control !== "none")
        .map((spec) => {
          const labelId = `${base}-${spec.name}`;
          const value = valueOf(spec, props);
          const source = sourceOf(spec, props.stored, props.sources);
          const disabled = !props.canEdit;
          const effect = EFFECT_TEXT[spec.effect];
          let control;
          if (spec.control === "bool") {
            control = (
              <input type="checkbox" aria-labelledby={labelId} checked={value === true} disabled={disabled} onChange={(event) => props.onChange(spec.name, event.target.checked)} />
            );
          } else if (spec.control === "int-range") {
            control = <IntField spec={spec} value={value} disabled={disabled} labelId={labelId} onCommit={(v) => props.onChange(spec.name, v)} />;
          } else {
            const choices: readonly SettingOption[] =
              spec.control === "enum" ? (spec.choices ?? []).map((c) => ({ value: String(c.value), label: c.label })) : (props.options?.[spec.name] ?? []);
            const current = value === null ? "" : String(value);
            const listed = choices.some((c) => c.value === current) || current === "" ? choices : [...choices, { value: current, label: current }];
            control = (
              <select
                aria-labelledby={labelId}
                className="nb-input"
                value={current}
                disabled={disabled}
                onChange={(event) => {
                  const picked = (spec.choices ?? []).find((c) => String(c.value) === event.target.value);
                  props.onChange(spec.name, picked ? picked.value : event.target.value);
                }}
              >
                {current === "" ? <option value="">{SOURCE_TEXT.detected}</option> : null}
                {listed.map((c) => (
                  <option key={c.value} value={c.value}>
                    {c.label}
                  </option>
                ))}
              </select>
            );
          }
          return (
            <div className="nb-setting" key={spec.name} data-setting={spec.name}>
              <div className="nb-setting__head">
                <span id={labelId} className="nb-setting__label">
                  {spec.label}
                </span>
                {source !== "notebook" ? (
                  <span className="nb-muted nb-setting__source">{SOURCE_TEXT[source]}</span>
                ) : props.canEdit ? (
                  <button type="button" className="nb-link" onClick={() => props.onChange(spec.name, null)}>
                    Use the {spec.type === "env" ? "detected" : "default"} value
                  </button>
                ) : null}
              </div>
              {control}
              <p className="nb-muted nb-setting__help">{effect ? `${spec.help} ${effect}` : spec.help}</p>
            </div>
          );
        })}
    </div>
  );
}
