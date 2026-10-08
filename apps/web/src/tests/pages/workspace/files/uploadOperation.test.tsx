// What a queued commit does to the tray.
//
// `POST …/uploads/{id}/complete` always answers 202 and an operation: the parts
// are agreed, the commit is not run. So every verdict a person needs — it
// landed, the name was taken, the store refused it — arrives only from
// `GET …/drives/{drive}/operations/{id}`. Reading the 202 as success is how a
// refused upload says "Uploaded" and never appears in the folder, which these
// cases pin.
//
// The scripted server here answers exactly what the real one does: a 202 with
// `{id, kind, state, done, total}`, then an operation that is `queued`, then
// `running`, then terminal. Nothing asserts on a mock echoing its own input —
// every assertion is on the requests the client made and on what the tray says.

import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
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

const DRIVE = "drive-1";

/** The version every node read back is at, so the stamp has something to name. */
const NODE_ETAG = "etag-4";

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

/** One operation's life, read one look at a time: the last entry repeats. */
interface OperationScript {
  states: string[];
  errors?: { itemId: string | null; code: string; message: string }[];
  /** What a `done` operation says it landed. */
  resultNodeId?: string;
  resultUnchanged?: boolean;
}

interface Wire {
  touched: { method: string; path: string; ifMatch?: string; body?: unknown }[];
  /** The operation each `complete` hands back, oldest session first. */
  commits: OperationScript[];
  /** How many times each operation id was read back. */
  looks: Map<string, number>;
}

/**
 * The session API, scripted the way the server answers it.
 *
 * `commits` is consumed one entry per completion, so a second completion (the
 * one a conflict answer provokes) is scripted separately from the first.
 */
