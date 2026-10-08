// A question the tray can no longer answer has to end the upload behind it.
//
// A name collision parks the upload client inside `await onConflict(name)` — a
// promise the PAGE owns. If the tray goes away with the prompt still open (the
// person navigates, the panel closes, the route unmounts), nothing will ever
// call it: the upload holds its `File`, its session and its lane for the life
// of the tab, and the drop that started it never finishes.

import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { createQueryClient } from "@/api/queryClient";
import type { DropEntry, DropTarget } from "@/pages/workspace/files/dropHandlers";
import { UploadTray } from "@/pages/workspace/files/UploadTray";
import { useUploads, type DropTransfer } from "@/pages/workspace/files/useUploads";

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

/** The session API, with the completion always answering "that name is taken" —
 *  so the drop reaches the prompt and stops there. */
function stubFetch(): void {
  const json = (payload: unknown, status = 200): Response =>
    new Response(JSON.stringify(payload), {
      status,
      headers: { "content-type": "application/json" },
    });

  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
      const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
      const path = new URL(url, "http://localhost").pathname;
      const method = (init?.method ?? "GET").toUpperCase();

      if (method === "POST" && path.endsWith("/api/v1/files/uploads")) {
        return json({ uploadId: "sess-1", partSize: 1024, partsTotal: 1, expiresAt: "" });
      }
      if (path.endsWith("/complete")) {
        return json({ code: "files.exists", message: "that name is taken in this folder" }, 409);
      }
      if (path.includes("/uploads/sess-1")) {
        if (method === "PUT") return json({ ok: true });
        if (method === "DELETE") return new Response(null, { status: 204 });
        return json({
          uploadId: "sess-1",
          state: "open",
          offset: 0,
          length: 1024,
          complete: false,
          partsDone: 0,
          partsTotal: 1,
          acceptedParts: [],
        });
      }
      return json({ value: [], nextMarker: null });
    }),
  );
}

function memoryStorage() {
  const store = new Map<string, string>();
  return {
    getItem: (key: string): string | null => store.get(key) ?? null,
    setItem: (key: string, value: string): void => void store.set(key, value),
    removeItem: (key: string): void => void store.delete(key),
  };
}

function Harness({ entries, onDrained }: { entries: DropEntry[]; onDrained: () => void }) {
  const uploads = useUploads({
    driveId: "drive-1",
    doneLingerMs: 60_000,
    clientOptions: { storage: memoryStorage(), digest: async () => "deadbeef" },
  });
  return (
    <div>
      <button
        type="button"
        onClick={() => void uploads.onDrop(WRITABLE, transfer(entries)).then(onDrained, onDrained)}
      >
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
      />
    </div>
  );
}

async function until(condition: () => boolean, ms = 3000): Promise<void> {
  const started = Date.now();
  while (!condition()) {
    if (Date.now() - started > ms) throw new Error("the drop never got there");
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
}

let escaped: unknown[] = [];
const catchEscaped = (reason: unknown): void => {
  escaped.push(reason);
};

beforeEach(() => {
  escaped = [];
  process.on("unhandledRejection", catchEscaped);
  stubFetch();
});

afterEach(() => {
  process.off("unhandledRejection", catchEscaped);
  vi.unstubAllGlobals();
});

describe("a drop abandoned with a name collision open", () => {
  it("finishes instead of waiting for an answer that can no longer come", async () => {
    const user = userEvent.setup();
    let drained = false;
    const view = render(
      <QueryClientProvider client={createQueryClient()}>
        <MemoryRouter>
          <Harness
            entries={[fileEntry("report.csv")]}
            onDrained={() => {
              drained = true;
            }}
          />
        </MemoryRouter>
      </QueryClientProvider>,
    );

    await user.click(screen.getByRole("button", { name: "drop" }));
    // The prompt is up: the client is now parked inside `onConflict`.
    await screen.findByRole("button", { name: /keep both/i });
    expect(drained).toBe(false);

    view.unmount();
    await until(() => drained);

    expect(drained).toBe(true);
    expect(escaped).toEqual([]);
  });
});
