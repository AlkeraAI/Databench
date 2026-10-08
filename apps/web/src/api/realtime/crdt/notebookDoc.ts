// A notebook as a live Loro document: the browser's reading and writing of it.
//
// The document's shape is the server's (the notebook doc type):
//
//   meta: Map        format, generated_with, header (Text), app (Map)
//   settings: Map    reactivity, dataframe, env, outputs_in_git, autoreload, unknown (Text)
//   order: MovableList<str>
//   cells: Map<id, Map>   kind, name, source (Text), config (Map), meta (Map), extra (Map), deleted
//
// No run state, outputs or carets live in it. This module reads it into plain
// cells for the editor (reusing a cell's object while nothing about it
// changed, so an untouched cell does not redraw while somebody types in
// another), and writes the structure edits a person makes (insert, delete and
// restore, move, rename, change kind, config, settings) straight into the
// document as that person's own Loro peer: one commit per action, so undo
// takes back one action, and only ever this person's. Text inside a cell is
// written by the cell's editor binding, never here.
//
// The projection is tolerant where the server normalizes: a live cell missing
// from `order` is shown at the end, a repeated id once, a deleted cell not at
// all. Normalization itself is the server's.

import { spliceFor } from "@alkera/ui";
import type { CellConfig, CellKind, DocCell, NotebookOp, NotebookSettings } from "@alkera/notebook-ui";
// The settings schema alone: the package barrel carries the CodeMirror editor,
// which the editor (webview) build must not ship.
import { NOTEBOOK_SETTINGS_SCHEMA, settingValid } from "@alkera/notebook-ui/settings";

import type { LoroApi } from "./loro";
import { OpRuleRefused, applyEdit, checkInsertable, kindChange, locateEdit, templatedCode } from "./notebookOpRules";
import { LOCAL_ORIGIN, UNDO_MERGE_MS, UNDO_STEPS } from "./textBinding";

type LoroDoc = InstanceType<LoroApi["LoroDoc"]>;
type LoroMap = InstanceType<LoroApi["LoroMap"]>;
type LoroText = InstanceType<LoroApi["LoroText"]>;
type LoroMovableList = InstanceType<LoroApi["LoroMovableList"]>;
type UndoManager = InstanceType<LoroApi["UndoManager"]>;

/** Cell ids: 10 characters of lower-case Crockford base32 (50 bits). */
export const CELL_ID_PATTERN = /^[0-9a-hjkmnp-tv-z]{10}$/;
const CROCKFORD = "0123456789abcdefghjkmnpqrstvwxyz";

/** A new cell's id from 50 random bits. A pasted cell gets a new one too. */
export function newCellId(random: (bytes: Uint8Array) => Uint8Array = defaultRandom): string {
  const bytes = random(new Uint8Array(7));
  // 56 random bits; the first 50 make ten 5-bit digits.
  let bits = 0n;
  for (const b of bytes) bits = (bits << 8n) | BigInt(b);
  bits >>= 6n;
  let out = "";
  for (let i = 0; i < 10; i += 1) {
    out = CROCKFORD[Number(bits & 31n)] + out;
    bits >>= 5n;
  }
  return out;
}

function defaultRandom(bytes: Uint8Array): Uint8Array {
  return globalThis.crypto.getRandomValues(bytes);
}

/** Where a notebook document comes from: a live channel, or (in a test, or a
 *  standalone editor) a document held directly. */
export interface NotebookDocSource {
  readonly doc: LoroDoc | null;
  readonly canWrite: boolean;
  /** The document object itself was replaced (a new epoch, a reset). */
  listen(listeners: { replaced?(doc: LoroDoc): void; writable?(canWrite: boolean): void }): () => void;
  /** A local commit is ready to send. */
  localCommitted(): void;
}