function stubFetch(commits: OperationScript[]): Wire {
  const wire: Wire = { touched: [], commits: [...commits], looks: new Map() };
  const scripts = new Map<string, OperationScript>();
  let opened = 0;
  let queued = 0;

  const json = (payload: unknown, status = 200) =>
    new Response(JSON.stringify(payload), {
      status,
      headers: { "content-type": "application/json" },
    });

  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
      const path = new URL(url, "http://localhost").pathname;
      // The upload client calls `fetch(url, init)`; the typed SDK the rest of
      // the page uses hands over a built `Request` and no init at all, so both
      // shapes have to be read or a PATCH looks like a GET of the same path.
      const built = input instanceof Request ? input : null;
      const method = (init?.method ?? built?.method ?? "GET").toUpperCase();
      const sent =
        typeof init?.body === "string"
          ? JSON.parse(init.body)
          : built
            ? await built
                .clone()
                .json()
                .catch(() => undefined)
            : undefined;
      const precondition = new Headers(
        (init?.headers as HeadersInit | undefined) ?? built?.headers,
      ).get("If-Match");
      wire.touched.push({
        method,
        path,
        ...(precondition === null ? {} : { ifMatch: precondition }),
        ...(sent === undefined ? {} : { body: sent }),
      });

      if (method === "POST" && path.endsWith("/api/v1/files/uploads")) {
        opened += 1;
        return json({ uploadId: `sess-${opened}`, partSize: 1024, partsTotal: 1, expiresAt: "" });
      }

      const operation = /\/operations\/(op-\d+)$/.exec(path)?.[1];
      if (operation && method === "GET") {
        wire.looks.set(operation, (wire.looks.get(operation) ?? 0) + 1);
        const script = scripts.get(operation);
        if (!script) return json({ code: "files.not_found", message: "no operation" }, 404);
        const look = wire.looks.get(operation) ?? 1;
        const state = script.states[Math.min(look - 1, script.states.length - 1)] ?? "done";
        return json({
          id: operation,
          driveId: DRIVE,
          kind: "upload",
          state,
          done: state === "done" ? 1 : 0,
          total: 1,
          bytes: 0,
          skipped: script.errors?.length ?? 0,
          conflicts: [],
          errors: state === "done" ? [] : (script.errors ?? []),
          resultNodeId: state === "done" ? (script.resultNodeId ?? null) : null,
          resultVersionId: null,
          resultUnchanged: state === "done" && script.resultUnchanged === true,
        });
      }

      const sessionId = /\/uploads\/(sess-\d+)(\/|$)/.exec(path)?.[1];
      if (sessionId && path.endsWith("/complete")) {
        queued += 1;
        const id = `op-${queued}`;
        scripts.set(id, wire.commits.shift() ?? { states: ["done"] });
        // What the route answers: accepted, not committed.
        return json({ id, kind: "upload", state: "queued", done: 0, total: 1 }, 202);
      }
      if (sessionId && method === "GET") {
        return json({
          uploadId: sessionId,
          state: "open",
          offset: 0,
          length: 1024,
          complete: false,
          partsDone: 0,
          partsTotal: 1,
          acceptedParts: [],
        });
      }
      if (sessionId && method === "PUT") return json({ ok: true });
      if (sessionId && method === "DELETE") return new Response(null, { status: 204 });

      // The nodes read back for the version the stamp fences against: the
      // operation names which node was created and never which version, and
      // the finished files are asked for together.
      if (method === "POST" && /\/items\/lookup$/.test(path)) {
        const ids = ((sent as { ids?: string[] } | undefined)?.ids ?? []) as string[];
        return json({ value: ids.map((id) => ({ id, etag: NODE_ETAG, kind: "file" })) });
      }
      if (method === "PATCH") {
        // The items route refuses a mutation that does not name the version it
        // is changing, exactly as the live one does.
        if (precondition === null || precondition === "") {
          return json(
            { code: "files.if_match_required", message: "This mutation requires an If-Match header." },
            428,
          );
        }
        return json({ id: "node", etag: "etag-2" });
      }
      if (method === "GET" && /\/children$/.test(path)) {
        return json({ value: [{ id: "n1", name: "one.csv", etag: "etag-9" }], nextMarker: null });
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

function Harness({ entries }: { entries: DropEntry[] }) {
  const uploads = useUploads({
    driveId: DRIVE,
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
        onRetry={(id) => void uploads.retry(id)}
        onRetryFailed={() => void uploads.retryFailed()}
        onDismiss={uploads.dismiss}
      />
    </div>
  );
}

function renderTray(entries: DropEntry[]) {
  return render(
    <QueryClientProvider client={createQueryClient()}>
      <MemoryRouter>
        <Harness entries={entries} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn());
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("a queued commit is followed to its end", () => {
  it("waits for the operation before the row says the file landed", async () => {
    // Two non-terminal looks first, so a row that settles on the 202 alone
    // would be "Uploaded" before the commit had run at all.
    const wire = stubFetch([{ states: ["queued", "running", "done"] }]);
    const user = userEvent.setup();
    renderTray([fileEntry("one.csv")]);
    await user.click(screen.getByRole("button", { name: "drop" }));

    await waitFor(() => expect(screen.getByText("Uploaded")).toBeInTheDocument(), {
      timeout: 5000,
    });
    // Read back until terminal, not once: three looks for three states.
    expect(wire.looks.get("op-1")).toBe(3);
    expect(
      wire.touched.filter((call) => call.method === "GET" && /\/operations\//.test(call.path)),
    ).toHaveLength(3);
  });

  it("a taken name asks, and the answer is carried by a fresh session", async () => {
    // The first commit refuses on the name; the second, opened for the answer,
    // lands. The prose carries no recognisable phrase on purpose: the code is
    // the only thing the collision may be found by, because the library's
    // sentence is prose and may be reworded.
    const wire = stubFetch([
      {
        states: ["failed"],
        errors: [{ itemId: null, code: "files.exists", message: "Refused." }],
      },
      { states: ["done"] },
    ]);
    const user = userEvent.setup();
    renderTray([fileEntry("one.csv")]);
    await user.click(screen.getByRole("button", { name: "drop" }));

    const keepBoth = await screen.findByRole("button", { name: /keep both/i }, { timeout: 5000 });
    await user.click(keepBoth);

    await waitFor(() => expect(screen.getByText("Uploaded")).toBeInTheDocument(), {
      timeout: 5000,
    });
    expect(screen.getByText(/Kept both/)).toBeInTheDocument();
    // The refused commit aborted its session, so the answer went up under a
    // second one — and it asked for `rename`, not for `fail` again.
    const opens = wire.touched.filter(
      (call) => call.method === "POST" && call.path.endsWith("/api/v1/files/uploads"),
    );
    expect(opens).toHaveLength(2);
    expect(wire.looks.get("op-2")).toBe(1);
  });

  it("a failed commit shows the server's own sentence on the row", async () => {
    const wire = stubFetch([
      {
        states: ["running", "failed"],
        errors: [
          {
            itemId: null,
            code: "files.storage_unavailable",
            message: "The storage for this drive did not answer.",
          },
        ],
      },
    ]);
    const user = userEvent.setup();
    renderTray([fileEntry("one.csv")]);
    await user.click(screen.getByRole("button", { name: "drop" }));

    await waitFor(
      () =>
        expect(screen.getByText("The storage for this drive did not answer.")).toBeInTheDocument(),
      { timeout: 5000 },
    );
    expect(screen.queryByText("Uploaded")).not.toBeInTheDocument();
    // Nothing was re-sent: a failed commit is not a name collision.
    expect(
      wire.touched.filter(
        (call) => call.method === "POST" && call.path.endsWith("/api/v1/files/uploads"),
      ),
    ).toHaveLength(1);
  });

  it("stamps the dropped file's own modified time onto the node the commit named", async () => {
    // The operation is the only thing that names the node: the 202 carries an
    // operation id and nothing else, so without `resultNodeId` there is no
    // address to PATCH and the laptop's timestamp is lost.
    const wire = stubFetch([{ states: ["done"], resultNodeId: "node-7" }]);
    const user = userEvent.setup();
    renderTray([fileEntry("one.csv")]);
    await user.click(screen.getByRole("button", { name: "drop" }));

    await waitFor(() => expect(screen.getByText("Uploaded")).toBeInTheDocument(), {
      timeout: 5000,
    });
    const patches = () => wire.touched.filter((call) => call.method === "PATCH");
    await waitFor(() => expect(patches()).toHaveLength(1), { timeout: 5000 });
    expect(patches()[0]?.path).toContain("/items/node-7");
    // A stamp that does not name the version it is changing is refused: the
    // node is read back for it, because the operation does not carry one.
    expect(patches()[0]?.ifMatch).toBe(NODE_ETAG);
    // `lastModified` is Jan 1 2020 UTC in milliseconds; the attrs facet is
    // nanoseconds, and it is the laptop's time that must survive.
    expect(patches()[0]?.body).toEqual({ attrs: { mtime: 1577836800000000000 } });
  });

  it("counts a commit the drive already held instead of stamping it", async () => {
    const wire = stubFetch([
      { states: ["done"], resultNodeId: "node-7", resultUnchanged: true },
    ]);
    const user = userEvent.setup();
    renderTray([fileEntry("one.csv")]);
    await user.click(screen.getByRole("button", { name: "drop" }));

    await waitFor(() => expect(screen.getByText(/already in Files/i)).toBeInTheDocument(), {
      timeout: 5000,
    });
    // Nothing was written, so there is no version to stamp a time onto.
    expect(wire.touched.filter((call) => call.method === "PATCH")).toHaveLength(0);
  });
});
