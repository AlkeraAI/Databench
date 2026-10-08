// The notebook editor driven the way a person drives it: keys in both modes,
// the toolbar, the confirmation dialog, the queue, quick fixes, statuses and
// gutters, read-only and the narrow layout. The document is the in-memory
// host, so every assertion reads the document the editor wrote.

import { readFileSync } from "node:fs";
import { join } from "node:path";
import { act, fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ABSENT_KERNEL, CELL_STATUSES, EMPTY_RUNTIME, type CellRuntime, type CellStatus, type KernelView, type RunTarget } from "../model/types";
import { registerDefaultOutputRenderers } from "../outputs/defaults";
import { MemoryNotebook } from "./memoryHost";
import { INFO_NOTICE_MS, NotebookEditor, multipleDefinitions, type NotebookEditorProps } from "./NotebookEditor";
import type { NotebookActions, RuntimeSnapshot } from "./ports";
import { STATUS_LOOK } from "./status";

registerDefaultOutputRenderers();

const NOW = Date.parse("2026-10-05T12:00:00Z");

function runtime(cells: Record<string, Partial<CellRuntime>> = {}, extra: Partial<RuntimeSnapshot> = {}): RuntimeSnapshot {
  return {
    cells: Object.fromEntries(Object.entries(cells).map(([id, c]) => [id, { ...EMPTY_RUNTIME, ...c }])),
    kernel: { ...ABSENT_KERNEL, state: "idle" },
    graph: { edges: [], errors: {} },
    presence: [],
    confirmation: null,
    envs: [],
    packages: [],
    installing: false,
    notice: null,
    ...extra,
  };
}

interface Rig {
  doc: MemoryNotebook;
  runs: RunTarget[];
  kernel: string[];
  confirmed: string[];
  rerender(next: Partial<NotebookEditorProps>): void;
  root: HTMLElement;
}

function setup(opts: {
  cells?: ConstructorParameters<typeof MemoryNotebook>[0];
  rt?: RuntimeSnapshot;
  canEdit?: boolean;
  canRun?: boolean;
  narrow?: boolean;
} = {}): Rig {
  const doc = new MemoryNotebook(opts.cells ?? [
    { id: "c1", source: "x = 1" },
    { id: "c2", source: "y = x + 1" },
    { id: "c3", source: "print(y)" },
  ]);
  const runs: RunTarget[] = [];
  const kernel: string[] = [];
  const confirmed: string[] = [];
  const actions: NotebookActions = {
    run: (t) => runs.push(t),
    confirmRun: (id) => confirmed.push(`run:${id}`),
    cancelRun: (id) => confirmed.push(`cancel:${id}`),
    kernel: (a) => kernel.push(a),
    switchEnv: (id) => kernel.push(`env:${id}`),
    install: () => {},
  };
  const base: NotebookEditorProps = {
    doc,
    text: doc,
    runtime: opts.rt ?? runtime(),
    actions,
    permissions: { canEdit: opts.canEdit ?? true, canRun: opts.canRun ?? true },
    output: { theme: "light" },
    narrow: opts.narrow ?? false,
    platform: "other",
    now: () => NOW,
  };
  const view = render(<NotebookEditor {...base} />);
  return {
    doc,
    runs,
    kernel,
    confirmed,
    rerender: (next) => view.rerender(<NotebookEditor {...base} {...next} />),
    root: screen.getByTestId("notebook-editor"),
  };
}

const ids = (doc: MemoryNotebook) => doc.snapshot().cells.map((c) => c.id);
const press = (el: HTMLElement, key: string, mods: Partial<KeyboardEventInit> = {}) => fireEvent.keyDown(el, { key, ...mods });
const activeId = (root: HTMLElement) => root.querySelector("[aria-current='true']")?.getAttribute("data-cell-id");

