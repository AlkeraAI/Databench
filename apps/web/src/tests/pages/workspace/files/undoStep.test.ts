import { describe, expect, it } from "vitest";

import { ApiError } from "@/api/errors";
import { filesErrorCopy } from "@/lib/files/errors";
import { undoStep, type LandedWrite } from "@/pages/workspace/files/undo";

// Undo may only promise what the server will do. The server says, per operation,
// whether it recorded an inverse; a copy records none, so Cmd+Z after a copy used
// to be a promise that did nothing at all when it was kept.

function write(over: Partial<LandedWrite> = {}): LandedWrite {
  return {
    kind: "trash",
    driveId: "dr_1",
    operationId: "op_1",
    undoable: true,
    label: "Moved 1 item to trash",
    items: [{ id: "nd_1", etag: "et_1" }],
    ...over,
  };
}

describe("what a landed write earns as history", () => {
  it("records an operation the server says it can reverse", () => {
    expect(undoStep(write({ undoableUntil: "2026-01-01T00:00:00Z" }))).toEqual({
      operationId: "op_1",
      driveId: "dr_1",
      kind: "trash",
      label: "Moved 1 item to trash",
      undoableUntil: "2026-01-01T00:00:00Z",
      etag: "et_1",
    });
  });

  it("records nothing for an operation the server cannot reverse", () => {
    expect(undoStep(write({ kind: "copy", undoable: false }))).toBeNull();
  });

  it("records nothing when the server said nothing about the inverse", () => {
    const silent: LandedWrite = write({ kind: "copy" });
    delete silent.undoable;
    expect(undoStep(silent)).toBeNull();
  });

  it("records nothing for a write the server named no operation for", () => {
    expect(undoStep(write({ operationId: undefined, undoable: true }))).toBeNull();
  });

  it("still inverts a move the server ran inline, which names no operation", () => {
    expect(
      undoStep(
        write({
          kind: "move",
          operationId: undefined,
          undoable: false,
          fromParentId: "nd_from",
          node: { id: "nd_1", etag: "et_2", parentId: "nd_to" },
        }),
      ),
    ).toEqual({
      driveId: "dr_1",
      kind: "move",
      label: "Moved 1 item to trash",
      move: { itemId: "nd_1", fromParentId: "nd_from", toParentId: "nd_to", etag: "et_2" },
    });
  });

  it("drops an inline move that cannot say where it came from", () => {
    expect(
      undoStep(
        write({
          kind: "move",
          operationId: undefined,
          node: { id: "nd_1", etag: "et_2", parentId: "nd_to" },
        }),
      ),
    ).toBeNull();
  });

  it("fences the undo on the version the answer produced, not the one it was asked at", () => {
    const step = undoStep(write({ node: { id: "nd_1", etag: "et_after" } }));
    expect(step?.etag).toBe("et_after");
  });
});

describe("the refusal a server that cannot undo sends", () => {
  it("reads as a sentence, not as its code", () => {
    const copy = filesErrorCopy(
      new ApiError(409, { code: "files.not_undoable", message: "operation has no inverse" }, "x"),
    );
    expect(copy.title).toBe("This can't be undone.");
    expect(copy.title).not.toContain("files.");
    expect(copy.detail).toBeTruthy();
  });
});
