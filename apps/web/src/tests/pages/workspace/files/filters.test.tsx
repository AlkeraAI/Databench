import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { toChildrenParams } from "@/api/files";
import { createQueryClient } from "@/api/queryClient";
import { FilterBar, FilteredEmptyState } from "@/pages/workspace/files/FilterBar";
import {
  CHIPS,
  EMPTY_FILTER_STATE,
  activeFilterLabels,
  clearFilters,
  filterStateFromParams,
  filterStateToParams,
  toListFilters,
  toggleChip,
  useFilterState,
  type FilterState,
} from "@/pages/workspace/files/filterState";

// The filter bar driven through the same state the listing hooks read: a chip is asserted
// by the wire parameter `toChildrenParams` produces from it, and the URL by what the
// router's location actually carries after a click.

const NOW = new Date("2026-09-09T14:30:00.000Z");

function wireFor(chipIds: readonly string[], extra: Partial<FilterState> = {}) {
  const state: FilterState = { ...EMPTY_FILTER_STATE, chips: chipIds, ...extra };
  return toChildrenParams(toListFilters(state, NOW));
}

/** The bar bound to the URL exactly as the page binds it. */
function Harness({ onLocation }: { onLocation?: (search: string) => void }) {
  const [state, setState] = useFilterState();
  const location = useLocation();
  onLocation?.(location.search);
  return (
    <>
      <FilterBar state={state} onChange={setState} />
      <FilteredEmptyState state={state} onChange={setState} />
      <span data-testid="wire">{JSON.stringify(toChildrenParams(toListFilters(state, NOW)))}</span>
    </>
  );
}

function renderBar(initialEntry = "/files/nd_1") {
  let search = "";
  const client = createQueryClient();
  const view = render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[initialEntry]}>
        <Harness
          onLocation={(next) => {
            search = next;
          }}
        />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { view, currentSearch: () => search };
}

