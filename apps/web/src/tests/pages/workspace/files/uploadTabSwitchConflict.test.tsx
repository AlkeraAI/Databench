// A name collision asked while the person switches away from the Files pane.
//
// The pane unmounts, so the question goes with it. What must hold: the upload
// does not come back as a promise it cannot keep. Dropping the same file again
// either continues the session (it is still live: the question is asked
// again) or, when the refused commit already ended the session, starts over
// on a new one — never a resume whose completion the server refuses with a
// 409 forever. And the "from your last visit can continue" line goes once the
// file is dropped again.

import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { createQueryClient } from "@/api/queryClient";
import type { DropEntry, DropTarget } from "@/pages/workspace/files/dropHandlers";
import { UploadTray } from "@/pages/workspace/files/UploadTray";
import { useUploads, type DropTransfer } from "@/pages/workspace/files/useUploads";

const FOLDER: DropTarget = { id: "folder-1", name: "Project", kind: "folder", capabilities: { can_write: true } };
const ACCOUNT = { userId: "user-1", orgId: "org-1" };
const NAME = "pyproject.toml";
const BYTES = '[project]\nname = "demo"\n';
const MODIFIED = Date.UTC(2026, 9, 5);

function entry(): DropEntry {
  const file = new File([BYTES], NAME, { lastModified: MODIFIED });
  return { isFile: true, isDirectory: false, name: NAME, file: (onSuccess) => onSuccess(file) };
}

function transfer(): DropTransfer {
  const one = entry();
  return { items: [{ kind: "file", webkitGetAsEntry: () => one }], getData: () => "" };
}

function memoryStorage() {
  const store = new Map<string, string>();
  return {
    getItem: (key: string): string | null => store.get(key) ?? null,
    setItem: (key: string, value: string): void => void store.set(key, value),
    removeItem: (key: string): void => void store.delete(key),
    dump: (): Map<string, string> => new Map(store),
    restore: (from: Map<string, string>): void => {
      store.clear();
      for (const [key, value] of from) store.set(key, value);
    },
  };
}

interface Session {
  state: string;
  parts: Set<number>;
}

/** The session API over a folder that already holds `pyproject.toml`. How a
 *  plain completion is refused is the case: synchronously (the session stays
 *  open) or inside the queued commit (which aborts the session). */
function server(refusal: "sync" | "queued") {
  const sessions = new Map<string, Session>();
  const completions: { session: string; status: number; behaviour: string }[] = [];
  let opened = 0;
  let ops = 0;
  const json = (payload: unknown, status = 200): Response =>
    new Response(JSON.stringify(payload), { status, headers: { "content-type": "application/json" } });

  const fetchImpl = vi.fn(async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
    const path = new URL(url, "http://localhost").pathname;
    const method = (init?.method ?? "GET").toUpperCase();

    if (method === "POST" && path.endsWith("/api/v1/files/uploads")) {
      opened += 1;
      const id = `sess-${opened}`;
      sessions.set(id, { state: "open", parts: new Set() });
      return json({ uploadId: id, partSize: 1024, partsTotal: 1, expiresAt: "" });
    }
    const op = /\/operations\/(op-\d+)$/.exec(path);
    if (op) return json({ id: op[1], state: "failed", errors: [{ code: "files.exists", message: "that name is taken in this folder" }] });
    const complete = /\/uploads\/(sess-\d+)\/complete$/.exec(path);
    if (complete) {
      const id = complete[1]!;
      const session = sessions.get(id)!;
      const behaviour = (JSON.parse(String(init?.body)) as { conflictBehavior: string }).conflictBehavior;
      const answer = (status: number, body: unknown) => {
        completions.push({ session: id, status, behaviour });
        return json(body, status);
      };
      if (session.state !== "open" && session.state !== "uploading") {
        return answer(409, { code: "files.session_state", message: `session ${id} is ${session.state} and cannot complete` });
      }
      if (behaviour === "fail") {
        if (refusal === "sync") return answer(409, { code: "files.exists", message: "that name is taken in this folder" });
        session.state = "aborted";
        ops += 1;
        return answer(202, { id: `op-${ops}`, state: "queued" });
      }
      session.state = "committed";
      return answer(200, { item: { id: "node-pyproject", etag: "etag-2" }, unchanged: false });
    }
    const part = /\/uploads\/(sess-\d+)\/parts\/(\d+)$/.exec(path);
    if (part && method === "PUT") {
      sessions.get(part[1]!)!.parts.add(Number(part[2]));
      return json({ ok: true });
    }
    const status = /\/uploads\/(sess-\d+)$/.exec(path);
    if (status) {
      const session = sessions.get(status[1]!);
      if (!session) return json({ code: "files.not_found", message: "gone" }, 404);
      if (method === "DELETE") {
        session.state = "aborted";
        return new Response(null, { status: 204 });
      }
      return json({
        uploadId: status[1],
        state: session.state,
        offset: 0,
        length: BYTES.length,
        complete: false,
        partsDone: session.parts.size,
        partsTotal: 1,
        acceptedParts: [...session.parts],
      });
    }
    if (path.endsWith("/children")) {
      return json({ value: [{ id: "node-pyproject", name: NAME, nameDisplay: NAME, etag: "etag-1" }], nextMarker: null });
    }
    if (path.includes("/items/")) return json({ id: "node-pyproject", etag: "etag-3" });
    return json({ value: [], nextMarker: null });
  });
  // The folder lookup Replace makes goes through the app's own API client.
  vi.stubGlobal("fetch", fetchImpl);
  return { fetchImpl, completions, sessions };
}