describe("command mode keys", () => {
  it("B inserts below, A above, and D D deletes", () => {
    const { doc, root } = setup();
    press(root, "b");
    expect(ids(doc)).toHaveLength(4);
    expect(ids(doc)[1]).not.toMatch(/^c/);
    expect(root.getAttribute("data-mode")).toBe("edit");
    press(root.querySelector<HTMLElement>(`[data-cell-id="${ids(doc)[1]}"] .cm-content`)!, "Escape");
    act(() => root.querySelector<HTMLElement>("[data-cell-id='c3']")!.dispatchEvent(new MouseEvent("mousedown", { bubbles: true })));
    press(root, "a");
    expect(ids(doc)[3]).not.toMatch(/^c/);
    expect(ids(doc)[4]).toBe("c3");
  });

  it("D D deletes the active cell and Z restores it", () => {
    const { doc, root } = setup();
    press(root, "d");
    expect(ids(doc)).toEqual(["c1", "c2", "c3"]);
    press(root, "d");
    expect(ids(doc)).toEqual(["c2", "c3"]);
    press(root, "z");
    expect(ids(doc).sort()).toEqual(["c1", "c2", "c3"]);
  });

  it("a single D does nothing, and a slow second D starts over", () => {
    const { doc, root } = setup();
    const real = Date.now;
    let t = 1_000_000;
    Date.now = () => t;
    try {
      press(root, "d");
      t += 2000;
      press(root, "d");
      expect(ids(doc)).toEqual(["c1", "c2", "c3"]);
    } finally {
      Date.now = real;
    }
  });

  it.each([
    ["m", "markdown"],
    ["s", "sql"],
  ])("%s changes the kind to %s and Y back to Python", (key, kind) => {
    const { doc, root } = setup();
    press(root, key);
    expect(doc.snapshot().cells[0]!.kind).toBe(kind);
    press(root, "y");
    expect(doc.snapshot().cells[0]!.kind).toBe("python");
  });

  it("J and K move between cells", () => {
    const { root } = setup();
    expect(activeId(root)).toBe("c1");
    press(root, "j");
    press(root, "ArrowDown");
    expect(activeId(root)).toBe("c3");
    press(root, "k");
    expect(activeId(root)).toBe("c2");
  });

  it("Shift+Enter runs the cell and moves on; Mod+Enter runs in place", () => {
    const { root, runs } = setup();
    press(root, "Enter", { shiftKey: true });
    expect(runs).toEqual([{ kind: "cells", ids: ["c1"] }]);
    expect(activeId(root)).toBe("c2");
    press(root, "Enter", { ctrlKey: true });
    expect(runs[1]).toEqual({ kind: "cells", ids: ["c2"] });
    expect(activeId(root)).toBe("c2");
  });

  it("Alt+Enter runs and inserts a cell below", () => {
    const { doc, root, runs } = setup();
    press(root, "Enter", { altKey: true });
    expect(runs).toEqual([{ kind: "cells", ids: ["c1"] }]);
    expect(ids(doc)).toHaveLength(4);
    expect(ids(doc)[0]).toBe("c1");
    expect(ids(doc)[2]).toBe("c2");
  });

  it("I I interrupts and 0 0 asks before restarting", () => {
    const { root, kernel } = setup();
    press(root, "i");
    press(root, "i");
    expect(kernel).toEqual(["interrupt"]);
    press(root, "0");
    press(root, "0");
    const dialog = screen.getByRole("dialog");
    expect(kernel).toEqual(["interrupt"]);
    fireEvent.click(within(dialog).getByRole("button", { name: "Restart" }));
    expect(kernel).toEqual(["interrupt", "restart"]);
  });

  it("Mod+Z in command mode undoes the last structure edit", () => {
    const { doc, root } = setup();
    press(root, "b");
    press(root.querySelector<HTMLElement>(`[data-cell-id="${ids(doc)[1]}"] .cm-content`)!, "Escape");
    press(root, "z", { ctrlKey: true });
    expect(ids(doc)).toEqual(["c1", "c2", "c3"]);
  });

  it("keys typed into a panel input are not commands", () => {
    const { doc, root } = setup();
    press(root, "f", { ctrlKey: true });
    const input = screen.getAllByRole("textbox")[0]!;
    press(input, "b");
    expect(ids(doc)).toHaveLength(3);
  });
});

describe("edit mode", () => {
  it("Enter edits the cell, typing writes the document, Escape leaves", () => {
    const { doc, root } = setup();
    press(root, "Enter");
    expect(root.getAttribute("data-mode")).toBe("edit");
    const editor = root.querySelector<HTMLElement>("[data-cell-id='c1'] .cm-content")!;
    // The key resolver ignores keys from inside the editor in edit mode.
    press(root, "b");
    expect(ids(doc)).toHaveLength(3);
    press(editor, "Escape");
    expect(root.getAttribute("data-mode")).toBe("command");
  });

  it("Shift+Enter inside the editor runs the cell", () => {
    const { root, runs } = setup();
    const editor = root.querySelector<HTMLElement>("[data-cell-id='c2'] .cm-content")!;
    fireEvent.focus(editor);
    press(editor, "Enter", { shiftKey: true });
    expect(runs).toEqual([{ kind: "cells", ids: ["c2"] }]);
  });

  it("an edit someone else makes shows in the cell's editor", () => {
    const { doc, root } = setup();
    act(() => void doc.apply([{ op: "replace", cell_id: "c3", source: "print(y * 2)" }]));
    expect(root.querySelector("[data-cell-id='c3'] .cm-content")!.textContent).toBe("print(y * 2)");
  });
});