/** The notebook as the editor reads it. */
export interface NotebookSnapshot {
  cells: readonly DocCell[];
  settings: NotebookSettings;
  /** Soft-deleted cells, newest last, for "restore". */
  deleted: readonly DocCell[];
  /** Bumped on every change, for cheap comparison. */
  version: number;
}

export type NotebookOpError =
  | "cell_not_found"
  | "edit_not_found"
  | "edit_ambiguous"
  | "invalid_name"
  | "unknown_kind"
  | "invalid_config"
  | "setup_must_be_first"
  | "not_representable"
  | "read_only";

/** A batch the editor could not apply: nothing of it was written. */
export class NotebookOpRefused extends Error {
  constructor(
    readonly index: number,
    readonly code: NotebookOpError,
  ) {
    super(`op ${index}: ${code}`);
  }
}

export interface ApplyResult {
  /** The ids of inserted cells, in op order. */
  created: string[];
}

const IDENTIFIER = /^[A-Za-z_][A-Za-z0-9_]*$/;
const CONFIG_KEYS: Record<string, (value: unknown) => boolean> = {
  disabled: (v) => typeof v === "boolean",
  hide_code: (v) => typeof v === "boolean",
  expand_output: (v) => typeof v === "boolean",
  column: (v) => v === null || (typeof v === "number" && Number.isInteger(v) && v >= 0),
};

/** The settings each templated kind records, and the values each admits (the
 *  server's rule for a cell's `meta`). */
const META_KEYS: Record<string, Record<string, (value: unknown) => boolean>> = {
  sql: {
    output_var: (v) => typeof v === "string" && IDENTIFIER.test(v),
    connection: (v) => typeof v === "string" && /^[\p{L}\p{N}_ .-]{1,128}$/u.test(v),
    engine: (v) => typeof v === "string" && v.length > 0 && v.length <= 64,
    show_output: (v) => typeof v === "boolean",
  },
  markdown: { quote: (v) => v === "r" || v === "rf" },
};

function asRecord(value: unknown): Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
}

function isContainer(value: unknown, kind: "Map" | "Text"): boolean {
  if (typeof value !== "object" || value === null) return false;
  const probe = (value as { kind?: unknown }).kind;
  return typeof probe === "function" && (probe as () => string).call(value) === kind;
}

function sameJson(a: unknown, b: unknown): boolean {
  return JSON.stringify(a) === JSON.stringify(b);
}

function cellMapOf(doc: LoroDoc, id: string): LoroMap | undefined {
  const value = doc.getMap("cells").get(id) as unknown;
  return isContainer(value, "Map") ? (value as LoroMap) : undefined;
}

function isLive(doc: LoroDoc, id: string): boolean {
  const cell = cellMapOf(doc, id);
  return cell !== undefined && cell.get("deleted") !== true;
}

function sourceOf(doc: LoroDoc, id: string): string {
  const text = cellMapOf(doc, id)?.get("source") as unknown;
  return isContainer(text, "Text") ? (text as LoroText).toString() : "";
}

/** Live cell ids in the order shown: `order` first (a repeated id once), then
 *  any live cell the order lost (a concurrent delete and restore), at the
 *  end until the server's normalization places it. */
export function liveOrder(doc: LoroDoc): string[] {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const value of doc.getMovableList("order").toArray() as unknown[]) {
    if (typeof value !== "string" || seen.has(value)) continue;
    seen.add(value);
    if (isLive(doc, value)) out.push(value);
  }
  for (const id of doc.getMap("cells").keys()) {
    if (!seen.has(id) && isLive(doc, id)) out.push(id);
  }
  return out;
}

/** What a notebook copy would take from its reader if it were replaced: the
 *  source of every live cell `local` added or changed since `acked` (the
 *  part the server confirmed; `null` when none), unless `kept` (the copy
 *  replacing it) already shows that source. In the order shown, a blank line
 *  between cells. The channel offers it back ("Copy my text"). */
