import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Item } from "@/api/files";
import { createQueryClient } from "@/api/queryClient";
import { ThresholdFooter, describeShown } from "@/pages/workspace/files/ThresholdFooter";
import { useSoftThreshold } from "@/pages/workspace/files/useSoftThreshold";

// The soft threshold driven through the real hook against a stubbed 25,000-child
// folder: pages of 500 come back from `fetch`, so what the test asserts is how many
// rows the hook actually loaded and what the footer said about them — never a mock's
// echo. The listing's own count is the negative control for the footer's total: the
// footer must say 25,000 (the folder's `dir_stats`) while only 20,000 are loaded.

const DRIVE = "drv_1";
const PARENT = "nd_parent";
const CHILDREN = 25_000;
const PAGE = 500;

/** A page is only ever built from indices, so a "row" costs a single object. */
function pageAt(offset: number): { value: Item[]; nextMarker: string | null } {
  const value: Item[] = [];
  const end = Math.min(offset + PAGE, CHILDREN);
  for (let index = offset; index < end; index += 1) {
    value.push({ id: `nd_${index}`, name: `f${index}`, nameDisplay: `f${index}` } as Item);
  }
  return { value, nextMarker: end < CHILDREN ? String(end) : null };
}

let pageRequests = 0;
/** How long a children page takes to come back. Zero everywhere except the cancel
 *  test, which needs the background load to still be in flight when it clicks. */
let pageDelayMs = 0;

function stubDrive(): void {
  pageRequests = 0;
  pageDelayMs = 0;
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL) => {
      const raw =
        typeof input === "string" ? input : input instanceof Request ? input.url : input.toString();
      const url = new URL(raw, "http://localhost");
      let body: unknown;
      if (url.pathname.endsWith("/children")) {
        pageRequests += 1;
        body = pageAt(Number(url.searchParams.get("marker") ?? "0"));
      } else {
        // The parent folder itself, carrying the aggregate the footer reads.
        body = { id: PARENT, kind: "folder", dirStats: { direct_children: CHILDREN } };
      }
      const answer = new Response(JSON.stringify(body), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
      if (pageDelayMs === 0) return Promise.resolve(answer);
      return new Promise<Response>((resolve) => setTimeout(() => resolve(answer), pageDelayMs));
    }),
  );
}

/** The hook and the footer wired exactly as the browser wires them, plus a probe
 *  that puts the loaded count somewhere the test can read it. */
function Harness() {
  const threshold = useSoftThreshold(DRIVE, PARENT, { limit: PAGE });
  return (
    <>
      <output data-testid="loaded">{threshold.loaded}</output>
      <ThresholdFooter {...threshold} />
    </>
  );
}

function mount() {
  const client = createQueryClient({ retry: false });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <Harness />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function loaded(): number {
  return Number(screen.getByTestId("loaded").textContent);
}

async function settledAt(count: number): Promise<void> {
  await waitFor(() => expect(loaded()).toBe(count), { timeout: 20_000 });
}

beforeEach(stubDrive);
afterEach(() => vi.unstubAllGlobals());

describe("the soft threshold", () => {
  it("stops at 20,000 rows and names the folder's true total", async () => {
    mount();
    await settledAt(20_000);

    // The ceiling is the whole mechanism: it must hold even after the hook has
    // had time to keep paging.
    await new Promise((resolve) => setTimeout(resolve, 30));
    expect(loaded()).toBe(20_000);
    expect(pageRequests).toBe(40);

    expect(screen.getByText("20,000 of about 25,000 shown")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Load more" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Load all" })).toBeInTheDocument();
    // The filter bar is retired, and so is the footer button that focused it.
    expect(screen.queryByRole("button", { name: /filter/i })).not.toBeInTheDocument();
  }, 30_000);

  it("Load more appends the remaining 5,000 and the total becomes exact", async () => {
    const user = userEvent.setup();
    mount();
    await settledAt(20_000);

    await user.click(screen.getByRole("button", { name: "Load more" }));
    await settledAt(CHILDREN);

    // The last page came back without a marker, so the count is no longer "about".
    await waitFor(() => expect(screen.getByText("25,000 of 25,000 shown")).toBeInTheDocument());
    expect(screen.queryByRole("button", { name: "Load more" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Load all" })).not.toBeInTheDocument();
  }, 30_000);

  it("Load all pages to the end", async () => {
    const user = userEvent.setup();
    mount();
    await settledAt(20_000);

    await user.click(screen.getByRole("button", { name: "Load all" }));
    await settledAt(CHILDREN);
    expect(screen.getByText("25,000 of 25,000 shown")).toBeInTheDocument();
  }, 30_000);

  it("cancelling a Load all stops it and keeps every row already loaded", async () => {
    const user = userEvent.setup();
    mount();
    await settledAt(20_000);

    pageDelayMs = 30;
    await user.click(screen.getByRole("button", { name: "Load all" }));
    await user.click(await screen.findByRole("button", { name: "Cancel" }));

    const kept = loaded();
    expect(kept).toBeGreaterThanOrEqual(20_000);
    expect(kept).toBeLessThan(CHILDREN);

    // Nothing keeps paging after the cancel, and nothing already fetched is thrown away.
    await new Promise((resolve) => setTimeout(resolve, 60));
    expect(loaded()).toBeLessThan(CHILDREN);
    expect(loaded()).toBeGreaterThanOrEqual(kept);
    expect(screen.getByRole("button", { name: "Load more" })).toBeInTheDocument();
  }, 30_000);
});

describe("a folder too new to have been aggregated", () => {
  /** The same wire as above with ONE difference: the parent folder carries no
   *  `dir_stats` at all, which is what a folder created a moment ago looks like
   *  until the first aggregation pass runs. */
  function stubFreshDrive(total: number, pageSize: number): void {
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL) => {
        const raw =
          typeof input === "string"
            ? input
            : input instanceof Request
              ? input.url
              : input.toString();
        const url = new URL(raw, "http://localhost");
        let body: unknown;
        if (url.pathname.endsWith("/children")) {
          const offset = Number(url.searchParams.get("marker") ?? "0");
          const end = Math.min(offset + pageSize, total);
          const value: Item[] = [];
          for (let i = offset; i < end; i += 1) {
            value.push({ id: `nd_${i}`, name: `f${i}`, nameDisplay: `f${i}` } as Item);
          }
          body = { value, nextMarker: end < total ? String(end) : null };
        } else {
          body = { id: PARENT, kind: "folder" };
        }
        return Promise.resolve(
          new Response(JSON.stringify(body), {
            status: 200,
            headers: { "content-type": "application/json" },
          }),
        );
      }),
    );
  }

  function mountFresh(pageSize: number) {
    const client = createQueryClient({ retry: false });
    function Fresh() {
      const threshold = useSoftThreshold(DRIVE, PARENT, { limit: pageSize, threshold: pageSize });
      return (
        <>
          <output data-testid="loaded">{threshold.loaded}</output>
          <ThresholdFooter {...threshold} />
        </>
      );
    }
    return render(
      <QueryClientProvider client={client}>
        <MemoryRouter>
          <Fresh />
        </MemoryRouter>
      </QueryClientProvider>,
    );
  }

  it("counts from its own listing once the listing ends", async () => {
    stubFreshDrive(3, 500);
    mountFresh(500);
    // No aggregate exists, so the rows themselves are the answer — and they are
    // the exact answer, not an "about".
    await waitFor(() => expect(screen.getByText("3 of 3 shown")).toBeInTheDocument());
  });

  it("says it is still counting while neither the aggregate nor the listing knows", async () => {
    stubFreshDrive(30, 10);
    mountFresh(10);
    await waitFor(() => expect(loaded()).toBe(10));
    // The floor is on screen and the sentence admits the total is not known yet,
    // instead of reading as a bare count that looks like the whole folder.
    expect(screen.getByText("10 shown · counting…")).toBeInTheDocument();
    expect(screen.queryByText("10 of 10 shown")).not.toBeInTheDocument();
  });
});