function Pane({ storage, fetchImpl }: { storage: ReturnType<typeof memoryStorage>; fetchImpl: typeof fetch }) {
  const uploads = useUploads({
    driveId: "drive-1",
    account: ACCOUNT,
    doneLingerMs: 60_000,
    clientOptions: { storage, fetchImpl, digest: async () => "deadbeef" },
  });
  return (
    <div>
      <button type="button" onClick={() => void uploads.onDrop(FOLDER, transfer())}>
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

function mount(storage: ReturnType<typeof memoryStorage>, fetchImpl: typeof fetch) {
  return render(
    <QueryClientProvider client={createQueryClient()}>
      <MemoryRouter>
        <Pane storage={storage} fetchImpl={fetchImpl} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const CONTINUE = /from your last visit can continue/;

let escaped: unknown[] = [];
const catchEscaped = (reason: unknown): void => void escaped.push(reason);

beforeEach(() => {
  escaped = [];
  process.on("unhandledRejection", catchEscaped);
});

afterEach(() => {
  process.off("unhandledRejection", catchEscaped);
  vi.unstubAllGlobals();
});

describe("a name collision left open by switching away from the Files pane", () => {
  it.each([
    ["refused on the call, the session still open", "sync" as const, ["sess-1"]],
    // The commit refusing the name ends the session it was asked of, so the
    // answer rides on a new one each time.
    ["refused inside the queued commit, which ended the session", "queued" as const, ["sess-1", "sess-2", "sess-3"]],
  ])("asks again when the file is dropped again (%s), and Replace lands", async (_case, refusal, sessionsUsed) => {
    const user = userEvent.setup();
    const storage = memoryStorage();
    const wire = server(refusal);

    const first = mount(storage, wire.fetchImpl as unknown as typeof fetch);
    await user.click(screen.getByRole("button", { name: "drop" }));
    await screen.findByRole("button", { name: "Replace" });
    // Another tab: the pane and its question go away.
    first.unmount();
    await new Promise((resolve) => setTimeout(resolve, 20));

    mount(storage, wire.fetchImpl as unknown as typeof fetch);
    if (refusal === "sync") {
      // The session can still complete, so it is offered.
      expect(screen.getByText(CONTINUE)).toHaveTextContent(NAME);
    } else {
      // The refused commit ended it: nothing is left to continue.
      expect(screen.queryByText(CONTINUE)).toBeNull();
    }

    await user.click(screen.getByRole("button", { name: "drop" }));
    await user.click(await screen.findByRole("button", { name: "Replace" }));
    await waitFor(() => expect(wire.completions.at(-1)).toMatchObject({ status: 200, behaviour: "replace" }));
    await screen.findByText(/Replaced: a new version of the file\./);

    expect(screen.queryByText(CONTINUE)).toBeNull();
    // No completion was refused for a session that could no longer complete.
    expect(wire.completions.filter((c) => c.status === 409 && c.behaviour !== "fail")).toEqual([]);
    expect(wire.completions.at(-1)).toMatchObject({ status: 200, behaviour: "replace" });
    expect([...new Set(wire.completions.map((c) => c.session))]).toEqual(sessionsUsed);
    expect(escaped).toEqual([]);
  });

  it("starts over on a new session when the remembered one was ended, rather than resuming it", async () => {
    const user = userEvent.setup();
    const storage = memoryStorage();
    const wire = server("queued");
    const first = mount(storage, wire.fetchImpl as unknown as typeof fetch);
    await user.click(screen.getByRole("button", { name: "drop" }));
    await screen.findByRole("button", { name: "Replace" });
    // The record of the session the refused commit ended, as a tab that kept
    // it left it behind.
    const kept = storage.dump();
    expect([...kept.values()].join("")).toContain("sess-1");
    first.unmount();
    await new Promise((resolve) => setTimeout(resolve, 20));
    storage.restore(kept);

    mount(storage, wire.fetchImpl as unknown as typeof fetch);
    await user.click(screen.getByRole("button", { name: "drop" }));
    await screen.findByRole("button", { name: "Replace" });
    // The ended session is never completed again; the question comes from a
    // new one.
    expect(wire.completions.map((c) => c.session)).toEqual(["sess-1", "sess-2"]);
    expect(wire.completions.filter((c) => c.status === 409)).toEqual([]);
    expect(escaped).toEqual([]);
  });
});
