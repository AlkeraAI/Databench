// Find and replace across the notebook's cells. The panel holds the search;
// the host applies the replace operations it emits and reveals each match.

import { useMemo, useState } from "react";
import type { KeyboardEvent } from "react";
import { findMatches, replaceAll, replaceOne } from "../../model/find";
import type { FindMatch, FindOptions, FindScope } from "../../model/find";
import type { CellRuntime, DocCell, NotebookOp } from "../../model/types";
import "./panels.css";

export interface FindReplacePanelProps {
  cells: readonly DocCell[];
  runtime: Readonly<Record<string, Pick<CellRuntime, "outputs"> | undefined>>;
  /** Can view or Can comment: find only. */
  readOnly?: boolean;
  initialQuery?: string;
  onApply: (ops: NotebookOp[]) => void;
  /** Called with the match to scroll to and select. */
  onReveal?: (match: FindMatch) => void;
  onClose?: () => void;
}

export function FindReplacePanel({ cells, runtime, readOnly = false, initialQuery = "", onApply, onReveal, onClose }: FindReplacePanelProps) {
  const [query, setQuery] = useState(initialQuery);
  const [replacement, setReplacement] = useState("");
  const [caseSensitive, setCaseSensitive] = useState(false);
  const [wholeWord, setWholeWord] = useState(false);
  const [regex, setRegex] = useState(false);
  const [scope, setScope] = useState<FindScope>({ code: true, markdown: true, outputs: false });
  const [current, setCurrent] = useState(0);

  const options: FindOptions = { query, caseSensitive, wholeWord, regex, scope };
  const result = useMemo(
    () => findMatches(cells, runtime, { query, caseSensitive, wholeWord, regex, scope }),
    [cells, runtime, query, caseSensitive, wholeWord, regex, scope],
  );
  const matches = result.ok ? result.matches : [];
  const index = matches.length === 0 ? -1 : Math.min(current, matches.length - 1);
  const match = index === -1 ? null : matches[index];
  const sourceMatches = matches.filter((m) => m.where === "source").length;

  const go = (step: 1 | -1): void => {
    if (matches.length === 0) return;
    const next = (index + step + matches.length) % matches.length;
    setCurrent(next);
    onReveal?.(matches[next]);
  };

  const replaceCurrent = (): void => {
    if (!match) return;
    const outcome = replaceOne(cells, match, options, replacement);
    if (outcome.ok && outcome.ops.length > 0) onApply(outcome.ops);
  };

  const replaceEvery = (): void => {
    const outcome = replaceAll(cells, options, replacement);
    if (outcome.ok && outcome.ops.length > 0) onApply(outcome.ops);
  };

  const onFindKey = (event: KeyboardEvent<HTMLInputElement>): void => {
    if (event.key === "Enter") {
      event.preventDefault();
      go(event.shiftKey ? -1 : 1);
    }
  };

  const onPanelKey = (event: KeyboardEvent<HTMLElement>): void => {
    if (event.key === "Escape" && onClose) {
      event.preventDefault();
      onClose();
    }
  };

  let status = "";
  if (!result.ok) status = "Invalid regular expression";
  else if (query !== "") status = matches.length === 0 ? "No results" : `${index + 1} of ${matches.length}`;

  const toggle = (label: string, text: string, value: boolean, set: (v: boolean) => void) => (
    <button type="button" className="nb-chip" aria-label={label} title={label} aria-pressed={value} onClick={() => set(!value)}>
      <span aria-hidden="true">{text}</span>
    </button>
  );

  const scopeBox = (key: keyof FindScope, label: string) => (
    <label className="nb-panel__row">
      <input type="checkbox" checked={scope[key]} onChange={(event) => setScope({ ...scope, [key]: event.target.checked })} />
      {label}
    </label>
  );

  return (
    <section className="nb-panel" aria-label="Find and replace" onKeyDown={onPanelKey}>
      <div className="nb-panel__row">
        <input
          className="nb-input"
          type="search"
          aria-label="Find"
          placeholder="Find"
          value={query}
          aria-invalid={!result.ok}
          onChange={(event) => {
            setQuery(event.target.value);
            setCurrent(0);
          }}
          onKeyDown={onFindKey}
        />
        {toggle("Match case", "Aa", caseSensitive, setCaseSensitive)}
        {toggle("Whole word", "W", wholeWord, setWholeWord)}
        {toggle("Regular expression", ".*", regex, setRegex)}
      </div>
      <div className="nb-panel__row">
        <span role="status" className={result.ok ? "nb-muted" : "nb-danger"}>
          {status}
        </span>
        <button type="button" className="nb-chip" onClick={() => go(-1)} disabled={matches.length === 0}>
          Previous
        </button>
        <button type="button" className="nb-chip" onClick={() => go(1)} disabled={matches.length === 0}>
          Next
        </button>
      </div>
      <div className="nb-panel__row">
        <input
          className="nb-input"
          aria-label="Replace"
          placeholder="Replace"
          value={replacement}
          disabled={readOnly}
          onChange={(event) => setReplacement(event.target.value)}
        />
        <button type="button" className="nb-chip" onClick={replaceCurrent} disabled={readOnly || !match || match.where !== "source"}>
          Replace
        </button>
        <button type="button" className="nb-chip" onClick={replaceEvery} disabled={readOnly || sourceMatches === 0}>
          Replace all
        </button>
      </div>
      {match?.where === "output" && !readOnly ? <p className="nb-muted">Outputs can't be replaced.</p> : null}
      <fieldset className="nb-panel__row" style={{ border: 0, padding: 0, margin: 0 }}>
        <legend className="nb-visually-hidden">Search in</legend>
        {scopeBox("code", "Code")}
        {scopeBox("markdown", "Markdown")}
        {scopeBox("outputs", "Outputs")}
      </fieldset>
    </section>
  );
}
