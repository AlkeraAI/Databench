// The notebook editor: cells with their editors and outputs, the toolbar, the
// panels, find and replace, the dialogs, and the keyboard.
//
// It owns only view state (which cell is active, command or edit mode, which
// panel is open, what is collapsed or highlighted). The document, the cells'
// text, the engine's state and every action come from the host through the
// ports in `ports.ts`, so this one component serves the portal, a standalone
// host and a test alike.
//
// Below 768 px it is a single column with no command mode: the focused cell
// carries its toolbar and run button, and panels open as bottom sheets. A reader (Can view, Can comment) sees everything live and none of
// the controls that edit or run.

import type { EditorView } from "@codemirror/view";
import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore, type KeyboardEvent, type ReactNode } from "react";

import { ConfirmRunDialog } from "../components/dialogs/ConfirmRunDialog";
import { Dialog } from "../components/dialogs/Dialog";
import { EnvironmentSwitchDialog } from "../components/dialogs/EnvironmentSwitchDialog";
import { RestartDialog } from "../components/dialogs/RestartDialog";
import { EnvironmentPanel } from "../components/panels/EnvironmentPanel";
import { SettingsForm } from "../components/panels/SettingsForm";
import { NOTEBOOK_SETTINGS_SCHEMA } from "../generated/notebookSettings";
import { FindReplacePanel } from "../components/panels/FindReplacePanel";
import { GraphPanel } from "../components/panels/GraphPanel";
import { OutlinePanel } from "../components/panels/OutlinePanel";
import { VariablesPanel } from "../components/panels/VariablesPanel";
import type { FindMatch } from "../model/find";
import { cellLinks, deriveGraph, upstreamOf, type CellNames } from "../model/graph";
import { checkNewName, makeLocal, renameDefinition, type QuickFixRefusal } from "../model/quickfix";
import { envLabel, envName } from "../model/env";
import { EMPTY_RUNTIME, type CellKind, type CellOutput, type CellRuntime, type DocCell, type EnvInfo, type ErrorInfo, type NotebookOp, type RunTarget } from "../model/types";
import { downloadImage } from "../outputs/image";
import { OutputArea, OutputView } from "../outputs/OutputView";
import { CellEditor } from "./CellEditor";
import { CellView, type CellLink } from "./CellView";
import { COMMANDS, KeyResolver, commandDef, describeKeys, platformOf, type CommandId, type EditorMode, type KeyOverrides, type Platform } from "./commands";
import type { MenuItem } from "./Menu";
import { kernelChipTarget, NotebookToolbar, PANEL_LABEL, type PanelId } from "./NotebookToolbar";
import type {
  CellTextBinding,
  CellTextPort,
  NotebookActions,
  NotebookDocumentPort,
  NotebookOutputSettings,
  NotebookPermissions,
  RuntimeSnapshot,
  SqlConnectionSlot,
} from "./ports";
import "../theme/notebook.css";
import "./editor.css";


/** How long an info notice stays before it leaves on its own. */
export const INFO_NOTICE_MS = 4_000;
export interface NotebookEditorProps {
  doc: NotebookDocumentPort;
  text: CellTextPort;
  runtime: RuntimeSnapshot;
  actions: NotebookActions;
  permissions: NotebookPermissions;
  output: NotebookOutputSettings;
  /** Below 768 px: one column, no command mode. */
  narrow?: boolean;
  keyOverrides?: KeyOverrides;
  platform?: Platform;
  /** The cell to show first (a cell link). */
  initialCellId?: string | null;
  /** Bring this cell into view, select it and mark it for a moment (a link
   *  from elsewhere, such as a chat's "Go to cell"). A new `seq` asks again for
   *  the same cell. A cell this notebook does not hold is ignored. */
  revealCell?: { id: string; seq: number } | null;
  /** Wall clock for "2 min ago"; injectable for tests. */
  now?: () => number;
  /** Extra CodeMirror extensions per cell (the host's caret drawing). */
  cellExtensions?: (cellId: string) => import("@codemirror/state").Extension;
  /** The host's control for a SQL cell's connection, drawn in the cell's
   *  header. Without one the header shows the connection's name. */
  sqlConnection?(slot: SqlConnectionSlot): ReactNode;
  /** Something the person did changed the document. */
  onEdit?(): void;
  /** A batch the document refused: the host says so. */
  onRefused?(error: unknown): void;
}

const NARROW_HIDDEN = new Set<CommandId>([
  "cell.insert_above",
  "cell.insert_below",
  "cell.delete",
  "cell.restore",
  "cell.to_python",
  "cell.to_markdown",
  "cell.to_sql",
  "nav.previous",
  "nav.next",
  "mode.edit",
  "kernel.interrupt",
  "kernel.restart",
  "output.toggle_collapse",
  "cell.merge_next",
]);

type Pending =
  | { kind: "restart"; runAll: boolean }
  | { kind: "env"; envId: string }
  | { kind: "rename"; cellId: string }
  | { kind: "rename_definition"; cellId: string; name: string };

/** What the editor says when the document refuses a change: a SQL or Markdown
 *  cell that could not hold its text says so (`not_representable`); anything
 *  else is a notebook that moved on under the change. */
export function refusalProblem(error: unknown): string {
  const code = typeof error === "object" && error !== null ? (error as { code?: unknown }).code : undefined;
  if (code === "not_representable") return "A SQL or Markdown cell can't hold three double quotes in a row.";
  return "That change could not be made. The notebook changed meanwhile; try again.";
}

/** How long a revealed cell stays marked. */
const REVEAL_MS = 1_600;

