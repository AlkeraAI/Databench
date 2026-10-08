import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { createQueryClient } from "@/api/queryClient";
import type { DropEntry, DropTarget } from "@/pages/workspace/files/dropHandlers";
import { UploadTray } from "@/pages/workspace/files/UploadTray";
import { useUploads, type DropTransfer } from "@/pages/workspace/files/useUploads";

// A whole drop against a server that commits the way the real one does: the
// completion is ACCEPTED, not done — a 202 and an operation — so the node id
// arrives on the operation and the version does not. Everything the batch does
// after that point is what this file is about, because that is where twelve
// dropped files became three.

const WRITABLE: DropTarget = {
  id: "folder-root",
  name: "Home",
  kind: "folder",
  capabilities: { can_write: true },
};

const DROPPED = 12;
const ITEM_ETAG = "7";
// The sentence the person reads, spelled here rather than imported: what the row
// says is the contract, not the name of the constant that holds it.
const MTIME_NOT_KEPT = "Uploaded, but the file's modified time could not be kept.";

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

interface Recorded {
  method: string;
  path: string;
  ifMatch: string | null;
  body: Record<string, unknown> | null;
}

interface Wire {
  calls: Recorded[];
  opens: () => Recorded[];
  completes: () => Recorded[];
  patches: () => Recorded[];
  /** Answer a read-back of the node with this status instead of the item. */
  itemReadStatus: number;
}