export function notebookUnacknowledged(acked: LoroDoc | null, local: LoroDoc, kept: LoroDoc | null): string {
  const lost: string[] = [];
  for (const id of liveOrder(local)) {
    const source = sourceOf(local, id);
    if (source.trim() === "") continue;
    if (acked !== null && isLive(acked, id) && sourceOf(acked, id) === source) continue;
    if (kept !== null && isLive(kept, id) && sourceOf(kept, id) === source) continue;
    lost.push(source);
  }
  return lost.join("\n\n");
}

export class NotebookDocument {
  private readonly loro: LoroApi;
  private readonly source: NotebookDocSource;
  private readonly newId: () => string;
  private doc: LoroDoc | null;
  private undoer: UndoManager | null = null;
  private stopDoc: (() => void) | null = null;
  private readonly stopSource: () => void;
  private readonly listeners = new Set<() => void>();
  private snapshotCache: NotebookSnapshot | null = null;
  private version = 0;
  private readonly cellCache = new Map<string, DocCell>();
  /** Set while an undo or redo runs: the cells its event touched. */
  private touched: Set<string> | null = null;
  /** The last local commit was a structure edit: the next one (typing) must
   *  start its own undo step rather than merge into it. */
  private structureLast = false;
  private applying = false;

  constructor(opts: { loro: LoroApi; source: NotebookDocSource; newId?: () => string }) {
    this.loro = opts.loro;
    this.source = opts.source;
    this.newId = opts.newId ?? (() => newCellId());
    this.doc = opts.source.doc;
    this.bind();
    this.stopSource = opts.source.listen({
      replaced: (doc) => {
        this.doc = doc;
        this.bind();
        this.changed();
      },
      writable: () => this.changed(),
    });
  }

  get canWrite(): boolean {
    return this.source.canWrite;
  }

  /** The Loro document now held (it is replaced on a new epoch). */
  get loroDoc(): LoroDoc | null {
    return this.doc;
  }

  dispose(): void {
    this.stopSource();
    this.stopDoc?.();
    this.stopDoc = null;
    this.undoer?.free?.();
    this.undoer = null;
    this.listeners.clear();
  }

  private bind(): void {
    this.stopDoc?.();
    this.undoer?.free?.();
    this.cellCache.clear();
    const doc = this.doc;
    if (doc === null) {
      this.stopDoc = null;
      this.undoer = null;
      return;
    }
    this.undoer = new this.loro.UndoManager(doc, { mergeInterval: UNDO_MERGE_MS, maxUndoSteps: UNDO_STEPS });
    this.stopDoc = doc.subscribe((batch) => {
      if (batch.by === "local" && !this.applying && this.structureLast) {
        // Typing after a structure edit was recorded as a step of its own;
        // keystrokes after it merge as usual.
        this.structureLast = false;
        this.undoer?.setMergeInterval(UNDO_MERGE_MS);
      }
      if (this.touched !== null) {
        for (const event of batch.events) {
          const path = event.path as unknown[];
          if (path[0] === "cells" && typeof path[1] === "string") this.touched.add(path[1]);
        }
      }
      this.changed();
    });
  }

  private changed(): void {
    this.version += 1;
    this.snapshotCache = null;
    this.listeners.forEach((l) => l());
  }

  /** Called on every change to the document (anyone's). */
  subscribe(listener: () => void): () => void {
    this.listeners.add(listener);
    return () => void this.listeners.delete(listener);
  }

  // -- reading ---------------------------------------------------------------

  private cellsMap(doc: LoroDoc): LoroMap {
    return doc.getMap("cells");
  }

  private orderList(doc: LoroDoc): LoroMovableList {
    return doc.getMovableList("order");
  }

  private cellMap(doc: LoroDoc, id: string): LoroMap | undefined {
    return cellMapOf(doc, id);
  }

  /** The source text of a cell, for its editor binding. */
  sourceText(doc: LoroDoc, id: string): LoroText | undefined {
    const cell = this.cellMap(doc, id);
    const text = cell?.get("source") as unknown;
    return isContainer(text, "Text") ? (text as LoroText) : undefined;
  }