export function NotebookEditor(props: NotebookEditorProps) {
  const { doc, text, runtime, actions, permissions, output } = props;
  const narrow = props.narrow === true;
  const platform = props.platform ?? platformOf(typeof navigator === "undefined" ? undefined : navigator);
  const canEdit = permissions.canEdit && doc.canWrite;
  const canRun = permissions.canRun;
  const now = (props.now ?? Date.now)();

  const snapshot = useSyncExternalStore(
    useCallback((l: () => void) => doc.subscribe(l), [doc]),
    () => doc.snapshot(),
    () => doc.snapshot(),
  );
  const cells = snapshot.cells;
  const order = useMemo(() => cells.map((c) => c.id), [cells]);

  const [activeId, setActiveId] = useState<string | null>(props.initialCellId ?? null);
  const [mode, setMode] = useState<EditorMode>("command");
  const [panel, setPanel] = useState<PanelId | null>(null);
  const [findOpen, setFindOpen] = useState(false);
  const [highlight, setHighlight] = useState<{ from: string; ids: ReadonlySet<string> } | null>(null);
  const [collapsed, setCollapsed] = useState<ReadonlySet<string>>(new Set());
  /** Outputs cleared from view, by the run they came from. */
  const [cleared, setCleared] = useState<ReadonlyMap<string, string | null>>(new Map());
  const [pending, setPending] = useState<Pending | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [editingMarkdown, setEditingMarkdown] = useState<ReadonlySet<string>>(new Set());

  const root = useRef<HTMLDivElement>(null);
  const views = useRef(new Map<string, EditorView>());
  const bindings = useRef(new Map<string, CellTextBinding>());
  const resolver = useMemo(() => new KeyResolver(props.keyOverrides), [props.keyOverrides]);

  // A cell's binding lives as long as the cell does.
  const bindingFor = (cellId: string): CellTextBinding => {
    let binding = bindings.current.get(cellId);
    if (binding === undefined) {
      binding = text.bind(cellId);
      bindings.current.set(cellId, binding);
    }
    return binding;
  };
  useEffect(() => {
    const live = new Set(order);
    for (const [id, binding] of bindings.current) {
      if (!live.has(id)) {
        binding.dispose();
        bindings.current.delete(id);
      }
    }
  }, [order]);
  useEffect(
    () => () => {
      for (const binding of bindings.current.values()) binding.dispose();
      bindings.current.clear();
    },
    [],
  );

  // The active cell is always a live one.
  useEffect(() => {
    if (activeId !== null && order.includes(activeId)) return;
    setActiveId(order[0] ?? null);
  }, [order, activeId]);

  const names = useMemo(() => {
    const out: Record<string, CellNames> = {};
    for (const [id, cell] of Object.entries(runtime.cells)) if (cell) out[id] = { defs: cell.defs, refs: cell.refs };
    return out;
  }, [runtime.cells]);
  const graph = useMemo(
    () => (runtime.graph.edges.length > 0 || Object.keys(runtime.graph.errors).length > 0 ? runtime.graph : deriveGraph(cells, names)),
    [runtime.graph, cells, names],
  );

  const runtimeOf = (id: string): CellRuntime => runtime.cells[id] ?? EMPTY_RUNTIME;
  const cellById = (id: string | null): DocCell | undefined => (id === null ? undefined : cells.find((c) => c.id === id));
  const nameOf = (id: string): string => {
    const cell = cellById(id);
    const index = order.indexOf(id);
    return cell && cell.name !== "_" ? cell.name : `Cell ${index + 1}`;
  };

  // -- writing -----------------------------------------------------------------

  const apply = (ops: NotebookOp[]): string[] | null => {
    if (!canEdit) return null;
    try {
      const { created } = doc.apply(ops);
      props.onEdit?.();
      return created;
    } catch (error) {
      props.onRefused?.(error);
      setProblem(refusalProblem(error));
      return null;
    }
  };

  /** Change a cell's settings through the document, as every edit is. */
  const setMeta = (cellId: string, meta: Record<string, unknown>) => {
    apply([{ op: "set_meta", cell_id: cellId, meta }]);
  };

  const focusCell = (id: string | null, edit: boolean) => {
    if (id === null) return;
    setActiveId(id);
    setMode(edit ? "edit" : "command");
    // Wait for the cell to exist (a cell just inserted renders next).
    queueMicrotask(() =>
      requestAnimationFrameSafe(() => {
        const view = views.current.get(id);
        if (edit && view) view.focus();
        else root.current?.querySelector<HTMLElement>(`[data-cell-id="${id}"]`)?.scrollIntoView({ block: "nearest" });
        if (!edit) root.current?.focus({ preventScroll: true });
      }),
    );
  };

  // A reveal waits for its cell: a notebook opened by the link may not hold
  // its cells yet on the first render.
  const revealId = props.revealCell?.id ?? null;
  const revealSeq = props.revealCell?.seq ?? 0;
  const revealHolds = revealId !== null && order.includes(revealId);
  useEffect(() => {
    if (revealId === null || !revealHolds) return;
    setActiveId(revealId);
    setMode("command");
    let element: HTMLElement | null = null;
    const timer = window.setTimeout(() => element?.removeAttribute("data-revealed"), REVEAL_MS);
    requestAnimationFrameSafe(() => {
      element = root.current?.querySelector<HTMLElement>(`[data-cell-id="${revealId}"]`) ?? null;
      element?.scrollIntoView({ block: "center" });
      element?.setAttribute("data-revealed", "");
      root.current?.focus({ preventScroll: true });
    });
    return () => {
      window.clearTimeout(timer);
      element?.removeAttribute("data-revealed");
    };
  }, [revealId, revealSeq, revealHolds]);

  const insert = (kind: CellKind, where: "above" | "below", at: string | null, edit = true) => {
    const op: NotebookOp =
      at === null
        ? { op: "insert", kind }
        : where === "above"
          ? { op: "insert", kind, before: at }
          : { op: "insert", kind, after: at };
    const created = apply([op]);
    const id = created?.[0];
    if (id !== undefined) {
      if (kind === "markdown") setEditingMarkdown((s) => new Set(s).add(id));
      focusCell(id, edit);
    }
    return id ?? null;
  };

  const run = (target: RunTarget) => {
    if (!canRun) return;
    actions.run(target);
  };

  // -- commands ------------------------------------------------------------------

  const runCommand = (id: CommandId, cellId: string | null = activeId, view?: EditorView) => {
    const def = commandDef(id);
    if ((def.run && !canRun) || (def.edit && !canEdit)) return;
    const cell = cellById(cellId);
    const at = cellId === null ? -1 : order.indexOf(cellId);
    const next = at >= 0 ? (order[at + 1] ?? null) : null;
    const previous = at > 0 ? (order[at - 1] ?? null) : null;
    switch (id) {
      case "run.cell":
        if (cell) {
          if (cell.kind === "markdown") stopEditingMarkdown(cell.id);
          run({ kind: "cells", ids: [cell.id] });
        }
        return;
      case "run.advance":
        if (!cell) return;
        if (cell.kind === "markdown") stopEditingMarkdown(cell.id);
        run({ kind: "cells", ids: [cell.id] });
        if (next !== null) focusCell(next, false);
        else if (canEdit) insert("python", "below", cell.id);
        return;
      case "run.insert_below":
        if (!cell) return;
        if (cell.kind === "markdown") stopEditingMarkdown(cell.id);
        run({ kind: "cells", ids: [cell.id] });
        insert("python", "below", cell.id);
        return;
      case "run.all":
        return run({ kind: "all" });
      case "run.stale":
        return run({ kind: "stale" });
      case "run.above":
        if (cell) run({ kind: "above", id: cell.id });
        return;
      case "run.below":
        if (cell) run({ kind: "below", id: cell.id });
        return;
      case "kernel.interrupt":
        return actions.kernel("interrupt");
      case "kernel.interrupt_clear":
        return actions.kernel("interrupt_all");
      case "kernel.restart":
        return setPending({ kind: "restart", runAll: false });
      case "kernel.restart_run_all":
        return setPending({ kind: "restart", runAll: true });
      case "kernel.shutdown":
        return actions.kernel("shutdown");
      case "cell.insert_above":
        insert("python", "above", cellId);
        return;
      case "cell.insert_below":
        insert("python", "below", cellId);
        return;
      case "cell.insert_markdown_below":
        insert("markdown", "below", cellId);
        return;
      case "cell.insert_sql_below":
        insert("sql", "below", cellId);
        return;
      case "cell.insert_markdown_above":
        insert("markdown", "above", cellId);
        return;
      case "cell.insert_sql_above":
        insert("sql", "above", cellId);
        return;
      case "output.download_image": {
        const image = cell ? firstImage(runtimeOf(cell.id).outputs) : null;
        if (image) downloadImage(image.mime, image.data, cell && cell.name !== "_" ? cell.name : "output");
        return;
      }
      case "cell.delete":
        if (!cell) return;
        if (apply([{ op: "delete", cell_id: cell.id }]) !== null) focusCell(next ?? previous, false);
        return;
      case "cell.restore": {
        const last = snapshot.deleted[snapshot.deleted.length - 1];
        if (!last) return;
        if (apply([{ op: "restore", cell_id: last.id, after: cellId }]) !== null) focusCell(last.id, false);
        return;
      }
      case "cell.duplicate":
        if (!cell) return;
        {
          const created = apply([{ op: "insert", kind: cell.kind, source: cell.source, config: cell.config, meta: cell.meta, after: cell.id }]);
          if (created?.[0]) focusCell(created[0], false);
        }
        return;
      case "cell.split": {
        const editor = view ?? (cellId ? views.current.get(cellId) : undefined);
        if (!cell || !editor) return;
        const pos = editor.state.selection.main.head;
        const source = editor.state.sliceDoc();
        const head = source.slice(0, pos).replace(/\n$/, "");
        const tail = source.slice(pos).replace(/^\n/, "");
        const created = apply([
          { op: "replace", cell_id: cell.id, source: head },
          { op: "insert", kind: cell.kind, source: tail, after: cell.id },
        ]);
        if (created?.[0]) focusCell(created[0], true);
        return;
      }
      case "cell.merge_next": {
        const below = cellById(next);
        if (!cell || !below) return;
        if (below.kind !== cell.kind) {
          setProblem("Only cells of the same kind can be merged.");
          return;
        }
        apply([
          { op: "replace", cell_id: cell.id, source: [cell.source, below.source].filter(Boolean).join("\n") },
          { op: "delete", cell_id: below.id },
        ]);
        return;
      }
      case "cell.move_up":
        if (cell && previous !== null) apply([{ op: "move", cell_id: cell.id, before: previous }]);
        return;
      case "cell.move_down":
        if (cell && next !== null) apply([{ op: "move", cell_id: cell.id, after: next }]);
        return;
      case "cell.to_python":
      case "cell.to_markdown":
      case "cell.to_sql": {
        const kind: CellKind = id === "cell.to_python" ? "python" : id === "cell.to_markdown" ? "markdown" : "sql";
        if (cell && cell.kind !== kind) apply([{ op: "set_kind", cell_id: cell.id, kind }]);
        return;
      }
      case "cell.toggle_disabled":
        if (cell) apply([{ op: "set_config", cell_id: cell.id, config: { disabled: !cell.config.disabled } }]);
        return;
      case "cell.toggle_hide_code":
        if (cell) apply([{ op: "set_config", cell_id: cell.id, config: { hide_code: !cell.config.hide_code } }]);
        return;
      case "cell.toggle_show_result":
        if (cell?.kind === "sql") setMeta(cell.id, { show_output: cell.meta.show_output === false });
        return;
      case "cell.toggle_expand_output":
        if (cell) apply([{ op: "set_config", cell_id: cell.id, config: { expand_output: !cell.config.expand_output } }]);
        return;
      case "cell.copy_link":
        if (cell) actions.copyCellLink?.(cell.id);
        return;
      case "cell.rename":
        if (cell) setPending({ kind: "rename", cellId: cell.id });
        return;
      case "output.clear":
        if (!cell) return;
        if (actions.clearOutputs) actions.clearOutputs([cell.id]);
        else setCleared((m) => new Map(m).set(cell.id, runtimeOf(cell.id).last_run?.run_id ?? null));
        return;
      case "output.clear_all":
        if (actions.clearOutputs) actions.clearOutputs(null);
        else setCleared(new Map(order.map((cid) => [cid, runtimeOf(cid).last_run?.run_id ?? null])));
        return;
      case "output.toggle_collapse":
        if (cell)
          setCollapsed((s) => {
            const copy = new Set(s);
            if (copy.has(cell.id)) copy.delete(cell.id);
            else copy.add(cell.id);
            return copy;
          });
        return;
      case "output.open_tab": {
        const first = cell ? runtimeOf(cell.id).outputs.find((o) => o.type === "display") : undefined;
        if (cell && first) actions.openOutput?.(cell.id, first.output_id);
        return;
      }
      case "nav.previous":
        if (previous !== null) focusCell(previous, false);
        return;
      case "nav.next":
        if (next !== null) focusCell(next, false);
        return;
      case "mode.edit":
        if (cell) {
          if (cell.kind === "markdown") setEditingMarkdown((s) => new Set(s).add(cell.id));
          focusCell(cell.id, true);
        }
        return;
      case "mode.command":
        if (cell?.kind === "markdown") stopEditingMarkdown(cell.id);
        setMode("command");
        root.current?.focus({ preventScroll: true });
        return;
      case "doc.undo":
      case "doc.redo": {
        const touched = id === "doc.undo" ? doc.undo() : doc.redo();
        const where = touched?.find((t) => order.includes(t));
        if (where) focusCell(where, false);
        return;
      }
      case "find.open":
      case "find.replace":
        setFindOpen(true);
        return;
      case "edit.toggle_comment":
        return;
    }
  };

  function stopEditingMarkdown(id: string) {
    setEditingMarkdown((s) => {
      if (!s.has(id)) return s;
      const copy = new Set(s);
      copy.delete(id);
      return copy;
    });
  }

  // -- keyboard ------------------------------------------------------------------

  const visibleCommands = narrow ? COMMANDS.filter((c) => !NARROW_HIDDEN.has(c.id)) : COMMANDS;
  const editKeys = useMemo(() => {
    const out: { spec: string; id: CommandId }[] = [];
    for (const def of visibleCommands) {
      if (!def.modes.includes("edit")) continue;
      for (const spec of resolver.keysFor(def.id)) if (!spec.includes(" ")) out.push({ spec, id: def.id });
    }
    return out;
  }, [resolver, visibleCommands]);

  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (mode !== "command") return;
    const target = event.target as HTMLElement;
    // Keys typed into a control (a panel's input, a dialog) are the control's.
    if (target !== root.current && target.closest("input, textarea, select, [contenteditable='true'], [role='dialog'], .nb-dock, .nb-panel, .nb-menu")) return;
    const result = resolver.resolve(event.nativeEvent, "command", platform, Date.now());
    if (result.kind === "none") return;
    event.preventDefault();
    if (result.kind === "pending") return;
    if (narrow && NARROW_HIDDEN.has(result.id)) return;
    runCommand(result.id);
  };

  const keyLabel = (id: CommandId): string | undefined => {
    const spec = resolver.keysFor(id)[0];
    return spec ? describeKeys(spec, platform) : undefined;
  };

  // -- the cell menu ---------------------------------------------------------------

  const cellMenu = (cell: DocCell): MenuItem[] => {
    const item = (id: CommandId, extra: Partial<MenuItem> = {}): MenuItem | null => {
      const def = commandDef(id);
      if ((def.run && !canRun) || (def.edit && !canEdit)) return null;
      return { id, label: def.label, keys: keyLabel(id), ...extra };
    };
    const items: (MenuItem | null)[] = [
      item("run.above"),
      item("run.below"),
      item("cell.insert_above", { separated: true }),
      item("cell.insert_below"),
      item("cell.insert_markdown_below"),
      item("cell.insert_sql_below"),
      item("cell.insert_markdown_above"),
      item("cell.insert_sql_above"),
      // Delete sits with the cell's own edits, not at the foot of a long list
      // where it scrolls out of sight.
      item("cell.duplicate"),
      item("cell.delete"),
      item("cell.split"),
      item("cell.merge_next"),
      item("cell.move_up"),
      item("cell.move_down"),
      item("cell.to_python", { separated: true, checked: cell.kind === "python" }),
      item("cell.to_markdown", { checked: cell.kind === "markdown" }),
      item("cell.to_sql", { checked: cell.kind === "sql" }),
      item("cell.rename"),
      item("cell.toggle_disabled", { checked: cell.config.disabled === true }),
      item("cell.toggle_hide_code", { checked: cell.config.hide_code === true }),
      ...(cell.kind === "sql" ? [item("cell.toggle_show_result", { checked: cell.meta.show_output !== false })] : []),
      item("cell.toggle_expand_output", { checked: cell.config.expand_output === true }),
      item("output.clear", { separated: true }),
      item("output.toggle_collapse", { checked: collapsed.has(cell.id) }),
      item("output.open_tab"),
      ...(firstImage(runtimeOf(cell.id).outputs) ? [item("output.download_image")] : []),
      item("cell.copy_link"),
    ];
    return items.filter((i): i is MenuItem => i !== null);
  };

  // An info notice ("Kernel restarted.") reports something that already went
  // fine, so it leaves on its own like a toast. A warning or a failure stays
  // until it is dismissed: it asks the reader to look.
  const noticeId = runtime.notice?.id ?? null;
  const noticeFades = runtime.notice?.tone === "info" && !runtime.notice.restorable;
  useEffect(() => {
    if (noticeId === null || !noticeFades || !actions.dismissNotice) return;
    const timer = setTimeout(() => actions.dismissNotice?.(noticeId), INFO_NOTICE_MS);
    return () => clearTimeout(timer);
  }, [noticeId, noticeFades, actions]);

  // -- quick fixes -------------------------------------------------------------------

  const errorOf = (cellId: string): ErrorInfo | null => {
    for (const out of runtimeOf(cellId).outputs) if (out.type === "error" && out.error.kind === "multiple_definitions") return out.error;
    return multipleDefinitions(cellId, runtime.cells, graph);
  };

  const quickFixes = (cell: DocCell): ReactNode => {
    const error = errorOf(cell.id);
    if (error === null || !canEdit) return null;
    const name = (error.names ?? [])[0] ?? "";
    return (
      <div className="nb-quickfix" role="group" aria-label="Fixes">
        <button
          type="button"
          className="nb-button"
          onClick={() => {
            const fix = makeLocal(cell.id, error, cells, names);
            if (fix.ok) apply(fix.ops);
            else setProblem(refusalText(fix.refusal));
          }}
        >
          Make local to this cell
        </button>
        <button type="button" className="nb-button" onClick={() => setPending({ kind: "rename_definition", cellId: cell.id, name })}>
          Rename this definition
        </button>
      </div>
    );
  };

  // -- outputs -----------------------------------------------------------------------

  const outputsFor = (cell: DocCell, runtimeCell: CellRuntime): ReactNode => {
    const clearedAt = cleared.get(cell.id);
    if (clearedAt !== undefined && clearedAt === (runtimeCell.last_run?.run_id ?? null)) return null;
    return (
      <OutputArea
        outputs={runtimeCell.outputs}
        context={{
          theme: output.theme,
          readonly: !canRun,
          cellId: cell.id,
          ...(actions.inspectFrame ? { inspectFrame: actions.inspectFrame } : {}),
          ...(output.frame ? { frame: output.frame } : {}),
          ...(output.blobUrl ? { blobUrl: output.blobUrl } : {}),
        }}
      />
    );
  };

  /** A Markdown cell's rendering: the kernel's when it has one for the text
   *  as it stands (it carries the values the text interpolates), its own
   *  source otherwise. */
  const renderedMarkdown = (cell: DocCell, fromKernel: CellOutput | undefined): ReactNode =>
    fromKernel !== undefined ? (
      <OutputView output={fromKernel} context={{ theme: output.theme, readonly: true, cellId: cell.id }} />
    ) : cell.source.trim() === "" ? null : (
      <OutputView
        output={{ output_id: `${cell.id}/md`, type: "display", data: { "text/markdown": cell.source } }}
        context={{ theme: output.theme, readonly: true, cellId: cell.id }}
      />
    );

  // -- panels --------------------------------------------------------------------------

  const variables = useMemo(
    () =>
      Object.entries(runtime.cells).flatMap(([id, cell]) => (cell?.variables ?? []).map((v) => ({ ...v, cell_id: v.cell_id ?? id }))),
    [runtime.cells],
  );

  const panelBody = (which: PanelId): ReactNode => {
    switch (which) {
      case "graph":
        return <GraphPanel cells={cells} graph={graph} names={names} selectedId={activeId} onJump={(id) => focusCell(id, false)} />;
      case "variables":
        return <VariablesPanel variables={variables} onJump={(id) => focusCell(id, false)} />;
      case "outline":
        return <OutlinePanel cells={cells} activeId={activeId} onJump={(id) => focusCell(id, false)} />;
      case "settings":
        return (
          <section className="nb-panel" aria-label="Notebook settings">
            <SettingsForm
              specs={NOTEBOOK_SETTINGS_SCHEMA.notebook}
              stored={snapshot.settings.stored ?? {}}
              effective={runtime.settings?.values}
              sources={runtime.settings?.sources}
              options={{ env: runtime.envs.filter((e) => e.recorded).map((e) => ({ value: e.recorded ?? "", label: envName(e) })) }}
              canEdit={canEdit}
              onChange={(name, value) => {
                if (name === "env" && typeof value === "string") actions.switchEnv(value);
                else apply([{ op: "set_setting", key: name, value }]);
              }}
            />
          </section>
        );
      case "environment":
        return (
          <EnvironmentPanel
            env={runtime.kernel.env}
            kernelState={runtime.kernel.state}
            packages={runtime.packages}
            requirements={runtime.requirements ?? []}
            canRun={canRun}
            installing={runtime.installing}
            install={runtime.install ?? null}
            shared={runtime.envsShared ?? true}
            onInstall={actions.install}
            onChange={actions.changeEnv}
          />
        );
    }
  };

  const revealMatch = (match: FindMatch) => {
    setActiveId(match.cell_id);
    const view = views.current.get(match.cell_id);
    if (match.where === "source" && view) {
      view.dispatch({ selection: { anchor: match.from, head: match.to }, scrollIntoView: true });
    } else {
      root.current?.querySelector<HTMLElement>(`[data-cell-id="${match.cell_id}"]`)?.scrollIntoView({ block: "center" });
    }
  };

  const hoverLinks = useCallback(
    (from: string, ids: readonly string[] | null) => setHighlight(ids === null ? null : { from, ids: new Set(ids) }),
    [],
  );

  const presenceIn = (id: string) => runtime.presence.filter((p) => p.cell_id === id);
  const runProblem = runtime.runProblem ?? null;
  const problemFor = (id: string): string | null =>
    runProblem !== null && (runProblem.cells === "all" || runProblem.cells.includes(id)) ? runProblem.message : null;
  // The notebook names one environment and the running kernel uses another
  // (a spec was added or switched while it ran): the kernel moves on restart.
  const selectedEnv = runtime.selectedEnv ?? null;
  const envAhead =
    selectedEnv !== null && runtime.kernel.env !== null && KERNEL_UP.has(runtime.kernel.state) && selectedEnv.env_id !== runtime.kernel.env.env_id
      ? selectedEnv
      : null;

  return (
    <div
      ref={root}
      className={`nb-root nb-notebook${narrow ? " nb-notebook--narrow" : ""}`}
      tabIndex={-1}
      onKeyDown={onKeyDown}
      data-mode={mode}
      data-nb-theme={output.theme}
      data-testid="notebook-editor"
    >
      <NotebookToolbar
        kernel={runtime.kernel}
        waking={runtime.waking === true}
        envs={runtime.envs}
        presence={runtime.presence}
        canRun={canRun}
        panel={panel}
        narrow={narrow}
        findOpen={findOpen}
        chipTarget={kernelChipTarget(order, runtimeOf)}
        keyFor={keyLabel}
        onCommand={(id) => runCommand(id)}
        onFindToggle={() => {
          if (!findOpen) {
            setFindOpen(true);
            return;
          }
          setFindOpen(false);
          root.current?.focus({ preventScroll: true });
        }}
        onShowCell={(id) => focusCell(id, false)}
        onEnv={(envId) => {
          if (envId !== runtime.kernel.env?.env_id) setPending({ kind: "env", envId });
        }}
        onPanel={setPanel}
      />
      {envAhead !== null && runtime.kernel.env !== null ? (
        <div className="nb-notice nb-notice--info" role="status" data-testid="env-pending">
          <span>
            The kernel uses {envName(runtime.kernel.env)} until a restart moves it to {envName(envAhead)}.
          </span>
          {canRun ? (
            <button type="button" className="nb-button" onClick={() => runCommand("kernel.restart")}>
              Restart
            </button>
          ) : null}
        </div>
      ) : null}
      {envAhead === null && runtime.kernel.env_outdated && KERNEL_UP.has(runtime.kernel.state) ? (
        <div className="nb-notice nb-notice--info" role="status" data-testid="env-newer">
          <span>A newer environment is ready.</span>
          {canRun ? (
            <button type="button" className="nb-button" onClick={() => runCommand("kernel.restart")}>
              Restart
            </button>
          ) : null}
        </div>
      ) : null}
      {runtime.notice ? (
        <div className={`nb-notice nb-notice--${runtime.notice.tone}`} role={runtime.notice.restorable ? "alert" : "status"}>
          <span>{runtime.notice.message}</span>
          {runtime.notice.restorable ? (
            <button
              type="button"
              className="nb-button"
              onClick={() => void navigator.clipboard?.writeText(runtime.notice?.restorable ?? "")}
            >
              Copy my text
            </button>
          ) : null}
          {actions.dismissNotice ? (
            <button type="button" className="nb-icon-button" aria-label="Dismiss" onClick={() => actions.dismissNotice?.(runtime.notice!.id)}>
              ×
            </button>
          ) : null}
        </div>
      ) : null}
      {runProblem ? (
        <div className={`nb-notice nb-notice--${runProblem.tone} nb-run-problem`} role="status" data-testid="run-problem">
          <span>{runProblem.message}</span>
        </div>
      ) : null}
      {problem ? (
        <div className="nb-notice nb-notice--warning" role="alert">
          <span>{problem}</span>
          <button type="button" className="nb-icon-button" aria-label="Dismiss" onClick={() => setProblem(null)}>
            ×
          </button>
        </div>
      ) : null}
      {findOpen ? (
        <div className="nb-find">
          <FindReplacePanel
            cells={cells}
            runtime={runtime.cells}
            readOnly={!canEdit}
            onApply={(ops) => apply(ops)}
            onReveal={revealMatch}
            onClose={() => {
              setFindOpen(false);
              root.current?.focus({ preventScroll: true });
            }}
          />
        </div>
      ) : null}
      <div className="nb-notebook__main">
        <div className="nb-cells" role="list" aria-label="Cells">
          {cells.length === 0 ? (
            <div className="nb-empty">
              {canEdit ? (
                <>
                  <button type="button" className="nb-button" onClick={() => insert("python", "below", null)}>
                    Add a Python cell
                  </button>
                  <button type="button" className="nb-button" onClick={() => insert("markdown", "below", null)}>
                    Add Markdown
                  </button>
                  <button type="button" className="nb-button" onClick={() => insert("sql", "below", null)}>
                    Add SQL
                  </button>
                </>
              ) : (
                <p>This notebook has no cells.</p>
              )}
            </div>
          ) : null}
          {cells.map((cell, index) => {
            const ran = runtimeOf(cell.id);
            const links = cellLinks(graph, cell.id, names, order);
            const toLink = (l: { cell_id: string; names: string[] }): CellLink => ({ id: l.cell_id, name: nameOf(l.cell_id), names: l.names });
            const isActive = cell.id === activeId;
            const editing = isActive && mode === "edit";
            // A Markdown cell shown rendered shows its rendering once: the
            // kernel's Markdown output is that rendering, not an output under it.
            const showRendered = cell.kind === "markdown" && !editingMarkdown.has(cell.id) && !(isActive && mode === "edit");
            const markdownOutput = showRendered && ran.status !== "edited" ? ran.outputs.find(isMarkdownOutput) : undefined;
            const rt = showRendered ? { ...ran, outputs: ran.outputs.filter((o) => !isMarkdownOutput(o)) } : ran;
            const markdownShown = showRendered ? renderedMarkdown(cell, markdownOutput) : null;
            const lineage = highlight === null ? null : highlight.ids.has(cell.id) ? (upstreamOf(graph, highlight.from).includes(cell.id) ? "upstream" : "downstream") : null;
            return (
              <div role="listitem" key={cell.id}>
                <CellView
                  cell={cell}
                  index={index}
                  runtime={rt}
                  active={isActive}
                  editing={editing}
                  narrow={narrow}
                  canEdit={canEdit}
                  canRun={canRun}
                  highlight={lineage}
                  upstream={links.readsFrom.map(toLink)}
                  downstream={links.readBy.map(toLink)}
                  presence={presenceIn(cell.id)}
                  connection={
                    cell.kind === "sql" && props.sqlConnection
                      ? props.sqlConnection({
                          cellId: cell.id,
                          connection: typeof cell.meta.connection === "string" ? cell.meta.connection : null,
                          canEdit,
                          onChange: (connection) => setMeta(cell.id, { connection }),
                        })
                      : undefined
                  }
                  runProblem={problemFor(cell.id)}
                  editor={
                    <CellEditor
                      binding={bindingFor(cell.id)}
                      kind={cell.kind}
                      readOnly={!canEdit}
                      keys={editKeys}
                      extensions={props.cellExtensions?.(cell.id)}
                      onCommand={(cmd, view) => runCommand(cmd, cell.id, view)}
                      onFocus={() => {
                        setActiveId(cell.id);
                        setMode("edit");
                      }}
                      onView={(view) => {
                        if (view) views.current.set(cell.id, view);
                        else views.current.delete(cell.id);
                      }}
                    />
                  }
                  rendered={markdownShown}
                  outputs={outputsFor(cell, rt)}
                  outputCollapsed={collapsed.has(cell.id)}
                  errorActions={quickFixes(cell)}
                  menu={cellMenu(cell)}
                  now={now}
                  onActivate={() => setActiveId(cell.id)}
                  onEdit={() => runCommand("mode.edit", cell.id)}
                  onRun={() => runCommand("run.cell", cell.id)}
                  onMenu={(cmd) => runCommand(cmd as CommandId, cell.id)}
                  onHoverLinks={(ids) => hoverLinks(cell.id, ids)}
                  onJump={(id) => focusCell(id, false)}
                  onToggleCode={() => runCommand("cell.toggle_hide_code", cell.id)}
                  onToggleOutput={() => runCommand("output.toggle_collapse", cell.id)}
                  onSetMeta={(meta) => setMeta(cell.id, meta)}
                  onDropCell={
                    canEdit && !narrow
                      ? (moved, where) => {
                          if (apply([{ op: "move", cell_id: moved, ...(where === "before" ? { before: cell.id } : { after: cell.id }) }]) !== null) {
                            focusCell(moved, false);
                          }
                        }
                      : undefined
                  }
                />
                {canEdit && !narrow ? (
                  <div className="nb-between">
                    <button type="button" className="nb-between__add" onClick={() => insert("python", "below", cell.id)} aria-label="Add a cell below">
                      + Python
                    </button>
                    <button type="button" className="nb-between__add" onClick={() => insert("markdown", "below", cell.id)} aria-label="Add Markdown below">
                      + Markdown
                    </button>
                    <button type="button" className="nb-between__add" onClick={() => insert("sql", "below", cell.id)} aria-label="Add SQL below">
                      + SQL
                    </button>
                  </div>
                ) : null}
              </div>
            );
          })}
          {/* Always there, wide or narrow: the between-cells keys only show on
              hover, so without this the way to add a cell is invisible. */}
          {canEdit && cells.length > 0 ? (
            <button type="button" className="nb-button nb-add-end" onClick={() => insert("python", "below", order[order.length - 1] ?? null)}>
              Add a cell
            </button>
          ) : null}
        </div>
        {panel !== null ? (
          <aside className={`nb-dock${narrow ? " nb-dock--sheet" : ""}`} aria-label={PANEL_LABEL[panel]}>
            <header className="nb-dock__header">
              <h2>{PANEL_LABEL[panel]}</h2>
              <button type="button" className="nb-icon-button" aria-label="Close" onClick={() => setPanel(null)}>
                ×
              </button>
            </header>
            <div className="nb-dock__body">{panelBody(panel)}</div>
          </aside>
        ) : null}
      </div>
      {runtime.confirmation ? (
        <ConfirmRunDialog
          confirmation={runtime.confirmation}
          onRun={() => actions.confirmRun(runtime.confirmation!.run_id)}
          onCancel={() => actions.cancelRun(runtime.confirmation!.run_id)}
        />
      ) : null}
      {pending?.kind === "restart" ? (
        <RestartDialog
          runAll={pending.runAll}
          onCancel={() => setPending(null)}
          onConfirm={() => {
            setPending(null);
            actions.kernel("restart");
            if (pending.runAll) actions.run({ kind: "all" });
          }}
        />
      ) : null}
      {pending?.kind === "env" ? (
        <EnvironmentSwitchDialog
          environment={switchTarget(runtime.envs.find((e) => e.env_id === pending.envId))}
          onCancel={() => setPending(null)}
          onConfirm={() => {
            setPending(null);
            const target = runtime.envs.find((e) => e.env_id === pending.envId);
            if (target?.recorded) actions.switchEnv(target.recorded);
          }}
        />
      ) : null}
      {pending?.kind === "rename" ? (
        <NameDialog
          title="Rename cell"
          initial={cellById(pending.cellId)?.name === "_" ? "" : (cellById(pending.cellId)?.name ?? "")}
          validate={(value) => (value === "" || /^[A-Za-z_][A-Za-z0-9_]*$/.test(value) ? null : "A name is letters, digits and underscores, not starting with a digit.")}
          onCancel={() => setPending(null)}
          onSave={(value) => {
            setPending(null);
            apply([{ op: "rename", cell_id: pending.cellId, name: value === "" ? "_" : value }]);
          }}
        />
      ) : null}
      {pending?.kind === "rename_definition" ? (
        <NameDialog
          title={`Rename ${pending.name}`}
          initial={pending.name}
          validate={(value) => {
            const refusal = checkNewName(pending.name, value);
            return refusal ? refusalText(refusal) : null;
          }}
          onCancel={() => setPending(null)}
          onSave={(value) => {
            setPending(null);
            const fix = renameDefinition({ cellId: pending.cellId, name: pending.name, newName: value, cells, runtime: names, graph });
            if (fix.ok) apply(fix.ops);
            else setProblem(refusalText(fix.refusal));
          }}
        />
      ) : null}
    </div>
  );
}

