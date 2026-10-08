/**
 * What the Files treegrid answers to: the pointer, the modifiers and the row.
 *
 * Every rule here is the one a file manager has: a plain click picks one row and
 * moves the anchor, the platform's command modifier toggles one row in or out of
 * what is already picked, Shift ranges from the anchor and Shift+arrows carry the
 * range with the focus. The whole row is the target — a listing is mostly Kind,
 * Size, Modified and Owner, and a person clicking those means the row.
 *
 * Driven through `FilesBrowser`, which owns the selection the grid dispatches into
 * and is the very component the chat's Files dock renders, so one pass covers both
 * surfaces. The platform is resolved the way the product resolves it — off
 * `navigator` — rather than handed in, because the accelerator being read off the
 * wrong platform is what made Cmd do nothing on a Mac.
 */

import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { createQueryClient } from "@/api/queryClient";
import type { Item } from "@/api/files";
import { FilesBrowser } from "@/pages/workspace/files/FilesBrowser";

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
    attrs: { mtime: "2026-01-02T00:00:00Z" },
    file: { size: 1200 },
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
  item({ id: "nd_a", name: "alpha.txt" }),
  item({ id: "nd_b", name: "bravo.md" }),
  item({ id: "nd_c", name: "charlie.sql" }),
  item({ id: "nd_d", name: "delta.csv" }),
  item({ id: "nd_e", name: "echo.json" }),
];

function stubChildren(rows: Item[] = ROWS): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(() =>
      Promise.resolve(
        new Response(JSON.stringify({ value: rows, nextMarker: null }), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
      ),
    ),
  );
}

/** Pin what `detectPlatform` reads. Both signals are set: a browser that reports a
 *  platform hint the table does not know must not shadow the one that says Mac. */
function stubNavigator(over: { uaDataPlatform?: string | undefined; platform: string }): void {
  Object.defineProperty(window.navigator, "userAgentData", {
    value: over.uaDataPlatform === undefined ? undefined : { platform: over.uaDataPlatform },
    configurable: true,
  });
  Object.defineProperty(window.navigator, "platform", {
    value: over.platform,
    configurable: true,
  });
}