  private readCell(doc: LoroDoc, id: string): DocCell | null {
    const map = this.cellMap(doc, id);
    if (map === undefined) return null;
    const kind = map.get("kind");
    const name = map.get("name");
    const source = this.sourceText(doc, id)?.toString() ?? "";
    const config = asRecord((map.get("config") as LoroMap | undefined)?.toJSON?.()) as CellConfig;
    const meta = asRecord((map.get("meta") as LoroMap | undefined)?.toJSON?.());
    const next: DocCell = {
      id,
      kind: typeof kind === "string" && kind !== "" ? (kind as CellKind) : "python",
      name: typeof name === "string" && name !== "" ? name : "_",
      source,
      config,
      meta,
    };
    const prev = this.cellCache.get(id);
    if (
      prev !== undefined &&
      prev.kind === next.kind &&
      prev.name === next.name &&
      prev.source === next.source &&
      sameJson(prev.config, next.config) &&
      sameJson(prev.meta, next.meta)
    ) {
      return prev;
    }
    this.cellCache.set(id, next);
    return next;
  }

  private isDeleted(doc: LoroDoc, id: string): boolean {
    return this.cellMap(doc, id)?.get("deleted") === true;
  }

  /** Live cell ids in the order shown. */
  order(): string[] {
    return this.doc === null ? [] : liveOrder(this.doc);
  }

  snapshot(): NotebookSnapshot {
    if (this.snapshotCache !== null) return this.snapshotCache;
    const doc = this.doc;
    const cells: DocCell[] = [];
    const deleted: DocCell[] = [];
    let settings: NotebookSettings = { reactivity: "autorun" };
    if (doc !== null) {
      for (const id of this.order()) {
        const cell = this.readCell(doc, id);
        if (cell !== null) cells.push(cell);
      }
      for (const id of this.cellsMap(doc).keys()) {
        if (!this.isDeleted(doc, id)) continue;
        const cell = this.readCell(doc, id);
        if (cell !== null) deleted.push(cell);
      }
      settings = this.readSettings(doc);
    }
    this.snapshotCache = { cells, settings, deleted, version: this.version };
    return this.snapshotCache;
  }

  private readSettings(doc: LoroDoc): NotebookSettings {
    const raw = asRecord(doc.getMap("settings").toJSON());
    const settings: NotebookSettings = { reactivity: raw.reactivity === "lazy" ? "lazy" : "autorun" };
    if (typeof raw.dataframe === "string") settings.dataframe = raw.dataframe;
    if (typeof raw.env === "string") settings.env = raw.env;
    if (typeof raw.outputs_in_git === "boolean") settings.outputs_in_git = raw.outputs_in_git;
    if (typeof raw.autoreload === "string") settings.autoreload = raw.autoreload;
    // Every setting the file holds, by the schema's names: the live document
    // keeps only values the server validated.
    settings.stored = Object.fromEntries(
      NOTEBOOK_SETTINGS_SCHEMA.notebook.flatMap((spec) => (raw[spec.name] === undefined || raw[spec.name] === null ? [] : [[spec.name, raw[spec.name]]])),
    );
    return settings;
  }

  // -- writing ---------------------------------------------------------------

  /** Apply a batch of operations as this person's peer, in one commit. A
   *  batch that cannot apply whole writes nothing. */
  apply(ops: readonly NotebookOp[]): ApplyResult {
    const doc = this.doc;
    if (doc === null || !this.source.canWrite) throw new NotebookOpRefused(0, "read_only");
    ops.forEach((op, index) => this.check(doc, op, index));
    const created: string[] = [];
    for (const op of ops) {
      const id = this.write(doc, op);
      if (id !== null) created.push(id);
    }
    // One action, one undo step: never merged with typing either side of it.
    this.undoer?.setMergeInterval(0);
    this.applying = true;
    try {
      doc.commit({ origin: LOCAL_ORIGIN });
    } finally {
      this.applying = false;
    }
    this.structureLast = true;
    this.source.localCommitted();
    return { created };
  }