describe("statuses and gutters", () => {
  it.each(CELL_STATUSES.map((s) => [s]))("draws %s with its label and tooltip", (status: CellStatus) => {
    const { root } = setup({ rt: runtime({ c1: { status } }) });
    const dot = within(root.querySelector<HTMLElement>("[data-cell-id='c1']")!).getByTestId("cell-status");
    expect(dot).toHaveAttribute("aria-label", STATUS_LOOK[status].label);
    expect(dot.getAttribute("title")).toContain(STATUS_LOOK[status].detail);
  });

  it("shows what a cell reads from and who reads it, highlights on hover, jumps on click", () => {
    const rt = runtime(
      { c1: { defs: ["x"] }, c2: { defs: ["y"], refs: ["x"] }, c3: { refs: ["y"] } },
      { graph: { edges: [["c1", "c2"], ["c2", "c3"]], errors: {} } },
    );
    const { root } = setup({ rt });
    const c2 = root.querySelector<HTMLElement>("[data-cell-id='c2']")!;
    const up = within(c2).getByRole("button", { name: "Reads from 1 cell" });
    within(c2).getByRole("button", { name: "Read by 1 cell" });
    fireEvent.mouseEnter(up);
    expect(root.querySelector("[data-cell-id='c1']")!.className).toContain("nb-cell--upstream");
    expect(root.querySelector("[data-cell-id='c3']")!.className).not.toMatch(/nb-cell--(upstream|downstream)/);
    fireEvent.mouseLeave(up);
    fireEvent.click(up);
    fireEvent.click(within(c2).getByRole("button", { name: /Cell 1/ }));
    expect(activeId(root)).toBe("c1");
  });

  it("marks saved outputs as not from this kernel, and outdated ones", () => {
    const out = [{ output_id: "o1", type: "stream" as const, name: "stdout" as const, text: "hello" }];
    const { root } = setup({ rt: runtime({ c1: { outputs: out, saved: true }, c2: { outputs: out, saved: true, outdated: true } }) });
    expect(within(root.querySelector<HTMLElement>("[data-cell-id='c1']")!).getByText("Not run in this kernel")).toBeInTheDocument();
    expect(root.querySelector("[data-cell-id='c2']")!.textContent).toContain("Outdated");
  });

  const ranBy = (by: { kind: "person" | "agent"; id: string; display_name: string; acting_for?: { id: string; display_name: string } }) =>
    setup({
      rt: runtime({
        c1: { status: "fresh", duration_ms: 1200, last_run: { run_id: "r1", by, trigger: "run", started_at: null, finished_at: "2026-10-05T11:58:00Z" } },
      }),
    });
  const withStyles = () => {
    const style = document.createElement("style");
    style.textContent = readFileSync(join(process.cwd(), "src/editor/editor.css"), "utf8");
    document.head.appendChild(style);
    return () => style.remove();
  };

  it("says when a cell ran and for how long, and who only on hover or focus", () => {
    const unstyle = withStyles();
    try {
      const { root } = ranBy({ kind: "person", id: "u1", display_name: "Alice" });
      const cell = root.querySelector<HTMLElement>("[data-cell-id='c1']")!;
      const footer = cell.querySelector("footer")!;
      expect(footer.textContent).toBe("2 min ago · 1.2 s · by Alice");
      expect(within(footer).getByText("2 min ago · 1.2 s")).toBeInTheDocument();
      const who = cell.querySelector<HTMLElement>(".nb-cell__run-by")!;
      fireEvent.mouseDown(root.querySelector<HTMLElement>("[data-cell-id='c2']")!);
      expect(cell.classList.contains("nb-cell--active")).toBe(false);
      expect(getComputedStyle(who).opacity).toBe("0");
      // The cell the keyboard is on (command mode keeps focus on the notebook).
      fireEvent.mouseDown(cell);
      expect(cell.classList.contains("nb-cell--active")).toBe(true);
      expect(getComputedStyle(who).opacity).toBe("1");
    } finally {
      unstyle();
    }
  });

  it("names an agent's run by the person it ran for, on hover", () => {
    const { root } = ranBy({ kind: "agent", id: "agent:1", display_name: "Alkera agent", acting_for: { id: "user:ada", display_name: "Ada" } });
    expect(root.querySelector(".nb-cell__run-by")!.textContent).toBe(" · by Agent for Ada");
    expect(root.querySelector(".nb-cell__run-when")!.textContent).toBe("2 min ago · 1.2 s");
  });
});

describe("a cell whose re-run waits for someone editing it", () => {
  it("says so on the cell, with no banner to close, and stops when they leave", () => {
    const rig = setup({ rt: runtime({ c2: { status: "stale", rerun_waits_for: "Ada" } }) });
    const c2 = rig.root.querySelector<HTMLElement>("[data-cell-id='c2']")!;
    expect(within(c2).getByTestId("cell-waiting")).toHaveTextContent("Not re-run while Ada is editing it");
    expect(within(rig.root.querySelector<HTMLElement>("[data-cell-id='c1']")!).queryByTestId("cell-waiting")).toBeNull();
    expect(rig.root.querySelector(".nb-notice")).toBeNull();
    expect(screen.queryByRole("button", { name: "Dismiss" })).toBeNull();
    rig.rerender({ runtime: runtime({ c2: { status: "fresh", rerun_waits_for: null } }) });
    expect(screen.queryByTestId("cell-waiting")).toBeNull();
  });

  it("shows what the engine said about one cell on that cell", () => {
    const rig = setup({ rt: runtime({ c2: { notice: { kind: "module_missing", message: "No module named polars" } } }) });
    const c2 = rig.root.querySelector<HTMLElement>("[data-cell-id='c2']")!;
    expect(within(c2).getByTestId("cell-notice")).toHaveTextContent("No module named polars");
    expect(rig.root.querySelector(".nb-notice")).toBeNull();
  });
});