const IMAGE_TYPES = ["image/png", "image/jpeg", "image/gif", "image/webp"];

/** The first raster image among a cell's outputs, for "Download image". */
export function firstImage(outputs: readonly CellOutput[]): { mime: string; data: string } | null {
  for (const out of outputs) {
    if (out.type !== "display") continue;
    for (const mime of IMAGE_TYPES) {
      const data = out.data[mime];
      if (typeof data === "string") return { mime, data };
    }
  }
  return null;
}

/** A display output whose content is Markdown: what running a Markdown cell
 *  produces. */
function isMarkdownOutput(output: CellOutput): boolean {
  return output.type === "display" && typeof output.data["text/markdown"] === "string";
}

/** The kernel states in which a kernel is there, on an environment. */
const KERNEL_UP = new Set(["starting", "idle", "busy"]);

function switchTarget(env: EnvInfo | undefined): string {
  if (!env) return "this environment";
  return env.kind === "default" ? "the default environment" : envLabel(env);
}

function requestAnimationFrameSafe(fn: () => void): void {
  if (typeof requestAnimationFrame === "function") requestAnimationFrame(() => fn());
  else fn();
}

/** A `multiple_definitions` graph error on a cell, as the quick fixes take it.
 *  The graph names it either bare or with the name (`multiple_definitions:x`);
 *  bare, the names are this cell's definitions another cell also defines. */
