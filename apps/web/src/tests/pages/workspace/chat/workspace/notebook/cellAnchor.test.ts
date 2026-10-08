// The page anchor that takes a notebook to one cell, and the reveal a chat's
// "Go to cell" asks for while the notebook is already open.

import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import {
  cellFromHash,
  cellHash,
  revealNotebookCell,
  useRevealedCell,
} from "@/pages/workspace/chat/workspace/notebook/cellAnchor";

afterEach(() => {
  window.history.replaceState(null, "", window.location.pathname);
});

describe("a notebook cell anchor", () => {
  it.each([
    ["#cell=a7yg9x7evz", "a7yg9x7evz"],
    ["#x=1&cell=a7yg9x7evz", "a7yg9x7evz"],
    ["#cell=Cell3", null],
    ["#cell=a7yg9x7evzz", null],
    ["", null],
  ])("reads %j as %j", (hash, cell) => {
    expect(cellFromHash(hash)).toBe(cell);
  });

  it("round-trips the anchor it writes", () => {
    expect(cellFromHash(`#${cellHash("a7yg9x7evz")}`)).toBe("a7yg9x7evz");
  });

  it("is what a notebook opened by the link reveals", () => {
    revealNotebookCell("a7yg9x7evz");
    expect(window.location.hash).toBe("#cell=a7yg9x7evz");
    const { result } = renderHook(() => useRevealedCell());
    expect(result.current).toEqual({ id: "a7yg9x7evz", seq: 1 });
  });

  it("asks an open notebook again, for the same cell too", () => {
    const { result } = renderHook(() => useRevealedCell());
    expect(result.current).toBeNull();
    act(() => revealNotebookCell("bbbbbbbbbb"));
    expect(result.current).toEqual({ id: "bbbbbbbbbb", seq: 1 });
    act(() => revealNotebookCell("bbbbbbbbbb"));
    expect(result.current).toEqual({ id: "bbbbbbbbbb", seq: 2 });
  });
});
