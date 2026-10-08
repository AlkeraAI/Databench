// @vitest-environment jsdom
//
// A feed that has not answered yet says so, and paints when it does.
//
// The Recent feed was reported empty on the live portal while the route was
// answering 200 with a hundred rows. It is not empty and it is not stuck: on
// that stack `GET …/recent` takes seconds while every sibling feed answers in
// under one, and the feed sits on its busy line for the whole time. Every test
// over these feeds stubs an instant response, so none of them can tell the
// difference between slow, stuck and empty — which is exactly the distinction
// somebody reading the screen has to make.
//
// So the answer is held here until the assertions about the wait have been
// made, and only then released.

import { QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { Item } from "@/api/files";
import { createQueryClient } from "@/api/queryClient";
import { FEED_TEXT, FeedList } from "@/pages/workspace/files/FilesPage";

const DRIVE = "dr_1";

function item(over: Partial<Item> = {}): Item {
  return {
    id: "nd_recent",
    ino: 1,
    driveId: DRIVE,
    kind: "file",
    name: "opened-lately.md",
    nameDisplay: "opened-lately.md",
    nameEncoding: "utf-8",
    pathBytes: "/home/dana/opened-lately.md",
    parentId: "nd_home",
    parentName: "dana",
    etag: "0",
    ctag: "0",
    stale: false,
    locked: false,
    held: false,
    shared: false,
    trashed: false,
    ...over,
  } as Item;
}

/** A hundred rows, the size the live route answers with. */
const ROWS = Array.from({ length: 100 }, (_, at) =>
  item({ id: `nd_${at}`, name: `row-${at}.md`, nameDisplay: `row-${at}.md` }),
);

/** The answer, and the handle that lets it go. */
function heldFetch(): { release: (body: unknown) => void } {
  let release!: (body: unknown) => void;
  const answered = new Promise<unknown>((resolve) => {
    release = resolve;
  });
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => {
      const body = await answered;
      return new Response(JSON.stringify(body), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    }),
  );
  return { release };
}

function mount() {
  return render(
    <QueryClientProvider client={createQueryClient()}>
      <MemoryRouter>
        <FeedList driveId={DRIVE} place="recent" platform="other" />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("a feed whose read is slow", () => {
  it("says it is loading, and never reads as an empty feed", async () => {
    const { release } = heldFetch();
    mount();

    // The whole point: while the answer is outstanding the screen must not say
    // the feed holds nothing. That sentence sends a reader looking for a bug.
    expect(await screen.findByText("Loading recent…")).toBeInTheDocument();
    expect(screen.queryByText(FEED_TEXT.recent.empty)).toBeNull();
    expect(document.querySelectorAll("[data-row-id]").length).toBe(0);

    release({ value: ROWS, nextMarker: null });
    await waitFor(() => expect(screen.getByText("row-0.md")).toBeInTheDocument());
    expect(document.querySelectorAll("[data-row-id]").length).toBeGreaterThan(0);
    expect(screen.queryByText("Loading recent…")).toBeNull();
  });

  it("says the feed is empty only once the answer itself is empty", async () => {
    const { release } = heldFetch();
    mount();

    await screen.findByText("Loading recent…");
    release({ value: [], nextMarker: null });
    await waitFor(() => expect(screen.getByText(FEED_TEXT.recent.empty)).toBeInTheDocument());
  });

  it("marks the wait as busy, so a reader who cannot see it is told", async () => {
    heldFetch();
    mount();

    const line = await screen.findByText("Loading recent…");
    expect(line.getAttribute("aria-busy")).toBe("true");
  });
});