describe("switching the environment", () => {
  const env = (env_id: string, kind: string, recorded: string, spec_root: string) => ({
    env_id,
    kind,
    spec_root,
    python: "3.13.1",
    state: "ready",
    recorded_in_file: false,
    recorded,
  });

  it("asks for the environment by the value the notebook records, not the engine's id", async () => {
    const user = userEvent.setup();
    const current = env("default:.alkera/envs/default", "default", "default", ".alkera/envs/default");
    const project = env("uv_project:proj", "uv_project", "./proj", "proj");
    const { kernel } = setup({
      rt: runtime({}, { kernel: { ...ABSENT_KERNEL, state: "idle", env: current }, envs: [current, project] }),
    });
    // The environment menu, not the panel toggle of the same name.
    await user.click(screen.getAllByRole("button", { name: "Environment" }).find((b) => !b.hasAttribute("aria-pressed"))!);
    await user.click(screen.getByRole("menuitemcheckbox", { name: /proj/ }));
    await user.click(screen.getByRole("button", { name: "Switch" }));
    expect(kernel).toEqual(["env:./proj"]);
  });
});

describe("the toolbar", () => {
  it.each([
    ["none", "Interrupt"],
    ["signalled", "Interrupting"],
    ["resignalled", "Interrupting again"],
    ["restarting", "Restarting the kernel"],
  ] as const)("shows the interrupt escalation %s as %s", (phase, label) => {
    setup({ rt: runtime({}, { kernel: { ...ABSENT_KERNEL, state: "busy", interrupt: phase } }) });
    expect(screen.getByRole("button", { name: label })).toBeInTheDocument();
  });

  it.each([
    [0.79, false],
    [0.8, true],
  ])("warns about memory at %s of the limit: %s", (share, warns) => {
    const kernel: KernelView = { ...ABSENT_KERNEL, state: "idle", memory_bytes: share * 1000, memory_limit_bytes: 1000 };
    const { root } = setup({ rt: runtime({}, { kernel }) });
    expect(root.querySelector(".nb-memory--warn") !== null).toBe(warns);
  });

  it("switches reactivity in Settings, not on the toolbar", () => {
    const { doc } = setup();
    expect(screen.queryByRole("button", { name: "Reactivity" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Settings" }));
    fireEvent.change(screen.getByRole("combobox", { name: "When a cell runs" }), { target: { value: "lazy" } });
    expect(doc.snapshot().settings.reactivity).toBe("lazy");
  });
});

describe("the confirmation dialog", () => {
  it("asks before an expensive run and answers with the run id", () => {
    const rt = runtime({}, {
      confirmation: { run_id: "r9", estimate_s: 11520, plan: [{ cell_id: "c1", name: "train", reason: "descendant", last_duration_ms: 11_520_000 }] },
    });
    const { confirmed } = setup({ rt });
    const dialog = screen.getByRole("dialog");
    expect(dialog.textContent).toContain("train");
    expect(dialog.textContent).toContain("3 h 12 min");
    fireEvent.click(within(dialog).getByRole("button", { name: "Run" }));
    expect(confirmed).toEqual(["run:r9"]);
  });
});

describe("quick fixes", () => {
  it("makes a doubly defined name local to this cell", () => {
    const err = { output_id: "e", type: "error" as const, error: { ename: "MultipleDefinitionError", evalue: "x", traceback: [], kind: "multiple_definitions", names: ["x"], cells: ["c1", "c2"] } };
    const { doc } = setup({
      cells: [{ id: "c1", source: "x = 1" }, { id: "c2", source: "x = 2\nprint(x)" }],
      rt: runtime({ c2: { status: "error", outputs: [err], defs: ["x"] } }),
    });
    fireEvent.click(screen.getByRole("button", { name: "Make local to this cell" }));
    expect(doc.snapshot().cells[1]!.source).toBe("_x = 2\nprint(_x)");
    expect(doc.snapshot().cells[0]!.source).toBe("x = 1");
  });

  it("renames a definition and the later cells that read it from this cell, not from the other", () => {
    const err = { output_id: "e", type: "error" as const, error: { ename: "E", evalue: "", traceback: [], kind: "multiple_definitions", names: ["df"] } };
    const { doc } = setup({
      cells: [{ id: "c1", source: "df = load()" }, { id: "c2", source: "df = other()" }, { id: "c3", source: "df.head()" }, { id: "c4", source: "df.tail()" }],
      rt: runtime(
        { c1: { status: "error", outputs: [err], defs: ["df"] }, c2: { defs: ["df"] }, c3: { refs: ["df"] }, c4: { refs: ["df"] } },
        { graph: { edges: [["c1", "c3"], ["c2", "c4"]], errors: {} } },
      ),
    });
    fireEvent.click(screen.getByRole("button", { name: "Rename this definition" }));
    const input = screen.getByRole("textbox", { name: "Name" });
    fireEvent.change(input, { target: { value: "raw" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(doc.snapshot().cells.map((c) => c.source)).toEqual(["raw = load()", "df = other()", "raw.head()", "df.tail()"]);
  });
});

describe("read-only", () => {
  it("shows everything and offers no edit or run control", () => {
    const out = [{ output_id: "o1", type: "stream" as const, name: "stdout" as const, text: "42" }];
    const { root, doc, runs } = setup({ canEdit: false, canRun: false, rt: runtime({ c1: { status: "fresh", outputs: out } }) });
    expect(root.textContent).toContain("42");
    expect(screen.queryByRole("button", { name: "Run cell" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Cell actions" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Run all" })).toBeNull();
    press(root, "b");
    press(root, "Enter", { shiftKey: true });
    expect(ids(doc)).toHaveLength(3);
    expect(runs).toEqual([]);
    expect(root.querySelector("[data-cell-id='c1'] .cm-content")!.getAttribute("contenteditable")).toBe("false");
  });
});

describe("narrow layout", () => {
  it("has no command mode, runs from the focused cell alone, and panels as sheets", () => {
    const { root, doc, runs } = setup({ narrow: true });
    press(root, "b");
    expect(ids(doc)).toHaveLength(3);
    // The focused cell's own button is the only run-cell control; nothing floats over the notebook.
    const runCell = screen.getAllByRole("button", { name: "Run cell" });
    expect(runCell).toHaveLength(1);
    expect(root.querySelector("[data-cell-id='c1']")!.contains(runCell[0]!)).toBe(true);
    fireEvent.click(runCell[0]!);
    expect(runs).toEqual([{ kind: "cells", ids: ["c1"] }]);
    fireEvent.click(screen.getByRole("button", { name: "Panels" }));
    fireEvent.click(screen.getByRole("menuitemcheckbox", { name: "Outline" }));
    expect(root.querySelector(".nb-dock--sheet")).not.toBeNull();
    // Only the focused cell carries its toolbar.
    expect(screen.getAllByRole("button", { name: "Cell actions" })).toHaveLength(1);
  });
});

describe("markdown cells", () => {
  it("show their rendering until edited", () => {
    const { root } = setup({ cells: [{ id: "m1", kind: "markdown", source: "# Weekly revenue" }] });
    expect(within(root.querySelector<HTMLElement>("[data-cell-id='m1']")!).getByRole("heading", { name: "Weekly revenue" })).toBeInTheDocument();
    press(root, "Enter");
    expect(root.querySelector("[data-cell-id='m1'] .cm-content")).not.toBeNull();
  });

  const ranMarkdown = (text: string, extra: Partial<CellRuntime> = {}) =>
    runtime({
      m1: {
        status: "fresh",
        outputs: [{ output_id: "m1/0", type: "display", data: { "text/markdown": text } }],
        ...extra,
      },
    });

  it("show a run's rendering once, not again as an output under it", () => {
    const { root } = setup({ cells: [{ id: "m1", kind: "markdown", source: "# Weekly revenue" }], rt: ranMarkdown("# Weekly revenue") });
    const cell = root.querySelector<HTMLElement>("[data-cell-id='m1']")!;
    expect(within(cell).getAllByRole("heading", { name: "Weekly revenue" })).toHaveLength(1);
  });

  it("show the kernel's rendering, which carries the values the text interpolates", () => {
    const { root } = setup({ cells: [{ id: "m1", kind: "markdown", source: "Total is {total}" }], rt: ranMarkdown("Total is 42") });
    const cell = root.querySelector<HTMLElement>("[data-cell-id='m1']")!;
    expect(cell).toHaveTextContent("Total is 42");
    expect(cell).not.toHaveTextContent("{total}");
  });

  it("show their own text once it is edited past the kernel's rendering", () => {
    const { root } = setup({
      cells: [{ id: "m1", kind: "markdown", source: "# Monthly revenue" }],
      rt: ranMarkdown("# Weekly revenue", { status: "edited" }),
    });
    const cell = root.querySelector<HTMLElement>("[data-cell-id='m1']")!;
    expect(within(cell).getByRole("heading", { name: "Monthly revenue" })).toBeInTheDocument();
    expect(within(cell).queryByRole("heading", { name: "Weekly revenue" })).toBeNull();
  });

  it("still show what else the run produced, such as an error", () => {
    const { root } = setup({
      cells: [{ id: "m1", kind: "markdown", source: "Total is {total}" }],
      rt: runtime({ m1: { status: "error", outputs: [{ output_id: "m1/e", type: "error", error: { ename: "NameError", evalue: "name 'total' is not defined", traceback: [] } }] } }),
    });
    const cell = root.querySelector<HTMLElement>("[data-cell-id='m1']")!;
    expect(cell).toHaveTextContent("NameError");
  });
});

describe("an environment the kernel has not moved to", () => {
  const env = (env_id: string, kind: string) => ({ env_id, kind, spec_root: "", python: "3.12.13", state: "ready", recorded_in_file: false });
  const DEFAULT = env("default:.alkera/envs/default", "default");
  const PROJECT = env("uv_project:.", "uv_project");

  it("says which one the running kernel uses until a restart, and offers the restart", () => {
    const { kernel } = setup({ rt: runtime({}, { kernel: { ...ABSENT_KERNEL, state: "idle", env: DEFAULT }, selectedEnv: PROJECT }) });
    const note = screen.getByTestId("env-pending");
    expect(note).toHaveTextContent("The kernel uses Default until a restart moves it to uv project.");
    fireEvent.click(within(note).getByRole("button", { name: "Restart" }));
    fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: /restart/i }));
    expect(kernel).toEqual(["restart"]);
    expect(screen.getByRole("toolbar", { name: "Notebook" })).toHaveTextContent("Default");
  });

  it.each([
    ["the kernel is on the named one", { kernel: { ...ABSENT_KERNEL, state: "idle" as const, env: PROJECT }, selectedEnv: PROJECT }],
    ["no kernel runs", { kernel: { ...ABSENT_KERNEL, state: "absent" as const, env: DEFAULT }, selectedEnv: PROJECT }],
    ["the notebook names none", { kernel: { ...ABSENT_KERNEL, state: "idle" as const, env: DEFAULT }, selectedEnv: null }],
  ])("says nothing when %s", (_, extra) => {
    setup({ rt: runtime({}, extra) });
    expect(screen.queryByTestId("env-pending")).toBeNull();
  });

  it("offers no restart to a reader", () => {
    setup({ canEdit: false, canRun: false, rt: runtime({}, { kernel: { ...ABSENT_KERNEL, state: "idle", env: DEFAULT }, selectedEnv: PROJECT }) });
    expect(within(screen.getByTestId("env-pending")).queryByRole("button")).toBeNull();
  });
});

describe("a newer build of the kernel's environment", () => {
  const DEFAULT = { env_id: "default:.alkera/envs/default", kind: "default", spec_root: "", python: "3.12.13", state: "ready", recorded_in_file: false };
  const outdated = { ...ABSENT_KERNEL, state: "idle" as const, env: DEFAULT, env_outdated: true };

  it("says a newer one is ready and offers the restart that moves the kernel to it", () => {
    const { kernel } = setup({ rt: runtime({}, { kernel: outdated }) });
    const note = screen.getByTestId("env-newer");
    expect(note).toHaveTextContent("A newer environment is ready.");
    fireEvent.click(within(note).getByRole("button", { name: "Restart" }));
    fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: /restart/i }));
    expect(kernel).toEqual(["restart"]);
  });

  it.each([
    ["the kernel is on the newest build", { kernel: { ...outdated, env_outdated: false } }],
    ["no kernel runs", { kernel: { ...outdated, state: "absent" as const } }],
  ])("says nothing when %s", (_, extra) => {
    setup({ rt: runtime({}, extra) });
    expect(screen.queryByTestId("env-newer")).toBeNull();
  });

  it("offers no restart to a reader", () => {
    setup({ canEdit: false, canRun: false, rt: runtime({}, { kernel: outdated }) });
    expect(within(screen.getByTestId("env-newer")).queryByRole("button")).toBeNull();
  });
});

