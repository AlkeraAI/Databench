// What a person with no pointer can reach, and what they can see while they do.
//
// Every control on these surfaces is reachable by Tab, operable by Enter or
// Space, and shows where focus is. The focus ring is a single zero-specificity
// rule in the shared reset (`:where(:focus-visible)`), so the thing a test can
// actually hold is that the control the keyboard lands on is the control that
// would draw it — `document.activeElement`, and a stylesheet that still carries
// the rule.

import { QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { createQueryClient } from "@/api/queryClient";
import type { Item } from "@/api/files";
import { FilesBrowser } from "@/pages/workspace/files/FilesBrowser";
import { FileTabHeader } from "@/pages/workspace/chat/workspace/FileTabHeader";

const DRIVE = "drv_1";
const PARENT = "nd_parent";

function item(overrides: Partial<Item> & { id: string; name: string }): Item {
  return {
    ino: 1,
    driveId: DRIVE,
    kind: "file",
    subtype: null,
    nameDisplay: overrides.name,
    nameEncoding: "utf-8",
    nameFlags: { windows_safe: true, macos_safe: true, display_warning: false },
    pathBytes: "",
    parentId: PARENT,
    path: null,
    etag: "e1",
    ctag: "c1",
    attrs: null,
    file: null,
    symlink: null,
    object: null,
    lease: null,
    stale: false,
    trust: null,
    locked: false,
    held: false,
    capabilities: {},
    shared: false,
    trashed: false,
    ...overrides,
  } as unknown as Item;
}

const ROWS: Item[] = [
  item({ id: "nd_a", name: "alpha.txt", file: { size: 1200 } as Item["file"] }),
  item({ id: "nd_b", name: "bravo.md", file: { size: 40 } as Item["file"] }),
];

function stubChildren(): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(() =>
      Promise.resolve(
        new Response(JSON.stringify({ value: ROWS, nextMarker: null }), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
      ),
    ),
  );
}

function mountBrowser() {
  return render(
    <QueryClientProvider client={createQueryClient({ retry: false })}>
      <MemoryRouter initialEntries={["/files/nd_parent"]}>
        <Routes>
          <Route
            path="/files/:nodeId"
            element={<FilesBrowser driveId={DRIVE} parentId={PARENT} platform="other" />}
          />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

/** Tab until `target` has focus, or give up. Returns how many presses it took,
 *  so a control that is reachable but buried still reads differently from one
 *  that cannot be reached at all. */
async function tabTo(user: ReturnType<typeof userEvent.setup>, target: Element, limit = 40): Promise<number> {
  for (let presses = 1; presses <= limit; presses += 1) {
    await user.tab();
    if (document.activeElement === target) return presses;
  }
  return -1;
}

beforeEach(() => {
  stubChildren();
  window.localStorage.clear();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  window.localStorage.clear();
});

describe("the Files listing by keyboard alone", () => {
  it("reaches the view switch and flips it with Enter", async () => {
    const user = userEvent.setup();
    mountBrowser();
    await waitFor(() => expect(document.querySelectorAll("[data-row-id]").length).toBe(ROWS.length));

    const grid = screen.getByRole("button", { name: "Grid" });
    expect(await tabTo(user, grid)).toBeGreaterThan(0);
    await user.keyboard("{Enter}");
    await waitFor(() => expect(screen.queryAllByRole("columnheader")).toHaveLength(0));
  });

  it("reaches a sort header and sorts with Space", async () => {
    const user = userEvent.setup();
    mountBrowser();
    await waitFor(() => expect(document.querySelectorAll("[data-row-id]").length).toBe(ROWS.length));

    const nameHeader = within(screen.getByRole("columnheader", { name: /Name/ })).getByRole("button");
    expect(await tabTo(user, nameHeader)).toBeGreaterThan(0);
    const before = screen.getByRole("columnheader", { name: /Name/ }).getAttribute("aria-sort");
    await user.keyboard("{ }");
    await waitFor(() =>
      expect(screen.getByRole("columnheader", { name: /Name/ }).getAttribute("aria-sort")).not.toBe(before),
    );
  });

  it("puts exactly one row in the tab order and moves the stop with the arrows", async () => {
    const user = userEvent.setup();
    mountBrowser();
    await waitFor(() => expect(document.querySelectorAll("[data-row-id]").length).toBe(ROWS.length));

    const rows = Array.from(document.querySelectorAll<HTMLElement>('[role="row"][data-row-id]'));
    const tabbable = rows.filter((row) => row.tabIndex === 0);
    // A roving tab stop: the grid is one stop, not one per row.
    expect(tabbable).toHaveLength(1);

    expect(await tabTo(user, tabbable[0]!)).toBeGreaterThan(0);
    // The first press lands the caret on the row the stop was sitting on; the
    // next one moves it, which is what proves the arrows steer the grid.
    await user.keyboard("{ArrowDown}");
    await waitFor(() => expect(document.activeElement).toBe(rows[0]));
    await user.keyboard("{ArrowDown}");
    await waitFor(() => expect(document.activeElement).toBe(rows[1]));
    expect(rows[1]!.tabIndex).toBe(0);
    expect(rows[0]!.tabIndex).toBe(-1);
  });
});

describe("the compact file-tab header by keyboard alone", () => {
  it("reaches every action in the row and fires it with Enter", async () => {
    const user = userEvent.setup();
    const fired: string[] = [];
    render(
      <FileTabHeader
        name="notes.md"
        path="/notes.md"
        facts={["1 KB"]}
        actions={[
          { id: "wrap", label: "Soft wrap", icon: <svg aria-hidden />, run: () => fired.push("wrap"), pressed: false },
          { id: "close", label: "Close", icon: <svg aria-hidden />, run: () => fired.push("close") },
        ]}
      />,
    );

    const wrap = screen.getByRole("button", { name: "Soft wrap" });
    expect(await tabTo(user, wrap)).toBe(1);
    await user.keyboard("{Enter}");
    await user.tab();
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "Close" }));
    await user.keyboard("{ }");
    expect(fired).toEqual(["wrap", "close"]);
  });

  it("says which setting is on, so a toggle is not announced as a command", () => {
    render(
      <FileTabHeader
        name="notes.md"
        path="/notes.md"
        facts={[]}
        actions={[
          { id: "wrap", label: "Soft wrap", icon: <svg aria-hidden />, run: () => undefined, pressed: true },
          { id: "close", label: "Close", icon: <svg aria-hidden />, run: () => undefined },
        ]}
      />,
    );
    expect(screen.getByRole("button", { name: "Soft wrap" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: "Close" })).not.toHaveAttribute("aria-pressed");
  });
});

describe("the focus ring", () => {
  // jsdom applies no stylesheet, so the ring is held where it is written: one
  // zero-specificity rule that covers every focusable thing in the product. A
  // change that drops it (or narrows it to a selector list) fails here.
  it("is one rule over every focusable element, not a per-component list", () => {
    // vitest runs with apps/web as the working directory.
    const reset = readFileSync(resolve(process.cwd(), "../../packages/ui/src/theme/reset.css"), "utf8");
    expect(reset).toMatch(/:where\(:focus-visible\)\s*\{[^}]*outline:/);
  });
});
