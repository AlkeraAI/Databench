import { render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { FilesStatusBar } from "@/pages/workspace/chat/workspace/FilesStatusBar";
import type { FolderLiveness } from "@/pages/workspace/files/liveRoot/liveness";

// What a screen reader hears from the foot of the chat's Files tab.
//
// The bar is a permanent fixture beside a conversation: its counts move on
// every lease frame and its clock re-spells itself every thirty seconds for as
// long as the chat is open. So the bar itself must not be the live region —
// announcing it is announcing a ticking clock — and what IS announced is the
// state in one or two words, only when that state actually changes, and no
// more often than a person can use.

const LIVE: FolderLiveness = {
  state: "live",
  holder: "Dana",
  machine: "box-1",
  since: null,
  landing: 0,
  onBox: 0,
};

const LANDING: FolderLiveness = { ...LIVE, landing: 3, onBox: 1 };

const OFFLINE: FolderLiveness = {
  state: "persisted",
  asOf: null,
  reason: "offline",
  machine: "box-1",
};

const SAVED: FolderLiveness = { state: "persisted", asOf: null, reason: "no-lease" };

/** The one node a reader is meant to hear, by its live-region role. */
function announced(): string {
  const regions = screen.queryAllByRole("status");
  expect(regions, "the bar has exactly one live region").toHaveLength(1);
  return regions[0]?.textContent ?? "";
}

let clock = 1_000_000;

beforeEach(() => {
  clock = 1_000_000;
  vi.spyOn(Date, "now").mockImplementation(() => clock);
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("what the chat's Files status bar announces", () => {
  it("does not make the whole bar a live region", () => {
    const { container } = render(<FilesStatusBar liveness={LANDING} />);
    const bar = container.querySelector(".alk-ws-status");

    expect(bar).not.toBeNull();
    // Counts and a clock live in here. A reader who asked for the page once
    // must not be read them again for the life of the conversation.
    expect(bar).not.toHaveAttribute("role", "status");
    expect(bar).not.toHaveAttribute("aria-live");
    expect(bar?.querySelectorAll("[aria-live]")).toHaveLength(0);
    expect(bar).toHaveTextContent("3 files syncing");
  });

  it("stays silent through a clock tick that only re-spells the time", () => {
    const view = render(<FilesStatusBar liveness={SAVED} savedAgo="3 min ago" />);
    const before = announced();

    clock += 30_000;
    view.rerender(<FilesStatusBar liveness={SAVED} savedAgo="4 min ago" />);

    expect(announced()).toBe(before);
    // The reader can still SEE the new time; it is only the speech that is held.
    expect(view.container.querySelector(".alk-ws-status")).toHaveTextContent("Saved 4 min ago");
  });

  it("stays silent when only the counts move under one state", () => {
    const view = render(<FilesStatusBar liveness={LANDING} />);
    const before = announced();

    clock += 30_000;
    view.rerender(<FilesStatusBar liveness={{ ...LANDING, landing: 7, onBox: 4 }} />);

    expect(announced()).toBe(before);
    expect(view.container.querySelector(".alk-ws-status")).toHaveTextContent("7 files syncing");
  });

  it("stays silent when files start or finish landing: the folder is live throughout", () => {
    const view = render(<FilesStatusBar liveness={LIVE} />);
    const before = announced();

    clock += 30_000;
    view.rerender(<FilesStatusBar liveness={LANDING} />);
    expect(announced()).toBe(before);
    expect(view.container.querySelector(".alk-ws-status")).toHaveAttribute("data-state", "landing");

    clock += 30_000;
    view.rerender(<FilesStatusBar liveness={LIVE} />);
    expect(announced()).toBe(before);
  });

  it("says the new state once when the machine finishes", () => {
    const view = render(<FilesStatusBar liveness={LANDING} />);

    clock += 30_000;
    view.rerender(<FilesStatusBar liveness={SAVED} savedAgo="1 min ago" />);
    expect(announced()).toBe("Saved copy");

    // A re-render that changes nothing about the state does not repeat it.
    clock += 30_000;
    view.rerender(<FilesStatusBar liveness={SAVED} savedAgo="2 min ago" />);
    expect(announced()).toBe("Saved copy");
  });

  it("holds back a second change that lands inside the floor", () => {
    const view = render(<FilesStatusBar liveness={OFFLINE} />);

    clock += 30_000;
    view.rerender(<FilesStatusBar liveness={LIVE} />);
    expect(announced()).toBe("Live");

    // A lease that flaps would otherwise read the bar out on every frame.
    clock += 1_000;
    view.rerender(<FilesStatusBar liveness={OFFLINE} />);
    expect(announced()).toBe("Live");
    expect(view.container.querySelector(".alk-ws-status")).toHaveAttribute("data-state", "offline");

    // Past the floor, the state that stands is spoken.
    clock += 5_000;
    view.rerender(<FilesStatusBar liveness={SAVED} savedAgo="1 min ago" />);
    expect(announced()).toBe("Saved copy");
  });
});

describe("what the chat's Files status bar shows", () => {
  it.each([
    { name: "live, naming the machine", liveness: LIVE, state: "live", text: "Livebox-1" },
    {
      name: "live with files landing",
      liveness: LANDING,
      state: "landing",
      text: "Livebox-13 files syncing1 file on the machine",
    },
    {
      name: "offline, and what is on screen",
      liveness: { ...OFFLINE, asOf: "2026-09-16T12:04:00Z" },
      state: "offline",
      text: /^Offlinebox-1Since .+ · showing last sync$/,
    },
  ])("reads $name", ({ liveness, state, text }) => {
    const { container } = render(<FilesStatusBar liveness={liveness as FolderLiveness} />);
    const bar = container.querySelector(".alk-ws-status");
    expect(bar).toHaveAttribute("data-state", state);
    expect(bar).toHaveTextContent(text);
    // "Saving" was the word before bytes trailed their rows; it is gone.
    expect(bar?.textContent).not.toMatch(/saving/i);
  });
});
