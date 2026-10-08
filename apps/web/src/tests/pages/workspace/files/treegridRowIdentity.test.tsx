// A row's element belongs to the NODE, not to the position it happens to be in.
//
// The virtualizer keys a lane by its index by default, so deleting a row above
// the one a reader is on handed the survivors each other's DOM elements: the
// browser's focus stayed on an element that now drew a different file, and the
// inline rename editor — a live <input> inside that element — carried on holding
// what was typed for the row above the one it now belonged to. Both are silent:
// nothing on screen says the caret moved to another node.

import { QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { Item } from "@/api/files";
import { createQueryClient } from "@/api/queryClient";
import { Treegrid } from "@/pages/workspace/files/Treegrid";
import { emptySelection, type SelectionState } from "@/pages/workspace/files/state/selection";

function item(id: string, name: string): Item {
  return {
    id,
    ino: 7,
    driveId: "dr_1",
    kind: "file",
    name,
    nameDisplay: name,
    nameEncoding: "utf-8",
    etag: "et_1",
    attrs: { mtime: "2026-03-04T10:00:00Z" },
    file: { size: 4096 },
  } as Item;
}

const A = item("nd_a", "a.csv");
const B = item("nd_b", "b.csv");
const C = item("nd_c", "c.csv");

function selectionOn(id: string): SelectionState {
  return { ...emptySelection, selected: new Set([id]), focusedId: id, anchorId: id };
}

/** The grid, with `subject` rendering a live editor in its name cell — the shape
 *  an inline rename takes. */
function draw(rows: readonly Item[], focused: string, subject: string) {
  return (
    <QueryClientProvider client={createQueryClient()}>
      <Treegrid
        rows={rows}
        view="list"
        selection={selectionOn(focused)}
        onSelectionAction={vi.fn()}
        orderBy={{ field: "name", direction: "asc" }}
        onSort={vi.fn()}
        platform="mac"
        initialRect={{ width: 1200, height: 480 }}
        renderNameOverride={(row) =>
          row.id === subject ? <input aria-label="New name" defaultValue={row.name} /> : null
        }
      />
    </QueryClientProvider>
  );
}

function rowFor(id: string): HTMLElement {
  const el = document.querySelector<HTMLElement>(`[data-row-id="${id}"]`);
  if (!el) throw new Error(`no row for ${id}`);
  return el;
}

afterEach(cleanup);

describe("a row that survives a deletion above it", () => {
  it("keeps its own element, so focus does not hop to the next file", () => {
    const view = render(draw([A, B, C], B.id, "none"));
    const before = rowFor(B.id);
    before.focus();
    expect(document.activeElement).toBe(before);

    // The row above is trashed by somebody else and the listing comes back one
    // row shorter. Nothing about b.csv changed.
    view.rerender(draw([B, C], B.id, "none"));

    expect(rowFor(B.id)).toBe(before);
    expect(document.activeElement).toBe(rowFor(B.id));
    // The negative half: the element the reader is on must not now be c.csv.
    expect(document.activeElement?.textContent).toContain("b.csv");
  });

  it("does not throw away the inline rename editor the reader is typing in", () => {
    const view = render(draw([A, B, C], B.id, B.id));
    const field = screen.getByRole("textbox", { name: "New name" });
    field.focus();
    expect(document.activeElement).toBe(field);

    view.rerender(draw([B, C], B.id, B.id));

    // The SAME input, not a replacement holding the same text: an editor
    // rebuilt under the reader loses the caret, the selection and — for the
    // real editor, which holds its draft in state — everything typed so far.
    const after = screen.getByRole("textbox", { name: "New name" });
    expect(after).toBe(field);
    expect(document.activeElement).toBe(after);
    expect(rowFor(B.id).contains(after)).toBe(true);
  });

  it("gives a row inserted above it no claim on its element either", () => {
    const view = render(draw([B, C], B.id, "none"));
    const before = rowFor(B.id);

    view.rerender(draw([A, B, C], B.id, "none"));

    expect(rowFor(B.id)).toBe(before);
    expect(rowFor(A.id)).not.toBe(before);
  });
});