describe("moving and inserting by kind", () => {
  function drag(from: HTMLElement, to: HTMLElement, clientY: number) {
    const store = new Map<string, string>();
    const dataTransfer = {
      setData: (t: string, v: string) => void store.set(t, v),
      getData: (t: string) => store.get(t) ?? "",
      get types() {
        return [...store.keys()];
      },
      effectAllowed: "",
    };
    to.getBoundingClientRect = () => new DOMRect(0, 100, 400, 100);
    fireEvent.dragStart(from, { dataTransfer });
    fireEvent.dragOver(to, { dataTransfer, clientY });
    fireEvent.drop(to, { dataTransfer, clientY });
  }

  it("drops a dragged cell after the cell it lands on", () => {
    const { doc, root } = setup();
    const handle = within(root.querySelector<HTMLElement>("[data-cell-id='c1']")!).getByRole("img", { name: "Drag to move the cell" });
    drag(handle, root.querySelector<HTMLElement>("[data-cell-id='c3']")!, 180);
    expect(ids(doc)).toEqual(["c2", "c3", "c1"]);
  });

  it("drops before the cell when it lands on its upper half", () => {
    const { doc, root } = setup();
    const handle = within(root.querySelector<HTMLElement>("[data-cell-id='c3']")!).getByRole("img", { name: "Drag to move the cell" });
    drag(handle, root.querySelector<HTMLElement>("[data-cell-id='c1']")!, 120);
    expect(ids(doc)).toEqual(["c3", "c1", "c2"]);
  });

  it("ignores a drop that carries no cell", () => {
    const { doc, root } = setup();
    fireEvent.drop(root.querySelector<HTMLElement>("[data-cell-id='c1']")!, { dataTransfer: { getData: () => "", types: [] } });
    expect(ids(doc)).toEqual(["c1", "c2", "c3"]);
  });

  it.each([
    ["Insert Markdown above", "markdown", 1],
    ["Insert SQL above", "sql", 1],
    ["Insert SQL below", "sql", 2],
  ])("%s from the cell menu", (label, kind, at) => {
    const { doc, root } = setup();
    const c2 = root.querySelector<HTMLElement>("[data-cell-id='c2']")!;
    fireEvent.click(within(c2).getByRole("button", { name: "Cell actions" }));
    fireEvent.click(screen.getByRole("menuitem", { name: label }));
    expect(doc.snapshot().cells[at]!.kind).toBe(kind);
    expect(ids(doc)).toHaveLength(4);
  });

  it("Clear output asks the host to clear that cell for everyone when it can", () => {
    const { root, rerender } = setup();
    const cleared: (string[] | null)[] = [];
    rerender({
      actions: {
        run: () => {},
        confirmRun: () => {},
        cancelRun: () => {},
        kernel: () => {},
        switchEnv: () => {},
        install: () => {},
        clearOutputs: (cellIds) => void cleared.push(cellIds),
      },
    });
    const c2 = root.querySelector<HTMLElement>("[data-cell-id='c2']")!;
    fireEvent.click(within(c2).getByRole("button", { name: "Cell actions" }));
    fireEvent.click(screen.getByRole("menuitem", { name: "Clear output" }));
    expect(cleared).toEqual([["c2"]]);
  });

  it("offers Delete cell beside Duplicate, where it is seen without scrolling, and deletes", () => {
    const { doc, root } = setup();
    const c2 = root.querySelector<HTMLElement>("[data-cell-id='c2']")!;
    fireEvent.click(within(c2).getByRole("button", { name: "Cell actions" }));
    const labels = screen.getAllByRole("menuitem").map((item) => item.textContent ?? "");
    const duplicate = labels.findIndex((l) => l.startsWith("Duplicate cell"));
    expect(labels[duplicate + 1]).toMatch(/^Delete cell/);
    fireEvent.click(screen.getByRole("menuitem", { name: /Delete cell/ }));
    expect(ids(doc)).not.toContain("c2");
  });

  it("offers Download image only for a cell with an image", () => {
    const png = { output_id: "p", type: "display" as const, data: { "image/png": "iVBORw0KGgo=" } };
    const { root } = setup({ rt: runtime({ c1: { outputs: [png] } }) });
    fireEvent.click(within(root.querySelector<HTMLElement>("[data-cell-id='c1']")!).getByRole("button", { name: "Cell actions" }));
    expect(screen.getByRole("menuitem", { name: "Download image" })).toBeInTheDocument();
    fireEvent.keyDown(screen.getByRole("menu"), { key: "Escape" });
    fireEvent.click(within(root.querySelector<HTMLElement>("[data-cell-id='c2']")!).getByRole("button", { name: "Cell actions" }));
    expect(screen.queryByRole("menuitem", { name: "Download image" })).toBeNull();
  });
});

