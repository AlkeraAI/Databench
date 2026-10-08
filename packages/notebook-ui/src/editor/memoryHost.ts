// A notebook held in memory: the document and cell-text ports with no live
// document behind them. A standalone host (one person, a file) and the
// editor's own tests use it; the platform uses its Loro document instead.

import { Annotation, type Extension } from "@codemirror/state";
import { EditorView, ViewPlugin } from "@codemirror/view";

import type { CellConfig, DocCell, NotebookOp, NotebookSettings } from "../model/types";
import type { CellTextBinding, CellTextPort, DocumentSnapshot, NotebookDocumentPort } from "./ports";

const external = Annotation.define<boolean>();
const IDENTIFIER = /^[A-Za-z_][A-Za-z0-9_]*$/;

interface State {
  cells: DocCell[];
  deleted: DocCell[];
  settings: NotebookSettings;
}

export class MemoryNotebook implements NotebookDocumentPort, CellTextPort {
  private state: State;
  private readonly undoStack: { state: State; touched: string[] }[] = [];
  private readonly redoStack: { state: State; touched: string[] }[] = [];
  private readonly listeners = new Set<() => void>();
  private snap: DocumentSnapshot | null = null;
  private version = 0;
  private counter = 0;

  constructor(
    cells: readonly Partial<DocCell>[] = [],
    readonly canWrite = true,
    private readonly newId: () => string = () => `m${String(this.counter++).padStart(9, "0")}`,
  ) {
    this.state = {
      cells: cells.map((c) => this.make(c)),
      deleted: [],
      settings: { reactivity: "autorun" },
    };
  }

  private make(c: Partial<DocCell>): DocCell {
    return { id: c.id ?? this.newId(), kind: c.kind ?? "python", name: c.name ?? "_", source: c.source ?? "", config: c.config ?? {}, meta: c.meta ?? {} };
  }

  snapshot(): DocumentSnapshot {
    this.snap ??= { ...this.state, version: this.version };
    return this.snap;
  }

  subscribe(listener: () => void): () => void {
    this.listeners.add(listener);
    return () => void this.listeners.delete(listener);
  }

  private commit(next: State, touched: string[], record = true): void {
    if (record) {
      this.undoStack.push({ state: this.state, touched });
      this.redoStack.length = 0;
    }
    this.state = next;
    this.version += 1;
    this.snap = null;
    this.listeners.forEach((l) => l());
  }

  apply(ops: readonly NotebookOp[]): { created: string[] } {
    if (!this.canWrite) throw new Error("read_only");
    let cells = [...this.state.cells];
    let deleted = [...this.state.deleted];
    let settings = { ...this.state.settings };
    const created: string[] = [];
    const touched: string[] = [];
    const index = (id: string): number => {
      const at = cells.findIndex((c) => c.id === id);
      if (at < 0) throw new Error(`cell_not_found: ${id}`);
      return at;
    };
    const place = (cell: DocCell, after?: string | null, before?: string | null): void => {
      let at = cells.length;
      if (after != null) at = index(after) + 1;
      else if (before != null) at = index(before);
      cells.splice(at, 0, cell);
    };
    const update = (id: string, patch: Partial<DocCell>): void => {
      const at = index(id);
      cells[at] = { ...cells[at]!, ...patch };
    };
    for (const op of ops) {
      switch (op.op) {
        case "insert": {
          if (op.name && op.name !== "_" && !IDENTIFIER.test(op.name)) throw new Error("invalid_name");
          const cell = this.make({ kind: op.kind, source: op.source, name: op.name, config: op.config, meta: op.meta });
          place(cell, op.after, op.before);
          created.push(cell.id);
          touched.push(cell.id);
          break;
        }
        case "replace":
          update(op.cell_id, { source: op.source });
          touched.push(op.cell_id);
          break;
        case "edit": {
          let source = cells[index(op.cell_id)]!.source;
          for (const e of op.edits) {
            if (!source.includes(e.old)) throw new Error("edit_not_found");
            source = source.replace(e.old, e.new);
          }
          update(op.cell_id, { source });
          touched.push(op.cell_id);
          break;
        }
        case "delete": {
          const [cell] = cells.splice(index(op.cell_id), 1);
          deleted = [...deleted, cell!];
          touched.push(op.cell_id);
          break;
        }
        case "restore": {
          const cell = deleted.find((c) => c.id === op.cell_id);
          if (!cell) throw new Error("cell_not_found");
          deleted = deleted.filter((c) => c.id !== op.cell_id);
          place(cell, op.after);
          touched.push(op.cell_id);
          break;
        }
        case "move": {
          const [cell] = cells.splice(index(op.cell_id), 1);
          place(cell!, op.after, op.before);
          touched.push(op.cell_id);
          break;
        }
        case "rename":
          if (op.name !== "_" && !IDENTIFIER.test(op.name)) throw new Error("invalid_name");
          update(op.cell_id, { name: op.name });
          touched.push(op.cell_id);
          break;
        case "set_kind":
          update(op.cell_id, { kind: op.kind });
          touched.push(op.cell_id);
          break;
        case "set_config": {
          const config: CellConfig = { ...cells[index(op.cell_id)]!.config };
          for (const [k, v] of Object.entries(op.config)) {
            if (v === null || v === false || v === undefined) delete config[k];
            else config[k] = v;
          }
          update(op.cell_id, { config });
          touched.push(op.cell_id);
          break;
        }
        case "set_meta": {
          const meta: Record<string, unknown> = { ...cells[index(op.cell_id)]!.meta };
          for (const [k, v] of Object.entries(op.meta)) {
            if (v === null || v === undefined) delete meta[k];
            else meta[k] = v;
          }
          update(op.cell_id, { meta });
          touched.push(op.cell_id);
          break;
        }
        case "set_setting": {
          // `null` takes the setting out of the file.
          const stored = { ...(settings.stored ?? {}) };
          if (op.value === null) delete stored[op.key];
          else stored[op.key] = op.value;
          settings = { ...settings, [op.key]: op.value ?? undefined, stored };
          break;
        }
      }
    }
    cells = cells.slice();
    this.commit({ cells, deleted, settings }, touched);
    return { created };
  }

  undo(): string[] | null {
    const step = this.undoStack.pop();
    if (!step) return null;
    this.redoStack.push({ state: this.state, touched: step.touched });
    this.commit(step.state, step.touched, false);
    return step.touched;
  }

  redo(): string[] | null {
    const step = this.redoStack.pop();
    if (!step) return null;
    this.undoStack.push({ state: this.state, touched: step.touched });
    this.commit(step.state, step.touched, false);
    return step.touched;
  }

  /** A cell's editor kept in step with this document's text. */
  bind(cellId: string): CellTextBinding {
    const textOf = () => this.state.cells.find((c) => c.id === cellId)?.source ?? "";
    let view: EditorView | null = null;
    const stop = this.subscribe(() => {
      if (view === null) return;
      const shown = view.state.sliceDoc();
      const now = textOf();
      if (shown !== now) view.dispatch({ changes: { from: 0, to: shown.length, insert: now }, annotations: external.of(true) });
    });
    const extension: Extension = [
      EditorView.updateListener.of((update) => {
        if (!update.docChanged) return;
        if (update.transactions.every((tr) => tr.annotation(external) === true)) return;
        this.apply([{ op: "replace", cell_id: cellId, source: update.state.sliceDoc() }]);
      }),
      ViewPlugin.define((v) => {
        view = v;
        return {
          destroy: () => {
            if (view === v) view = null;
          },
        };
      }),
    ];
    return {
      text: textOf,
      extension,
      dispose: stop,
    };
  }
}
