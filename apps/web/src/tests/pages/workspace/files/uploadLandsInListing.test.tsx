// A file that has landed is in the folder on screen at once, not when the
// listing's refetch gets through.
//
// The tray said "Uploaded" while the folder above it went on without the file
// for about thirty seconds: the refetch that brings the new row was waiting out
// the server's pause on reads. The listing is now given the landed file the
// moment its commit is done, and the refetch replaces it with the server's row
// whenever it arrives. Here the refetch never arrives at all.

import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { flattenChildren, useChildren } from "@/api/files";
import { keys } from "@/api/keys";
import { createQueryClient } from "@/api/queryClient";
import type { DropEntry, DropTarget } from "@/pages/workspace/files/dropHandlers";
import { seedLandedFile, useUploads, type DropTransfer } from "@/pages/workspace/files/useUploads";

const FOLDER: DropTarget = {
  id: "folder-root",
  name: "Home",
  kind: "folder",
  capabilities: { can_write: true },
};

const EXISTING = { id: "node-old", name: "notes.txt", kind: "file" };

function fileEntry(name: string): DropEntry {
  const file = new File(["id,name\n1,ok\n"], name, { type: "text/csv", lastModified: Date.UTC(2020, 0, 1) });
  return { isFile: true, isDirectory: false, name, file: (onSuccess) => onSuccess(file) };
}

function transfer(entries: DropEntry[]): DropTransfer {
  return {
    items: entries.map((entry) => ({ kind: "file", webkitGetAsEntry: () => entry })),
    getData: () => "",
  };
}

function memoryStorage() {
  const store = new Map<string, string>();
  return {
    getItem: (key: string) => store.get(key) ?? null,
    setItem: (key: string, value: string) => void store.set(key, value),
    removeItem: (key: string) => void store.delete(key),
  };
}

/** A server that takes the upload at once and, after the first, never answers
 *  a listing read: the pause a limited read is held in, made permanent. */
function stubServer(): void {
  let sessions = 0;
  let listings = 0;
  const json = (payload: unknown, status = 200) =>
    new Response(JSON.stringify(payload), { status, headers: { "content-type": "application/json" } });
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
        // Queued, as the real server answers it: the operation names the node.
        return json(
          { id: `op-${session}`, kind: "upload.commit", state: "done", resultNodeId: `node-${session}`, resultUnchanged: false },
          202,
        );
      }
      if (method === "GET" && /\/operations\/op-sess-\d+$/.test(path)) {
        const id = path.slice(path.lastIndexOf("/") + 1);
        return json({ id, kind: "upload.commit", state: "done", resultNodeId: `node-${id.slice(3)}`, resultUnchanged: false });
      }
      if (method === "POST" && /\/items\/lookup$/.test(path)) {
        return json({ value: [] });
      }
      if (session && method === "PUT") return json({ ok: true });
      if (session && method === "GET") {
        return json({ uploadId: session, state: "open", offset: 0, length: 1024, complete: false, partsDone: 0, partsTotal: 1, acceptedParts: [] });
      }
      if (method === "GET" && /\/children$/.test(path)) {
        listings += 1;
        if (listings === 1) return json({ value: [EXISTING], nextMarker: null });
        return new Promise<Response>(() => {});
      }
      if (method === "PATCH") return json({ id: "node", etag: "2" });
      return json({ code: "unexpected", message: url }, 500);
    }),
  );
}

function Harness() {
  const listing = useChildren("drive-1", FOLDER.id);
  const uploads = useUploads({
    driveId: "drive-1",
    doneLingerMs: 60_000,
    clientOptions: { storage: memoryStorage(), digest: async () => "deadbeef" },
  });
  return (
    <div>
      <button type="button" onClick={() => void uploads.onDrop(FOLDER, transfer([fileEntry("otter_counts.csv")]))}>
        drop
      </button>
      <ul aria-label="Folder">
        {flattenChildren(listing.data).map((item) => (
          <li key={item.id}>{item.name}</li>
        ))}
      </ul>
    </div>
  );
}

beforeEach(() => {
  stubServer();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("a file that has landed", () => {
  it("is in the folder's listing before the listing's own refetch answers", async () => {
    render(
      <QueryClientProvider client={createQueryClient()}>
        <MemoryRouter>
          <Harness />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    const folder = await screen.findByRole("list", { name: "Folder" });
    await waitFor(() => expect(folder).toHaveTextContent("notes.txt"));

    await userEvent.click(screen.getByRole("button", { name: "drop" }));

    await waitFor(() => expect(folder).toHaveTextContent("otter_counts.csv"), { timeout: 5_000 });
    expect(folder).toHaveTextContent("notes.txt");
  });
});

describe("seedLandedFile", () => {
  it("adds the file to an unfiltered listing once, and leaves a filtered one alone", () => {
    const qc = createQueryClient();
    const page = { value: [EXISTING], nextMarker: null };
    const plain = keys.files.children("drive-1", FOLDER.id, { orderBy: "name" }, 100);
    const folders = keys.files.children("drive-1", FOLDER.id, { kind: "folder" }, 100);
    qc.setQueryData(plain, { pages: [page], pageParams: [undefined] });
    qc.setQueryData(folders, { pages: [{ value: [], nextMarker: null }], pageParams: [undefined] });
    qc.setQueryData(keys.files.item(FOLDER.id), {
      id: FOLDER.id,
      capabilities: { can_read: true, can_write: true, can_share: false, refusals: {} },
    });

    const landed = { nodeId: "node-new", name: "a.csv", size: 12, mimeType: "text/csv" };
    seedLandedFile(qc, "drive-1", FOLDER.id, landed);
    seedLandedFile(qc, "drive-1", FOLDER.id, landed);

    const listed = qc.getQueryData<{ pages: { value: { id: string; name?: string; capabilities?: unknown }[] }[] }>(plain);
    const rows = listed!.pages.flatMap((p) => p.value);
    expect(rows.map((r) => r.id)).toEqual(["node-old", "node-new"]);
    // What the reader may do with it is the folder's say until the server's row arrives.
    expect(rows[1]!.capabilities).toMatchObject({ can_write: true });
    const filtered = qc.getQueryData<{ pages: { value: unknown[] }[] }>(folders);
    expect(filtered!.pages[0]!.value).toEqual([]);
  });

  it("leaves a listing that already holds the node (a new version of a file that was there)", () => {
    const qc = createQueryClient();
    const plain = keys.files.children("drive-1", FOLDER.id, {}, 100);
    qc.setQueryData(plain, { pages: [{ value: [EXISTING], nextMarker: null }], pageParams: [undefined] });
    seedLandedFile(qc, "drive-1", FOLDER.id, { nodeId: "node-old", name: "notes.txt", size: 1, mimeType: "" });
    const listed = qc.getQueryData<{ pages: { value: unknown[] }[] }>(plain);
    expect(listed!.pages[0]!.value).toEqual([EXISTING]);
  });
});
