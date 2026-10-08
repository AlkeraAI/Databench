// @vitest-environment node
//
// A file the machine holding its folder has written and the drive has not yet
// received is listed before any byte of it lands. Its Size and Modified cells
// read the machine's disk until then, and the drive's own copy from the moment
// a head version exists — even when the machine has since moved past it.

import { describe, expect, it } from "vitest";

import type { Item } from "@/api/files";
import { modifiedOf, sizeOf } from "@/lib/files/columns";

const ON_MACHINE = "2026-09-23T10:15:00Z";
const ON_DRIVE = "2026-09-22T08:00:00Z";

function row(over: Record<string, unknown>): Item {
  return {
    id: "n1",
    kind: "file",
    name: "README.md",
    attrs: { mtime: ON_DRIVE },
    ...over,
  } as unknown as Item;
}

const holder = {
  state: "writing",
  content: "unlanded",
  holder_size: 4096,
  holder_mtime: ON_MACHINE,
};

describe("a row's size and time before its bytes land", () => {
  it.each([
    {
      name: "a file with no head version reads the machine's disk",
      item: row({ file: { size: 0, content_hash: "" }, live: holder }),
      size: 4096,
      at: ON_MACHINE,
    },
    {
      name: "a file with no file facet at all reads the machine's disk",
      item: row({ file: null, live: holder }),
      size: 4096,
      at: ON_MACHINE,
    },
    {
      name: "a file whose bytes landed reads its own, even with the machine ahead",
      item: row({
        file: { size: 900, content_hash: "b3:aa" },
        live: { ...holder, content: "behind" },
      }),
      size: 900,
      at: ON_DRIVE,
    },
    {
      name: "a file with no holder facet reads its own",
      item: row({ file: { size: 12, content_hash: "" }, live: null }),
      size: 12,
      at: ON_DRIVE,
    },
    {
      name: "a holder facet with no numbers leaves the row's own",
      item: row({
        file: { size: 12, content_hash: "" },
        live: { state: "writing", content: "unlanded", holder_size: null, holder_mtime: "" },
      }),
      size: 12,
      at: ON_DRIVE,
    },
  ])("$name", ({ item, size, at }) => {
    expect(sizeOf(item)).toBe(size);
    expect(modifiedOf(item).at).toBe(at);
  });

  it("a folder keeps its aggregated size and its own time", () => {
    const folder = row({
      kind: "folder",
      dirStats: { totalBytes: 77 },
      live: holder,
    });
    expect(sizeOf(folder)).toBe(77);
    expect(modifiedOf(folder).at).toBe(ON_DRIVE);
  });
});
