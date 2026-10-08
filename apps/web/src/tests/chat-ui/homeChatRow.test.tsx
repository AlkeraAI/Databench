// The grouped home row: a childless chat is a plain row reserving nothing, a
// parent trails its title with ONE capsule that fuses count and caret, and an
// open family shares a single frame however deep the spawning ran. The whole
// line is a door to the chat, and the capsule and the menu outrank it.
//
// The spoken labels below are pinned by value on purpose: they are what a
// screen reader says, and a test that imported them from ChatRow would keep
// passing while the wording drifted.

import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { ChatRow, type ChatRowView, type SubchatRowView } from "@alkera/ui";

const SHOW_1 = "Show 1 subagent chat";
const SHOW_2 = "Show 2 subagent chats";
const SHOW_3 = "Show 3 subagent chats";
const HIDE_SUBS = "Hide subagent chats";

const row = (id: string, over: Partial<SubchatRowView> = {}): SubchatRowView => ({
  id,
  title: `Chat ${id}`,
  status: "idle",
  ...over,
});

function renderRow(
  view: ChatRowView,
  wired: { onOpenInEditor?: (id: string) => void; onDelete?: (id: string) => void } = {},
): { container: HTMLElement; onOpen: ReturnType<typeof vi.fn> } {
  const onOpen = vi.fn();
  const { container } = render(
    <div className="chat-root">
      <ul>
        <ChatRow row={view} onOpen={onOpen} onOpenInEditor={wired.onOpenInEditor} onDelete={wired.onDelete} />
      </ul>
    </div>,
  );
  return { container, onOpen };
}

describe("home row disclosure", () => {
  it("leaves a childless chat a plain row", () => {
    renderRow(row("solo"));
    expect(screen.getByRole("button", { name: "Chat solo" })).toBeInTheDocument();
    // Nothing to disclose, so no capsule and no second list under the row.
    expect(screen.queryByRole("button", { name: /subagent chat/u })).toBeNull();
    expect(screen.getAllByRole("list")).toHaveLength(1);
    expect(screen.getAllByRole("listitem")).toHaveLength(1);
  });

  it("trails a parent's title with one capsule", () => {
    renderRow(row("p", { children: [row("a"), row("b"), row("c")] }));
    // One control for the whole family, not one per child.
    expect(screen.getAllByRole("button", { name: /subagent chat/u })).toHaveLength(1);
    const capsule = screen.getByRole("button", { name: SHOW_3 });
    expect(capsule).toHaveAttribute("aria-expanded", "false");
    expect(capsule.querySelector("span")?.textContent).toBe("3");
    expect(capsule.querySelector("svg")).not.toBeNull();
    // Closed: the children are not in the tree at all.
    expect(screen.getAllByRole("listitem")).toHaveLength(1);
    expect(screen.queryByRole("button", { name: "Chat a" })).toBeNull();

    renderRow(row("q", { children: [row("only")] }));
    expect(screen.getByRole("button", { name: SHOW_1 })).toBeInTheDocument();
  });

  it("opens the cluster on press and closes it on the next", async () => {
    const user = userEvent.setup();
    const { onOpen } = renderRow(row("p", { children: [row("a"), row("b")] }));
    await user.click(screen.getByRole("button", { name: SHOW_2 }));

    // The children join the tree as their own list, in the order they were given.
    const [, subs] = screen.getAllByRole("list");
    const subRows = within(subs).getAllByRole("listitem");
    expect(subRows).toHaveLength(2);
    expect(within(subRows[0]).getByRole("button", { name: "Chat a" })).toBeInTheDocument();
    expect(within(subRows[1]).getByRole("button", { name: "Chat b" })).toBeInTheDocument();
    const capsule = screen.getByRole("button", { name: HIDE_SUBS });
    expect(capsule).toHaveAttribute("aria-expanded", "true");

    await user.click(capsule);
    expect(screen.getAllByRole("list")).toHaveLength(1);
    expect(screen.getAllByRole("listitem")).toHaveLength(1);
    expect(screen.queryByRole("button", { name: "Chat a" })).toBeNull();
    expect(screen.getByRole("button", { name: SHOW_2 })).toHaveAttribute("aria-expanded", "false");
    expect(onOpen).not.toHaveBeenCalled();
  });

  it("nests a deeper level under one frame", async () => {
    const user = userEvent.setup();
    renderRow(row("p", { children: [row("a", { children: [row("g")] })] }));
    await user.click(screen.getByRole("button", { name: SHOW_1 }));

    const [, subs] = screen.getAllByRole("list");
    const subRow = within(subs).getByRole("listitem");
    expect(within(subRow).getByRole("button", { name: "Chat a" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Chat g" })).toBeNull();

    // The outer capsule now reads "Hide...", so this is the child's own.
    await user.click(screen.getByRole("button", { name: SHOW_1 }));
    const nested = within(subRow).getByRole("list");
    expect(within(nested).getByRole("button", { name: "Chat g" })).toBeInTheDocument();
    // One frame holds the whole family: each level nests inside the one above it
    // rather than opening a second cluster beside it.
    const lists = screen.getAllByRole("list");
    expect(lists).toHaveLength(3);
    expect(lists[0]).toContainElement(lists[1]);
    expect(lists[1]).toContainElement(lists[2]);
  });

  it("opens the chat from a row's name at either depth", async () => {
    const user = userEvent.setup();
    const { onOpen } = renderRow(row("p", { children: [row("a")] }));
    await user.click(screen.getByRole("button", { name: SHOW_1 }));

    await user.click(screen.getByRole("button", { name: "Chat p" }));
    expect(onOpen).toHaveBeenLastCalledWith("p");
    await user.click(screen.getByRole("button", { name: "Chat a" }));
    expect(onOpen).toHaveBeenLastCalledWith("a");
  });

  it("makes the whole line a door at either depth", async () => {
    const user = userEvent.setup();
    const { container, onOpen } = renderRow(row("p", { freshness: "12m", children: [row("a")] }));
    await user.click(screen.getByRole("button", { name: SHOW_1 }));

    await user.click(container.querySelector<HTMLElement>(".chat-hrow__line")!);
    expect(onOpen).toHaveBeenLastCalledWith("p");
    // The freshness label is part of the line, not a hole in it.
    await user.click(container.querySelector<HTMLElement>(".chat-hrow__fresh")!);
    expect(onOpen).toHaveBeenLastCalledWith("p");
    await user.click(container.querySelector<HTMLElement>(".chat-hrow__sline")!);
    expect(onOpen).toHaveBeenLastCalledWith("a");
  });
});

describe("home row actions menu", () => {
  it("gives the menu to the root alone, at every open depth", async () => {
    const user = userEvent.setup();
    renderRow(row("p", { title: "Root chat", children: [row("a", { children: [row("g")] })] }), {
      onOpenInEditor: vi.fn(),
      onDelete: vi.fn(),
    });
    await user.click(screen.getByRole("button", { name: SHOW_1 }));
    await user.click(screen.getByRole("button", { name: SHOW_1 }));
    // Both levels are open, so a menu on a subagent row would show up here.
    expect(screen.getByRole("button", { name: "Chat g" })).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: /^Actions for /u })).toHaveLength(1);
    expect(screen.getByRole("button", { name: "Actions for Root chat" })).toBeInTheDocument();
  });

  it("routes the wired actions with the root's id", async () => {
    const user = userEvent.setup();
    const onOpenInEditor = vi.fn();
    const onDelete = vi.fn();
    const { onOpen } = renderRow(row("p", { title: "Root chat" }), { onOpenInEditor, onDelete });

    await user.click(screen.getByRole("button", { name: "Actions for Root chat" }));
    const items = screen.getAllByRole("menuitem");
    expect(items.map((item) => item.textContent)).toEqual(["Open chat", "Delete chat"]);
    expect(items[0]).not.toHaveAttribute("data-danger");
    expect(items[1]).toHaveAttribute("data-danger");

    await user.click(items[0]);
    expect(onOpenInEditor).toHaveBeenCalledWith("p");
    expect(screen.queryByRole("menu")).toBeNull();

    await user.click(screen.getByRole("button", { name: "Actions for Root chat" }));
    await user.click(screen.getByRole("menuitem", { name: "Delete chat" }));
    expect(onDelete).toHaveBeenCalledWith("p");
    // The menu outranks the line's door: neither trigger nor row press opened the chat.
    expect(onOpen).not.toHaveBeenCalled();
  });

  it("carries only the actions the host wired", async () => {
    const user = userEvent.setup();
    renderRow(row("p", { title: "Root chat" }), { onDelete: vi.fn() });
    await user.click(screen.getByRole("button", { name: "Actions for Root chat" }));
    expect(screen.getAllByRole("menuitem").map((item) => item.textContent)).toEqual(["Delete chat"]);

    renderRow(row("q", { title: "Bare chat" }));
    expect(screen.queryByRole("button", { name: "Actions for Bare chat" })).toBeNull();
  });
});

