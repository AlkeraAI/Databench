// The three Files columns are the reader's to arrange, and the arrangement
// keeps.
//
// One answer for the whole drive, not one per folder: the rail names the same
// places and the details pane says the same kind of thing wherever the reader
// is, so a width set in one folder is the width they meant everywhere. Below
// the breakpoint there are no columns to arrange, so there is nothing to
// remember either.

import { QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ReactNode } from "react";

import { createQueryClient } from "@/api/queryClient";
import {
  FILES_NARROW_QUERY,
  FILES_PANE_BOUNDS,
  FILES_RAIL_BOUNDS,
  FilesPage,
} from "@/pages/workspace/files/FilesPage";

function stubViewport(narrow: boolean): void {
  vi.stubGlobal(
    "matchMedia",
    (query: string): MediaQueryList =>
      ({
        media: query,
        matches: query === FILES_NARROW_QUERY ? narrow : false,
        onchange: null,
        addEventListener: () => undefined,
        removeEventListener: () => undefined,
        addListener: () => undefined,
        removeListener: () => undefined,
        dispatchEvent: () => false,
      }) as unknown as MediaQueryList,
  );
}

function mount(pane: ReactNode = <p>details</p>) {
  return render(
    <QueryClientProvider client={createQueryClient({ retry: false })}>
      <MemoryRouter initialEntries={["/files/nd_1"]}>
        <Routes>
          <Route
            path="/files/:nodeId"
            element={<FilesPage browser={<div>listing</div>} pane={pane} />}
          />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function gutter(name: string): HTMLElement {
  return screen.getByRole("separator", { name });
}

function widthOf(paneId: string): number {
  const split = document.querySelector<HTMLElement>(".alk-split");
  return Number.parseInt(split?.style.getPropertyValue(`--alk-split-${paneId}`) ?? "0", 10);
}

function stored(key: string): { collapsed: boolean; width: number } | null {
  const raw = window.localStorage.getItem(`alkera.size:${key}`);
  return raw ? (JSON.parse(raw) as { collapsed: boolean; width: number }) : null;
}

beforeEach(() => {
  vi.unstubAllGlobals();
  window.localStorage.clear();
  stubViewport(false);
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  window.localStorage.clear();
});

describe("the Files columns", () => {
  it("offers a gutter for each side column, reachable by keyboard", () => {
    mount();
    expect(gutter("Resize Places")).toHaveAttribute("tabindex", "0");
    expect(gutter("Resize Details")).toHaveAttribute("tabindex", "0");
    expect(screen.getByRole("region", { name: "Files" })).toBeInTheDocument();
    expect(screen.getByRole("complementary", { name: "Details" })).toBeInTheDocument();
  });

  it("comes back at the width the reader dragged to", () => {
    const first = mount();
    const before = widthOf("rail");
    fireEvent.keyDown(gutter("Resize Places"), { key: "ArrowRight", shiftKey: true });
    const after = widthOf("rail");
    expect(after).toBeGreaterThan(before);
    first.unmount();

    // A fresh mount is the next visit: nothing but the browser carried this.
    mount();
    expect(widthOf("rail")).toBe(after);
    expect(stored("files.rail")?.width).toBe(after);
  });

  it("remembers a column folded away, and the strip brings it back", () => {
    const first = mount();
    fireEvent.keyDown(gutter("Resize Details"), { key: "Enter" });
    expect(screen.queryByRole("complementary", { name: "Details" })).toBeNull();
    first.unmount();

    mount();
    expect(screen.queryByRole("complementary", { name: "Details" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Show Details" }));
    expect(screen.getByRole("complementary", { name: "Details" })).toBeInTheDocument();
  });

  it("refuses a width outside the range, however it got into the store", () => {
    window.localStorage.setItem(
      "alkera.size:files.rail",
      JSON.stringify({ collapsed: false, width: 4_000 }),
    );
    window.localStorage.setItem("alkera.size:files.pane", JSON.stringify({ width: "wide" }));
    mount();
    expect(widthOf("rail")).toBe(FILES_RAIL_BOUNDS.max);
    expect(widthOf("details")).toBe(FILES_PANE_BOUNDS.size);
  });

  it("stacks into one column below the breakpoint, with no gutters to arrange", () => {
    stubViewport(true);
    mount();
    expect(screen.queryAllByRole("separator")).toHaveLength(0);
    expect(document.querySelector(".alk-split")).toBeNull();
    // The details pane is the sheet the narrow layout raises, not a column.
    expect(screen.getByRole("dialog", { hidden: true })).toBeInTheDocument();
  });

  it("lays out with no details pane at all, and keeps the rail's gutter", () => {
    mount(null);
    expect(gutter("Resize Places")).toBeInTheDocument();
    expect(screen.queryByRole("separator", { name: "Resize Details" })).toBeNull();
    expect(screen.getByText("listing")).toBeInTheDocument();
  });

  it("lays out when the store refuses to answer", () => {
    vi.stubGlobal("localStorage", {
      getItem: () => {
        throw new Error("denied");
      },
      setItem: () => {
        throw new Error("denied");
      },
      removeItem: () => undefined,
    } as unknown as Storage);
    mount();
    expect(widthOf("rail")).toBe(FILES_RAIL_BOUNDS.size);
    expect(screen.getByRole("region", { name: "Files" })).toBeInTheDocument();
  });
});