export function multipleDefinitions(
  cellId: string,
  cells: Readonly<Record<string, CellRuntime | undefined>>,
  graph: { errors: Record<string, string[]> },
): ErrorInfo | null {
  const own = cells[cellId];
  const errors = [...(own?.graph_errors ?? []), ...(graph.errors[cellId] ?? [])].filter((e) => e.startsWith("multiple_definitions"));
  if (errors.length === 0) return null;
  let names = errors.map((e) => e.split(/[:\s]+/)[1]).filter((n): n is string => typeof n === "string" && n !== "");
  if (names.length === 0) {
    const elsewhere = new Set(Object.entries(cells).flatMap(([id, c]) => (id === cellId ? [] : (c?.defs ?? []))));
    names = (own?.defs ?? []).filter((d) => elsewhere.has(d));
  }
  if (names.length === 0) return null;
  return { ename: "MultipleDefinitionError", evalue: names.join(", "), traceback: [], kind: "multiple_definitions", names: [...new Set(names)] };
}

/** What a refused quick fix tells the person. */
export function refusalText(refusal: QuickFixRefusal): string {
  switch (refusal.reason) {
    case "same_name":
      return "That is the name it already has.";
    case "keyword":
      return `${refusal.name} is a Python keyword.`;
    case "invalid_name":
      return "A name is letters, digits and underscores, not starting with a digit.";
    case "already_defined":
    case "name_in_use":
      return `${refusal.name} is already used in this notebook.`;
    case "not_python":
    case "reader_not_python":
      return "Only Python cells can be renamed this way. Rename it by hand.";
    case "not_defined_here":
      return "This cell does not define that name.";
    default:
      return "This fix could not be applied. Rename it by hand.";
  }
}

function NameDialog(props: {
  title: string;
  initial: string;
  validate(value: string): string | null;
  onSave(value: string): void;
  onCancel(): void;
}) {
  const [value, setValue] = useState(props.initial);
  const input = useRef<HTMLInputElement>(null);
  const error = props.validate(value.trim());
  const save = () => {
    if (error === null) props.onSave(value.trim());
  };
  return (
    <Dialog
      title={props.title}
      onCancel={props.onCancel}
      initialFocus={input}
      actions={
        <>
          <button type="button" className="nb-button" onClick={props.onCancel}>
            Cancel
          </button>
          <button type="button" className="nb-button nb-button--primary" disabled={error !== null} onClick={save}>
            Save
          </button>
        </>
      }
    >
      <input
        ref={input}
        className="nb-input"
        aria-label="Name"
        value={value}
        onChange={(e) => setValue(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter") save();
        }}
      />
      {error ? <p className="nb-field-error">{error}</p> : null}
    </Dialog>
  );
}
