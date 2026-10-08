import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useChildren } from "@/api/files";
import { createQueryClient } from "@/api/queryClient";
import type { DropEntry, DropTarget } from "@/pages/workspace/files/dropHandlers";
import { useUploads, type DropTransfer } from "@/pages/workspace/files/useUploads";

// The listing behind a tray, while the tray is still running. A drop of a dozen
// files is the case a per-batch refresh gets wrong: every file lands within the
// same moment, the coalescing window is re-armed by each one, and the folder a
// person is watching stays empty until the whole drop is over.
//
// The proof is the wire: how many times the folder's children were re-read, and
// when relative to the files settling. It is asserted as a band — at least one
// refresh before the last file has landed, and far fewer than one per file —
// because the exact number is the cadence's business, not the contract's.

const FOLDER = "folder-root";

const WRITABLE: DropTarget = {
  id: FOLDER,
  name: "Home",
  kind: "folder",
  capabilities: { can_write: true },
};

function fileEntry(name: string): DropEntry {
  const file = new File(["id,name\n1,ok\n"], name, { lastModified: Date.UTC(2020, 0, 1) });
  return { isFile: true, isDirectory: false, name, file: (onSuccess) => onSuccess(file) };
}

function transfer(entries: DropEntry[]): DropTransfer {
  return {
    items: entries.map((entry) => ({ kind: "file", webkitGetAsEntry: () => entry })),
    getData: () => "",
  };
}

interface Wire {
  /** How many times the folder's children have been read. */
  listings: number;
  /** How many uploads have been completed by the server. */
  completed: number;
  /** `listings` as it stood when each file completed. */
  listingsAtCompletion: number[];
}

function stubFetch(): Wire {
  let sessions = 0;
  const wire: Wire = { listings: 0, completed: 0, listingsAtCompletion: [] };
  const json = (payload: unknown, status = 200) =>
    new Response(JSON.stringify(payload), {
      status,
      headers: { "content-type": "application/json" },
    });

  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const asRequest = input instanceof Request ? input : null;
      const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
      const path = new URL(url, "http://localhost").pathname;
      const method = (init?.method ?? asRequest?.method ?? "GET").toUpperCase();

      if (method === "POST" && path.endsWith("/api/v1/files/uploads")) {
        sessions += 1;
        return json({ uploadId: `sess-${sessions}`, partSize: 1024, partsTotal: 1, expiresAt: "" });
      }
      const session = /\/uploads\/(sess-\d+)(\/|$)/.exec(path)?.[1];
      if (session && path.endsWith("/complete")) {
        wire.completed += 1;
        wire.listingsAtCompletion.push(wire.listings);
        return json({ item: { id: `node-${session}`, etag: "etag-1" }, unchanged: false });
      }
      if (session && method === "GET") {
        return json({
          uploadId: session,
          state: "open",
          offset: 0,
          length: 1024,
          complete: false,
          partsDone: 0,
          partsTotal: 1,
          acceptedParts: [],
        });
      }
      if (session && method === "PUT") return json({ ok: true });
      if (method === "PATCH") return json({ id: "node", etag: "etag-2" });
      if (method === "GET" && /\/children$/.test(path)) {
        wire.listings += 1;
        return json({ value: [], nextMarker: null });
      }
      return json({ code: "unexpected", message: url }, 500);
    }),
  );
  return wire;
}

function memoryStorage() {
  const store = new Map<string, string>();
  return {
    getItem: (key: string) => store.get(key) ?? null,
    setItem: (key: string, value: string) => void store.set(key, value),
    removeItem: (key: string) => void store.delete(key),
  };
}

/** The tray and the listing of the folder it is dropping into, side by side —
 *  which is what the Files page is while a drop runs. */
function Harness({ entries }: { entries: DropEntry[] }) {
  const uploads = useUploads({
    driveId: "drive-1",
    doneLingerMs: 60_000,
    clientOptions: { storage: memoryStorage(), digest: async () => "deadbeef" },
  });
  const children = useChildren("drive-1", FOLDER);
  return (
    <div>
      <button type="button" onClick={() => void uploads.onDrop(WRITABLE, transfer(entries))}>
        drop
      </button>
      <output>{children.isSuccess ? "listed" : "listing"}</output>
      <output data-testid="finished">{uploads.finished}</output>
    </div>
  );
}

function mount(entries: DropEntry[]) {
  return render(
    <QueryClientProvider client={createQueryClient()}>
      <MemoryRouter>
        <Harness entries={entries} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const names = (count: number) =>
  Array.from({ length: count }, (_, index) => fileEntry(`f${index + 1}.csv`));

let wire: Wire;

beforeEach(() => {
  wire = stubFetch();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("the listing behind a running tray", () => {
  it("picks up a twelve-file drop as it lands, not when the batch is over", async () => {
    const user = userEvent.setup();
    mount(names(12));
    await waitFor(() => expect(screen.getByText("listed")).toBeInTheDocument());
    const beforeDrop = wire.listings;

    await user.click(screen.getByRole("button", { name: "drop" }));
    await waitFor(() => expect(screen.getByTestId("finished")).toHaveTextContent("12"), {
      timeout: 10_000,
    });
    await waitFor(() => expect(wire.listings).toBeGreaterThan(beforeDrop));

    // The listing was re-read while files were still completing: at least one
    // refresh landed before the last file did. Comparing the refresh count at the
    // first completion with the count at the last is what distinguishes "the rows
    // grow as files land" from "the rows appear when the tray settles".
    const [atFirst] = wire.listingsAtCompletion;
    const atLast = wire.listingsAtCompletion.at(-1) ?? 0;
    expect(wire.listingsAtCompletion).toHaveLength(12);
    expect(atLast).toBeGreaterThan(atFirst ?? 0);

    // And it is still coalesced: nowhere near one refetch per file.
    const refreshes = wire.listings - beforeDrop;
    expect(refreshes).toBeGreaterThanOrEqual(1);
    expect(refreshes).toBeLessThan(12);
  }, 20_000);
});
