// What the product says out loud.
//
// A state that changes on its own — an upload advancing, a lease going live, a
// connection dropping, a message put in the queue, a selection growing — is
// invisible to a reader who is not watching that corner of the screen. Each is
// announced by a polite live region; a refusal is announced by an assertive
// one, because it is the reason the thing they asked for did not happen.
//
// A live region is only a live region while the element is IN the document
// before its text changes, so each case asserts the region exists AND that it
// carries the new words after the change — a `role="status"` painted together
// with its first text announces nothing.

import { cleanup, render, screen } from "@testing-library/react";
import { act } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Item } from "@/api/files";
import { RECONNECTING_NOTE, ReconnectingNote } from "@/pages/workspace/chat/ReconnectingNote";
import { SelectionBar } from "@/pages/workspace/files/SelectionBar";
import { UploadTray } from "@/pages/workspace/files/UploadTray";

function item(id: string, name: string, size: number): Item {
  return {
    id,
    ino: 1,
    driveId: "drv_1",
    kind: "file",
    nameDisplay: name,
    parentId: "nd_parent",
    etag: "e1",
    file: { size },
    capabilities: {},
  } as unknown as Item;
}

const MENU = [
  { id: "download", label: "Download", onSelect: () => undefined },
] as unknown as Parameters<typeof SelectionBar>[0]["menuItems"];

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("a status that changes announces itself", () => {
  it("the selection count is a status region, and carries the new count", () => {
    const { rerender } = render(
      <SelectionBar selection={[item("a", "alpha.txt", 10), item("b", "bravo.md", 20)]} menuItems={MENU} />,
    );
    const region = screen.getByRole("status");
    expect(region).toHaveTextContent("2 items");

    rerender(
      <SelectionBar
        selection={[item("a", "alpha.txt", 10), item("b", "bravo.md", 20), item("c", "c.sql", 30)]}
        menuItems={MENU}
      />,
    );
    // The SAME node now says the new count — which is what a live region is.
    expect(region).toHaveTextContent("3 items");
    expect(screen.getByRole("status")).toBe(region);
  });

  it("the upload batch line is a status region and the refusal is an alert", () => {
    const rows = [
      { uploadId: "u1", name: "alpha.txt", state: "uploading", sent: 10, total: 100 },
    ] as unknown as Parameters<typeof UploadTray>[0]["rows"];
    const { rerender } = render(
      <UploadTray
        rows={rows}
        skippedSidecars={0}
        identicalCopies={0}
        alreadyInFiles={0}
        finished={0}
        batch={{ total: 2, done: 0, failed: 0, current: "alpha.txt", stopped: null }}
        conflicts={[]}
        resumable={[]}
        refusal={null}
        onAnswerConflict={() => undefined}
        onPause={() => undefined}
        onResume={() => undefined}
        onCancel={() => undefined}
        onRetry={() => undefined}
      />,
    );
    const batch = screen.getByRole("status");
    expect(batch).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();

    rerender(
      <UploadTray
        rows={rows}
        skippedSidecars={0}
        identicalCopies={0}
        alreadyInFiles={0}
        finished={0}
        batch={{ total: 2, done: 1, failed: 0, current: "alpha.txt", stopped: null }}
        conflicts={[]}
        resumable={[]}
        refusal="The drive is out of room."
        onAnswerConflict={() => undefined}
        onPause={() => undefined}
        onResume={() => undefined}
        onCancel={() => undefined}
        onRetry={() => undefined}
      />,
    );
    expect(screen.getByRole("alert")).toHaveTextContent("out of room");
    expect(screen.getByRole("status")).toBe(batch);
  });

  it("each upload's progress is named, so the bar is not an unlabelled number", () => {
    const rows = [
      { uploadId: "u1", name: "alpha.txt", state: "uploading", sent: 10, total: 100 },
    ] as unknown as Parameters<typeof UploadTray>[0]["rows"];
    render(
      <UploadTray
        rows={rows}
        skippedSidecars={0}
        identicalCopies={0}
        alreadyInFiles={0}
        finished={0}
        batch={null}
        conflicts={[]}
        resumable={[]}
        refusal={null}
        onAnswerConflict={() => undefined}
        onPause={() => undefined}
        onResume={() => undefined}
        onCancel={() => undefined}
        onRetry={() => undefined}
      />,
    );
    expect(screen.getByRole("progressbar", { name: "alpha.txt progress" })).toBeInTheDocument();
  });
});

describe("a connection that drops says so", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  it("says nothing for a blip and announces a real outage as a status", () => {
    vi.stubGlobal("navigator", { ...navigator, onLine: true });
    render(<ReconnectingNote />);
    expect(screen.queryByRole("status")).toBeNull();

    act(() => {
      window.dispatchEvent(new Event("offline"));
      vi.advanceTimersByTime(1_000);
    });
    // A hop between access points is not worth saying.
    expect(screen.queryByRole("status")).toBeNull();

    act(() => {
      window.dispatchEvent(new Event("online"));
      window.dispatchEvent(new Event("offline"));
      vi.advanceTimersByTime(10_000);
    });
    expect(screen.getByRole("status")).toHaveTextContent(RECONNECTING_NOTE);

    act(() => {
      window.dispatchEvent(new Event("online"));
    });
    expect(screen.queryByRole("status")).toBeNull();
  });
});
