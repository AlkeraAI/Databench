// The drop path, end to end through the real upload client.
//
// Nothing here mocks `UploadClient`: the tests stub `fetch` and let the real
// session client open, part, complete and resume, so a change that breaks the
// wire order breaks these tests rather than passing against a mock's echo.

import { QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { createQueryClient } from "@/api/queryClient";
import { REPLACE_NEEDS_ETAG, uploadStorageKey } from "@/api/filesUpload";
import {
  dropVerdict,
  expandDrop,
  indexTree,
  isSidecar,
  readMovePayload,
  writeMovePayload,
  type DropEntry,
  type DropItem,
} from "@/pages/workspace/files/dropHandlers";
import { STALE_PRECONDITION, UploadTray } from "@/pages/workspace/files/UploadTray";
import { conflictsReducer, useUploads, type DropTransfer } from "@/pages/workspace/files/useUploads";
import type { DropTarget } from "@/pages/workspace/files/dropHandlers";

/** Who the uploads are remembered for. */
const ACCOUNT = { userId: "usr_dana", orgId: "org_a" };

// ---------------------------------------------------------------- fixtures --

/** The CSV the fixture folder holds, and its byte length — the resume record is
 *  keyed on the size, so the two must agree. */
const CSV_BODY = "id,name\n1,ok\n";
const CSV_BYTES = CSV_BODY.length;

/** 1999 — the whole point of the mtime patch: a file older than the account. */
const NINETEEN_NINETY_NINE = Date.UTC(1999, 4, 1, 12, 0, 0);

function fileEntry(name: string, body: string, lastModified: number): DropEntry {
  const file = new File([body], name, { lastModified });
  return {
    isFile: true,
    isDirectory: false,
    name,
    file: (onSuccess) => onSuccess(file),
  };
}

function dirEntry(name: string, children: DropEntry[]): DropEntry {
  return {
    isFile: false,
    isDirectory: true,
    name,
    createReader: () => {
      // The real reader answers in batches and ends with an empty one; a
      // reader that answered everything forever would loop.
      let drained = false;
      return {
        readEntries: (onSuccess) => {
          if (drained) {
            onSuccess([]);
            return;
          }
          drained = true;
          onSuccess(children);
        },
      };
    },
  };
}

function items(entries: DropEntry[]): DropItem[] {
  return entries.map((entry) => ({ kind: "file", webkitGetAsEntry: () => entry }));
}

function transfer(entries: DropEntry[], moveData = ""): DropTransfer {
  return { items: items(entries), getData: () => moveData };
}

const WRITABLE: DropTarget = {
  id: "folder-root",
  name: "Home",
  kind: "folder",
  capabilities: { can_write: true },
};

const READ_ONLY: DropTarget = {
  id: "folder-locked",
  name: "Shared",
  kind: "folder",
  capabilities: { can_write: false },
};

/** The drop the acceptance criterion names: a folder holding an empty
 *  subfolder, a 1999-dated CSV and a macOS sidecar. */
function laptopFolder(): DropEntry {
  return dirEntry("photos", [
    dirEntry("empty", []),
    fileEntry("a.csv", CSV_BODY, NINETEEN_NINETY_NINE),
    fileEntry(".DS_Store", "junk", NINETEEN_NINETY_NINE),
  ]);
}

// --------------------------------------------------------------- the stub ---

interface Recorded {
  method: string;
  url: string;
  /** The url without its query, which is what the assertions match on. */
  path: string;
  body: unknown;
  headers: Record<string, string>;
}

interface StubOptions {
  /** Parts the server claims it already holds when the session is first read —
   *  the resume-after-reload case. */
  acceptedAtStart?: number[];
  /** Refuse the first `complete` with a 409, as a name collision does. */
  conflictOnce?: boolean;
  unchanged?: boolean;
  /** The folder answers a listing that does NOT hold the colliding name, so a
   *  Replace has no version to fence against. */
  nameNotInFolder?: boolean;
  /** The commit is refused because the node moved on since the etag was read. */
  replaceStale?: boolean;
}

function stubFetch(calls: Recorded[], options: StubOptions = {}) {
  const accepted = new Set(options.acceptedAtStart ?? []);
  let conflictsLeft = options.conflictOnce ? 1 : 0;

  // The generated client hands `fetch` a `Request`; the upload client hands it a
  // url plus an init. Both shapes are read the same way here.
  return vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const asRequest = input instanceof Request ? input : null;
    const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
    const path = new URL(url, "http://localhost").pathname;
    const method = (init?.method ?? asRequest?.method ?? "GET").toUpperCase();
    const rawHeaders: Record<string, string> = {};
    if (asRequest) asRequest.headers.forEach((value, key) => void (rawHeaders[key] = value));
    for (const [key, value] of Object.entries((init?.headers ?? {}) as Record<string, string>)) {
      rawHeaders[key.toLowerCase()] = value;
    }
    const rawBody =
      typeof init?.body === "string" ? init.body : asRequest ? await asRequest.clone().text() : "";
    let body: unknown = null;
    if (rawBody) {
      try {
        body = JSON.parse(rawBody);
      } catch {
        body = rawBody;
      }
    }
    calls.push({ method, url, path, body, headers: rawHeaders });

    const json = (payload: unknown, status = 200): Response =>
      new Response(JSON.stringify(payload), {
        status,
        headers: { "content-type": "application/json" },
      });

    if (method === "POST" && path.endsWith("/api/v1/files/uploads")) {
      return json({ uploadId: "sess-1", partSize: 1024, partsTotal: 1, expiresAt: "" });
    }
    if (method === "GET" && /\/uploads\/sess-1$/.test(path)) {
      return json({
        uploadId: "sess-1",
        state: "open",
        offset: accepted.size * 1024,
        length: 1024,
        complete: accepted.size >= 1,
        partsDone: accepted.size,
        partsTotal: 1,
        acceptedParts: [...accepted],
      });
    }
    if (method === "PUT" && /\/uploads\/sess-1\/parts\/(\d+)$/.test(path)) {
      const part = Number(/\/parts\/(\d+)$/.exec(path)?.[1] ?? "0");
      accepted.add(part);
      return json({ ok: true });
    }
    if (method === "POST" && /\/uploads\/sess-1\/complete$/.test(path)) {
      if (conflictsLeft > 0) {
        conflictsLeft -= 1;
        return json({ code: "files.name_taken", message: "taken" }, 409);
      }
      if (options.replaceStale && (body as { conflictBehavior?: string }).conflictBehavior === "replace") {
        return json({ code: "files.precondition_failed", message: "node node-a moved on" }, 412);
      }
      return json({
        item: { id: "node-csv", etag: "etag-1" },
        unchanged: options.unchanged === true,
      });
    }
    // The folder Replace looks the colliding name up in: the 409 carries only a
    // code, so the version being replaced is found the way the listing finds
    // anything.
    if (method === "GET" && /\/items\/[^/]+\/children$/.test(path)) {
      return json({
        value: options.nameNotInFolder
          ? [{ id: "node-other", name: "other.csv", nameDisplay: "other.csv", etag: "et-other" }]
          : [{ id: "node-a", name: "a.csv", nameDisplay: "a.csv", etag: "et-existing" }],
        nextMarker: null,
      });
    }
    if (method === "DELETE" && /\/uploads\/sess-1$/.test(path)) {
      return new Response(null, { status: 204 });
    }
    if (method === "POST" && /\/items\/[^/]+\/tree$/.test(path)) {
      const paths = ((body as { paths?: string[] }).paths ?? []).map((path, index) => ({
        id: `dir-${index}`,
        name: path.split("/").pop() ?? path,
        path,
      }));
      return json(paths);
    }
    if (method === "PATCH" && /\/items\/[^/]+$/.test(path)) {
      return json({ id: "node-csv", etag: "etag-2" });
    }
    return json({ code: "unexpected", message: url }, 500);
  });
}

