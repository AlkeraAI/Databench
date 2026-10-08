// A link from elsewhere (a chat's "Go to cell") lands on its cell: the editor
// selects it, scrolls it into view and marks it for a moment. A cell the
// notebook does not hold moves nothing, and asking again reveals again.
import { act, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ABSENT_KERNEL, type RunTarget } from "../model/types";
import { registerDefaultOutputRenderers } from "../outputs/defaults";
import { MemoryNotebook } from "./memoryHost";
import { NotebookEditor, type NotebookEditorProps } from "./NotebookEditor";

registerDefaultOutputRenderers();

const CELLS = [
  { id: "aaaaaaaaaa", source: "x = 1" },
  { id: "bbbbbbbbbb", source: "y = x + 1" },
  { id: "cccccccccc", source: "print(y)" },
];

let scrolled: string[] = [];

beforeEach(() => {
  vi.useFakeTimers();
  scrolled = [];
  Element.prototype.scrollIntoView = function (this: Element) {
    scrolled.push(this.getAttribute("data-cell-id") ?? "");
  };
});

afterEach(() => {
  vi.useRealTimers();
});

function editor(revealCell: NotebookEditorProps["revealCell"], doc = new MemoryNotebook(CELLS)) {
  const runs: RunTarget[] = [];
  const props: NotebookEditorProps = {
    doc,
    text: doc,
    runtime: {
      cells: {},
      kernel: { ...ABSENT_KERNEL, state: "idle" },
      graph: { edges: [], errors: {} },
      presence: [],
      confirmation: null,
      envs: [],
      packages: [],
      installing: false,
      notice: null,
    },
    actions: { run: (t) => runs.push(t), confirmRun: () => {}, cancelRun: () => {}, kernel: () => {}, switchEnv: () => {}, install: () => {} },
    permissions: { canEdit: true, canRun: true },
    output: { theme: "light" },
    platform: "other",
    revealCell,
  };
  const view = render(<NotebookEditor {...props} />);
  return {
    root: screen.getByTestId("notebook-editor"),
    reveal: (next: NotebookEditorProps["revealCell"]) => view.rerender(<NotebookEditor {...props} revealCell={next} />),
  };
}

const cell = (root: HTMLElement, id: string) => root.querySelector<HTMLElement>(`[data-cell-id="${id}"]`)!;
const flush = () => act(() => vi.advanceTimersByTime(20));

describe("revealing a cell from a link", () => {
  it("selects the cell, scrolls it into view and marks it for a moment", () => {
    const { root } = editor({ id: "cccccccccc", seq: 1 });
    flush();
    expect(cell(root, "cccccccccc").getAttribute("aria-current")).toBe("true");
    expect(scrolled).toEqual(["cccccccccc"]);
    expect(cell(root, "cccccccccc").hasAttribute("data-revealed")).toBe(true);
    expect(cell(root, "aaaaaaaaaa").hasAttribute("data-revealed")).toBe(false);
    act(() => vi.advanceTimersByTime(2_000));
    expect(cell(root, "cccccccccc").hasAttribute("data-revealed")).toBe(false);
    expect(cell(root, "cccccccccc").getAttribute("aria-current")).toBe("true");
  });

  it("reveals a cell asked for while the notebook is open, and the same cell again on a new ask", () => {
    const { root, reveal } = editor(null);
    flush();
    expect(scrolled).toEqual([]);
    reveal({ id: "bbbbbbbbbb", seq: 1 });
    flush();
    expect(cell(root, "bbbbbbbbbb").getAttribute("aria-current")).toBe("true");
    reveal({ id: "bbbbbbbbbb", seq: 2 });
    flush();
    expect(scrolled).toEqual(["bbbbbbbbbb", "bbbbbbbbbb"]);
  });

  it("waits for a cell the notebook does not hold yet, and reveals it once it arrives", () => {
    const doc = new MemoryNotebook(CELLS.slice(0, 1));
    const { root, reveal } = editor(null, doc);
    let created = "";
    act(() => {
      created = doc.apply([{ op: "insert", source: "z = 3" }]).created[0];
    });
    const asked = { id: "zzzzzzzzzz", seq: 1 };
    reveal(asked);
    flush();
    // Nothing here holds that cell: nothing moves.
    expect(scrolled).toEqual([]);
    expect(root.querySelector("[data-revealed]")).toBeNull();
    reveal({ id: created, seq: 2 });
    flush();
    expect(scrolled).toEqual([created]);
    expect(cell(root, created).hasAttribute("data-revealed")).toBe(true);
  });
});
