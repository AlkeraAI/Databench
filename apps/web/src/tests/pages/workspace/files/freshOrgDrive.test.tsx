import { QueryClientProvider, type QueryClient } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Item } from "@/api/files";
import { createQueryClient } from "@/api/queryClient";
import { FILES_NARROW_QUERY, FilesScreen } from "@/pages/workspace/files/FilesPage";

// `/files` carries no node, so the page's ONLY way out of it is the drive's
// root listing. Three answers end that listing with nowhere to go, and none of
// them may leave "Opening your files…" or "Waiting for your drive to load." on
// screen for ever with no error and no retry:
//
//   * the drive comes back naming no root node, so the root listing is never
//     asked for at all;
//   * the root listing refuses;
//   * the drive read itself refuses.
//
// Each is driven here through the real route over a scripted network, together
// with the fresh-org answer that must still converge: a brand-new org's drive
// is provisioned on its first read and its root holds `home`, which is where
// `/files` lands.

function folder(id: string, name: string): Item {
  return {
    id,
    ino: 3,
    driveId: "dr_new",
    kind: "folder",
    name,
    nameDisplay: name,
    nameEncoding: "utf-8",
    pathBytes: `/${name}`,
    path: null,
    parentId: "nd_root",
    etag: "0",
    ctag: "0",
    stale: false,
    locked: false,
    held: false,
    shared: false,
    trashed: false,
    capabilities: {
      can_read: true,
      can_write: true,
      can_share: true,
      can_delete: true,
      can_rename: true,
      can_download: true,
    },
  } as unknown as Item;
}

/** What a brand-new org's drive read answers: one bare object, camelCase, with
 *  the root node the listing is addressed by. */
const FRESH_DRIVE = {
  id: "dr_new",
  orgId: "or_new",
  rootId: "nd_root",
  homeId: "nd_home",
  quotaBytes: 100_000_000_000,
};

interface Script {
  /** Every request the page made, in order. */
  seen: string[];
  /** Answered for the drive read from the second call on, so a retry can heal. */
  healed: boolean;
  /** True once the ROOT listing has been ANSWERED with its refusal. A request
   *  that is out but not back has refused nothing yet, so a case about what a
   *  refusal does to the page has to wait for this rather than for the request. */
  refusedRoot: boolean;
}

/** The one row home lists, named so a test can wait for it by sight. */
const HOME_ROW = "quarter-close.md";

interface StubOptions {
  /** The drive body the first read answers. */
  drive?: Record<string, unknown>;
  /** Status the drive read answers with (200 unless set). */
  driveStatus?: number;
  /** Status the ROOT listing answers with (200 unless set). */
  rootStatus?: number;
}

function stubApi(options: StubOptions = {}): Script {
  const script: Script = { seen: [], healed: false, refusedRoot: false };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = input instanceof Request ? input.url : String(input);
      script.seen.push(url);
      const answer = (body: unknown, status = 200) =>
        new Response(JSON.stringify(body), {
          status,
          headers: { "content-type": "application/json" },
        });

      if (/\/files\/drives\/?(\?|$)/.test(url)) {
        if (script.healed) return answer(FRESH_DRIVE);
        if (options.driveStatus && options.driveStatus !== 200) {
          return answer({ code: "files.denied", message: "no" }, options.driveStatus);
        }
        return answer(options.drive ?? FRESH_DRIVE);
      }
      if (url.includes("/items/nd_root/children")) {
        if (!script.healed && options.rootStatus && options.rootStatus !== 200) {
          script.refusedRoot = true;
          return answer({ code: "files.denied", message: "no" }, options.rootStatus);
        }
        return answer({ value: [folder("nd_home", "home"), folder("nd_shared", "Shared")], nextMarker: null });
      }
      // Home holds something, so "the reader reached their files" is a row on
      // screen rather than the empty frame the page paints before it has asked.
      if (url.includes("/items/nd_home/children")) {
        return answer({ value: [folder("nd_report", HOME_ROW)], nextMarker: null });
      }
      if (url.includes("/children")) return answer({ value: [], nextMarker: null });
      if (url.includes("/leases")) return answer([]);
      if (url.includes("/items/")) return answer(folder("nd_home", "home"));
      return answer({});
    }),
  );
  return script;
}

function stubViewport(): void {
  vi.stubGlobal(
    "matchMedia",
    (query: string): MediaQueryList =>
      ({
        media: query,
        matches: query === FILES_NARROW_QUERY ? false : false,
        onchange: null,
        addEventListener: () => undefined,
        removeEventListener: () => undefined,
        addListener: () => undefined,
        removeListener: () => undefined,
        dispatchEvent: () => false,
      }) as unknown as MediaQueryList,
  );
}

/** Every client a case mounted, so the case can be taken off the network before
 *  its environment goes away. A read still in flight answers into a cache whose
 *  page is gone, and the notification it schedules outlives the jsdom it was
 *  written for. */