// ------------------------------------------------------------- the harness --

interface HarnessProps {
  target: DropTarget;
  entries: DropEntry[];
  moveData?: string;
  storage: Pick<Storage, "getItem" | "setItem" | "removeItem">;
  /** The linger before a clean tray clears itself; short so a test can watch it. */
  doneLingerMs?: number;
}

/** Shorter than any test's patience, longer than a render. */
const LINGER = 60;

/** Long enough that a finished tray is never a window a loaded machine can
 *  miss. A test that wants to see the linger END shortens it instead of
 *  waiting — the clock is the flaky part, not the mechanism. */
const PATIENT_LINGER = 60_000;

function Harness({ target, entries, moveData = "", storage, doneLingerMs }: HarnessProps) {
  const uploads = useUploads({
    driveId: "drive-1",
    doneLingerMs,
    account: ACCOUNT,
    clientOptions: {
      storage,
      // Pinned so the test asserts on the request order, not on SubtleCrypto's
      // availability in jsdom.
      digest: async () => "deadbeef",
    },
  });
  return (
    <div>
      <button
        type="button"
        onClick={() => {
          void uploads.onDrop(target, transfer(entries, moveData));
        }}
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

function memoryStorage(seed: Record<string, string> = {}) {
  const store = new Map(Object.entries(seed));
  return {
    getItem: (key: string) => store.get(key) ?? null,
    setItem: (key: string, value: string) => void store.set(key, value),
    removeItem: (key: string) => void store.delete(key),
  };
}

function mount(props: HarnessProps) {
  const client = createQueryClient();
  const tree = (next: HarnessProps) => (
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <Harness {...next} />
      </MemoryRouter>
    </QueryClientProvider>
  );
  const view = render(tree(props));
  return {
    ...view,
    /** Change the mounted harness's props without remounting it — the uploads
     *  and their tray survive, which is what lets a test shorten the linger it
     *  would otherwise have to sit and watch. */
    reprop: (over: Partial<HarnessProps>) => view.rerender(tree({ ...props, ...over })),
  };
}

let calls: Recorded[] = [];

beforeEach(() => {
  calls = [];
});

afterEach(() => {
  vi.unstubAllGlobals();
});

// ------------------------------------------------------------------ tests ---

describe("expandDrop", () => {
  it("records a directory that holds nothing", async () => {
    const expansion = await expandDrop(items([laptopFolder()]));

    expect(expansion.directories).toEqual(["photos", "photos/empty"]);
  });

  it("folds macOS sidecars out of the upload and counts them", async () => {
    const expansion = await expandDrop(items([laptopFolder()]));

    expect(expansion.files.map((entry) => entry.relativePath)).toEqual(["photos/a.csv"]);
    expect(expansion.skippedSidecars).toBe(1);
  });

  it("keeps the laptop's modification time on the expanded file", async () => {
    const expansion = await expandDrop(items([laptopFolder()]));

    expect(expansion.files[0]?.file.lastModified).toBe(NINETEEN_NINETY_NINE);
  });

  it("drains a directory reader that answers in batches", async () => {
    let batch = 0;
    const paged: DropEntry = {
      isFile: false,
      isDirectory: true,
      name: "big",
      createReader: () => ({
        readEntries: (onSuccess) => {
          batch += 1;
          if (batch === 1) onSuccess([fileEntry("one.txt", "1", 0)]);
          else if (batch === 2) onSuccess([fileEntry("two.txt", "2", 0)]);
          else onSuccess([]);
        },
      }),
    };

    const expansion = await expandDrop(items([paged]));

    expect(expansion.files.map((entry) => entry.relativePath)).toEqual([
      "big/one.txt",
      "big/two.txt",
    ]);
  });

  it("takes a flat file from an item with no entry API", async () => {
    const file = new File(["x"], "loose.txt", { lastModified: NINETEEN_NINETY_NINE });
    const expansion = await expandDrop([{ kind: "file", getAsFile: () => file }]);

    expect(expansion.files.map((entry) => entry.relativePath)).toEqual(["loose.txt"]);
  });

  it("ignores a dragged string, which is not a file at all", async () => {
    const expansion = await expandDrop([{ kind: "string", getAsFile: () => null }]);

    expect(expansion.files).toEqual([]);
  });
});

describe("isSidecar", () => {
  it.each([
    [".DS_Store", true],
    ["._photo.jpg", true],
    // The negative twins: a name that merely starts with a dot, or contains the
    // sidecar spelling, is an ordinary file.
    [".gitignore", false],
    ["my._notes.txt", false],
    ["DS_Store", false],
  ])("%s → %s", (name, expected) => {
    expect(isSidecar(name)).toBe(expected);
  });
});

describe("dropVerdict", () => {
  it("accepts a folder the caller can write", () => {
    expect(dropVerdict(WRITABLE)).toEqual({ accepted: true });
  });

  it("refuses a folder the caller cannot write, and names it", () => {
    const verdict = dropVerdict(READ_ONLY);

    expect(verdict.accepted).toBe(false);
    expect(verdict.accepted ? "" : verdict.reason).toContain("Shared");
  });

  it("refuses a file, which is not somewhere things go", () => {
    expect(
      dropVerdict({ id: "f", name: "a.csv", kind: "file", capabilities: { can_write: true } })
        .accepted,
    ).toBe(false);
  });

  it("refuses a target with no capabilities at all", () => {
    expect(dropVerdict({ id: "f", name: "Unknown", kind: "folder" }).accepted).toBe(false);
  });

  it("refuses empty space", () => {
    expect(dropVerdict(null).accepted).toBe(false);
  });
});

describe("move payload", () => {
  it("round-trips the dragged rows with their versions", () => {
    const bag = new Map<string, string>();
    const dt = {
      setData: (type: string, value: string) => void bag.set(type, value),
      getData: (type: string) => bag.get(type) ?? "",
    };

    writeMovePayload(dt, [{ id: "n1", etag: "e1" }]);

    expect(readMovePayload(dt)).toEqual([{ id: "n1", etag: "e1" }]);
  });

  it("is absent on a desktop drag", () => {
    expect(readMovePayload({ getData: () => "" })).toBeNull();
  });

  it("refuses a payload that is not the shape it claims", () => {
    expect(readMovePayload({ getData: () => "{not json" })).toBeNull();
    expect(readMovePayload({ getData: () => '[{"id":"n1"}]' })).toBeNull();
  });
});

describe("indexTree", () => {
  it("pairs each requested path with the node the server created for it", () => {
    const map = indexTree(
      ["photos", "photos/empty"],
      [
        { id: "d1", name: "photos", path: "photos" },
        { id: "d2", name: "empty", path: "photos/empty" },
      ],
    );

    expect(map).toEqual({ photos: "d1", "photos/empty": "d2" });
  });

  it("matches when the server answers absolute paths", () => {
    const map = indexTree(
      ["photos/empty"],
      [{ id: "d2", name: "empty", path: "/home/me/photos/empty" }],
    );

    expect(map["photos/empty"]).toBe("d2");
  });
});

describe("conflictsReducer", () => {
  it("queues one prompt per upload", () => {
    const ask = { type: "ask", uploadId: "u1", name: "a.csv", parentId: "dir-0" } as const;
    const asked = conflictsReducer({ queue: [] }, ask);
    const again = conflictsReducer(asked, ask);

    expect(again.queue).toHaveLength(1);
    expect(again).toBe(asked);
  });

  it("queues two prompts for two uploads that spell the name the same way", () => {
    // The case the name key could not express: one drop, two files called
    // `a.csv`, each with its own question about its own bytes.
    let state = conflictsReducer(
      { queue: [] },
      { type: "ask", uploadId: "u1", name: "a.csv", parentId: "dir-0" },
    );
    state = conflictsReducer(state, {
      type: "ask",
      uploadId: "u2",
      name: "a.csv",
      parentId: "dir-0",
    });

    expect(state.queue.map((prompt) => prompt.uploadId)).toEqual(["u1", "u2"]);
  });

  it("answering removes only the prompt that was answered", () => {
    let state = conflictsReducer(
      { queue: [] },
      { type: "ask", uploadId: "u1", name: "a.csv", parentId: "dir-0" },
    );
    state = conflictsReducer(state, {
      type: "ask",
      uploadId: "u2",
      name: "b.csv",
      parentId: "dir-0",
    });
    state = conflictsReducer(state, { type: "answered", uploadId: "u1" });

    expect(state.queue.map((prompt) => prompt.name)).toEqual(["b.csv"]);
  });
});

describe("a directory drop", () => {
  it("sends one tree call carrying both folders, uploads the file with its 1999 mtime, and never mentions the sidecar", async () => {
    const fetchStub = stubFetch(calls);
    vi.stubGlobal("fetch", fetchStub);
    const user = userEvent.setup();

    mount({ target: WRITABLE, entries: [laptopFolder()], storage: memoryStorage() });
    await user.click(screen.getByRole("button", { name: "drop" }));

    await waitFor(() => {
      expect(calls.some((call) => call.method === "PATCH")).toBe(true);
    });

    const trees = calls.filter((call) => call.path.endsWith("/tree"));
    expect(trees).toHaveLength(1);
    expect((trees[0]?.body as { paths: string[] }).paths).toEqual(["photos", "photos/empty"]);

    // The file goes into the folder the tree call created, not the drop target.
    const opened = calls.find((call) => call.path.endsWith("/api/v1/files/uploads"));
    expect((opened?.body as { parentId: string; name: string }).parentId).toBe("dir-0");
    expect((opened?.body as { name: string }).name).toBe("a.csv");

    const patch = calls.find((call) => call.method === "PATCH");
    expect((patch?.body as { attrs: { mtime: number } }).attrs.mtime).toBe(
      NINETEEN_NINETY_NINE * 1_000_000,
    );

    expect(calls.some((call) => JSON.stringify(call.body ?? "").includes("DS_Store"))).toBe(false);
    expect(calls.some((call) => call.url.includes("DS_Store"))).toBe(false);

    expect(await screen.findByText("1 system file skipped")).toBeInTheDocument();
  });

  it("walks the row from uploading to uploaded in the tray", async () => {
    vi.stubGlobal("fetch", stubFetch(calls));
    const user = userEvent.setup();

    mount({ target: WRITABLE, entries: [laptopFolder()], storage: memoryStorage() });
    await user.click(screen.getByRole("button", { name: "drop" }));

    expect(await screen.findByText("a.csv")).toBeInTheDocument();
    expect(await screen.findByText("Uploaded")).toBeInTheDocument();
  });

  it("clears itself a moment after every upload finished cleanly", async () => {
    vi.stubGlobal("fetch", stubFetch(calls));
    const user = userEvent.setup();

    const view = mount({
      target: WRITABLE,
      entries: [laptopFolder()],
      storage: memoryStorage(),
      doneLingerMs: PATIENT_LINGER,
    });
    await user.click(screen.getByRole("button", { name: "drop" }));

    // It is on screen for the whole linger, to be read…
    expect(await screen.findByText("Uploaded")).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Uploads" })).toBeInTheDocument();

    // …and when the linger is up it is gone, rows and counts alike, with nobody
    // clicking anything. Shortening it is how the end of the wait is watched
    // without the test sitting through it.
    view.reprop({ doneLingerMs: 0 });
    await waitFor(() => expect(screen.queryByRole("region", { name: "Uploads" })).toBeNull());
    expect(screen.queryByText("Uploaded")).toBeNull();
  });

  it("stays when an upload failed, until Dismiss", async () => {
    vi.stubGlobal("fetch", stubFetch(calls, { conflictOnce: true, replaceStale: true }));
    const user = userEvent.setup();

    mount({
      target: WRITABLE,
      entries: [laptopFolder()],
      storage: memoryStorage(),
      doneLingerMs: LINGER,
    });
    await user.click(screen.getByRole("button", { name: "drop" }));
    await user.click(await screen.findByRole("button", { name: "Replace" }));
    expect(await screen.findByText(STALE_PRECONDITION)).toBeInTheDocument();

    // Well past the linger: the failed row is still there, with its sentence, because
    // it is the only place the person learns the file is not in Files.
    await new Promise((resolve) => setTimeout(resolve, LINGER * 5));
    expect(screen.getByText(STALE_PRECONDITION)).toBeInTheDocument();
    expect(screen.getByText("Failed")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Dismiss" }));
    expect(screen.queryByRole("region", { name: "Uploads" })).toBeNull();
  });

  it("counts a file the server already held instead of patching its time", async () => {
    vi.stubGlobal("fetch", stubFetch(calls, { unchanged: true }));
    const user = userEvent.setup();

    mount({ target: WRITABLE, entries: [laptopFolder()], storage: memoryStorage() });
    await user.click(screen.getByRole("button", { name: "drop" }));

    expect(await screen.findByText("1 of 1 already in Files")).toBeInTheDocument();
    expect(calls.some((call) => call.method === "PATCH")).toBe(false);
  });
});

describe("a resumed session", () => {
  it("re-sends nothing the server already accepted before the reload", async () => {
    vi.stubGlobal("fetch", stubFetch(calls, { acceptedAtStart: [1] }));
    const user = userEvent.setup();
    // What the previous page load left behind, keyed the way the client keys it.
    const seeded = memoryStorage({
      [uploadStorageKey(ACCOUNT)]: JSON.stringify({
        [`dir-0:a.csv:${CSV_BYTES}:${NINETEEN_NINETY_NINE}`]: {
          uploadId: "sess-1",
          name: "a.csv",
          size: CSV_BYTES,
          lastModified: NINETEEN_NINETY_NINE,
          parentId: "dir-0",
          partSize: 1024,
          // What the previous load's first part hashed to. Without it the
          // record names a destination and a label but nothing about the
          // bytes, and a session that cannot be identified is not rejoined.
          partDigests: { "1": "deadbeef" },
        },
      }),
    });

    mount({ target: WRITABLE, entries: [laptopFolder()], storage: seeded });
    await user.click(screen.getByRole("button", { name: "drop" }));

    await waitFor(() => {
      expect(calls.some((call) => call.path.endsWith("/complete"))).toBe(true);
    });

    // The session was rejoined: no new session was opened and no part re-sent.
    expect(calls.filter((call) => call.path.endsWith("/api/v1/files/uploads"))).toHaveLength(0);
    expect(calls.filter((call) => call.method === "PUT")).toHaveLength(0);
  });
});

describe("a refused drop", () => {
  it("shows the reason and sends nothing at all", async () => {
    const fetchStub = stubFetch(calls);
    vi.stubGlobal("fetch", fetchStub);
    const user = userEvent.setup();

    mount({ target: READ_ONLY, entries: [laptopFolder()], storage: memoryStorage() });
    await user.click(screen.getByRole("button", { name: "drop" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "You do not have permission to add to Shared.",
    );
    expect(fetchStub).not.toHaveBeenCalled();
  });
});

describe("a move by drop", () => {
  it("patches the dragged row's parent exactly once", async () => {
    vi.stubGlobal("fetch", stubFetch(calls));
    const user = userEvent.setup();

    mount({
      target: WRITABLE,
      entries: [],
      moveData: JSON.stringify([{ id: "node-9", etag: "etag-9" }]),
      storage: memoryStorage(),
    });
    await user.click(screen.getByRole("button", { name: "drop" }));

    await waitFor(() => {
      expect(calls.filter((call) => call.method === "PATCH")).toHaveLength(1);
    });
    const move = calls.find((call) => call.method === "PATCH");
    expect(move?.url).toContain("/items/node-9");
    expect(move?.body).toEqual({ parentId: "folder-root" });
    expect(move?.headers["if-match"]).toBe("etag-9");
    // A move never opens an upload session.
    expect(calls.some((call) => call.path.endsWith("/api/v1/files/uploads"))).toBe(false);
  });

  it("does not move a row onto itself", async () => {
    vi.stubGlobal("fetch", stubFetch(calls));
    const user = userEvent.setup();

    mount({
      target: WRITABLE,
      entries: [],
      moveData: JSON.stringify([{ id: WRITABLE.id, etag: "etag-root" }]),
      storage: memoryStorage(),
    });
    await user.click(screen.getByRole("button", { name: "drop" }));

    await act(async () => {
      await Promise.resolve();
    });
    expect(calls.filter((call) => call.method === "PATCH")).toHaveLength(0);
  });
});

describe("a name collision", () => {
  it("asks once and keeps both when the person says so", async () => {
    vi.stubGlobal("fetch", stubFetch(calls, { conflictOnce: true }));
    const user = userEvent.setup();

    mount({ target: WRITABLE, entries: [laptopFolder()], storage: memoryStorage() });
    await user.click(screen.getByRole("button", { name: "drop" }));

    await user.click(await screen.findByRole("button", { name: "Keep both" }));

    await waitFor(() => {
      const completes = calls.filter((call) => call.path.endsWith("/complete"));
      expect(completes).toHaveLength(2);
      expect((completes[1]?.body as { conflictBehavior: string }).conflictBehavior).toBe("rename");
    });
  });

  it("replaces the file already there, fencing against the version it was shown", async () => {
    // Replace was a hard-coded disabled button with a hover-only reason, while
    // the session API had honoured `replace` since the commit behaviour landed.
    // What was missing was the precondition: `replace` writes onto a node that
    // already exists, so it must name the version it believes it is replacing.
    vi.stubGlobal("fetch", stubFetch(calls, { conflictOnce: true }));
    const user = userEvent.setup();

    mount({ target: WRITABLE, entries: [laptopFolder()], storage: memoryStorage() });
    await user.click(screen.getByRole("button", { name: "drop" }));

    const replace = await screen.findByRole("button", { name: "Replace" });
    expect(replace).not.toBeDisabled();
    await user.click(replace);

    await waitFor(() => {
      const completes = calls.filter((call) => call.path.endsWith("/complete"));
      expect(completes).toHaveLength(2);
      expect((completes[1]?.body as { conflictBehavior: string }).conflictBehavior).toBe("replace");
      // The etag of the file already holding the name, read from the folder.
      expect(completes[1]?.headers["if-match"] ?? completes[1]?.headers["If-Match"]).toBe(
        "et-existing",
      );
    });
    // One node, a new version on it — not a second `a (2).csv`.
    expect(calls.filter((call) => call.path.endsWith("/complete"))).toHaveLength(2);
    expect(
      calls.some(
        (call) =>
          call.path.endsWith("/complete") &&
          (call.body as { conflictBehavior?: string }).conflictBehavior === "rename",
      ),
    ).toBe(false);
  });

  it("says nothing was replaced when the version moved on under it", async () => {
    vi.stubGlobal("fetch", stubFetch(calls, { conflictOnce: true, replaceStale: true }));
    const user = userEvent.setup();

    mount({ target: WRITABLE, entries: [laptopFolder()], storage: memoryStorage() });
    await user.click(screen.getByRole("button", { name: "drop" }));
    await user.click(await screen.findByRole("button", { name: "Replace" }));

    expect(await screen.findByText(STALE_PRECONDITION)).toBeInTheDocument();
  });

  it("refuses a Replace it could not read a version for, rather than overwriting blindly", async () => {
    vi.stubGlobal("fetch", stubFetch(calls, { conflictOnce: true, nameNotInFolder: true }));
    const user = userEvent.setup();

    mount({ target: WRITABLE, entries: [laptopFolder()], storage: memoryStorage() });
    await user.click(screen.getByRole("button", { name: "drop" }));
    await user.click(await screen.findByRole("button", { name: "Replace" }));

    expect(await screen.findByText(REPLACE_NEEDS_ETAG)).toBeInTheDocument();
    // Nothing was sent a second time: no completion went out without a version.
    expect(calls.filter((call) => call.path.endsWith("/complete"))).toHaveLength(1);
  });
});
