import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { BlobPage, BlobReference } from "@alkera/chat-model";

import { BlobView } from "./BlobView";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

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

describe("BlobView (paginated, read-only)", () => {
  const reference: BlobReference = { handle: "sha-1", name: "Big table" };

  const countText = (container: HTMLElement) =>
    container.querySelector(".alk-datatable-pager__count")?.textContent ?? "";

  it("walks pages with the pager", async () => {
    const fetchBlob = vi.fn(async (_handle: string, offset: number, limit?: number) => rowsPage(offset, limit ?? 100));
    const { container } = render(<BlobView reference={reference} fetchBlob={fetchBlob} />);

    // First fetch uses the daemon default (no limit).
    await waitFor(() => expect(fetchBlob).toHaveBeenCalledWith("sha-1", 0, undefined));
    await waitFor(() => expect(countText(container)).toContain("showing 1–100 of 250"));
    expect(screen.getByRole("button", { name: "Previous page" })).toBeDisabled();

    fireEvent.click(screen.getByRole("button", { name: "Next page" }));
    await waitFor(() => expect(countText(container)).toContain("showing 101–200 of 250"));
    expect(fetchBlob).toHaveBeenLastCalledWith("sha-1", 100, 100);

    fireEvent.click(screen.getByRole("button", { name: "Next page" }));
    await waitFor(() => expect(countText(container)).toContain("showing 201–250 of 250"));
    // Last page → Next disabled.
    expect(screen.getByRole("button", { name: "Next page" })).toBeDisabled();

    fireEvent.click(screen.getByRole("button", { name: "Previous page" }));
    await waitFor(() => expect(countText(container)).toContain("showing 101–200 of 250"));
  });

  it("pages by the size the daemon returned", async () => {
    // The first fetch sends no limit, so the daemon returns ITS default
    // page (DEFAULT_ROW_PAGE = 50) and reports page.limit = 50. The pager must page
    // by THAT — pageCount = ceil(250/50) = 5, Next → offset 50 — not by a hardcoded
    // client size of 100, which would label "of 3" and skip rows 50–99 jumping to
    // offset 100. (The other tests use a mock whose page happens to be 100, which is
    // why they can't catch a 100 constant; here the server pages by 50.)
    const fetchBlob = vi.fn(async (_handle: string, offset: number, limit?: number) =>
      rowsPage(offset, limit ?? 50, 250),
    );
    const { container } = render(<BlobView reference={reference} fetchBlob={fetchBlob} />);
    await waitFor(() => expect(countText(container)).toContain("showing 1–50 of 250"));
    expect(screen.getByLabelText("Page number").closest(".alk-datatable-pager__pagelabel")?.textContent).toContain(
      "of 5",
    );

    fireEvent.click(screen.getByRole("button", { name: "Next page" }));
    await waitFor(() => expect(fetchBlob).toHaveBeenLastCalledWith("sha-1", 50, 50));
    await waitFor(() => expect(countText(container)).toContain("showing 51–100 of 250"));
  });

  it("jumps to a typed page, clamping past the end", async () => {
    // 250 rows / 100 per page = 3 pages. Typing a page number must refetch from
    // that page's offset — page 2 → offset (2-1)·100 = 100.
    const fetchBlob = vi.fn(async (_handle: string, offset: number, limit?: number) => rowsPage(offset, limit ?? 100));
    const { container } = render(<BlobView reference={reference} fetchBlob={fetchBlob} />);
    await waitFor(() => expect(countText(container)).toContain("showing 1–100 of 250"));

    const input = screen.getByLabelText("Page number");
    // The total page count is shown beside the input.
    expect(input.closest(".alk-datatable-pager__pagelabel")?.textContent).toContain("of 3");

    fireEvent.change(input, { target: { value: "2" } });
    fireEvent.blur(input);
    await waitFor(() => expect(fetchBlob).toHaveBeenLastCalledWith("sha-1", 100, 100));
    await waitFor(() => expect(countText(container)).toContain("showing 101–200 of 250"));

    // 99 clamps to page 3 → offset 200, NOT 9800.
    const jump = screen.getByLabelText("Page number");
    fireEvent.change(jump, { target: { value: "99" } });
    fireEvent.blur(jump);
    await waitFor(() => expect(fetchBlob).toHaveBeenLastCalledWith("sha-1", 200, 100));
    await waitFor(() => expect(countText(container)).toContain("showing 201–250 of 250"));
  });

  it("paginates text by character", async () => {
    const body = "x".repeat(20000);
    const fetchBlob = vi.fn(async (_handle: string, offset: number, limit?: number) => {
      const size = limit ?? 8000;
      const chunk = body.slice(offset, offset + size);
      return {
        kind: "text" as const,
        columns: [],
        rows: [],
        text: chunk,
        offset,
        limit: size,
        total: body.length,
        returned: chunk.length,
        hasMore: offset + chunk.length < body.length,
        nextOffset: offset + chunk.length < body.length ? offset + chunk.length : null,
      };
    });
    const { container } = render(<BlobView reference={reference} fetchBlob={fetchBlob} />);
    await waitFor(() => expect(countText(container)).toContain("characters"));
    expect(countText(container)).toContain("of 20,000 characters");
    // 20,000 chars / 8,000 per page = ceil → 3 pages.
    expect(screen.getByLabelText("Page number").closest(".alk-datatable-pager__pagelabel")?.textContent).toContain(
      "of 3",
    );
  });

  it("a single-page blob has no nav", async () => {
    // total (40) ≤ limit (100) ⇒ one page. pageCount = ceil(40/100) = 1, so the
    // nav must be hidden; the range still reads the real bounds, not "1–100".
    const fetchBlob = vi.fn(async (_handle: string, offset: number, limit?: number) =>
      rowsPage(offset, limit ?? 100, 40),
    );
    const { container } = render(<BlobView reference={reference} fetchBlob={fetchBlob} />);
    await waitFor(() => expect(countText(container)).toContain("showing 1–40 of 40"));
    expect(screen.queryByLabelText("Page number")).toBeNull();
    expect(screen.queryByRole("button", { name: "Next page" })).toBeNull();
  });

  it("an empty blob reads 0–0 of 0, never 1–0", async () => {
    // The total===0 guard: with offset 0 / returned 0 the naive `offset+1` would
    // print "showing 1–0 of 0". A 0-total blob must read 0–0 of 0.
    const fetchBlob = vi.fn(async (_handle: string, offset: number, limit?: number) =>
      rowsPage(offset, limit ?? 100, 0),
    );
    const { container } = render(<BlobView reference={reference} fetchBlob={fetchBlob} />);
    await waitFor(() => expect(countText(container)).toContain("showing 0–0 of 0"));
    expect(screen.queryByRole("button", { name: "Next page" })).toBeNull();
  });

  it("surfaces a blob error inline", async () => {
    const fetchBlob = vi.fn(async () => {
      throw new Error("no result blob 'sha-1'");
    });
    render(<BlobView reference={reference} fetchBlob={fetchBlob} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("no result blob 'sha-1'");
  });
});
