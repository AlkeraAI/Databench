/**
 * New notebook on the top-level Files page, and opening one there.
 *
 * The notebook editor lives only in a chat's workspace pane, so the server
 * names the chat that would run a notebook in the listed folder. Where it
 * names one, the bar offers New notebook beside New folder: it uploads the
 * notebook into the folder being listed, through the same create the chat's
 * Files pane uses, and opens it in that chat's editor. A refused upload keeps
 * the field open holding what was typed. Where it names none (no workspace or
 * chat folder, so no kernel), New notebook is not offered. Opening a notebook
 * row goes to its editor the same way, and one no kernel reaches is previewed.
 */

import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Item } from "@/api/files";
import { createQueryClient } from "@/api/queryClient";
import { FilesScreen } from "@/pages/workspace/files/FilesPage";

const DRIVE = "dr_1";

function item(over: Partial<Item> = {}): Item {
  return {
    id: "nd_1",
    ino: 7,
    driveId: DRIVE,
    kind: "file",
    name: "report.csv",
    nameDisplay: "report.csv",
    nameEncoding: "utf-8",
    pathBytes: "/home/dana/report.csv",
    path: "/home/dana/report.csv",
    etag: "et_1",
    ctag: "ct_1",
    stale: false,
    locked: false,
    held: false,
    shared: false,
    trashed: false,
    capabilities: { can_read: true, can_write: true, can_share: true, can_delete: true },
    ...over,
  } as Item;
}

const HOME = item({ id: "nd_home", kind: "folder", name: "home", nameDisplay: "home" });

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

// jsdom's Blob reads neither as text nor as bytes; the upload reads its part.
if (typeof Blob.prototype.arrayBuffer !== "function") {
  Blob.prototype.arrayBuffer = function readSlice(this: Blob): Promise<ArrayBuffer> {
    return new Promise((done, fail) => {
      const reader = new FileReader();
      reader.onload = () => done(reader.result as ArrayBuffer);
      reader.onerror = () => fail(reader.error);
      reader.readAsArrayBuffer(this);
    });
  };
}

interface Seen {
  opened: Record<string, unknown>[];
  completed: number;
}

const CHAT = "33333333-3333-3333-3333-333333333333";
const NOTEBOOK_ROW = item({ id: "nd_nb", name: "demo3.alknb.py", nameDisplay: "demo3.alknb.py", parentId: "nd_home" });

interface Stub {
  refuseUpload?: boolean;
  /** The chat the server names for the editor, or `null` for none. */
  editorChat?: string | null;
  rows?: Item[];
}

function stubApi({ refuseUpload = false, editorChat = CHAT, rows = [item()] }: Stub = {}): Seen {
  const seen: Seen = { opened: [], completed: 0 };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const request = input instanceof Request ? input : null;
      const url = request ? request.url : String(input);
      const method = request?.method ?? init?.method ?? "GET";
      if (method === "POST" && url.endsWith("/api/v1/files/uploads")) {
        if (refuseUpload) {
          return json({ error: { code: "files.forbidden", message: "You can view this folder, not add to it." } }, 403);
        }
        seen.opened.push(JSON.parse(String(init?.body)) as Record<string, unknown>);
        return json(
          { uploadId: "u1", partSize: 1 << 20, partsTotal: 1, limits: { maxPartBytes: 1 << 20, maxParts: 4 }, expiresAt: "2026-12-01T00:00:00Z" },
          201,
        );
      }
      if (url.endsWith("/api/v1/files/uploads/u1")) {
        return json({ uploadId: "u1", state: "open", offset: 0, length: 0, complete: false, partsDone: 0, partsTotal: 1, acceptedParts: [] });
      }
      if (method === "PUT" && url.includes("/uploads/u1/parts/")) return json({ partNo: 1, size: 1, duplicate: false });
      if (url.endsWith("/uploads/u1/complete")) {
        seen.completed += 1;
        return json({ id: "op1", kind: "upload", state: "done", done: 1, total: 1, resultNodeId: "nd_new_nb" }, 202);
      }
      if (url.includes("/api/v1/notebooks/") && url.endsWith("/editor")) return json({ chat_id: editorChat });
      if (url.includes("/leases")) return json([]);
      if (url.includes("/permissions")) return json({ value: [] });
      if (url.includes("/versions")) return json({ value: [] });
      if (url.includes("/search")) return json({ value: [], nextMarker: null });
      if (url.includes("/trash")) return json({ entries: [], nextMarker: null });
      if (/\/files\/drives\/?(\?|$)/.test(url)) {
        return json({ id: DRIVE, orgId: "or_1", rootId: "nd_root", quotaBytes: 0 });
      }
      if (url.includes("/items/nd_root/children")) return json({ value: [HOME], nextMarker: null });
      if (url.includes("/children")) return json({ value: rows, nextMarker: null });
      if (url.includes("/items/")) return json(HOME);
      return json({});
    }),
  );
  return seen;
}

