// Rename… on a chat's row in Files.
//
// A chat arrives in the drive as a `<Title>.alkerachat` FOLDER whose name was
// minted once from the title and is a filesystem name from then on — the row
// already renders the object's live title. So Rename… on that row must write the
// CHAT's title through the object route and leave the node's name alone;
// renaming the node would edit a string nobody has ever seen and leave the title
// on the row untouched. A plain folder keeps the node rename it has always had.

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { Item } from "@/api/files";
import { createQueryClient } from "@/api/queryClient";
import { RenameInline } from "@/pages/workspace/files/RenameInline";

interface Call {
  readonly method: string;
  readonly path: string;
  readonly body: Record<string, unknown> | null;
}

let calls: Call[];

function script({ putStatus = 200 }: { putStatus?: number } = {}): void {
  calls = [];
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
      calls.push({
        method,
        path: at,
        body: raw ? (JSON.parse(String(raw)) as Record<string, unknown>) : null,
      });
      if (at.startsWith("/api/v1/objects/")) {
        if (method === "PUT" && putStatus !== 200) {
          return json(
            { detail: { code: "version_conflict", message: "This object moved on" } },
            putStatus,
          );
        }
        return json({ id: "obj_chat", type: "chat", title: "Warehouse spike", version: 11, spec: {} });
      }
      return json({ id: "nd_x", name: "renamed", etag: "2" });
    }),
  );
}

const CHAT_ROW = {
  id: "nd_chat",
  driveId: "drv_1",
  kind: "folder",
  name: "3952c9e2-4d6a-4a01-9f53-000000000001.alkerachat",
  nameDisplay: "3952c9e2-4d6a-4a01-9f53-000000000001.alkerachat",
  parentId: "nd_root",
  etag: "1",
  ctag: "c1",
  object: { id: "obj_chat", type: "chat", title: "Warehouse spike" },
  capabilities: { can_read: true, can_write: true, can_rename: true, refusals: {} },
} as unknown as Item;

const PLAIN_FOLDER = {
  id: "nd_papers",
  driveId: "drv_1",
  kind: "folder",
  name: "papers",
  nameDisplay: "papers",
  parentId: "nd_root",
  etag: "1",
  ctag: "c2",
  capabilities: { can_read: true, can_write: true, can_rename: true, refusals: {} },
} as unknown as Item;

function renderRename(item: Item, onDone = vi.fn()): { onDone: ReturnType<typeof vi.fn> } {
  const client = createQueryClient({ retry: false });
  render(
    <QueryClientProvider client={client}>
      <RenameInline driveId="drv_1" item={item} onDone={onDone} />
    </QueryClientProvider>,
  );
  return { onDone };
}

const put = (): Call | undefined => calls.find((call) => call.method === "PUT");
const patch = (): Call | undefined =>
  calls.find((call) => call.method === "PATCH" || call.method === "POST");

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

describe("Rename… on a chat's row in Files", () => {
  it("opens on the chat's title, never on the .alkerachat name", () => {
    script();
    renderRename(CHAT_ROW);
    expect((screen.getByRole("textbox") as HTMLInputElement).value).toBe("Warehouse spike");
    expect(screen.queryByDisplayValue(/alkerachat/)).not.toBeInTheDocument();
  });

  it("Enter writes the CHAT's title through the object route, naming the version it read", async () => {
    script();
    const { onDone } = renderRename(CHAT_ROW);
    const field = screen.getByRole("textbox");
    fireEvent.change(field, { target: { value: "Q3 warehouse spike" } });
    fireEvent.keyDown(field, { key: "Enter" });

    await waitFor(() => expect(put()).toBeDefined());
    expect(put()?.path).toBe("/api/v1/objects/obj_chat");
    expect(put()?.body).toEqual({ title: "Q3 warehouse spike", expected_version: 11 });
    // The node is untouched: no rename went to the Files item route at all.
    expect(calls.some((call) => call.path.includes("/files/drives/"))).toBe(false);
    await waitFor(() => expect(onDone).toHaveBeenCalled());
  });

  it("Escape abandons the edit and writes nothing", () => {
    script();
    const { onDone } = renderRename(CHAT_ROW);
    const field = screen.getByRole("textbox");
    fireEvent.change(field, { target: { value: "Q3 warehouse spike" } });
    fireEvent.keyDown(field, { key: "Escape" });
    expect(put()).toBeUndefined();
    expect(onDone).toHaveBeenCalled();
  });

  it("refuses an empty title in the browser, from the shared rule table", async () => {
    script();
    const { onDone } = renderRename(CHAT_ROW);
    const field = screen.getByRole("textbox");
    fireEvent.change(field, { target: { value: "  " } });
    fireEvent.keyDown(field, { key: "Enter" });

    expect(await screen.findByRole("alert")).toHaveTextContent("Enter a title.");
    expect(put()).toBeUndefined();
    // The editor stays open holding what was typed rather than closing on a
    // rename that never happened.
    expect(onDone).not.toHaveBeenCalled();
  });

  it("says the chat changed underneath the reader when the version conflicts", async () => {
    script({ putStatus: 409 });
    const { onDone } = renderRename(CHAT_ROW);
    const field = screen.getByRole("textbox");
    fireEvent.change(field, { target: { value: "Q3 warehouse spike" } });
    fireEvent.keyDown(field, { key: "Enter" });

    expect(await screen.findByRole("alert")).toHaveTextContent(/changed underneath you/);
    expect(onDone).not.toHaveBeenCalled();
  });

  it("still renames the NODE on a row that is not an object", async () => {
    script();
    renderRename(PLAIN_FOLDER);
    const field = screen.getByRole("textbox");
    expect((field as HTMLInputElement).value).toBe("papers");
    fireEvent.change(field, { target: { value: "drafts" } });
    fireEvent.keyDown(field, { key: "Enter" });

    await waitFor(() => expect(patch()).toBeDefined());
    // A plain folder never reaches the object route.
    expect(calls.some((call) => call.path.startsWith("/api/v1/objects/"))).toBe(false);
  });
});
