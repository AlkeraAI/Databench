// The kernel's variables: sortable, filterable, and each one a way back to the
// cell that defines it.

import { useMemo, useState } from "react";
import type { VarSummary } from "../../model/types";
import { formatBytes } from "./labels";
import "./panels.css";

/** The last part of a dotted type name ("polars.dataframe.frame.DataFrame"
 *  reads as "DataFrame"); the full name is the cell's title. */
export function shortType(type: string): string {
  const parts = type.split(".");
  return parts[parts.length - 1] || type;
}

export type VariableSortKey = "name" | "type" | "size";

export interface VariablesPanelProps {
  variables: readonly VarSummary[];
  onJump: (cellId: string) => void;
}

function sizeText(variable: VarSummary): string {
  const parts: string[] = [];
  if (variable.shape && variable.shape.length > 0)
    parts.push(variable.shape.join(" × "));
  if (variable.size_bytes != null) parts.push(formatBytes(variable.size_bytes));
  return parts.join(", ");
}

/** Variables in the order a column asks for; an unknown size sorts last
 *  whichever way, and ties fall back to the name. */
export function sortVariables(
  variables: readonly VarSummary[],
  key: VariableSortKey,
  descending: boolean,
): VarSummary[] {
  const byName = (a: VarSummary, b: VarSummary): number =>
    a.name.localeCompare(b.name);
  return [...variables].sort((a, b) => {
    if (key === "size") {
      const x = a.size_bytes ?? null;
      const y = b.size_bytes ?? null;
      if (x === null || y === null)
        return x === y ? byName(a, b) : x === null ? 1 : -1;
      const diff = descending ? y - x : x - y;
      return diff !== 0 ? diff : byName(a, b);
    }
    const primary =
      key === "type" ? a.type.localeCompare(b.type) : byName(a, b);
    const ordered = descending ? -primary : primary;
    return ordered !== 0 ? ordered : byName(a, b);
  });
}

export function VariablesPanel({ variables, onJump }: VariablesPanelProps) {
  const [filter, setFilter] = useState("");
  const [sortKey, setSortKey] = useState<VariableSortKey>("name");
  const [descending, setDescending] = useState(false);

  const rows = useMemo(() => {
    const needle = filter.trim().toLowerCase();
    const kept = needle
      ? variables.filter(
          (v) =>
            v.name.toLowerCase().includes(needle) ||
            v.type.toLowerCase().includes(needle),
        )
      : variables;
    return sortVariables(kept, sortKey, descending);
  }, [variables, filter, sortKey, descending]);

  const sortBy = (key: VariableSortKey): void => {
    if (key === sortKey) setDescending(!descending);
    else {
      setSortKey(key);
      setDescending(false);
    }
  };

  const header = (key: VariableSortKey, label: string) => (
    <th
      scope="col"
      aria-sort={
        sortKey === key ? (descending ? "descending" : "ascending") : "none"
      }
    >
      <button type="button" className="nb-link" onClick={() => sortBy(key)}>
        {label}
        {sortKey === key ? (
          <span aria-hidden="true">{descending ? " ↓" : " ↑"}</span>
        ) : null}
      </button>
    </th>
  );

  return (
    <section className="nb-panel" aria-label="Variables">
      <input
        className="nb-input"
        type="search"
        aria-label="Filter variables"
        placeholder="Filter"
        value={filter}
        onChange={(event) => setFilter(event.target.value)}
      />
      {variables.length === 0 ? (
        <p className="nb-panel__empty">No variables</p>
      ) : rows.length === 0 ? (
        <p className="nb-panel__empty">No matches</p>
      ) : (
        <div className="nb-table-wrap">
          <table className="nb-table">
            <thead>
              <tr>
                {header("name", "Name")}
                {header("type", "Type")}
                {header("size", "Size")}
                <th scope="col">Value</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((variable) => (
                <tr key={`${variable.cell_id ?? ""}:${variable.name}`}>
                  <th scope="row" className="nb-mono">
                    {variable.cell_id ? (
                      <button
                        type="button"
                        className="nb-link"
                        onClick={() => onJump(variable.cell_id as string)}
                      >
                        {variable.name}
                      </button>
                    ) : (
                      variable.name
                    )}
                  </th>
                  <td className="nb-muted" title={variable.type}>
                    {shortType(variable.type)}
                  </td>
                  <td>{sizeText(variable)}</td>
                  <td className="nb-mono nb-table__value" title={variable.repr}>
                    {variable.repr}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