  private check(doc: LoroDoc, op: NotebookOp, index: number): void {
    const refuse = (code: NotebookOpError): never => {
      throw new NotebookOpRefused(index, code);
    };
    const live = (id: string): void => {
      if (this.cellMap(doc, id) === undefined || this.isDeleted(doc, id)) refuse("cell_not_found");
    };
    const anchor = (id: string | null | undefined): void => {
      if (id !== null && id !== undefined) live(id);
    };
    // The shared op rules (`notebookOpRules`), a refusal reported as this op's.
    const rule = <T>(check: () => T): T => {
      try {
        return check();
      } catch (error) {
        if (error instanceof OpRuleRefused) refuse(error.code);
        throw error;
      }
    };
    const cell = (id: string): DocCell => this.readCell(doc, id) as DocCell;
    const config = (c: CellConfig): void => {
      for (const [key, value] of Object.entries(c)) {
        if (value === undefined) continue;
        const valid = CONFIG_KEYS[key];
        // `null` resets a key to its default.
        if (valid === undefined || (value !== null && !valid(value))) refuse("invalid_config");
      }
    };
    switch (op.op) {
      case "insert":
        rule(() => checkInsertable(op.kind ?? "python"));
        rule(() => templatedCode(op.kind ?? "python", op.source ?? "", op.meta ?? {}));
        if (op.name !== undefined && op.name !== "_" && !IDENTIFIER.test(op.name)) refuse("invalid_name");
        anchor(op.after);
        anchor(op.before);
        config(op.config ?? {});
        if ((op.kind ?? "python") === "setup") {
          const order = this.order();
          const first = this.position(order, op.after ?? null, op.before ?? null) === 0;
          const another = order.some((id) => this.cellMap(doc, id)?.get("kind") === "setup");
          if (!first || another) refuse("setup_must_be_first");
        }
        return;
      case "edit": {
        live(op.cell_id);
        const { kind, meta } = cell(op.cell_id);
        let text = this.sourceText(doc, op.cell_id)?.toString() ?? "";
        for (const edit of op.edits) text = rule(() => applyEdit(text, edit.old, edit.new, edit.occurrence ?? null)).text;
        rule(() => templatedCode(kind, text, meta));
        return;
      }
      case "replace": {
        live(op.cell_id);
        const { kind, meta } = cell(op.cell_id);
        rule(() => templatedCode(kind, op.source, meta));
        return;
      }
      case "delete":
        live(op.cell_id);
        return;
      case "restore":
        if (this.cellMap(doc, op.cell_id) === undefined) refuse("cell_not_found");
        anchor(op.after);
        return;
      case "move":
        live(op.cell_id);
        anchor(op.after);
        anchor(op.before);
        return;
      case "rename":
        live(op.cell_id);
        if (op.name !== "_" && !IDENTIFIER.test(op.name)) refuse("invalid_name");
        return;
      case "set_kind": {
        live(op.cell_id);
        rule(() => checkInsertable(op.kind));
        const { kind, source, meta } = cell(op.cell_id);
        if (kind !== op.kind) rule(() => kindChange(kind, op.kind, source, meta));
        return;
      }
      case "set_config":
        live(op.cell_id);
        config(op.config);
        return;
      case "set_meta": {
        live(op.cell_id);
        const rules = META_KEYS[String(this.cellMap(doc, op.cell_id)?.get("kind"))];
        if (rules === undefined) refuse("invalid_config");
        for (const [key, value] of Object.entries(op.meta)) {
          const valid = rules![key];
          // `null` resets a key to its default.
          if (valid === undefined || (value !== null && value !== undefined && !valid(value))) refuse("invalid_config");
        }
        const { kind, source, meta } = cell(op.cell_id);
        const after: Record<string, unknown> = { ...meta };
        for (const [key, value] of Object.entries(op.meta)) {
          if (value === null || value === undefined) delete after[key];
          else after[key] = value;
        }
        rule(() => templatedCode(kind, source, after));
        return;
      }
      case "set_setting": {
        // The header text is the document's own; every other key is checked
        // here exactly as the server will, so a value it would refuse is never
        // committed (and nothing acts on it meanwhile).
        if (op.key === "header") return;
        const spec = NOTEBOOK_SETTINGS_SCHEMA.notebook.find((s) => s.name === op.key);
        if (spec === undefined || !settingValid(spec, op.value)) refuse("invalid_config");
        return;
      }
    }
  }

