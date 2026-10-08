// Sharing the chat, from the header's action group.
//
// The key is a glyph in the same cluster as Delete, not a word on a row of its
// own below the chrome, so the two actions a reader takes on the open chat sit
// together and read as the pair they are. What is pinned here is the seam: a
// host that wires nothing gets no key at all (the editor's webview is exactly
// that host), and where one IS wired the key lands to the LEFT of Delete, so
// tabbing through the header reaches the thing that lets people in before the
// thing that throws the chat away.

import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { Chrome } from "./Chrome";

const TITLE = "Yesterday's orders";

const actionNames = (): string[] => {
  const group = document.querySelector(".chat-chrome__actions");
  expect(group).not.toBeNull();
  return Array.from((group as HTMLElement).querySelectorAll("button")).map(
    (key) => key.getAttribute("aria-label") ?? "",
  );
};

describe("a chrome with no share seam", () => {
  it("carries no Share key", () => {
    render(<Chrome title={TITLE} onDeleteChat={() => {}} />);

    expect(screen.queryByRole("button", { name: "Share" })).toBeNull();
    expect(actionNames()).toEqual(["Delete chat"]);
  });
});

describe("a chrome with a share seam", () => {
  it("offers Share as a glyph key, named for readers who cannot see it", () => {
    render(<Chrome title={TITLE} onShareChat={() => {}} onDeleteChat={() => {}} />);

    const key = screen.getByRole("button", { name: "Share" });
    // The tip carries the same word the label does: a glyph with no accessible
    // name and no tip is a key nobody can identify.
    expect(key.getAttribute("title")).toBe("Share");
    // Same size and hover treatment as every other key in the cluster; the one
    // class those are painted through is what says so.
    expect(key.className).toContain("chat-icon");
    expect(key.querySelector("svg")).not.toBeNull();
    // And no word: the cluster is glyphs.
    expect(key.textContent).toBe("");
  });

  it("sits immediately before Delete, so the header is reached in that order", () => {
    render(
      <Chrome title={TITLE} onNewChat={() => {}} onShareChat={() => {}} onDeleteChat={() => {}} />,
    );

    const names = actionNames();
    expect(names).toEqual(["New chat", "Share", "Delete chat"]);
  });

  it("hands the press to the host, and nothing else", () => {
    const onShareChat = vi.fn();
    const onDeleteChat = vi.fn();
    render(<Chrome title={TITLE} onShareChat={onShareChat} onDeleteChat={onDeleteChat} />);

    fireEvent.click(screen.getByRole("button", { name: "Share" }));

    expect(onShareChat).toHaveBeenCalledTimes(1);
    // The two keys are neighbours; pressing one must not reach the other.
    expect(onDeleteChat).not.toHaveBeenCalled();
  });

  it("stands on its own, for a chat whose header offers no delete", () => {
    // A reader who may share but may not delete still gets the key: the two
    // seams are wired independently, not as one cluster that is all or none.
    render(<Chrome title={TITLE} onShareChat={() => {}} />);

    expect(actionNames()).toEqual(["Share"]);
  });
});

describe("a chrome with the workspace's machine", () => {
  it("draws the machine first in the action group, before Share", () => {
    render(
      <Chrome
        title={TITLE}
        machine={<button type="button" aria-label="Standard" />}
        onShareChat={() => {}}
        onDeleteChat={() => {}}
      />,
    );

    expect(actionNames()).toEqual(["Standard", "Share", "Delete chat"]);
  });

  it("draws nothing in its place when the host has no machine to show", () => {
    render(<Chrome title={TITLE} onShareChat={() => {}} onDeleteChat={() => {}} />);

    expect(actionNames()).toEqual(["Share", "Delete chat"]);
  });
});
