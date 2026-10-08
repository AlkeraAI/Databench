// The two fixes offered on a `multiple_definitions` graph error. Both are pure:
// cells and what the engine knows of their defs and refs in, document
// operations out. Where the engine has not reported a cell yet, its own code
// is read instead.

import { downstreamOf } from "./graph";
import type { CellNames } from "./graph";
import { PYTHON_KEYWORDS, definesName, isIdentifier, mentionsName, renameIdentifier } from "./python";
import type { DocCell, ErrorInfo, GraphView, ReplaceCellOp } from "./types";

export type QuickFixRefusal =
  /** The error is not a `multiple_definitions` error. */
  | { reason: "not_multiple_definitions" }
  | { reason: "cell_not_found"; cell_id: string }
  /** The cell's code is not Python (SQL, Markdown, unparsable). */
  | { reason: "not_python"; cell_id: string }
  /** The cell does not define the name (or none of the error's names). */
  | { reason: "not_defined_here"; name: string }
  | { reason: "invalid_name"; name: string }
  | { reason: "keyword"; name: string }
  | { reason: "same_name"; name: string }
  /** Another cell defines the new name. */
  | { reason: "already_defined"; name: string; cell_id: string }
  /** A cell the rename touches already uses the new name for something else. */
  | { reason: "name_in_use"; name: string; cell_id: string }
  /** A cell that reads the name is not Python, so it cannot be rewritten. */
  | { reason: "reader_not_python"; name: string; cell_id: string }
  /** The rename left the name defined, as a dotted `import name.sub` does. */
  | { reason: "cannot_rename"; name: string; cell_id: string };

export type QuickFixResult = { ok: true; ops: ReplaceCellOp[] } | { ok: false; refusal: QuickFixRefusal };

export type RuntimeNames = Readonly<Record<string, CellNames | undefined>>;

const NOT_PYTHON = new Set(["sql", "markdown", "unparsable"]);

/** A kind this build does not know is treated as Python, as the editor draws it. */
function isPython(cell: DocCell): boolean {
  return !NOT_PYTHON.has(cell.kind);
}

function defines(cell: DocCell, name: string, runtime: RuntimeNames): boolean {
  const known = runtime[cell.id];
  if (known) return known.defs.includes(name);
  return isPython(cell) && definesName(cell.source, name);
}

function reads(cell: DocCell, name: string, runtime: RuntimeNames): boolean {
  const known = runtime[cell.id];
  if (known) return known.refs.includes(name);
  return isPython(cell) && mentionsName(cell.source, name) && !definesName(cell.source, name);
}

function refuse(refusal: QuickFixRefusal): QuickFixResult {
  return { ok: false, refusal };
}

/** "Make local to this cell": rename each of the error's names this cell
 *  defines to `_name`, which marimo keeps private to the cell. */
export function makeLocal(cellId: string, error: ErrorInfo, cells: readonly DocCell[], runtime: RuntimeNames = {}): QuickFixResult {
  if (error.kind !== "multiple_definitions") return refuse({ reason: "not_multiple_definitions" });
  const cell = cells.find((c) => c.id === cellId);
  if (!cell) return refuse({ reason: "cell_not_found", cell_id: cellId });
  if (!isPython(cell)) return refuse({ reason: "not_python", cell_id: cellId });
  const names = (error.names ?? []).filter((name) => !name.startsWith("_") && defines(cell, name, runtime));
  if (names.length === 0) return refuse({ reason: "not_defined_here", name: (error.names ?? [])[0] ?? "" });
  let source = cell.source;
  for (const name of names) {
    const local = `_${name}`;
    if (mentionsName(source, local)) return refuse({ reason: "name_in_use", name: local, cell_id: cellId });
    source = renameIdentifier(source, name, local).code;
    if (definesName(source, name)) return refuse({ reason: "cannot_rename", name, cell_id: cellId });
  }
  return { ok: true, ops: [{ op: "replace", cell_id: cellId, source }] };
}

export interface RenameDefinitionInput {
  cellId: string;
  name: string;
  newName: string;
  cells: readonly DocCell[];
  runtime?: RuntimeNames;
  /** When given, only cells downstream of this one are updated; without it,
   *  every later cell that reads the name is. */
  graph?: GraphView;
}

/** Why a new name cannot be used, before anything else is checked. */
export function checkNewName(name: string, newName: string): QuickFixRefusal | null {
  if (newName === name) return { reason: "same_name", name: newName };
  if (PYTHON_KEYWORDS.has(newName)) return { reason: "keyword", name: newName };
  if (!isIdentifier(newName)) return { reason: "invalid_name", name: newName };
  return null;
}

/** "Rename this definition": rename `name` to `newName` in this cell and in
 *  every later cell that reads it from this one. */
export function renameDefinition(input: RenameDefinitionInput): QuickFixResult {
  const { cellId, name, newName, cells, graph } = input;
  const runtime = input.runtime ?? {};
  const invalid = checkNewName(name, newName);
  if (invalid) return refuse(invalid);
  const at = cells.findIndex((c) => c.id === cellId);
  if (at === -1) return refuse({ reason: "cell_not_found", cell_id: cellId });
  const cell = cells[at];
  if (!isPython(cell)) return refuse({ reason: "not_python", cell_id: cellId });
  if (!defines(cell, name, runtime)) return refuse({ reason: "not_defined_here", name });
  for (const other of cells) {
    if (other.id !== cellId && defines(other, newName, runtime)) {
      return refuse({ reason: "already_defined", name: newName, cell_id: other.id });
    }
  }
  const downstream = graph ? new Set(downstreamOf(graph, cellId, cells.map((c) => c.id))) : null;
  const readers = cells
    .slice(at + 1)
    .filter((c) => (downstream ? downstream.has(c.id) : true))
    .filter((c) => reads(c, name, runtime) && !defines(c, name, runtime));
  const ops: ReplaceCellOp[] = [];
  for (const target of [cell, ...readers]) {
    if (!isPython(target)) return refuse({ reason: "reader_not_python", name, cell_id: target.id });
    if (mentionsName(target.source, newName)) return refuse({ reason: "name_in_use", name: newName, cell_id: target.id });
    const renamed = renameIdentifier(target.source, name, newName);
    if (target.id === cellId && definesName(renamed.code, name)) {
      return refuse({ reason: "cannot_rename", name, cell_id: target.id });
    }
    if (renamed.count > 0) ops.push({ op: "replace", cell_id: target.id, source: renamed.code });
  }
  return { ok: true, ops };
}
