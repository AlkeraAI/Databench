// Select-all must never claim a folder it did not select.
//
// A 3,200-row folder pages in 500 rows at a time. Cmd+A selects the rows that
// are paged in — and while the bar reported that number as a flat total, a
// person could select 1,000 rows in a 3,200-row folder, read "1000 items", and
// send Move to trash believing they had trashed the folder. The bar therefore
// says how much of the folder the selection covers whenever there is more of it
// than is loaded, and offers the one control that makes the selection whole.

import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { Item } from "@/api/files";
import { FILES_SOFT_THRESHOLD_ROWS } from "@/lib/limits";
import {
  SelectionBar,
  describeSelection,
  summarize,
} from "@/pages/workspace/files/SelectionBar";

function row(id: string): Item {
  return {
    id,
    ino: 1,
    driveId: "drv_1",
    kind: "folder",
    name: id,
    nameDisplay: id,
    nameEncoding: "utf-8",
    pathBytes: "",
    parentId: "nd_folder",
    etag: `e-${id}`,
    ctag: `c-${id}`,
    stale: false,
    locked: false,
    held: false,
    shared: false,
    starred: false,
    trashed: false,
  } as Item;
}

function rows(count: number): Item[] {
  return Array.from({ length: count }, (_, at) => row(`m-${at}`));
}

describe("what the selection bar claims", () => {
  it("names the folder's whole size when the selection is only what is paged in", () => {
    const selected = rows(1000);
    expect(
      describeSelection(summarize(selected), { loaded: 1000, total: 3200, hasMore: true }),
    ).toBe("1,000 of 3,200 items selected");
  });

  it("says the folder holds more when its total is not known yet", () => {
    const selected = rows(500);
    expect(
      describeSelection(summarize(selected), { loaded: 500, total: null, hasMore: true }),
    ).toBe("500 items selected (the folder holds more)");
  });

  it("writes a thousand the same way in the sentence and in the button", () => {
    // One locale for the whole bar: `1,000 of 3,200 items selected` then, after
    // Select all, `3200 items` was the same bar disagreeing with itself.
    expect(
      describeSelection(summarize(rows(3200)), { loaded: 3200, total: 3200, hasMore: false }),
    ).toBe("3,200 items");
  });

  it("reports a flat count once every row is paged in", () => {
    const selected = rows(3200);
    expect(
      describeSelection(summarize(selected), { loaded: 3200, total: 3200, hasMore: false }),
    ).toBe("3,200 items");
  });

  it("reports a flat count for a selection that is not the whole loaded listing", () => {
    // Three rows picked by hand out of a thousand say nothing about the folder:
    // the reader chose them, so there is no whole they might be mistaken for.
    const selected = rows(3);
    expect(
      describeSelection(summarize(selected), { loaded: 1000, total: 3200, hasMore: true }),
    ).toBe("3 items");
  });

  it("offers the whole folder, and asks for it by its real size", () => {
    const rest = vi.fn();
    render(
      <SelectionBar
        selection={rows(1000)}
        menuItems={[]}
        scope={{ loaded: 1000, total: 3200, hasMore: true }}
        onSelectRest={rest}
      />,
    );
    const button = screen.getByRole("button", { name: "Select all 3,200" });
    button.click();
    expect(rest).toHaveBeenCalledTimes(1);
  });

  it("does not offer a folder it would have to page past the listing's own ceiling", () => {
    // `Select all` pages the rest in and holds every id in memory, so a folder
    // larger than the listing's ceiling is one the button must not promise. The
    // count still tells the truth about what IS selected.
    const rest = vi.fn();
    render(
      <SelectionBar
        selection={rows(1000)}
        menuItems={[]}
        scope={{ loaded: 1000, total: FILES_SOFT_THRESHOLD_ROWS + 1, hasMore: true }}
        onSelectRest={rest}
      />,
    );
    expect(screen.queryByRole("button", { name: /^Select all/ })).toBeNull();
    expect(screen.getByRole("status").textContent).toBe("1,000 of 20,001 items selected");
  });

  it("offers a folder that sits exactly on the ceiling", () => {
    render(
      <SelectionBar
        selection={rows(1000)}
        menuItems={[]}
        scope={{ loaded: 1000, total: FILES_SOFT_THRESHOLD_ROWS, hasMore: true }}
        onSelectRest={vi.fn()}
      />,
    );
    expect(screen.getByRole("button", { name: "Select all 20,000" })).toBeInTheDocument();
  });

  it("offers nothing more to select once the folder is whole", () => {
    render(
      <SelectionBar
        selection={rows(3200)}
        menuItems={[]}
        scope={{ loaded: 3200, total: 3200, hasMore: false }}
        onSelectRest={vi.fn()}
      />,
    );
    expect(screen.queryByRole("button", { name: /^Select all/ })).toBeNull();
    expect(screen.getByRole("status").textContent).toBe("3,200 items");
  });

  it("reads a partial selection out loud, so a trash is not sent on a wrong belief", () => {
    render(
      <SelectionBar
        selection={rows(1000)}
        menuItems={[]}
        scope={{ loaded: 1000, total: 3200, hasMore: true }}
        onSelectRest={vi.fn()}
      />,
    );
    expect(screen.getByRole("status").textContent).toBe("1,000 of 3,200 items selected");
  });
});