describe("the footer's sentence", () => {
  it("says 'about' only while the total may lag, and admits when there is none", () => {
    expect(describeShown(20_000, 1_204_311, false)).toBe("20,000 of about 1,204,311 shown");
    expect(describeShown(1_204_311, 1_204_311, true)).toBe("1,204,311 of 1,204,311 shown");
    expect(describeShown(12, null, false)).toBe("12 shown");
    expect(describeShown(12, null, false, true)).toBe("12 shown · counting…");
  });

  it("counts the rows kept out of sight out of what is shown, and names them", () => {
    expect(describeShown(5, 5, true, false, 2)).toBe("3 of 5 shown · 2 hidden");
    expect(describeShown(20_000, 1_204_311, false, false, 12)).toBe("19,988 of about 1,204,311 shown · 12 hidden");
    expect(describeShown(4, null, false, false, 1)).toBe("3 shown · 1 hidden");
    // Never more hidden than loaded, and nothing said when none are.
    expect(describeShown(2, 2, true, false, 9)).toBe("0 of 2 shown · 2 hidden");
    expect(describeShown(5, 5, true, false, 0)).toBe("5 of 5 shown");
  });

  it("offers no way to load more once the listing has ended", () => {
    render(
      <ThresholdFooter
        loaded={4}
        total={4}
        exact
        hasMore={false}
        loadingAll={false}
        loadMore={() => {}}
        loadAll={() => {}}
        cancelLoadAll={() => {}}
      />,
    );
    expect(screen.queryByRole("button", { name: "Load more" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Load all" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Cancel" })).not.toBeInTheDocument();
  });

  it("shows a progress count instead of the load actions while paging in the background", () => {
    render(
      <ThresholdFooter
        loaded={3_500}
        total={25_000}
        exact={false}
        hasMore
        loadingAll
        loadMore={() => {}}
        loadAll={() => {}}
        cancelLoadAll={() => {}}
      />,
    );
    expect(screen.getByText("Loading all · 3,500 so far")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Cancel" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Load all" })).not.toBeInTheDocument();
  });
});

describe("an empty folder", () => {
  const props = {
    loadingAll: false,
    loadMore: () => undefined,
    loadAll: () => undefined,
    cancelLoadAll: () => undefined,
  };

  it.each([
    ["an aggregate of zero", { total: 0, exact: true }],
    ["no aggregate yet", { total: null, exact: false }],
  ])("says the folder is empty, not '0 of 0 shown', with %s", (_case, counts) => {
    render(<ThresholdFooter {...props} {...counts} loaded={0} hasMore={false} />);
    expect(screen.getByText("This folder is empty.")).toBeInTheDocument();
    expect(screen.queryByText(/0 of 0 shown|^0 shown$/)).toBeNull();
  });

  it.each([
    ["more rows are still coming", { total: null, exact: false, hasMore: true, counting: false }],
    ["the count is still running", { total: null, exact: false, hasMore: false, counting: true }],
    ["a filter hides every row of a folder that has some", { total: 12, exact: true, hasMore: false, counting: false }],
  ])("keeps the count when %s", (_case, state) => {
    render(<ThresholdFooter {...props} {...state} loaded={0} />);
    expect(screen.queryByText("This folder is empty.")).toBeNull();
  });
});