function stubFetch(): Wire {
  let sessions = 0;
  const wire: Wire = {
    calls: [],
    opens: () => wire.calls.filter((c) => c.method === "POST" && c.path.endsWith("/api/v1/files/uploads")),
    completes: () => wire.calls.filter((c) => c.method === "POST" && c.path.endsWith("/complete")),
    patches: () => wire.calls.filter((c) => c.method === "PATCH"),
    itemReadStatus: 200,
  };
  const json = (payload: unknown, status = 200) =>
    new Response(JSON.stringify(payload), { status, headers: { "content-type": "application/json" } });

  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const asRequest = input instanceof Request ? input : null;
      const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
      const path = new URL(url, "http://localhost").pathname;
      const method = (init?.method ?? asRequest?.method ?? "GET").toUpperCase();
      const headers = new Headers((init?.headers as HeadersInit | undefined) ?? asRequest?.headers);
      const raw = typeof init?.body === "string" ? init.body : asRequest ? await asRequest.clone().text() : "";
      let body: Record<string, unknown> | null = null;
      try {
        body = raw ? (JSON.parse(raw) as Record<string, unknown>) : null;
      } catch {
        body = null;
      }
      wire.calls.push({ method, path, ifMatch: headers.get("If-Match"), body });

      if (method === "POST" && path.endsWith("/api/v1/files/uploads")) {
        sessions += 1;
        return json({ uploadId: `sess-${sessions}`, partSize: 1024, partsTotal: 1, expiresAt: "" });
      }
      const session = /\/uploads\/(sess-\d+)(\/|$)/.exec(path)?.[1];
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
      if (session && path.endsWith("/complete")) {
        // What the server really answers: the commit was queued. The operation
        // names the node it created and NOT the version it left it at.
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
        if (wire.itemReadStatus !== 200) {
          return json({ code: "files.not_found", message: "gone" }, wire.itemReadStatus);
        }
        const ids = ((body as { ids?: string[] } | null)?.ids ?? []) as string[];
        return json({ value: ids.map((id) => ({ id, etag: ITEM_ETAG, name: id, kind: "file" })) });
      }
      if (method === "PATCH") {
        // The items route refuses a mutation that does not name the version it
        // is changing — the same 428 the live stack answered the drop with.
        if (!headers.get("If-Match")) {
          return json({ code: "files.if_match_required", message: "This mutation requires an If-Match header." }, 428);
        }
        return json({ id: "node", etag: "8" });
      }
      if (method === "GET" && /\/children$/.test(path)) return json({ value: [], nextMarker: null });
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

function Harness({ entries }: { entries: DropEntry[] }) {
  const uploads = useUploads({
    driveId: "drive-1",
    doneLingerMs: 60_000,
    clientOptions: { storage: memoryStorage(), digest: async () => "deadbeef" },
  });
  return (
    <div>
      <button type="button" onClick={() => void uploads.onDrop(WRITABLE, transfer(entries))}>
        drop
      </button>
      <UploadTray
        rows={uploads.rows}
        skippedSidecars={uploads.skippedSidecars}
        identicalCopies={uploads.identicalCopies}
        alreadyInFiles={uploads.alreadyInFiles}
        finished={uploads.finished}
        refusal={uploads.refusal}
        batch={uploads.batch}
        conflicts={uploads.conflicts}
        resumable={uploads.resumable}
        onPause={uploads.pause}
        onResume={uploads.resume}
        onCancel={uploads.cancel}
        onAnswerConflict={uploads.answerConflict}
        onDismiss={uploads.dismiss}
      />
    </div>
  );
}

function mount(entries: DropEntry[]) {
  const client = createQueryClient();
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <Harness entries={entries} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const names = (count: number) => Array.from({ length: count }, (_, i) => fileEntry(`f${i + 1}.csv`));

let wire: Wire;

beforeEach(() => {
  wire = stubFetch();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("a drop of twelve against a server that queues the commit", () => {
  it("asks for all twelve, commits all twelve, and shows all twelve", async () => {
    const user = userEvent.setup();
    mount(names(DROPPED));
    await user.click(screen.getByRole("button", { name: "drop" }));

    const status = await screen.findByRole("status");
    await waitFor(() => expect(status).toHaveTextContent(`${DROPPED} files uploaded`), {
      timeout: 5000,
    });
    expect(wire.opens()).toHaveLength(DROPPED);
    expect(wire.completes()).toHaveLength(DROPPED);
    for (let i = 1; i <= DROPPED; i += 1) {
      expect(screen.getByLabelText(`f${i}.csv progress`)).toBeInTheDocument();
    }
    expect(status.textContent).not.toMatch(/failed/);
  });

  it("stamps the modified time against the version the node is at", async () => {
    const user = userEvent.setup();
    mount(names(DROPPED));
    await user.click(screen.getByRole("button", { name: "drop" }));

    await waitFor(() => expect(wire.patches()).toHaveLength(DROPPED), { timeout: 5000 });
    for (const patch of wire.patches()) {
      expect(patch.ifMatch).toBe(ITEM_ETAG);
      expect(patch.body).toEqual({ attrs: { mtime: Date.UTC(2020, 0, 1) * 1_000_000 } });
    }
    // The versions came from reading the nodes back, because the operation the
    // commit answered with does not carry them — and read back together: every
    // dropped node named exactly once, in far fewer requests than files.
    const lookups = wire.calls.filter((c) => c.method === "POST" && /\/items\/lookup$/.test(c.path));
    expect(lookups.length).toBeGreaterThan(0);
    expect(lookups.length).toBeLessThan(DROPPED / 2);
    const asked = lookups.flatMap((c) => (c.body as { ids?: string[] } | null)?.ids ?? []);
    expect(asked).toHaveLength(DROPPED);
    expect(new Set(asked).size).toBe(DROPPED);
    expect(wire.calls.filter((c) => c.method === "GET" && /\/items\/node-sess-\d+$/.test(c.path))).toHaveLength(0);
  });

  it("sends no stamp at all when the version cannot be read, and says the time was not kept", async () => {
    wire.itemReadStatus = 404;
    const user = userEvent.setup();
    mount(names(2));
    await user.click(screen.getByRole("button", { name: "drop" }));

    const status = await screen.findByRole("status");
    await waitFor(() => expect(status).toHaveTextContent("2 files uploaded"), { timeout: 5000 });
    // The head counts a row the moment it lands; the row's own line about its
    // clock follows once the stamp has been decided, so that is what is waited
    // for before the stamps are counted — a PATCH with nothing to fence against
    // is a 428 nobody would ever see: the file landed, so it is named as landed
    // and the clock is what is lost.
    await waitFor(() => expect(screen.getAllByText(MTIME_NOT_KEPT)).toHaveLength(2), {
      timeout: 5000,
    });
    expect(wire.patches()).toHaveLength(0);
    expect(status.textContent).not.toMatch(/failed/);
  });
});
