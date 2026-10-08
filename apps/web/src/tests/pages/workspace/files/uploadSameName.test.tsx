import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useMemo } from "react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { createQueryClient } from "@/api/queryClient";
import type { DropTarget } from "@/pages/workspace/files/dropHandlers";
import { UploadTray } from "@/pages/workspace/files/UploadTray";
import { useUploads, type DropTransfer } from "@/pages/workspace/files/useUploads";

// Two files that a browser cannot tell apart by name are the case where an
// upload queue can lose bytes without anybody being told. The server behind
// these tests is the real namespace's rule and nothing else: one live name per
// folder, `rename` takes the next free one, `replace` writes a version onto the
// node already there, and a completion onto a name the folder already holds is
// refused ON THE CALL with `files.exists`, leaving the session and its parts
// where they were. A commit that only finds the name taken once it runs (two
// uploads of one name racing) comes back as a FAILED operation carrying the
// same code, and that session is gone.
//
// Every assertion is a read-back of the bodies the server ended up holding,
// because "a row appeared" is exactly the evidence that was there while the
// bytes were gone.

const WRITABLE: DropTarget = {
  id: "folder-root",
  name: "Home",
  kind: "folder",
  capabilities: { can_write: true },
};

/** Same folder, same name, same size, same modification time: the four facts a
 *  remembered session is recognised by, so two files agreeing on all of them is
 *  the pair a resume cannot tell apart. */
const STAMP = Date.UTC(2020, 0, 1);

interface StoredNode {
  id: string;
  name: string;
  parentId: string;
  body: string;
  etag: number;
}

/** What the server holds for one part: the bytes, and the size and digest it
 *  was handed them under. A commit declares all three back. */
interface HeldPart {
  body: string;
  size: number;
  checksum: string;
}

interface Session {
  id: string;
  name: string;
  parentId: string;
  parts: Map<number, HeldPart>;
}

/** One part of a commit's declared list, as the session API takes it. */
interface PartRef {
  partNo: number;
  size: number;
  checksum: string;
}

/**
 * The server's own agreement rule (`_agree`, `alkera_core/files/uploads.py`).
 *
 * A commit declares a size and a digest for every part the server holds, and a
 * list that is not exactly what it holds is refused with `files.parts_mismatch`.
 * The fake enforces it so these tests see the failure production would produce
 * rather than a commit nothing checked.
 */
function agree(declared: readonly PartRef[], held: ReadonlyMap<number, HeldPart>): boolean {
  if (declared.length !== held.size) return false;
  return declared.every((part) => {
    const mine = held.get(part.partNo);
    return mine !== undefined && mine.size === part.size && mine.checksum === part.checksum;
  });
}

interface OperationRow {
  id: string;
  state: string;
  resultNodeId: string | null;
  resultUnchanged: boolean;
  errors: { code: string; message: string }[];
}

interface Server {
  nodes: StoredNode[];
  /** What the folder holds now, as `name → body`. */
  contents(): Record<string, string>;
  opens: number;
  /** Part bodies received, in order, across every session. */
  puts: string[];
  completes: { session: string; name: string; behaviour: string }[];
  /** Leave every collision to the queued commit, as a race between two
   *  completions of one name does. */
  refuseInCommit: boolean;
  /** Held open until resolved: every completion that answers a collision
   *  waits on it, so a test can read the row while the answer is in flight. */
  answerGate: Promise<void> | null;
}

/** `report.txt` → `report (2).txt`, the way the namespace's own rename does. */
function nextFreeName(taken: ReadonlySet<string>, name: string): string {
  const dot = name.lastIndexOf(".");
  const stem = dot > 0 ? name.slice(0, dot) : name;
  const suffix = dot > 0 ? name.slice(dot) : "";
  for (let n = 2; ; n += 1) {
    const candidate = `${stem} (${n})${suffix}`;
    if (!taken.has(candidate)) return candidate;
  }
}