  private write(doc: LoroDoc, op: NotebookOp): string | null {
    switch (op.op) {
      case "insert": {
        const id = this.mintId(doc);
        const cell = this.cellsMap(doc).setContainer(id, new this.loro.LoroMap()) as LoroMap;
        cell.set("kind", op.kind ?? "python");
        cell.set("name", op.name ?? "_");
        const text = cell.setContainer("source", new this.loro.LoroText()) as LoroText;
        if (op.source) text.insert(0, op.source);
        const config = cell.setContainer("config", new this.loro.LoroMap()) as LoroMap;
        for (const [key, value] of Object.entries(op.config ?? {})) if (value !== undefined) config.set(key, value as never);
        const meta = cell.setContainer("meta", new this.loro.LoroMap()) as LoroMap;
        for (const [key, value] of Object.entries(op.meta ?? {})) if (value !== undefined) meta.set(key, value as never);
        cell.setContainer("extra", new this.loro.LoroMap());
        cell.set("deleted", false);
        this.place(doc, id, op.after ?? null, op.before ?? null);
        return id;
      }
      case "edit": {
        const text = this.sourceText(doc, op.cell_id);
        if (text === undefined) return null;
        for (const edit of op.edits) {
          const at = locateEdit(text.toString(), edit.old, edit.occurrence ?? null);
          if (edit.old.length > 0) text.delete(at, edit.old.length);
          if (edit.new) text.insert(at, edit.new);
        }
        return null;
      }
      case "replace": {
        const text = this.sourceText(doc, op.cell_id);
        if (text === undefined) return null;
        const splice = spliceFor(text.toString(), op.source);
        if (splice !== null) {
          if (splice.remove > 0) text.delete(splice.index, splice.remove);
          if (splice.insert) text.insert(splice.index, splice.insert);
        }
        return null;
      }
      case "delete": {
        this.cellMap(doc, op.cell_id)?.set("deleted", true);
        this.unplace(doc, op.cell_id);
        return null;
      }
      case "restore": {
        this.cellMap(doc, op.cell_id)?.set("deleted", false);
        this.unplace(doc, op.cell_id);
        this.place(doc, op.cell_id, op.after ?? null, null);
        return null;
      }
      case "move": {
        const list = this.orderList(doc);
        const ids = list.toArray() as unknown[];
        const from = ids.indexOf(op.cell_id);
        if (from < 0) {
          this.place(doc, op.cell_id, op.after ?? null, op.before ?? null);
          return null;
        }
        const rest = ids.filter((_, i) => i !== from);
        const to = this.position(rest, op.after ?? null, op.before ?? null);
        if (to !== from) list.move(from, to);
        return null;
      }
      case "rename":
        this.cellMap(doc, op.cell_id)?.set("name", op.name);
        return null;
      case "set_kind": {
        const cell = this.cellMap(doc, op.cell_id);
        const was = this.readCell(doc, op.cell_id);
        if (cell === undefined || was === null || was.kind === op.kind) return null;
        const next = kindChange(was.kind, op.kind, was.source, was.meta);
        cell.set("kind", op.kind);
        // A new text: typing that was in flight lands in the old one, which
        // the editor offers back rather than splicing into a different kind.
        const text = cell.setContainer("source", new this.loro.LoroText()) as LoroText;
        if (next.source) text.insert(0, next.source);
        let meta = cell.get("meta") as LoroMap | undefined;
        if (meta === undefined || typeof meta.set !== "function") meta = cell.setContainer("meta", new this.loro.LoroMap()) as LoroMap;
        for (const key of meta.keys()) if (next.meta[key] === undefined || next.meta[key] === null) meta.delete(key);
        for (const [key, value] of Object.entries(next.meta)) if (value !== undefined && value !== null) meta.set(key, value as never);
        return null;
      }
      case "set_config": {
        const cell = this.cellMap(doc, op.cell_id);
        if (cell === undefined) return null;
        let config = cell.get("config") as LoroMap | undefined;
        if (config === undefined || typeof config.set !== "function") {
          config = cell.setContainer("config", new this.loro.LoroMap()) as LoroMap;
        }
        for (const [key, value] of Object.entries(op.config)) {
          // A default value is not stored: the file writes only non-defaults.
          if (value === undefined || value === null || value === false) config.delete(key);
          else config.set(key, value as never);
        }
        return null;
      }
      case "set_meta": {
        const cell = this.cellMap(doc, op.cell_id);
        if (cell === undefined) return null;
        let meta = cell.get("meta") as LoroMap | undefined;
        if (meta === undefined || typeof meta.set !== "function") {
          meta = cell.setContainer("meta", new this.loro.LoroMap()) as LoroMap;
        }
        for (const [key, value] of Object.entries(op.meta)) {
          if (value === undefined || value === null) meta.delete(key);
          else meta.set(key, value as never);
        }
        return null;
      }
      case "set_setting":
        doc.getMap("settings").set(op.key, op.value as never);
        return null;
    }
  }

