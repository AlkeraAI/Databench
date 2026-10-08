// A plain single click on a file row is a passing preview, for a host that
// shows files beside the listing. A modified click is a selection gesture, the
// second click of a double click is the open, and a folder is never previewed.

import { QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { Item } from "@/api/files";
import { createQueryClient } from "@/api/queryClient";
import { Treegrid } from "@/pages/workspace/files/Treegrid";
import { emptySelection } from "@/pages/workspace/files/state/selection";

function item(id: string, name: string, kind: "file" | "folder" = "file"): Item {
  return {
    id,
    ino: 7,
    driveId: "dr_1",
    kind,
    name,
    nameDisplay: name,
    nameEncoding: "utf-8",
    etag: "et_1",
    attrs: { mtime: "2026-03-04T10:00:00Z" },
    file: kind === "file" ? { size: 4096 } : null,
  } as Item;
}

const NOTE = item("nd_note", "note.md");
const FOLDER = item("nd_dir", "charts", "folder");

function draw(onPreview?: (row: Item) => void, onOpen = vi.fn()) {
  render(
    <QueryClientProvider client={createQueryClient()}>
      <Treegrid
        rows={[FOLDER, NOTE]}
        view="list"
        selection={emptySelection}
        onSelectionAction={vi.fn()}
        orderBy={{ field: "name", direction: "asc" }}
        onSort={vi.fn()}
        onOpen={onOpen}
        {...(onPreview ? { onPreview } : {})}
        platform="mac"
        initialRect={{ width: 1200, height: 480 }}
      />
    </QueryClientProvider>,
  );
  return onOpen;
}

function row(id: string): HTMLElement {
  const el = document.querySelector<HTMLElement>(`[data-row-id="${id}"]`);
  if (!el) throw new Error(`no row for ${id}`);
  return el;
}

afterEach(cleanup);

describe("a single click on a row", () => {
  it("previews a file", () => {
    const onPreview = vi.fn();
    draw(onPreview);
    fireEvent.click(row(NOTE.id), { detail: 1 });
    expect(onPreview).toHaveBeenCalledWith(NOTE);
  });

  it.each([
    ["Shift", { shiftKey: true }],
    ["Cmd", { metaKey: true }],
    ["Ctrl", { ctrlKey: true }],
    ["Alt", { altKey: true }],
  ])("with %s held only selects", (_label, modifiers) => {
    const onPreview = vi.fn();
    draw(onPreview);
    fireEvent.click(row(NOTE.id), { detail: 1, ...modifiers });
    expect(onPreview).not.toHaveBeenCalled();
  });

  it("never previews a folder", () => {
    const onPreview = vi.fn();
    draw(onPreview);
    fireEvent.click(row(FOLDER.id), { detail: 1 });
    expect(onPreview).not.toHaveBeenCalled();
  });

  it("previews once across a double click, which then opens", () => {
    const onPreview = vi.fn();
    const onOpen = draw(onPreview);
    fireEvent.click(row(NOTE.id), { detail: 1 });
    fireEvent.click(row(NOTE.id), { detail: 2 });
    fireEvent.doubleClick(row(NOTE.id));
    expect(onPreview).toHaveBeenCalledTimes(1);
    expect(onOpen).toHaveBeenCalledWith(NOTE);
  });

  it("does nothing more for a host that shows no previews", () => {
    const onOpen = draw(undefined);
    fireEvent.click(row(NOTE.id), { detail: 1 });
    expect(onOpen).not.toHaveBeenCalled();
  });
});