function stubServer(): Server {
  const sessions = new Map<string, Session>();
  const operations = new Map<string, OperationRow>();
  let sessionNo = 0;
  let nodeNo = 0;
  const server: Server = {
    nodes: [],
    contents: () => Object.fromEntries(server.nodes.map((node) => [node.name, node.body] as const)),
    opens: 0,
    puts: [],
    completes: [],
    refuseInCommit: false,
    answerGate: null,
  };

  const json = (payload: unknown, status = 200): Response =>
    new Response(JSON.stringify(payload), {
      status,
      headers: { "content-type": "application/json" },
    });

  const commit = (session: Session, behaviour: string): OperationRow => {
    const bytes = [...session.parts.entries()]
      .sort((a, b) => a[0] - b[0])
      .map(([, part]) => part.body)
      .join("");
    const op: OperationRow = {
      id: `op-${session.id}`,
      state: "done",
      resultNodeId: null,
      resultUnchanged: false,
      errors: [],
    };
    const head = server.nodes.find(
      (node) => node.parentId === session.parentId && node.name === session.name,
    );
    if (head && behaviour === "fail") {
      op.state = "failed";
      op.errors = [{ code: "files.exists", message: "that name is taken in this folder" }];
      return op;
    }
    if (head && behaviour === "replace") {
      op.resultUnchanged = head.body === bytes;
      head.body = bytes;
      head.etag += 1;
      op.resultNodeId = head.id;
      return op;
    }
    nodeNo += 1;
    const taken = new Set(
      server.nodes.filter((node) => node.parentId === session.parentId).map((node) => node.name),
    );
    const name = head ? nextFreeName(taken, session.name) : session.name;
    const created: StoredNode = {
      id: `node-${nodeNo}`,
      name,
      parentId: session.parentId,
      body: bytes,
      etag: 1,
    };
    server.nodes.push(created);
    op.resultNodeId = created.id;
    return op;
  };

  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const asRequest = input instanceof Request ? input : null;
      const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
      const path = new URL(url, "http://localhost").pathname;
      const method = (init?.method ?? asRequest?.method ?? "GET").toUpperCase();
      const raw =
        typeof init?.body === "string"
          ? init.body
          : init?.body instanceof Blob
            ? await blobText(init.body)
            : asRequest
              ? await asRequest.clone().text()
              : "";

      if (method === "POST" && path.endsWith("/api/v1/files/uploads")) {
        server.opens += 1;
        sessionNo += 1;
        const asked = JSON.parse(raw) as { name: string; parentId: string };
        const session: Session = {
          id: `sess-${sessionNo}`,
          name: asked.name,
          parentId: asked.parentId,
          parts: new Map(),
        };
        sessions.set(session.id, session);
        return json({
          uploadId: session.id,
          partSize: 1024,
          partsTotal: 1,
          expiresAt: "",
        });
      }

      const held = /\/uploads\/(sess-\d+)(\/|$)/.exec(path)?.[1];
      const session = held ? sessions.get(held) : undefined;

      if (session && path.endsWith("/complete")) {
        const asked = JSON.parse(raw) as { conflictBehavior: string; parts?: PartRef[] };
        server.completes.push({
          session: session.id,
          name: session.name,
          behaviour: asked.conflictBehavior,
        });
        if (!agree(asked.parts ?? [], session.parts)) {
          return json(
            { code: "files.parts_mismatch", message: "the completing part list disagrees" },
            409,
          );
        }
        if (asked.conflictBehavior !== "fail" && server.answerGate) await server.answerGate;
        const taken = server.nodes.some(
          (node) => node.parentId === session.parentId && node.name === session.name,
        );
        if (taken && asked.conflictBehavior === "fail" && !server.refuseInCommit) {
          return json({ code: "files.exists", message: "that name is taken in this folder" }, 409);
        }
        const op = commit(session, asked.conflictBehavior);
        operations.set(op.id, op);
        return json(op, 202);
      }
      if (session && method === "PUT") {
        const part = Number(path.slice(path.lastIndexOf("/") + 1));
        const checksum = new Headers(init?.headers ?? {}).get("x-part-checksum") ?? "";
        session.parts.set(part, { body: raw, size: raw.length, checksum });
        server.puts.push(raw);
        return json({ ok: true });
      }
      if (session && method === "DELETE") {
        sessions.delete(session.id);
        return json({}, 204);
      }
      if (session && method === "GET") {
        const accepted = [...session.parts.keys()].sort((a, b) => a - b);
        return json({
          uploadId: session.id,
          state: "open",
          offset: accepted.length * 1024,
          length: 1024,
          complete: false,
          partsDone: accepted.length,
          partsTotal: 1,
          acceptedParts: accepted,
        });
      }

      const op = /\/operations\/(op-sess-\d+)$/.exec(path)?.[1];
      if (op && method === "GET") {
        const row = operations.get(op);
        return row ? json(row) : json({ code: "files.not_found", message: "gone" }, 404);
      }
      if (method === "POST" && /\/items\/lookup$/.test(path)) {
        const ids = (JSON.parse(raw) as { ids: string[] }).ids;
        return json({
          value: server.nodes
            .filter((node) => ids.includes(node.id))
            .map((node) => ({
              id: node.id,
              etag: String(node.etag),
              name: node.name,
              kind: "file",
            })),
        });
      }
      if (method === "PATCH") return json({ id: "node", etag: "9" });
      if (method === "GET" && /\/children$/.test(path)) {
        return json({
          value: server.nodes.map((node) => ({
            id: node.id,
            name: node.name,
            nameDisplay: node.name,
            etag: String(node.etag),
            kind: "file",
          })),
          nextMarker: null,
        });
      }
      return json({ code: "unexpected", message: url }, 500);
    }),
  );
  return server;
}