  private mintId(doc: LoroDoc): string {
    const cells = this.cellsMap(doc);
    for (;;) {
      const id = this.newId();
      if (cells.get(id) === undefined) return id;
    }
  }

  private position(ids: readonly unknown[], after: string | null, before: string | null): number {
    if (after !== null) {
      const at = ids.indexOf(after);
      if (at >= 0) return at + 1;
    }
    if (before !== null) {
      const at = ids.indexOf(before);
      if (at >= 0) return at;
    }
    return ids.length;
  }

  private place(doc: LoroDoc, id: string, after: string | null, before: string | null): void {
    const list = this.orderList(doc);
    const ids = list.toArray() as unknown[];
    if (ids.includes(id)) return;
    list.insert(this.position(ids, after, before), id);
  }

  private unplace(doc: LoroDoc, id: string): void {
    const list = this.orderList(doc);
    const ids = list.toArray() as unknown[];
    for (let i = ids.length - 1; i >= 0; i -= 1) if (ids[i] === id) list.delete(i, 1);
  }

  // -- history ---------------------------------------------------------------

  /** Take back this person's last change, anywhere in the notebook; the cells
   *  it touched, so the editor can show where it happened. */
  undo(): string[] | null {
    return this.history(true);
  }

  redo(): string[] | null {
    return this.history(false);
  }

  canUndo(): boolean {
    return this.undoer?.canUndo() ?? false;
  }

  canRedo(): boolean {
    return this.undoer?.canRedo() ?? false;
  }

  private history(undo: boolean): string[] | null {
    const undoer = this.undoer;
    if (undoer === null || !this.source.canWrite) return null;
    const touched = new Set<string>();
    this.touched = touched;
    let moved = false;
    try {
      moved = undo ? undoer.undo() : undoer.redo();
    } finally {
      this.touched = null;
    }
    if (!moved) return null;
    this.source.localCommitted();
    return [...touched];
  }

  /** The history as a binding takes it: undo and redo of the whole notebook. */
  get sharedHistory(): { undo(): boolean; redo(): boolean } {
    return {
      undo: () => this.undo() !== null,
      redo: () => this.redo() !== null,
    };
  }
}