describe("quick fixes from the graph", () => {
  it("offers the fixes when the graph says a cell's name is defined twice", () => {
    const { doc } = setup({
      cells: [{ id: "c1", source: "x = 1" }, { id: "c2", source: "x = 2" }],
      rt: runtime({ c1: { defs: ["x"] }, c2: { defs: ["x"], graph_errors: ["multiple_definitions"] } }),
    });
    fireEvent.click(screen.getByRole("button", { name: "Make local to this cell" }));
    expect(doc.snapshot().cells.map((c) => c.source)).toEqual(["x = 1", "_x = 2"]);
  });

  it.each([
    ["names the duplicate", ["multiple_definitions:y"], ["y"]],
    ["is bare", ["multiple_definitions"], ["x"]],
  ])("reads the names when the error %s", (_label, errors, names) => {
    const info = multipleDefinitions("c2", { c1: { ...EMPTY_RUNTIME, defs: ["x", "y"] }, c2: { ...EMPTY_RUNTIME, defs: ["x"], graph_errors: errors } }, { errors: {} });
    expect(info?.names).toEqual(names);
  });

  it("offers nothing without such an error", () => {
    expect(multipleDefinitions("c1", { c1: { ...EMPTY_RUNTIME, defs: ["x"], graph_errors: ["cycle"] } }, { errors: {} })).toBeNull();
  });
});