function memoryStorage() {
  const store = new Map<string, string>();
  return {
    getItem: (key: string) => store.get(key) ?? null,
    setItem: (key: string, value: string) => void store.set(key, value),
    removeItem: (key: string) => void store.delete(key),
  };
}

/** A digest of the bytes themselves, so two parts holding the same text hash the
 *  same and two holding different text do not — the only property the identical
 *  copy fold may rest on. */
/** jsdom's `Blob` is not the one `Response` knows how to consume, so the bytes
 *  are read the way the platform reads a picked file. */
function blobText(part: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result));
    reader.onerror = () => reject(reader.error ?? new Error("unreadable"));
    reader.readAsText(part);
  });
}

async function textDigest(part: Blob): Promise<string> {
  const text = await blobText(part);
  let hash = 2166136261;
  for (let at = 0; at < text.length; at += 1) {
    hash ^= text.charCodeAt(at);
    hash = Math.imul(hash, 16777619);
  }
  return (hash >>> 0).toString(16);
}

/** The shape the Upload files picker hands over: flat files, no entries. */
function picked(files: readonly File[]): DropTransfer {
  return {
    items: files.map((file) => ({ kind: "file", getAsFile: () => file })),
    getData: () => "",
  };
}

function madeFile(name: string, body: string): File {
  return new File([body], name, { lastModified: STAMP });
}

let storage: ReturnType<typeof memoryStorage>;

