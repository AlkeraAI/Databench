import { describe, expect, it } from "vitest";

import {
  PREVIEW_ACTIONS,
  previewActionsFor,
  type PreviewActionId,
} from "@/pages/workspace/files/preview/previewActions";

// One list, read by every surface that shows a file. The point of it being data
// rather than JSX is that the Files modal and the chat's file tab cannot drift
// into offering different things for the same bytes — which is exactly what had
// happened: one said "Open in Files", the other "Reveal in Files", and only one
// of them had a Close.

const ids = (state: Parameters<typeof previewActionsFor>[0]): PreviewActionId[] =>
  previewActionsFor(state).map((action) => action.id);

const FULL = {
  sealed: false,
  reachable: true,
  canOpenInFiles: true,
  canShare: true,
  canCopy: true,
} as const;

describe("the preview action list", () => {
  it("is the same order whatever the file is", () => {
    expect(ids(FULL)).toEqual([
      "download",
      "open-in-files",
      "share",
      "copy",
      "copy-link",
      "open-new-tab",
      "close",
    ]);
  });

  it("always ends on Close, which is the one action every state keeps", () => {
    const states = [
      FULL,
      { ...FULL, sealed: true, reachable: false },
      { ...FULL, canOpenInFiles: false, canShare: false, canCopy: true },
    ];
    for (const state of states) {
      const list = ids(state);
      expect(list[list.length - 1]).toBe("close");
    }
  });

  it("drops what a file with downloads off cannot do, and keeps the rest", () => {
    const sealed = ids({ ...FULL, sealed: true, reachable: false });
    // No bytes to take away and none to open whole; the link and the folder are
    // still there, because neither is the bytes.
    expect(sealed).not.toContain("download");
    expect(sealed).not.toContain("open-new-tab");
    expect(sealed).toContain("copy-link");
    expect(sealed).toContain("open-in-files");
  });

  it("drops the two the host has nowhere to send", () => {
    const alone = ids({ ...FULL, canOpenInFiles: false, canShare: false, canCopy: true });
    expect(alone).not.toContain("open-in-files");
    expect(alone).not.toContain("share");
    expect(alone).toContain("download");
  });

  it("names Soft wrap as the renderer's, so a host does not draw a second one", () => {
    const wrap = PREVIEW_ACTIONS.find((action) => action.id === "soft-wrap");
    expect(wrap?.owner).toBe("renderer");
    // It is part of the set a reader is promised, and it is drawn by whichever
    // renderer it applies to — so it must never come back in a host's row.
    expect(ids(FULL)).not.toContain("soft-wrap");
  });

  it("gives every action a label, so a surface renders the list and nothing else", () => {
    for (const action of PREVIEW_ACTIONS) {
      expect(action.label.length, action.id).toBeGreaterThan(2);
    }
  });

  it("offers Copy only when the content has a form a reader can paste", () => {
    expect(ids(FULL)).toContain("copy");
    expect(ids({ ...FULL, canCopy: false })).not.toContain("copy");
    // It sits with the other take-aways, right before the link.
    const list = ids(FULL);
    expect(list.indexOf("copy")).toBe(list.indexOf("copy-link") - 1);
  });
});