const mounted: QueryClient[] = [];

function mount(at = "/files") {
  // No retries: an error path must settle in the test as fast as it settles the
  // page's own state, not three backoffs later. Overridden through the factory's
  // own argument, never `setDefaultOptions` — that call REPLACES the defaults
  // rather than merging into them, so it took the portal's `staleTime` and
  // `refetchOnWindowFocus: false` with it and left every query stale the instant
  // it answered. The counts below (exactly one drive read) then depend on no
  // second observer, remount or focus change landing after the first answer —
  // ambient timing this page's behaviour does not actually have.
  const client = createQueryClient({ retry: false });
  mounted.push(client);
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[at]}>
        <Routes>
          <Route path="/files" element={<FilesScreen platform="mac" />} />
          <Route path="/files/trash" element={<FilesScreen trash platform="mac" />} />
          <Route path="/files/:nodeId" element={<FilesScreen platform="mac" />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const browserMain = () => screen.getByRole("region", { name: "Files" });

beforeEach(() => stubViewport());
// Unmount first — a read that answers afterwards then has no observer to tell —
// and only then take each client off the network and drop its cache.
afterEach(async () => {
  cleanup();
  for (const client of mounted.splice(0)) {
    await client.cancelQueries();
    client.clear();
  }
  vi.unstubAllGlobals();
});

describe("/files opens a brand-new org's drive", () => {
  it("provisions on the first read, lands on home and lights the rail", async () => {
    const script = stubApi();
    mount();

    await waitFor(() => expect(screen.getByRole("treegrid")).toBeTruthy());
    // The one drive read answered a bare object, and the page addressed the
    // root listing with the `rootId` it named — not a list, not `root_id`.
    expect(script.seen.filter((url) => /\/files\/drives\/?(\?|$)/.test(url))).toHaveLength(1);
    expect(script.seen.some((url) => url.includes("/items/nd_root/children"))).toBe(true);
    // `/files` resolved to the home folder rather than staying on the index —
    // a second hop the page takes once the root has answered, so it is waited
    // for rather than read off the first paint.
    await waitFor(() =>
      expect(script.seen.some((url) => url.includes("/items/nd_home/children"))).toBe(true),
    );
    // The rail's node-addressed place is reachable, so it no longer explains
    // itself with "Waiting for your drive to load."
    expect(screen.queryByText("Waiting for your drive to load.")).toBeNull();
  });
});

describe("/files never waits on a listing that is not coming", () => {
  it("offers a retry when the drive names no root node, and heals on it", async () => {
    // The listing is addressed by the root node, so a drive answered without one
    // leaves the query DISABLED: no request is made, nothing fails, and the page
    // has nothing to wait for.
    const script = stubApi({ drive: { id: "dr_new", orgId: "or_new", quotaBytes: 0 } });
    mount();

    await waitFor(() =>
      expect(refusalInBrowserSlot("Your files could not be opened.")).toBeTruthy(),
    );
    expect(script.seen.some((url) => url.includes("/children"))).toBe(false);

    script.healed = true;
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    await waitFor(() => expect(screen.getByRole("treegrid")).toBeTruthy());
  });

  it("opens home from the drive's own home id even when the root listing refuses", async () => {
    // The drive names the caller's home directly, so the root listing (which only
    // feeds the rail's places) refusing must not stand between the reader and
    // their files: home lists, and the refusal is not painted over it.
    const script = stubApi({ rootStatus: 403 });
    mount();

    // Both halves are waited for, and neither is the treegrid frame: the frame
    // is in the DOM a beat BEFORE the listing under it is even asked for, so a
    // case that reads the page the moment it appears is asking about a page
    // that has neither listed home nor heard the refusal yet.
    await waitFor(() => expect(script.refusedRoot).toBe(true));
    expect(await screen.findByText(HOME_ROW)).toBeInTheDocument();
    expect(script.seen.some((url) => url.includes("/items/nd_home/children"))).toBe(true);
    // The refusal is back and home is on screen: the browser slot showing the
    // rows and not the refusal is now a statement about the page's answer.
    expect(() => refusalInBrowserSlot("Your files could not be opened.")).toThrow();
  });

  it("offers a retry when the drive read itself refuses", async () => {
    const script = stubApi({ driveStatus: 403 });
    mount();

    await waitFor(() =>
      expect(refusalInBrowserSlot("Your files could not be opened.")).toBeTruthy(),
    );

    script.healed = true;
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    await waitFor(() => expect(screen.getByRole("treegrid")).toBeTruthy());
  });
});

/** The refusal belongs in the browser slot, not anywhere on the page. */
function refusalInBrowserSlot(text: string): HTMLElement {
  const main = browserMain();
  const found = Array.from(main.querySelectorAll("p")).find((node) => node.textContent === text);
  if (!found) throw new Error(`"${text}" is not in the browser slot`);
  return found;
}
