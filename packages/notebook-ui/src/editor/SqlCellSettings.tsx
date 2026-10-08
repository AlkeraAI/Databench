// A SQL cell's result name, under its header: the name its result is bound to
// (so other cells can read it), and a quiet note when the result is hidden.
// Showing or hiding the result is a cell menu command. Both live in the
// cell's `meta`, written through the document's `set_meta`.

import { useEffect, useState } from "react";

import { isIdentifier } from "../model/python";

/** The name a SQL cell binds its result to when it names none. */
export const DEFAULT_OUTPUT_VAR = "_df";
export const INVALID_NAME = "Not a valid Python name";

export interface SqlCellSettingsProps {
  meta: Record<string, unknown>;
  canEdit: boolean;
  onSetMeta(meta: Record<string, unknown>): void;
}

export function SqlCellSettings({ meta, canEdit, onSetMeta }: SqlCellSettingsProps) {
  const outputVar = typeof meta.output_var === "string" ? meta.output_var : DEFAULT_OUTPUT_VAR;
  const showOutput = meta.show_output !== false;
  const [draft, setDraft] = useState(outputVar);
  // A collaborator's rename reaches the field unless this person is mid-edit.
  const [editing, setEditing] = useState(false);
  useEffect(() => {
    if (!editing) setDraft(outputVar);
  }, [outputVar, editing]);

  if (!canEdit) {
    return (
      <span className="nb-sql-settings">
        <span className="nb-sql-settings__var" title="Result name">
          {outputVar}
        </span>
        {showOutput ? null : <span className="nb-sql-settings__hidden">Result hidden</span>}
      </span>
    );
  }

  const valid = isIdentifier(draft);
  const commit = () => {
    setEditing(false);
    if (!valid) {
      setDraft(outputVar);
      return;
    }
    if (draft !== outputVar) onSetMeta({ output_var: draft });
  };
  return (
    <span className="nb-sql-settings">
      <input
        className="nb-sql-settings__var-input"
        aria-label="Result name"
        title="Result name"
        aria-invalid={valid ? undefined : true}
        spellCheck={false}
        value={draft}
        size={Math.max(4, draft.length)}
        onFocus={() => setEditing(true)}
        onChange={(event) => setDraft(event.currentTarget.value)}
        onBlur={commit}
        onKeyDown={(event) => {
          // The notebook's own keys (run, command mode) stay out of the field.
          event.stopPropagation();
          if (event.key === "Enter") event.currentTarget.blur();
          if (event.key === "Escape") {
            setDraft(outputVar);
            setEditing(false);
          }
        }}
        onMouseDown={(event) => event.stopPropagation()}
      />
      {valid ? null : (
        <span className="nb-sql-settings__error" role="alert">
          {INVALID_NAME}
        </span>
      )}
      {showOutput ? null : <span className="nb-sql-settings__hidden">Result hidden</span>}
    </span>
  );
}