function Harness({ drops }: { drops: File[][] }) {
  const clientOptions = useMemo(() => ({ storage, digest: textDigest }), []);
  const uploads = useUploads({ driveId: "drive-1", doneLingerMs: 60_000, clientOptions });
  return (
    <div>
      {drops.map((files, at) => (
        <button key={at} type="button" onClick={() => void uploads.onDrop(WRITABLE, picked(files))}>
          drop {at + 1}
        </button>
      ))}
      {/* The rows as the hook hands them to the tray. Two files of one name
          render the same name, so the only way to say WHICH row carries a
          sentence is the size beside it. */}
      <div data-testid="rows">
        {JSON.stringify(uploads.rows.map((row) => ({ total: row.total, outcome: row.outcome })))}
      </div>
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

function mount(drops: File[][]) {
  return render(
    <QueryClientProvider client={createQueryClient()}>
      <MemoryRouter>
        <Harness drops={drops} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

let server: Server;

beforeEach(() => {
  storage = memoryStorage();
  server = stubServer();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("two files with one name in a single drop", () => {
  it("keeps both sets of bytes when the contents differ", async () => {
    const user = userEvent.setup();
    mount([[madeFile("collide.txt", "I am from dupA"), madeFile("collide.txt", "I am from dupB")]]);
    await user.click(screen.getByRole("button", { name: "drop 1" }));

    const status = await screen.findByRole("status");
    await waitFor(() => expect(status).toHaveTextContent("2 files uploaded"), { timeout: 5000 });

    // Read back what the folder holds: two nodes, and BOTH bodies among them.
    const bodies = Object.values(server.contents()).sort();
    expect(bodies).toEqual(["I am from dupA", "I am from dupB"]);
    expect(Object.keys(server.contents()).sort()).toEqual(["collide (2).txt", "collide.txt"]);
    expect(status.textContent).not.toMatch(/failed/);
  });

  it("puts the renamed sentence on the row that was actually renamed", async () => {
    const user = userEvent.setup();
    // Different lengths, so the two rows can be told apart at all: they render
    // the same name, and the tray is the only place a person learns which of
    // their two files went in under a name nobody typed.
    mount([[madeFile("c.txt", "abc"), madeFile("c.txt", "abcdef")]]);
    await user.click(screen.getByRole("button", { name: "drop 1" }));

    const status = await screen.findByRole("status");
    await waitFor(() => expect(status).toHaveTextContent("2 files uploaded"), { timeout: 5000 });
    // The 3-byte file kept the name; the 6-byte one became `c (2).txt`.
    expect(server.contents()).toEqual({ "c.txt": "abc", "c (2).txt": "abcdef" });

    const rows = JSON.parse(screen.getByTestId("rows").textContent ?? "[]") as {
      total: number;
      outcome?: string;
    }[];
    const renamed = rows.find((row) => row.total === 6);
    const kept = rows.find((row) => row.total === 3);
    expect(renamed?.outcome).toBe("Kept both. This one was saved under a new name.");
    expect(kept?.outcome).toBeUndefined();
  });

  it("says once that a copy was identical, and keeps one node", async () => {
    const user = userEvent.setup();
    mount([[madeFile("notes.md", "the same bytes"), madeFile("notes.md", "the same bytes")]]);
    await user.click(screen.getByRole("button", { name: "drop 1" }));

    await waitFor(() => expect(server.nodes).toHaveLength(1), { timeout: 5000 });
    expect(server.contents()).toEqual({ "notes.md": "the same bytes" });
    expect(await screen.findByText("1 identical copy was skipped")).toBeInTheDocument();
    // The fold is decided before anything is sent: an identical copy costs no
    // session at all, which is what keeps a 300-file drop of copies cheap.
    expect(server.opens).toBe(1);
  });

  it("folds the same file picked twice into one upload", async () => {
    const user = userEvent.setup();
    const once = madeFile("report.csv", "a,b\n1,2\n");
    mount([[once, once]]);
    await user.click(screen.getByRole("button", { name: "drop 1" }));

    await waitFor(() => expect(server.nodes).toHaveLength(1), { timeout: 5000 });
    expect(server.contents()).toEqual({ "report.csv": "a,b\n1,2\n" });
    expect(server.opens).toBe(1);
  });

  it("treats a case-only difference as two names, because the folder does", async () => {
    const user = userEvent.setup();
    mount([[madeFile("Note.txt", "upper"), madeFile("note.txt", "lower")]]);
    await user.click(screen.getByRole("button", { name: "drop 1" }));

    const status = await screen.findByRole("status");
    await waitFor(() => expect(status).toHaveTextContent("2 files uploaded"), { timeout: 5000 });
    expect(server.contents()).toEqual({ "Note.txt": "upper", "note.txt": "lower" });
  });
});

describe("a name that is already in the folder", () => {
  it("asks the person rather than dropping the new bytes", async () => {
    const user = userEvent.setup();
    mount([[madeFile("two.txt", "first fifteen!")], [madeFile("two.txt", "a longer second body")]]);
    await user.click(screen.getByRole("button", { name: "drop 1" }));
    await waitFor(() => expect(server.nodes).toHaveLength(1), { timeout: 5000 });

    await user.click(screen.getByRole("button", { name: "drop 2" }));
    // The refusal the commit came back with is a question, not a failure: the
    // bytes are still on the server and the person decides what happens to them.
    const prompt = await screen.findByRole("group", { name: "Name taken: two.txt" });
    await user.click(await within(prompt).findByRole("button", { name: "Keep both" }));

    await waitFor(() => expect(server.nodes).toHaveLength(2), { timeout: 5000 });
    expect(server.contents()).toEqual({
      "two.txt": "first fifteen!",
      "two (2).txt": "a longer second body",
    });
  });

  it("writes the new bytes as a version when the answer is Replace", async () => {
    const user = userEvent.setup();
    mount([[madeFile("two.txt", "first fifteen!")], [madeFile("two.txt", "a longer second body")]]);
    await user.click(screen.getByRole("button", { name: "drop 1" }));
    await waitFor(() => expect(server.nodes).toHaveLength(1), { timeout: 5000 });

    await user.click(screen.getByRole("button", { name: "drop 2" }));
    const prompt = await screen.findByRole("group", { name: "Name taken: two.txt" });
    await user.click(await within(prompt).findByRole("button", { name: "Replace" }));

    await waitFor(() => expect(server.contents()).toEqual({ "two.txt": "a longer second body" }), {
      timeout: 5000,
    });
    expect(server.nodes).toHaveLength(1);
    expect(server.nodes[0]!.etag).toBe(2);
  });
  it("keeps both by finishing the bytes already sent, without sending them again", async () => {
    const user = userEvent.setup();
    mount([[madeFile("b.txt", "first fifteen!")], [madeFile("b.txt", "a longer second body")]]);
    await user.click(screen.getByRole("button", { name: "drop 1" }));
    await waitFor(() => expect(server.nodes).toHaveLength(1), { timeout: 5000 });

    await user.click(screen.getByRole("button", { name: "drop 2" }));
    const prompt = await screen.findByRole("group", { name: "Name taken: b.txt" });
    let release = (): void => {};
    server.answerGate = new Promise<void>((resolve) => {
      release = resolve;
    });
    await user.click(await within(prompt).findByRole("button", { name: "Keep both" }));

    // Every byte is already on the server while the answer is carried out, so
    // the row says the file is being finished, never that nothing is sent yet.
    const statuses = (): string[] =>
      [...document.querySelectorAll(".alk-files-uploads__status")].map(
        (cell) => cell.textContent ?? "",
      );
    await waitFor(() => expect(statuses()).toContain("Finishing"), { timeout: 5000 });
    expect(statuses()).not.toContain("0%");
    release();

    await waitFor(() => expect(server.nodes).toHaveLength(2), { timeout: 5000 });
    expect(server.contents()).toEqual({
      "b.txt": "first fifteen!",
      "b (2).txt": "a longer second body",
    });
    // One session per file and one send of each body: the answer completed
    // the session the refusal left open.
    expect(server.opens).toBe(2);
    expect(server.puts.filter((body) => body === "a longer second body")).toHaveLength(1);
    const second = server.completes.filter((done) => done.session !== server.completes[0]!.session);
    expect(second.map((done) => done.behaviour)).toEqual(["fail", "rename"]);
    expect(new Set(second.map((done) => done.session)).size).toBe(1);
  });

  it("sends the bytes again when the commit itself found the name taken", async () => {
    const user = userEvent.setup();
    server.refuseInCommit = true;
    mount([[madeFile("b.txt", "first fifteen!")], [madeFile("b.txt", "a longer second body")]]);
    await user.click(screen.getByRole("button", { name: "drop 1" }));
    await waitFor(() => expect(server.nodes).toHaveLength(1), { timeout: 5000 });

    await user.click(screen.getByRole("button", { name: "drop 2" }));
    const prompt = await screen.findByRole("group", { name: "Name taken: b.txt" });
    await user.click(await within(prompt).findByRole("button", { name: "Keep both" }));

    await waitFor(() => expect(server.nodes).toHaveLength(2), { timeout: 5000 });
    expect(server.contents()).toEqual({
      "b.txt": "first fifteen!",
      "b (2).txt": "a longer second body",
    });
    // The refused commit released its session, so the answer needed a new one.
    expect(server.opens).toBe(3);
  });
});
