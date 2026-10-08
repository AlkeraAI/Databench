// A new notebook: the text the format's own `alkera-notebook new` writes, its
// file name, and its upload through a Files session.

import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { afterEach, describe, expect, it, vi } from "vitest";

import { CELL_ID_PATTERN } from "@/api/realtime/crdt/notebookDoc";
import { createNotebook, newNotebookText } from "@/pages/workspace/chat/workspace/notebook/newNotebook";
import { notebookFileName } from "@/pages/workspace/chat/workspace/notebook/notebookNames";

// The text of a new notebook, which `alkera-notebook new` is tested against
// too (packages/alkera-notebook/tests/test_nbeng_cli.py).
const VECTOR = JSON.parse(
  readFileSync(resolve(__dirname, "../../../../../../../../../packages/alkera-notebook/tests/vectors/new_notebook.json"), "utf-8"),
) as { setup_id: string; cell_id: string; text: string };
const IDS = { setup: VECTOR.setup_id, cell: VECTOR.cell_id };
const FROM_THE_CLI = VECTOR.text;

if (typeof Blob !== "undefined" && typeof Blob.prototype.text !== "function") {
  Blob.prototype.text = function readText(this: Blob): Promise<string> {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result));
      reader.onerror = () => reject(reader.error);
      reader.readAsText(this);
    });
  };
}

afterEach(() => vi.unstubAllGlobals());

describe("the new notebook's text", () => {
  it("is what the format's writer writes for a new notebook", () => {
    expect(newNotebookText(IDS)).toBe(FROM_THE_CLI);
  });

  it("imports alkera in the setup block, ahead of the first cell", () => {
    const text = newNotebookText({ setup: "aaaaaaaaa1", cell: "aaaaaaaaa2" });
    const lines = text.split("\n");
    const setup = lines.indexOf('with app.setup(alkera_id="aaaaaaaaa1"):');
    expect(setup).toBeGreaterThan(lines.indexOf("app = marimo.App()"));
    expect(lines[setup + 1]).toBe("    import alkera");
    expect(lines.indexOf('@app.cell(alkera_id="aaaaaaaaa2")')).toBeGreaterThan(setup + 1);
  });

  it("gives its two cells fresh ids of 10 Crockford base32 characters", () => {
    const ids = Array.from({ length: 20 }, () => [...newNotebookText().matchAll(/alkera_id="([^"]+)"/g)].map((m) => m[1]!)).flat();
    expect(ids).toHaveLength(40);
    for (const id of ids) expect(id).toMatch(CELL_ID_PATTERN);
    expect(new Set(ids).size).toBe(ids.length);
  });
});

describe("the new notebook's name", () => {
  it.each([
    ["", "Untitled.alknb.py"],
    ["  ", "Untitled.alknb.py"],
    ["Revenue", "Revenue.alknb.py"],
    ["revenue.alknb.py", "revenue.alknb.py"],
    ["Revenue.ALKNB.PY", "Revenue.ALKNB.PY"],
    ["report.py", "report.alknb.py"],
  ])("%j is %j", (typed, name) => {
    expect(notebookFileName(typed)).toBe(name);
  });
});

describe("creating a notebook", () => {
  function server(complete: () => Response, open?: () => Response) {
    const calls: { method: string; url: string; body: unknown }[] = [];
    const impl = vi.fn(async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
      const url = String(input);
      const method = init?.method ?? "GET";
      const body = init?.body;
      calls.push({ method, url, body: typeof body === "string" ? JSON.parse(body) : body instanceof Blob ? await body.text() : body });
      const json = (status: number, payload: unknown) => new Response(JSON.stringify(payload), { status, headers: { "content-type": "application/json" } });
      if (method === "POST" && url.endsWith("/api/v1/files/uploads")) {
        if (open) return open();
        return json(201, { uploadId: "u1", partSize: 1 << 20, partsTotal: 1, limits: { maxPartBytes: 1 << 20, maxParts: 10 }, expiresAt: "2026-12-01T00:00:00Z" });
      }
      if (method === "GET" && url.endsWith("/uploads/u1")) {
        return json(200, { uploadId: "u1", state: "open", offset: 0, length: 0, complete: false, partsDone: 0, partsTotal: 1, acceptedParts: [] });
      }
      if (method === "PUT" && url.includes("/parts/")) return json(200, { partNo: 1, size: 1, duplicate: false });
      if (method === "POST" && url.endsWith("/complete")) return complete();
      return json(500, { error: { code: "unexpected", message: url } });
    });
    return { impl: impl as unknown as typeof fetch, calls };
  }
  const digest = () => Promise.resolve("00");

  it("uploads the notebook's text into the folder, asking for a new name when it is taken", async () => {
    const s = server(
      () =>
        new Response(JSON.stringify({ id: "op1", kind: "upload", state: "done", done: 1, total: 1, resultNodeId: "node-9" }), {
          status: 202,
          headers: { "content-type": "application/json" },
        }),
    );
    const created = await createNotebook("drive-1", "folder-1", "Revenue", { fetchImpl: s.impl, digest, ids: IDS });
    expect(created).toEqual({ nodeId: "node-9", name: "Revenue.alknb.py" });
    const open = s.calls.find((c) => c.method === "POST" && c.url.endsWith("/api/v1/files/uploads"));
    expect(open?.body).toMatchObject({ name: "Revenue.alknb.py", parentId: "folder-1", declaredSize: new TextEncoder().encode(FROM_THE_CLI).length });
    expect(s.calls.find((c) => c.method === "PUT")?.body).toBe(FROM_THE_CLI);
    expect(s.calls.find((c) => c.url.endsWith("/complete"))?.body).toMatchObject({ conflictBehavior: "rename" });
  });

  it("rejects with the server's refusal, before a byte is sent", async () => {
    const refusal = () =>
      new Response(JSON.stringify({ error: { code: "files.quota_exceeded", message: "This drive is full." } }), {
        status: 507,
        headers: { "content-type": "application/json" },
      });
    const s = server(refusal, refusal);
    await expect(createNotebook("drive-1", "folder-1", "", { fetchImpl: s.impl, digest })).rejects.toMatchObject({ status: 507 });
    expect(s.calls.filter((c) => c.method === "PUT")).toEqual([]);
  });
});
