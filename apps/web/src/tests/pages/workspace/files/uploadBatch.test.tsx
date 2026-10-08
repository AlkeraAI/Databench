import { QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { createQueryClient } from "@/api/queryClient";
import { UPLOAD_CONCURRENCY } from "@/pages/workspace/files/dragDrop";
import type { DropEntry, DropTarget } from "@/pages/workspace/files/dropHandlers";
import { batchLine, UploadTray } from "@/pages/workspace/files/UploadTray";
import { useUploads, type DropTransfer } from "@/pages/workspace/files/useUploads";

// A dropped batch as a whole, through the real upload client over a stubbed
// wire: how many sessions are open at once, what the tray says while it runs,
// and what happens when the server says no more will fit.

const WRITABLE: DropTarget = {
  id: "folder-root",
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

interface Recorded {
  method: string;
  path: string;
  body: Record<string, unknown> | null;
}

interface Wire {
  calls: Recorded[];
  opens: () => Recorded[];
  /** Session opens the server has not answered yet, oldest first. */
  pending: (() => void)[];
  /** Answer the nth open (1-based) with this refusal instead of a session. */
  refuseOpen: { at: number; status: number; code: string } | null;
  /** Hold every open until released through `pending`. */
  hold: boolean;
}

function stubFetch(): Wire {
  let sessions = 0;
  const wire: Wire = {
    calls: [],
    opens: () => wire.calls.filter((c) => c.method === "POST" && c.path.endsWith("/api/v1/files/uploads")),
    pending: [],
    refuseOpen: null,
    hold: false,
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
      const raw = typeof init?.body === "string" ? init.body : asRequest ? await asRequest.clone().text() : "";
      let body: Record<string, unknown> | null = null;
      try {
        body = raw ? (JSON.parse(raw) as Record<string, unknown>) : null;
      } catch {
        body = null;
      }
      wire.calls.push({ method, path, body });

      if (method === "POST" && path.endsWith("/api/v1/files/uploads")) {
        sessions += 1;
        const nth = sessions;
        if (wire.hold) {
          await new Promise<void>((resolve) => wire.pending.push(resolve));
        }
        if (wire.refuseOpen && wire.refuseOpen.at === nth) {
          return json({ code: wire.refuseOpen.code, message: "" }, wire.refuseOpen.status);
        }
        return json({ uploadId: `sess-${nth}`, partSize: 1024, partsTotal: 1, expiresAt: "" });
      }
      const session = /\/uploads\/(sess-\d+)(\/|$)/.exec(path)?.[1];
      if (session && method === "GET") {
        return json({ uploadId: session, state: "open", offset: 0, length: 1024, complete: false, partsDone: 0, partsTotal: 1, acceptedParts: [] });
      }
      if (session && method === "PUT") return json({ ok: true });
      if (session && path.endsWith("/complete")) {
        return json({ item: { id: `node-${session}`, etag: "etag-1" }, unchanged: false });
      }
      if (method === "PATCH") return json({ id: "node", etag: "etag-2" });
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

describe("how many at once", () => {
  it("opens only the concurrency's worth of sessions until one settles", async () => {
    wire.hold = true;
    const user = userEvent.setup();
    mount(names(UPLOAD_CONCURRENCY + 3));
    await user.click(screen.getByRole("button", { name: "drop" }));

    await waitFor(() => expect(wire.opens()).toHaveLength(UPLOAD_CONCURRENCY));
    // Nothing more while all lanes are busy.
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 30));
    });
    expect(wire.opens()).toHaveLength(UPLOAD_CONCURRENCY);

    // One answered — one more may start. The lane count, not the file count, gates it.
    wire.pending.shift()!();
    await waitFor(() => expect(wire.opens()).toHaveLength(UPLOAD_CONCURRENCY + 1));
    while (wire.pending.length > 0) wire.pending.shift()!();
    await waitFor(() => expect(wire.opens()).toHaveLength(UPLOAD_CONCURRENCY + 3));
  });

  it("says how far through the batch it is, and which file it is on", async () => {
    wire.hold = true;
    const user = userEvent.setup();
    mount(names(2));
    await user.click(screen.getByRole("button", { name: "drop" }));

    const status = await screen.findByRole("status");
    expect(status).toHaveTextContent("Uploading 0 of 2: f2.csv");

    while (wire.pending.length > 0) wire.pending.shift()!();
    await waitFor(() => expect(status).toHaveTextContent("2 files uploaded"));
  });
});

describe("a refusal that ends the batch", () => {
  it.each([
    [507, "files.quota_bytes", "Your organization is out of storage."],
    [507, "files.user_quota_bytes", "Your storage limit is reached."],
    [403, "files.forbidden", ""],
  ])("%s %s stops the files not yet started and says why", async (status, code, sentence) => {
    // Lanes fill at once, so opens 1..CONCURRENCY are in flight before any answer;
    // the refusal on one of them must keep the rest of the batch on the laptop.
    wire.refuseOpen = { at: 2, status, code };
    const user = userEvent.setup();
    const total = UPLOAD_CONCURRENCY + 4;
    mount(names(total));
    await user.click(screen.getByRole("button", { name: "drop" }));

    const alert = await screen.findByRole("alert");
    if (sentence) expect(alert).toHaveTextContent(sentence);
    expect(alert.textContent).not.toMatch(/Request failed/);

    const statusLine = await screen.findByRole("status");
    await waitFor(() => expect(statusLine).toHaveTextContent(/^Stopped after/));
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 40));
    });
    // The lanes that were already open ran to the end; nothing after them started.
    expect(wire.opens()).toHaveLength(UPLOAD_CONCURRENCY);
    expect(statusLine).toHaveTextContent(`${total - UPLOAD_CONCURRENCY} not uploaded`);
  });

  it("a refusal of one file that is not about room leaves the rest running", async () => {
    wire.refuseOpen = { at: 2, status: 409, code: "files.name_taken" };
    const user = userEvent.setup();
    const total = UPLOAD_CONCURRENCY + 2;
    mount(names(total));
    await user.click(screen.getByRole("button", { name: "drop" }));

    await waitFor(() => expect(wire.opens()).toHaveLength(total));
    const statusLine = await screen.findByRole("status");
    await waitFor(() => expect(statusLine).toHaveTextContent(`${total - 1} of ${total} uploaded`));
    expect(screen.queryByRole("alert")).toBeNull();
  });
});

describe("batchLine", () => {
  it.each([
    [{ total: 4, done: 1, failed: 0, current: "a/b.csv", stopped: null }, "Uploading 1 of 4: a/b.csv"],
    [{ total: 4, done: 4, failed: 0, current: "d.csv", stopped: null }, "4 files uploaded"],
    [{ total: 1, done: 1, failed: 0, current: "d.csv", stopped: null }, "1 file uploaded"],
    // A finished batch names its losses: "3 of 4 uploaded" alone reads as a total.
    [{ total: 4, done: 4, failed: 1, current: "d.csv", stopped: null }, "3 of 4 uploaded, 1 failed"],
    [{ total: 4, done: 2, failed: 1, current: "b.csv", stopped: "no room" }, "Stopped after 2 of 4, 2 not uploaded"],
    [{ total: 2, done: 2, failed: 2, current: "b.csv", stopped: "no room" }, "Stopped with 2 of 2 failed"],
  ])("%j", (batch, expected) => {
    expect(batchLine(batch)).toBe(expected);
  });
});