function chip(name: string) {
  return screen.getByRole("button", { name });
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("chip to parameter", () => {
  it.each([
    ["kind:folders", { kind: "folder" }],
    ["kind:files", { kind: "file" }],
    ["kind:chats", { objectType: "chat" }],
    ["kind:templates", { objectType: "chat_template" }],
    ["kind:results", { objectType: "result" }],
    ["kind:boards", { objectType: "board" }],
    ["kind:images", { mimeClass: "image" }],
    ["kind:tabular", { mimeClass: "tabular" }],
    ["kind:code", { mimeClass: "code" }],
    ["kind:archives", { mimeClass: "archive" }],
    ["kind:other", { mimeClass: "other" }],
    ["owner:me", { owner: "me" }],
    ["size:small", { sizeMax: "1048576" }],
    ["size:medium", { sizeMin: "1048576", sizeMax: "104857600" }],
    ["size:large", { sizeMin: "104857600" }],
    ["name:windows", { nameFlag: "windows_safe" }],
    ["name:macos", { nameFlag: "macos_safe" }],
    ["name:warning", { nameFlag: "display_warning" }],
    ["state:starred", { starred: "true" }],
    ["state:shared", { shared: "true" }],
    ["state:leased", { leased: "true" }],
    ["state:trashed", { trashed: "true" }],
  ])("%s sends %o", (chipId, expected) => {
    expect(wireFor([chipId])).toEqual(expected);
  });

  it("the relative modified windows are computed against the clock, not hardcoded", () => {
    // Independent of the implementation: the 7- and 30-day windows are exactly
    // that many days before the injected clock, and "today" is the local
    // midnight at or before it.
    const at = (chipId: string) => new Date(wireFor([chipId]).modifiedAfter ?? "");
    expect(NOW.getTime() - at("modified:7d").getTime()).toBe(7 * 86_400_000);
    expect(NOW.getTime() - at("modified:30d").getTime()).toBe(30 * 86_400_000);

    const today = at("modified:today");
    expect(today.getTime()).toBeLessThanOrEqual(NOW.getTime());
    expect(NOW.getTime() - today.getTime()).toBeLessThan(86_400_000);
    expect([today.getHours(), today.getMinutes(), today.getSeconds()]).toEqual([0, 0, 0]);
  });

  it("crosses a day boundary: one minute past midnight narrows to that minute", () => {
    const justAfterMidnight = new Date(NOW.getTime());
    justAfterMidnight.setHours(0, 1, 0, 0);
    const state: FilterState = { ...EMPTY_FILTER_STATE, chips: ["modified:today"] };
    const after = new Date(toListFilters(state, justAfterMidnight).modifiedAfter ?? "");
    expect(justAfterMidnight.getTime() - after.getTime()).toBe(60_000);
  });

  it("a custom range sends both edges, and a half-filled one sends only what it has", () => {
    expect(
      wireFor(["modified:custom"], {
        ranges: { "modified:custom": { from: "2026-01-01T00:00:00Z", to: "2026-02-01T00:00:00Z" } },
      }),
    ).toEqual({ modifiedAfter: "2026-01-01T00:00:00Z", modifiedBefore: "2026-02-01T00:00:00Z" });

    expect(wireFor(["size:custom"], { ranges: { "size:custom": { from: "4096" } } })).toEqual({
      sizeMin: "4096",
    });
  });

  it("a custom chip with nothing filled in sends nothing", () => {
    // The negative twin: an opened-but-empty custom chip must not narrow the
    // listing to an unsatisfiable range.
    expect(wireFor(["size:custom"])).toEqual({});
    expect(wireFor(["owner:person"])).toEqual({});
    expect(wireFor(["owner:person"], { ownerId: "usr_9" })).toEqual({ owner: "usr_9" });
  });

  it("every registered chip is reachable and unique", () => {
    const ids = CHIPS.map((c) => c.id);
    expect(new Set(ids).size).toBe(ids.length);
  });

  it("narrows to templates, and no longer offers the retired queries", () => {
    // A saved query is not a thing in the drive any more — the SQL it held lives
    // in a template — so the chip that asked for one would narrow a folder by a
    // facet nothing carries. A link someone bookmarked with it drops the chip on
    // read rather than sending the server a dead parameter.
    expect(CHIPS.map((chip) => chip.label)).toContain("Templates");
    expect(CHIPS.map((chip) => chip.label)).not.toContain("Queries");
    expect(CHIPS.map((chip) => chip.id)).not.toContain("kind:queries");
    expect(filterStateFromParams(new URLSearchParams("chips=kind:queries")).chips).toEqual([]);
    expect(filterStateFromParams(new URLSearchParams("chips=kind:templates")).chips).toEqual([
      "kind:templates",
    ]);
  });
});

describe("chips combine", () => {
  it("ANDs across groups", () => {
    expect(wireFor(["kind:files", "owner:me", "state:starred", "size:small"])).toEqual({
      kind: "file",
      owner: "me",
      starred: "true",
      sizeMax: "1048576",
    });
  });

  it("an exclusive group keeps only the last chip picked", () => {
    const once = toggleChip({ ...EMPTY_FILTER_STATE }, "modified:today");
    const twice = toggleChip(once, "modified:7d");
    expect(twice.chips).toEqual(["modified:7d"]);
    // The negative twin: without the exclusivity rule both windows would be on
    // and the later `apply` would silently win.
    expect(toListFilters(twice, NOW).modifiedAfter).toBe(
      new Date("2026-09-02T14:30:00.000Z").toISOString(),
    );
  });

  it("a non-exclusive group stacks, and toggling the same chip removes it", () => {
    const on = toggleChip(toggleChip({ ...EMPTY_FILTER_STATE }, "state:starred"), "state:shared");
    expect(on.chips).toEqual(["state:starred", "state:shared"]);
    expect(toggleChip(on, "state:starred").chips).toEqual(["state:shared"]);
  });

  it("an unknown chip id changes nothing", () => {
    const before = { ...EMPTY_FILTER_STATE, chips: ["state:shared"] };
    expect(toggleChip(before, "kind:hologram")).toBe(before);
  });
});

describe("the URL", () => {
  it("round-trips a filter state", () => {
    const state: FilterState = {
      text: "budget",
      scope: "drive",
      chips: ["kind:files", "state:starred", "size:custom"],
      ranges: { "size:custom": { from: "10", to: "20" } },
      ownerId: "usr_9",
      orderBy: { field: "mtime", direction: "desc" },
    };
    const restored = filterStateFromParams(filterStateToParams(state));
    expect(restored).toEqual(state);
  });

  it("an unfiltered state leaves a clean address", () => {
    expect(filterStateToParams(EMPTY_FILTER_STATE).toString()).toBe("");
  });

  it("opens a search on the whole drive, and carries only the narrowed scope", () => {
    // The default is the whole drive, so it costs the address nothing; narrowing
    // to the folder on screen is the choice that has to survive a reload.
    expect(filterStateFromParams(new URLSearchParams("")).scope).toBe("drive");
    expect(filterStateFromParams(new URLSearchParams("scope=folder")).scope).toBe("folder");
    expect(filterStateFromParams(new URLSearchParams("scope=nonsense")).scope).toBe("drive");
    expect(filterStateToParams({ ...EMPTY_FILTER_STATE, scope: "folder" }).get("scope")).toBe(
      "folder",
    );
    expect(filterStateToParams({ ...EMPTY_FILTER_STATE, scope: "drive" }).get("scope")).toBeNull();
  });

  it("drops a chip id it no longer knows rather than sending it on", () => {
    const params = new URLSearchParams("chips=kind:files,kind:hologram");
    expect(filterStateFromParams(params).chips).toEqual(["kind:files"]);
  });

  it("keeps parameters this module does not own", () => {
    const base = new URLSearchParams("pane=details&chips=state:shared");
    const next = filterStateToParams({ ...EMPTY_FILTER_STATE, chips: ["kind:files"] }, base);
    expect(next.get("pane")).toBe("details");
    expect(next.get("chips")).toBe("kind:files");
  });

  it("survives a remount — a filtered view is linkable", async () => {
    const user = userEvent.setup();
    const first = renderBar();
    await user.click(chip("Starred"));
    await user.click(chip("Folders"));
    const linked = first.currentSearch();
    expect(linked).toContain("chips=");
    first.view.unmount();

    const second = renderBar(`/files/nd_1${linked}`);
    expect(chip("Starred")).toHaveAttribute("aria-pressed", "true");
    expect(chip("Folders")).toHaveAttribute("aria-pressed", "true");
    expect(JSON.parse(screen.getByTestId("wire").textContent ?? "{}")).toEqual({
      kind: "folder",
      starred: "true",
    });
    second.view.unmount();
  });

  it("a chip click writes the URL", async () => {
    const user = userEvent.setup();
    const bar = renderBar();
    expect(bar.currentSearch()).toBe("");
    await user.click(chip("Code"));
    expect(bar.currentSearch()).toContain("chips=kind%3Acode");
  });
});

describe("the empty state", () => {
  it("names the active filters", () => {
    render(
      <MemoryRouter>
        <FilteredEmptyState
          state={{ ...EMPTY_FILTER_STATE, text: "budget", chips: ["kind:files", "state:starred"] }}
          onChange={vi.fn()}
        />
      </MemoryRouter>,
    );
    expect(screen.getByRole("status")).toHaveTextContent("“budget”");
    expect(screen.getByRole("status")).toHaveTextContent("Files");
    expect(screen.getByRole("status")).toHaveTextContent("Starred");
  });

  it("says the folder is empty when nothing is filtering it", () => {
    render(
      <MemoryRouter>
        <FilteredEmptyState state={EMPTY_FILTER_STATE} onChange={vi.fn()} />
      </MemoryRouter>,
    );
    expect(screen.getByRole("status")).toHaveTextContent("This folder is empty.");
    expect(screen.queryByRole("button", { name: "Clear filters" })).not.toBeInTheDocument();
  });

  it("clearing from the empty state resets the URL", async () => {
    const user = userEvent.setup();
    const bar = renderBar("/files/nd_1?q=budget&chips=kind:files&pane=details");
    expect(bar.currentSearch()).toContain("chips=");

    await user.click(screen.getAllByRole("button", { name: "Clear filters" })[0]);

    expect(bar.currentSearch()).not.toContain("chips=");
    expect(bar.currentSearch()).not.toContain("q=budget");
    // Clearing a filter is not a navigation: what the page owns stays.
    expect(bar.currentSearch()).toContain("pane=details");
  });

  it("the bar is one row with a summary of what a scroll would reach", async () => {
    const user = userEvent.setup();
    renderBar();
    const bar = screen.getByRole("group", { name: "Filters" });

    // Wrapping cost the head three rows at 1440 and overflowed the viewport at 390, so the
    // bar is a single row until someone asks for the rest. The count is computed here from
    // the registry, not from the bar's own arithmetic: everything past the leading group.
    const expected = CHIPS.filter((chip) => chip.group !== "kind").length;
    expect(expected).toBeGreaterThan(0);
    expect(bar).not.toHaveAttribute("data-expanded");
    const more = screen.getByRole("button", { name: `+${expected} more filters` });

    await user.click(more);
    expect(bar).toHaveAttribute("data-expanded", "true");
    expect(screen.getByRole("button", { name: "Fewer filters" })).toHaveAttribute(
      "aria-expanded",
      "true",
    );

    // Nothing was ever removed to make the row fit: a chip from the last group is still
    // reachable, which is why the summary is a scroll hint and not a filter that vanished.
    expect(screen.getByRole("button", { name: "Trashed" })).toBeInTheDocument();
  });

  it("clearing keeps the scope and the order", () => {
    const state: FilterState = {
      text: "x",
      scope: "drive",
      chips: ["kind:files"],
      ranges: { "size:custom": { from: "1" } },
      ownerId: "usr_9",
      orderBy: { field: "size", direction: "desc" },
    };
    const cleared = clearFilters(state);
    expect(cleared.chips).toEqual([]);
    expect(cleared.text).toBe("");
    expect(cleared.ranges).toEqual({});
    expect(cleared.ownerId).toBeUndefined();
    expect(cleared.scope).toBe("drive");
    expect(cleared.orderBy).toEqual({ field: "size", direction: "desc" });
    expect(activeFilterLabels(cleared)).toEqual([]);
  });
});