function mount(props: Partial<React.ComponentProps<typeof FilesBrowser>> = {}) {
  const client = createQueryClient({ retry: false });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <FilesBrowser driveId={DRIVE} parentId={PARENT} {...props} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

/** The rows the grid says are selected, read off the element that carries the row
 *  role — which is where a screen reader driving a treegrid looks. */
function selectedIds(): string[] {
  return Array.from(
    document.querySelectorAll('[role="row"][aria-selected="true"]'),
  ).map((element) => element.getAttribute("data-row-id") ?? "");
}

function row(id: string): HTMLElement {
  const found = document.querySelector<HTMLElement>(`[role="row"][data-row-id="${id}"]`);
  if (found === null) throw new Error(`no row ${id}`);
  return found;
}

/** One cell of a row, by the column it is under. */
function cell(id: string, column: string): HTMLElement {
  const found = row(id).querySelector<HTMLElement>(`[data-column="${column}"]`);
  if (found === null) throw new Error(`no ${column} cell in ${id}`);
  return found;
}

async function rowsPainted(): Promise<void> {
  await waitFor(() => expect(document.querySelectorAll("[data-row-id]")).toHaveLength(ROWS.length));
}

/** A click with the platform's command modifier held, as a pointer really sends it. */
async function accelClick(user: ReturnType<typeof userEvent.setup>, target: HTMLElement, key: "Meta" | "Control") {
  await user.keyboard(`{${key}>}`);
  await user.click(target);
  await user.keyboard(`{/${key}}`);
}

async function shiftClick(user: ReturnType<typeof userEvent.setup>, target: HTMLElement) {
  await user.keyboard("{Shift>}");
  await user.click(target);
  await user.keyboard("{/Shift}");
}

/** Whether the keystroke was still live by the time it reached the document — i.e.
 *  whether the browser's own Select All would have run over the whole app chrome. */
function watchDefault(): { leaked: boolean } {
  const seen = { leaked: false };
  document.addEventListener(
    "keydown",
    (event) => {
      if (event.key.toLowerCase() === "a") seen.leaked = !event.defaultPrevented;
    },
    { once: true },
  );
  return seen;
}

beforeEach(() => {
  stubChildren();
  window.localStorage.clear();
});

afterEach(() => {
  vi.unstubAllGlobals();
  window.localStorage.clear();
});

describe("select all, on the platform the reader is on", () => {
  beforeEach(() => {
    stubNavigator({ uaDataPlatform: "Unknown", platform: "MacIntel" });
  });

  it("takes Cmd+A on a Mac, and does not leave the page's own Select All to run", async () => {
    const user = userEvent.setup();
    mount();
    await rowsPainted();
    await user.click(cell("nd_a", "name"));

    const watched = watchDefault();
    await user.keyboard("{Meta>}a{/Meta}");

    expect(selectedIds()).toEqual(ROWS.map((r) => r.id));
    expect(watched.leaked).toBe(false);
  });

  it("does not let Ctrl+A on a Mac paint the app chrome in the browser's selection", async () => {
    const user = userEvent.setup();
    mount();
    await rowsPainted();
    await user.click(cell("nd_a", "name"));

    const watched = watchDefault();
    await user.keyboard("{Control>}a{/Control}");

    // Ctrl is not this platform's accelerator, so it selects nothing — but the
    // keystroke is still the grid's, and letting it through highlighted the nav,
    // the breadcrumb and every button on the page.
    expect(selectedIds()).toEqual(["nd_a"]);
    expect(watched.leaked).toBe(false);
  });

  it("takes Ctrl+A everywhere else", async () => {
    stubNavigator({ uaDataPlatform: "Windows", platform: "Win32" });
    const user = userEvent.setup();
    mount();
    await rowsPainted();
    await user.click(cell("nd_a", "name"));

    await user.keyboard("{Control>}a{/Control}");
    expect(selectedIds()).toEqual(ROWS.map((r) => r.id));
  });
});

describe("what a modified click does to the selection", () => {
  beforeEach(() => {
    stubNavigator({ uaDataPlatform: "Unknown", platform: "MacIntel" });
  });

  it("toggles one row in and back out, leaving the rest of the selection alone", async () => {
    const user = userEvent.setup();
    mount();
    await rowsPainted();
    await user.click(cell("nd_a", "name"));

    await accelClick(user, cell("nd_c", "name"), "Meta");
    expect(selectedIds()).toEqual(["nd_a", "nd_c"]);

    await accelClick(user, cell("nd_c", "name"), "Meta");
    expect(selectedIds()).toEqual(["nd_a"]);
  });

  it("keeps a Shift range when a later Cmd+click adds one more row", async () => {
    const user = userEvent.setup();
    mount();
    await rowsPainted();
    await user.click(cell("nd_a", "name"));
    await shiftClick(user, cell("nd_c", "name"));
    expect(selectedIds()).toEqual(["nd_a", "nd_b", "nd_c"]);

    await accelClick(user, cell("nd_e", "name"), "Meta");
    expect(selectedIds()).toEqual(["nd_a", "nd_b", "nd_c", "nd_e"]);
  });

  it("ranges from the anchor, and a plain click moves the anchor", async () => {
    const user = userEvent.setup();
    mount();
    await rowsPainted();
    await user.click(cell("nd_d", "name"));
    await shiftClick(user, cell("nd_b", "name"));
    expect(selectedIds()).toEqual(["nd_b", "nd_c", "nd_d"]);

    // The plain click is the new anchor: the range that follows grows from it,
    // not from where the last one started.
    await user.click(cell("nd_b", "name"));
    await shiftClick(user, cell("nd_c", "name"));
    expect(selectedIds()).toEqual(["nd_b", "nd_c"]);
  });

  it("carries the selection with Shift+arrow", async () => {
    const user = userEvent.setup();
    mount();
    await rowsPainted();
    await user.click(cell("nd_b", "name"));

    await user.keyboard("{Shift>}{ArrowDown}{ArrowDown}{/Shift}");
    expect(selectedIds()).toEqual(["nd_b", "nd_c", "nd_d"]);

    await user.keyboard("{Shift>}{ArrowUp}{/Shift}");
    expect(selectedIds()).toEqual(["nd_b", "nd_c"]);
  });
});

describe("what part of a row is the target", () => {
  beforeEach(() => {
    stubNavigator({ uaDataPlatform: "Windows", platform: "Win32" });
  });

  it.each(["kind", "size", "modified", "owner"])(
    "selects the row from its %s cell",
    async (column) => {
      const user = userEvent.setup();
      mount();
      await rowsPainted();
      await user.click(cell("nd_a", "name"));

      await user.click(cell("nd_d", column));
      expect(selectedIds()).toEqual(["nd_d"]);
    },
  );

  it("ranges from a cell that is not the name", async () => {
    const user = userEvent.setup();
    mount();
    await rowsPainted();
    await user.click(cell("nd_b", "size"));
    await shiftClick(user, cell("nd_d", "modified"));
    expect(selectedIds()).toEqual(["nd_b", "nd_c", "nd_d"]);
  });

  it("leaves the selection alone when the click lands in a control inside the row", async () => {
    const user = userEvent.setup();
    mount({
      renderNameOverride: (candidate) =>
        candidate.id === "nd_c" ? <input aria-label="New name" defaultValue="charlie.sql" /> : null,
    });
    await rowsPainted();
    await user.click(cell("nd_a", "name"));

    // The rename editor owns its own pointer: a click placing the caret must not
    // be read as a click on the row under it.
    await user.click(screen.getByLabelText("New name"));
    expect(selectedIds()).toEqual(["nd_a"]);
  });
});

describe("what a screen reader is told", () => {
  beforeEach(() => {
    stubNavigator({ uaDataPlatform: "Windows", platform: "Win32" });
  });

  it("puts aria-selected on the row and multi-selectable on the grid", async () => {
    const user = userEvent.setup();
    mount();
    await rowsPainted();
    await user.click(cell("nd_b", "name"));

    expect(row("nd_b")).toHaveAttribute("aria-selected", "true");
    expect(row("nd_a")).toHaveAttribute("aria-selected", "false");
    expect(screen.getByRole("treegrid")).toHaveAttribute("aria-multiselectable", "true");
  });

  it("never marks a cell selected — the row is what is selected", async () => {
    const user = userEvent.setup();
    mount();
    await rowsPainted();
    await user.keyboard("{Control>}a{/Control}");
    await user.click(cell("nd_b", "name"));

    expect(document.querySelectorAll('[role="gridcell"][aria-selected]')).toHaveLength(0);
  });
});