describe("a run that did not go through", () => {
  const cellOf = (id: string) => document.querySelector<HTMLElement>(`[data-cell-id="${id}"]`)!;

  it("says why in the run status area and on every cell a run of all was for", () => {
    setup({ rt: runtime({}, { runProblem: { run_id: "r1", message: "The machine did not answer. Try again.", tone: "warning", cells: "all" } }) });
    expect(screen.getByTestId("run-problem")).toHaveTextContent("The machine did not answer. Try again.");
    for (const id of ["c1", "c2", "c3"]) {
      expect(within(cellOf(id)).getByTestId("cell-problem")).toHaveTextContent("The machine did not answer. Try again.");
    }
  });

  it("marks only the cells the run was for", () => {
    setup({ rt: runtime({}, { runProblem: { run_id: "r1", message: "The run failed: no such environment.", tone: "danger", cells: ["c2"] } }) });
    expect(within(cellOf("c2")).getByTestId("cell-problem")).toHaveTextContent("no such environment");
    expect(within(cellOf("c1")).queryByTestId("cell-problem")).toBeNull();
    expect(within(cellOf("c3")).queryByTestId("cell-problem")).toBeNull();
    expect(screen.getByTestId("run-problem")).toHaveClass("nb-notice--danger");
  });

  it("shows nothing once there is no problem to show", () => {
    const rig = setup({ rt: runtime({}, { runProblem: { run_id: "r1", message: "The run did not start.", tone: "warning", cells: "all" } }) });
    rig.rerender({ runtime: runtime({}, { runProblem: null }) });
    expect(screen.queryByTestId("run-problem")).toBeNull();
    expect(screen.queryAllByTestId("cell-problem")).toEqual([]);
  });
});