describe("home row marks", () => {
  it("rides an ask in the mark column wearing its kind", () => {
    const asks = [
      ["permission", "Waiting for permission"],
      ["question", "Waiting for your answer"],
      ["plan", "Waiting for plan approval"],
    ] as const;
    for (const [kind, label] of asks) {
      const { container } = renderRow(row("p", { status: "working", ask: kind }));
      const ask = container.querySelector(".chat-hrow__mark .chat-hrow__ask");
      expect(ask, kind).not.toBeNull();
      expect(ask, kind).toHaveAttribute("data-kind", kind);
      expect(ask, kind).toHaveAttribute("aria-label", label);
      // The ask outranks the turn: no spinner beside it.
      expect(container.querySelector(".chat-hrow__work"), kind).toBeNull();
    }
  });

  it("spins a working chat in the mark column", () => {
    const { container } = renderRow(row("p", { status: "working" }));
    const work = container.querySelector(".chat-hrow__mark .chat-hrow__work");
    expect(work).toHaveAttribute("aria-label", "The agent is working");
    expect(work?.querySelector(".chat-spin")).not.toBeNull();
  });

  it("marks a finished chat read only when read is true", () => {
    const silent = renderRow(row("p", { status: "finished" }));
    const done = silent.container.querySelector(".chat-hrow__mark .chat-hrow__done");
    expect(done).toHaveAttribute("aria-label", "The agent finished this turn");
    expect(done).not.toHaveAttribute("data-read");

    const read = renderRow(row("q", { status: "finished", read: true }));
    expect(read.container.querySelector(".chat-hrow__done")).toHaveAttribute("data-read", "true");
    // An explicit false stays unread, never the string "false".
    const unread = renderRow(row("r", { status: "finished", read: false }));
    expect(unread.container.querySelector(".chat-hrow__done")).not.toHaveAttribute("data-read");
  });

  it("shows freshness only when known", () => {
    const fresh = renderRow(row("p", { freshness: "12m" }));
    expect(fresh.container.querySelector(".chat-hrow__fresh")?.textContent).toBe("12m");
    const unknown = renderRow(row("q"));
    expect(unknown.container.querySelector(".chat-hrow__fresh")).toBeNull();
  });
});
