import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactElement } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { BlobPage, BlobReference } from "@alkera/chat-model";

import { ReferenceStoreProvider, type ReferenceActionsState } from "../ReferenceStoreProvider";
import { ReferenceList } from "./ReferenceList";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

/** Render a node under a reference-actions store seeded with `actions` — the
 *  consumers (chips, rows) read fetchBlob/onOpen/onExport there. */
function renderWithActions(node: ReactElement, actions?: ReferenceActionsState) {
  return render(<ReferenceStoreProvider actions={actions}>{node}</ReferenceStoreProvider>);
}

function rowsPage(offset: number, limit = 100, total = 250): BlobPage {
  const end = Math.min(offset + limit, total);
  const rows: unknown[][] = [];
  for (let i = offset; i < end; i += 1) rows.push([i, `row-${i}`]);
  return {
    kind: "rows",
    columns: ["id", "label"],
    rows,
    text: "",
    offset,
    limit,
    total,
    returned: rows.length,
    hasMore: end < total,
    nextOffset: end < total ? end : null,
  };
}

describe("ReferenceList / ReferenceChip", () => {
  const reference: BlobReference = { handle: "sha-1", name: "Q3 revenue" };

  it.each([undefined, []])("renders nothing for %s references", (references) => {
    const { container } = renderWithActions(<ReferenceList references={references} />);
    expect(container.firstChild).toBeNull();
  });

  it("renders a named chip and opens it via the store's onOpen", () => {
    const onOpen = vi.fn();
    renderWithActions(<ReferenceList references={[reference]} />, { onOpen });
    const chip = screen.getByRole("button", { name: /open Q3 revenue/i });
    expect(screen.getByText("Q3 revenue")).toBeInTheDocument();
    fireEvent.click(chip);
    expect(onOpen).toHaveBeenCalledWith(reference);
  });

  it("lazily fetches the first page for the hover preview, once", async () => {
    const fetchBlob = vi.fn(async () => rowsPage(0, 50, 3));
    const { container } = renderWithActions(<ReferenceList references={[reference]} />, {
      fetchBlob,
      onOpen: vi.fn(),
    });
    const wrap = container.querySelector(".alk-refstore-chip-wrap") as HTMLElement;
    fireEvent.mouseEnter(wrap);
    await waitFor(() => expect(fetchBlob).toHaveBeenCalledWith("sha-1", 0, 50));
    // Re-hovering doesn't refetch.
    fireEvent.mouseEnter(wrap);
    expect(fetchBlob).toHaveBeenCalledTimes(1);
  });

  it("focus opens the anchored preview panel", async () => {
    const fetchBlob = vi.fn(async () => rowsPage(0, 50, 3));
    renderWithActions(<ReferenceList references={[reference]} />, { fetchBlob, onOpen: vi.fn() });
    // The popover opens on focus with no dwell timer, and the chip wrap preloads on the same event.
    fireEvent.focusIn(screen.getByRole("button", { name: /open Q3 revenue/i }));
    const panel = await screen.findByRole("dialog", { name: "Preview of Q3 revenue" });
    expect(panel.textContent).toContain("3 rows × 2 cols");
    expect(panel.querySelector(".alk-blobv-table")).not.toBeNull();
  });

  it("a chip with no wired actions is inert", () => {
    // An empty store and no Provider at all must land the same way: a disabled
    // button rather than a click that fires an undefined callback.
    const { unmount } = renderWithActions(<ReferenceList references={[reference]} />);
    expect(screen.getByRole("button", { name: "Q3 revenue" })).toBeDisabled();
    unmount();

    render(<ReferenceList references={[reference]} />);
    expect(screen.getByRole("button", { name: "Q3 revenue" })).toBeDisabled();
    expect(screen.queryByRole("button", { name: /export/i })).not.toBeInTheDocument();
  });

  it("the export affordance needs onExport", () => {
    const onExport = vi.fn();
    // No onExport → no download button.
    const { unmount } = renderWithActions(<ReferenceList references={[reference]} />, { onOpen: vi.fn() });
    expect(screen.queryByRole("button", { name: /export Q3 revenue/i })).not.toBeInTheDocument();
    unmount();

    renderWithActions(<ReferenceList references={[reference]} />, { onExport });
    const exportBtn = screen.getByRole("button", { name: /export Q3 revenue/i });
    fireEvent.click(exportBtn);
    expect(onExport).toHaveBeenCalledWith(reference);
  });
});

describe("ReferenceList list variant (artifact rows)", () => {
  const reference: BlobReference = { handle: "sha-2", name: "metrics.csv", refType: "rows" };

  it("a row shows its kind detail and opens via onOpen", () => {
    const onOpen = vi.fn();
    renderWithActions(<ReferenceList references={[reference]} variant="list" />, { onOpen });
    expect(screen.getByText("metrics.csv")).toBeInTheDocument();
    // Before any fetch, the row shows the declared refType as its detail.
    expect(screen.getByText("rows")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /open metrics\.csv/i }));
    expect(onOpen).toHaveBeenCalledWith(reference);
  });

  it("the row download calls onExport", () => {
    const onExport = vi.fn();
    renderWithActions(<ReferenceList references={[reference]} variant="list" />, { onExport, onOpen: vi.fn() });
    fireEvent.click(screen.getByRole("button", { name: /download metrics\.csv/i }));
    expect(onExport).toHaveBeenCalledWith(reference);
  });

  it("a row with no wired actions is inert", () => {
    // No onOpen → the main button falls back to the bare name and is inert,
    // matching the chip variant rather than firing an undefined callback.
    const { unmount } = renderWithActions(<ReferenceList references={[reference]} variant="list" />);
    expect(screen.getByRole("button", { name: "metrics.csv" })).toBeDisabled();
    expect(screen.queryByRole("button", { name: /download metrics\.csv/i })).not.toBeInTheDocument();
    unmount();

    render(<ReferenceList references={[reference]} variant="list" />);
    expect(screen.getByRole("button", { name: "metrics.csv" })).toBeDisabled();
    expect(screen.queryByRole("button", { name: /download metrics\.csv/i })).not.toBeInTheDocument();
  });

  it("routes each row's action to its own reference", () => {
    // Two distinct rows: a single-row test can't catch a row that hardwires
    // every callback to references[0]. The right handle must reach each callback.
    const first: BlobReference = { handle: "sha-a", name: "alpha.csv", refType: "rows" };
    const second: BlobReference = { handle: "sha-b", name: "beta.txt", refType: "text" };
    const onOpen = vi.fn();
    const onExport = vi.fn();
    renderWithActions(<ReferenceList references={[first, second]} variant="list" />, { onOpen, onExport });

    expect(screen.getByText("alpha.csv")).toBeInTheDocument();
    expect(screen.getByText("beta.txt")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /open beta\.txt/i }));
    expect(onOpen).toHaveBeenCalledTimes(1);
    expect(onOpen).toHaveBeenCalledWith(second);

    fireEvent.click(screen.getByRole("button", { name: /download alpha\.csv/i }));
    expect(onExport).toHaveBeenCalledTimes(1);
    expect(onExport).toHaveBeenCalledWith(first);
  });
});