beforeEach(() => {
  vi.stubGlobal(
    "matchMedia",
    (query: string): MediaQueryList =>
      ({
        media: query,
        matches: false,
        onchange: null,
        addEventListener: () => undefined,
        removeEventListener: () => undefined,
        addListener: () => undefined,
        removeListener: () => undefined,
        dispatchEvent: () => false,
      }) as unknown as MediaQueryList,
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
});

function Where() {
  const location = useLocation();
  return <p data-testid="chat-page">{`${location.pathname}${location.search}`}</p>;
}

function renderFiles(): void {
  render(
    <QueryClientProvider client={createQueryClient({ retry: false })}>
      <MemoryRouter initialEntries={["/files/nd_home"]}>
        <Routes>
          <Route path="/files/:nodeId" element={<FilesScreen />} />
          <Route path="/chat/:chatId" element={<Where />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

async function openNewNotebook(): Promise<HTMLInputElement> {
  renderFiles();
  const creates = await screen.findByRole("group", { name: "Create" });
  await userEvent.click(await within(creates).findByRole("button", { name: "New notebook" }));
  return (await screen.findByRole("textbox", { name: "Notebook name" })) as HTMLInputElement;
}

describe("New notebook on the Files page", () => {
  it("creates the notebook in the folder being listed and opens it in its chat's editor", async () => {
    const seen = stubApi();
    const field = await openNewNotebook();
    expect(field).toHaveValue("Untitled");
    await userEvent.clear(field);
    await userEvent.type(field, "Revenue{Enter}");

    expect(await screen.findByTestId("chat-page")).toHaveTextContent(`/chat/${CHAT}?open=nd_new_nb`);
    expect(seen.opened).toEqual([expect.objectContaining({ name: "Revenue.alknb.py", parentId: "nd_home" })]);
    expect(seen.completed).toBe(1);
  });

  it("is not offered in a folder no kernel can reach", async () => {
    stubApi({ editorChat: null });
    renderFiles();
    const creates = await screen.findByRole("group", { name: "Create" });
    expect(within(creates).getByRole("button", { name: "New folder" })).toBeInTheDocument();
    // The editor's answer has landed: still nothing to offer.
    await waitFor(() =>
      expect(vi.mocked(fetch).mock.calls.some(([input]) => String(input instanceof Request ? input.url : input).endsWith("/editor"))).toBe(true),
    );
    expect(within(creates).queryByRole("button", { name: "New notebook" })).toBeNull();
  });

  it("keeps the field open with what was typed when the upload is refused", async () => {
    const seen = stubApi({ refuseUpload: true });
    const field = await openNewNotebook();
    await userEvent.clear(field);
    await userEvent.type(field, "Revenue{Enter}");

    const form = await screen.findByRole("form", { name: "New notebook" });
    expect(await within(form).findByRole("alert")).toBeInTheDocument();
    expect(within(form).getByRole("textbox", { name: "Notebook name" })).toHaveValue("Revenue");
    expect(seen.completed).toBe(0);
  });
});

describe("Opening a notebook on the Files page", () => {
  it("opens it in the editor of the chat the server names", async () => {
    stubApi({ rows: [NOTEBOOK_ROW] });
    renderFiles();
    await userEvent.dblClick(await screen.findByText("demo3.alknb.py"));

    expect(await screen.findByTestId("chat-page")).toHaveTextContent(`/chat/${CHAT}?open=nd_nb`);
  });

  it("previews it where no kernel can reach it", async () => {
    stubApi({ rows: [NOTEBOOK_ROW], editorChat: null });
    renderFiles();
    await userEvent.dblClick(await screen.findByText("demo3.alknb.py"));

    expect(await screen.findByRole("dialog")).toBeInTheDocument();
    expect(screen.queryByTestId("chat-page")).toBeNull();
  });
});