describe("a cell's kind label", () => {
  it("names every kind the same way, Python included", () => {
    const { root } = setup({
      cells: [
        { id: "p1", source: "x = 1" },
        { id: "q1", kind: "sql", source: "select 1" },
        { id: "m1", kind: "markdown", source: "# Notes" },
      ],
    });
    const kinds = Array.from(root.querySelectorAll(".nb-cell__kind")).map((el) => el.textContent);
    expect(kinds).toEqual(["Cell 1 · Python", "Cell 2 · SQL", "Cell 3 · Markdown"]);
  });
});

describe("the theme the editor is given", () => {
  it("is stamped on the editor and on each output, and follows a change", () => {
    const out = [{ output_id: "o1", type: "stream" as const, name: "stdout" as const, text: "hello" }];
    const { root, rerender } = setup({ rt: runtime({ c1: { outputs: out } }) });
    const outputs = () => [...root.querySelectorAll<HTMLElement>(".nb-output-area, .nb-output")].map((el) => el.dataset.nbTheme);
    expect(root.dataset.nbTheme).toBe("light");
    expect(outputs().length).toBeGreaterThanOrEqual(1);
    expect(new Set(outputs())).toEqual(new Set(["light"]));
    rerender({ output: { theme: "dark" } });
    expect(root.dataset.nbTheme).toBe("dark");
    expect(new Set(outputs())).toEqual(new Set(["dark"]));
    rerender({ output: { theme: "light" } });
    expect(root.dataset.nbTheme).toBe("light");
  });

  it("colours code by token class, never by a fixed colour", () => {
    const { root } = setup();
    const code = root.querySelector<HTMLElement>(".cm-content")!;
    const marked = [...code.querySelectorAll<HTMLElement>("span[class]")];
    expect(marked.some((span) => span.classList.contains("nb-tok-number") || span.classList.contains("nb-tok-name") || span.classList.contains("nb-tok-punct"))).toBe(true);
    for (const span of marked) expect(span.getAttribute("style")).toBeNull();
  });
});

describe("a notice", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("that only reports news leaves on its own; one that asks for a look stays", () => {
    const dismissed: string[] = [];
    const { rerender } = setup();
    const actions: NotebookActions = { run: () => {}, confirmRun: () => {}, cancelRun: () => {}, kernel: () => {}, switchEnv: () => {}, install: () => {}, dismissNotice: (id: string) => void dismissed.push(id) };
    rerender({ actions, runtime: runtime({}, { notice: { id: "k1", message: "Kernel restarted.", tone: "info" } }) });
    act(() => vi.advanceTimersByTime(INFO_NOTICE_MS + 10));
    expect(dismissed).toEqual(["k1"]);

    rerender({ actions, runtime: runtime({}, { notice: { id: "w1", message: "The kernel stopped.", tone: "danger" } }) });
    act(() => vi.advanceTimersByTime(INFO_NOTICE_MS * 3));
    expect(dismissed).toEqual(["k1"]);
  });
});

describe("the find button", () => {
  it("opens find and, pressed again, closes it", () => {
    setup();
    const button = screen.getByRole("button", { name: "Find" });
    fireEvent.click(button);
    expect(screen.getByPlaceholderText("Find")).toBeInTheDocument();
    fireEvent.click(button);
    expect(screen.queryByPlaceholderText("Find")).toBeNull();
  });
});

describe("a change the document refuses", () => {
  /** A document refusing kind changes as the live document does, by code. */
  class Refusing extends MemoryNotebook {
    constructor(private readonly code: string) {
      super([{ id: "c1", source: 's = """x"""' }]);
    }
    override apply(ops: Parameters<MemoryNotebook["apply"]>[0]): { created: string[] } {
      if (ops.some((op) => op.op === "set_kind")) throw Object.assign(new Error(this.code), { code: this.code });
      return super.apply(ops);
    }
  }

  it.each([
    ["not_representable", "A SQL or Markdown cell can't hold three double quotes in a row."],
    ["cell_not_found", "That change could not be made. The notebook changed meanwhile; try again."],
  ])("(%s) says why", (code, sentence) => {
    const doc = new Refusing(code);
    const view = render(
      <NotebookEditor
        doc={doc}
        text={doc}
        runtime={runtime()}
        actions={{ run: () => {}, confirmRun: () => {}, cancelRun: () => {}, kernel: () => {}, switchEnv: () => {}, install: () => {} }}
        permissions={{ canEdit: true, canRun: true }}
        output={{ theme: "light" }}
        narrow={false}
        platform="other"
        now={() => NOW}
      />,
    );
    press(view.container.querySelector<HTMLElement>(".nb-notebook")!, "m");
    expect(screen.getByText(sentence)).toBeInTheDocument();
    expect(doc.snapshot().cells[0]?.kind).toBe("python");
  });
});

describe("adding a cell at the end", () => {
  it.each([false, true])("offers Add a cell under the last cell (narrow=%s) and adds one there", (narrow) => {
    const { doc } = setup({ narrow });
    fireEvent.click(screen.getByRole("button", { name: "Add a cell" }));
    const cells = doc.snapshot().cells;
    expect(cells).toHaveLength(4);
    expect(cells.slice(0, 3).map((c) => c.id)).toEqual(["c1", "c2", "c3"]);
  });

  it("offers none to someone who may not edit", () => {
    setup({ canEdit: false });
    expect(screen.queryByRole("button", { name: "Add a cell" })).toBeNull();
  });
});
