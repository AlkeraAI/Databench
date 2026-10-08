// Rename… on a chat template's row in Files.
//
// A template's title is the TEMPLATE's, not the folder's, so the rename goes to
// `PUT /chat-templates/{id}` — and that route is fenced on the version the
// caller read. A Files row carries the title and never the version, so the
// version has to be read before the write: a guessed one is a 409 the reader did
// nothing to deserve, and after the first successful rename it is wrong for ever
// (the template is at 2, the guess is still 1).

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { Item } from "@/api/files";
import { createQueryClient } from "@/api/queryClient";
import { RenameInline } from "@/pages/workspace/files/RenameInline";

const TEMPLATE_ID = "tpl_4";

interface Call {
  readonly method: string;
  readonly path: string;
  readonly body: Record<string, unknown> | null;
}

let calls: Call[];

/** The template as the server holds it. `version` is the row's live version —
 *  the whole point of the fence — and it moves on every accepted write. */
function script({ version = 7, putStatus = 200 }: { version?: number; putStatus?: number } = {}): {
  version: () => number;
} {
  calls = [];
  let live = version;
  let title = "Warehouse audit";
  const json = (payload: unknown, code = 200): Response =>
    new Response(JSON.stringify(payload), {
      status: code,
      headers: { "content-type": "application/json" },
    });
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = input instanceof Request ? input.url : String(input);
      const at = new URL(url, "http://x").pathname;
      const method = (input instanceof Request ? input.method : init?.method) ?? "GET";
      const raw = init?.body;
      const body = raw ? (JSON.parse(String(raw)) as Record<string, unknown>) : null;
      calls.push({ method, path: at, body });
      const row = () => ({
        id: TEMPLATE_ID,
        title,
        version: live,
        owner_user_id: "usr_1",
        created_at: "2026-09-01T00:00:00Z",
        updated_at: "2026-09-02T00:00:00Z",
        brief: "Audit the warehouse.",
        permission_mode: "read_only",
        saved_from_seq: 3,
      });
      if (at === `/api/v1/chat-templates/${TEMPLATE_ID}` && method === "PUT") {
        if (putStatus !== 200) {
          return json({ code: "version_conflict", message: "This template changed" }, putStatus);
        }
        // The server's own fence: a write that names the wrong version is refused.
        if (body?.["expected_version"] !== live) {
          return json({ code: "version_conflict", message: "This template changed" }, 409);
        }
        live += 1;
        title = String(body?.["title"] ?? title);
        return json(row());
      }
      if (at === `/api/v1/chat-templates/${TEMPLATE_ID}`) return json(row());
      return json({ id: "nd_x", name: "renamed", etag: "2" });
    }),
  );
  return { version: () => live };
}

const TEMPLATE_ROW = {
  id: "nd_tpl",
  driveId: "drv_1",
  kind: "folder",
  name: "Warehouse audit.alkerachat.template",
  nameDisplay: "Warehouse audit.alkerachat.template",
  parentId: "nd_root",
  etag: "1",
  ctag: "c1",
  object: {
    id: TEMPLATE_ID,
    type: "chat_template",
    title: "Warehouse audit",
    web_url: `/templates/${TEMPLATE_ID}`,
  },
  capabilities: { can_read: true, can_write: true, can_rename: true, refusals: {} },
} as unknown as Item;

function renderRename(onDone = vi.fn()): { onDone: ReturnType<typeof vi.fn> } {
  render(
    <QueryClientProvider client={createQueryClient({ retry: false })}>
      <RenameInline driveId="drv_1" item={TEMPLATE_ROW} onDone={onDone} />
    </QueryClientProvider>,
  );
  return { onDone };
}

function type(value: string): void {
  const field = screen.getByRole("textbox");
  fireEvent.change(field, { target: { value } });
  fireEvent.keyDown(field, { key: "Enter" });
}

const puts = (): Call[] => calls.filter((call) => call.method === "PUT");

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

describe("Rename… on a chat template's row in Files", () => {
  it("fences the write on the template's LIVE version, not on a guess", async () => {
    const server = script({ version: 7 });
    const { onDone } = renderRename();

    type("Warehouse audit Q3");

    await waitFor(() => expect(puts()).toHaveLength(1));
    expect(puts()[0]?.path).toBe(`/api/v1/chat-templates/${TEMPLATE_ID}`);
    expect(puts()[0]?.body).toEqual({ title: "Warehouse audit Q3", expected_version: 7 });
    expect(server.version()).toBe(8);
    await waitFor(() => expect(onDone).toHaveBeenCalled());
  });

  it("renames twice in a row, because the second write reads the version the first produced", async () => {
    script({ version: 1 });
    const first = renderRename();
    type("Warehouse audit Q3");
    await waitFor(() => expect(first.onDone).toHaveBeenCalled());
    cleanup();

    const second = renderRename();
    type("Warehouse audit Q4");

    await waitFor(() => expect(puts()).toHaveLength(2));
    // The bug this pins: a second rename that re-sends the first write's version
    // is refused for ever, and re-reading the row changes nothing.
    expect(puts()[1]?.body).toEqual({ title: "Warehouse audit Q4", expected_version: 2 });
    expect(screen.queryByRole("alert")).toBeNull();
    await waitFor(() => expect(second.onDone).toHaveBeenCalled());
  });

  it("says the template moved on when the fenced write is refused", async () => {
    script({ putStatus: 409 });
    const { onDone } = renderRename();

    type("Warehouse audit Q3");

    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(onDone).not.toHaveBeenCalled();
  });

  it("never renames the node behind the template", async () => {
    script();
    renderRename();

    type("Warehouse audit Q3");

    await waitFor(() => expect(puts()).toHaveLength(1));
    expect(calls.some((call) => call.path.includes("/files/drives/"))).toBe(false);
    expect(calls.some((call) => call.path.startsWith("/api/v1/objects/"))).toBe(false);
  });
});
