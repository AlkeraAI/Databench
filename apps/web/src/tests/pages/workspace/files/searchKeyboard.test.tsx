/**
 * The search results are reachable from the search box by arrow key.
 *
 * ArrowDown left the caret in the input: no result focused, no
 * `aria-activedescendant`, nothing moved. The only way into a table of hits was
 * Tab. For a control that answers with rows, ArrowDown is the move a reader
 * makes, and Enter on the row they land on is what opens it.
 */

import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { Item } from "@/api/files";
import { SearchBar, SearchResults } from "@/pages/workspace/files/SearchBar";
import type { FilterState } from "@/pages/workspace/files/filterState";
import type { SearchQueryResult } from "@/pages/workspace/files/useSearchQuery";

afterEach(cleanup);

function item(id: string, name: string): Item {
  return {
    id,
    ino: 1,
    driveId: "drv_1",
    kind: "file",
    name,
    nameDisplay: name,
    etag: "e1",
    path: `/home/admin/${name}`,
    attrs: null,
    file: null,
    object: null,
    lease: null,
    capabilities: {},
    trashed: false,
  } as unknown as Item;
}

const HITS = [
  item("nd_a", "qa-multi-01.txt"),
  item("nd_b", "qa-multi-02.txt"),
  item("nd_c", "qa-multi-03.txt"),
];

const STATE: FilterState = { text: "qa-multi", scope: "drive" } as FilterState;

function resultOf(items: readonly Item[], text: string): SearchQueryResult {
  return {
    items,
    settledText: text,
    isActive: true,
    isFetching: false,
  } as unknown as SearchQueryResult;
}

function result(): SearchQueryResult {
  return resultOf(HITS, "qa-multi");
}

function mount(onOpen = vi.fn()) {
  render(
    <>
      <SearchBar state={STATE} onChange={() => {}} platform="other" />
      <SearchResults result={result()} onOpen={onOpen} platform="other" />
    </>,
  );
  return { onOpen, field: screen.getByRole("searchbox", { name: "Search" }) };
}

function activeRowId(): string | null {
  return document.activeElement?.closest("[data-row-id]")?.getAttribute("data-row-id") ?? null;
}

/** The rows Tab can land on, in the order they are drawn. */
function tabStops(): string[] {
  return Array.from(document.querySelectorAll('[data-row-id][tabindex="0"]')).map(
    (element) => element.getAttribute("data-row-id") ?? "",
  );
}

describe("walking into the search results from the box", () => {
  it("lands on the first hit on ArrowDown", async () => {
    const user = userEvent.setup();
    const { field } = mount();
    field.focus();

    await user.keyboard("{ArrowDown}");

    expect(activeRowId()).toBe("nd_a");
    expect(document.activeElement).not.toBe(field);
  });

  it("walks down the hits and back up them", async () => {
    const user = userEvent.setup();
    const { field } = mount();
    field.focus();

    await user.keyboard("{ArrowDown}{ArrowDown}{ArrowDown}");
    expect(activeRowId()).toBe("nd_c");
    // The last hit is the floor: another press does not fall off the list.
    await user.keyboard("{ArrowDown}");
    expect(activeRowId()).toBe("nd_c");

    await user.keyboard("{ArrowUp}");
    expect(activeRowId()).toBe("nd_b");
  });

  it("gives the caret back to the box above the first hit", async () => {
    const user = userEvent.setup();
    const { field } = mount();
    field.focus();

    await user.keyboard("{ArrowDown}{ArrowUp}");

    expect(document.activeElement).toBe(field);
  });

  it("marks the row it walked to as the selected one", async () => {
    const user = userEvent.setup();
    const { field } = mount();
    field.focus();

    await user.keyboard("{ArrowDown}{ArrowDown}");

    const selected = Array.from(
      document.querySelectorAll('[data-row-id][aria-selected="true"]'),
    ).map((element) => element.getAttribute("data-row-id"));
    expect(selected).toEqual(["nd_b"]);
  });

  it("opens the hit the reader walked to", async () => {
    const user = userEvent.setup();
    const { field, onOpen } = mount();
    field.focus();

    await user.keyboard("{ArrowDown}{ArrowDown}{Enter}");

    expect(onOpen).toHaveBeenCalledTimes(1);
    expect(onOpen.mock.calls[0]?.[0]).toMatchObject({ id: "nd_b" });
  });

  it("leaves the caret alone for the keys that edit the query", async () => {
    const user = userEvent.setup();
    const { field } = mount();
    field.focus();

    // A search box is a text field first: Home, End and the sideways arrows
    // belong to the text in it, not to the list under it.
    await user.keyboard("{Home}{End}{ArrowLeft}{ArrowRight}");

    expect(document.activeElement).toBe(field);
  });

  it("keeps a tab stop on the results when the query changes under the walked-to row", async () => {
    const user = userEvent.setup();
    const onOpen = vi.fn();
    const { rerender } = render(
      <>
        <SearchBar state={STATE} onChange={() => {}} platform="other" />
        <SearchResults result={result()} onOpen={onOpen} platform="other" />
      </>,
    );
    screen.getByRole("searchbox", { name: "Search" }).focus();
    await user.keyboard("{ArrowDown}{ArrowDown}");
    expect(activeRowId()).toBe("nd_b");

    // One more character in the box, and the answer no longer holds the row the
    // walk had landed on — the ordinary way a search narrows.
    rerender(
      <>
        <SearchBar state={{ ...STATE, text: "qa-multi-04" }} onChange={() => {}} platform="other" />
        <SearchResults
          result={resultOf([item("nd_z", "qa-multi-04.txt")], "qa-multi-04")}
          onOpen={onOpen}
          platform="other"
        />
      </>,
    );

    // Exactly one: with none the results are reachable by nothing but the pointer,
    // and with several the Tab key stops inside the list instead of leaving it.
    expect(tabStops()).toEqual(["nd_z"]);
  });

  it("leaves the tab stop where the walk left it when the new answer still lists it", async () => {
    const user = userEvent.setup();
    const onOpen = vi.fn();
    const { rerender } = render(
      <>
        <SearchBar state={STATE} onChange={() => {}} platform="other" />
        <SearchResults result={result()} onOpen={onOpen} platform="other" />
      </>,
    );
    screen.getByRole("searchbox", { name: "Search" }).focus();
    await user.keyboard("{ArrowDown}{ArrowDown}");

    // A refetch of the same query that drops the rows below the reader. The row
    // they are on is still there and is not the first, so the stop must stay
    // with it rather than fall back to the top: a fallback that always picked
    // the first row would answer "nd_a" here.
    rerender(
      <>
        <SearchBar state={STATE} onChange={() => {}} platform="other" />
        <SearchResults
          result={resultOf(HITS.slice(0, 2), "qa-multi")}
          onOpen={onOpen}
          platform="other"
        />
      </>,
    );

    expect(tabStops()).toEqual(["nd_b"]);
  });
});
