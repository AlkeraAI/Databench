// The chat header's Share key, for a chat that sits in a workspace: a chat in a
// workspace that holds several chats is shared by sharing the workspace (its
// agent works in the workspace's whole shared tree), and a chat that is its own
// workspace is shared as the chat, as it always was.

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { createQueryClient } from "@/api/queryClient";
import { useChatShare } from "@/pages/workspace/chat/useChatShare";
import { SHARES_CONNECTIONS_NOTE } from "@/pages/workspace/files/ShareDialog";

const W = "11111111-1111-4111-8111-111111111111";

function stub(layout: "native" | "adopted" | "missing"): string[] {
  const paths: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = new URL(input instanceof Request ? input.url : String(input), "http://x");
      paths.push(url.pathname);
      const json = (body: unknown, status = 200) =>
        new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
      if (url.pathname === "/api/v1/chats/c1") {
        return json({
          id: "c1",
          title: "Revenue model",
          owner_user_id: "u1",
          machine_id: null,
          machine_status: "none",
          created_at: "2026-10-04T00:00:00Z",
          updated_at: "2026-10-04T00:00:00Z",
          last_seq: 0,
          files_node_id: "node-chat",
          workspace_id: W,
        });
      }
      if (url.pathname === `/api/v1/workspaces/${W}`) {
        if (layout === "missing") return json({ detail: "Not found" }, 404);
        return json({
          id: W,
          title: "Q4 forecast",
          kind: "project",
          layout,
          owner_user_id: "u1",
          version: 1,
          created_at: "2026-10-04T00:00:00Z",
          updated_at: "2026-10-04T00:00:00Z",
          files_node_id: layout === "native" ? "node-ws" : "node-chat",
          machine_status: "none",
        });
      }
      if (url.pathname === "/api/v1/files/drives") return json({ id: "drv", orgId: "o", rootId: "r", quotaBytes: 1 });
      if (url.pathname.endsWith("/permissions")) return json({ value: [] });
      if (url.pathname.includes("/items/")) {
        return json({
          id: url.pathname.split("/").pop(),
          name: "folder",
          capabilities: { can_read: true, can_write: true, can_share: true, refusals: {} },
        });
      }
      return json({});
    }),
  );
  return paths;
}

function Harness() {
  const share = useChatShare("c1");
  return (
    <>
      <button type="button" disabled={!share.onShare} onClick={share.onShare}>
        Share
      </button>
      {share.dialog}
    </>
  );
}

function renderHarness() {
  render(
    <QueryClientProvider client={createQueryClient({ retry: false })}>
      <MemoryRouter>
        <Harness />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("the header's Share key on a chat in a workspace", () => {
  it.each([
    { layout: "native" as const, node: "node-ws", other: "node-chat", name: "Q4 forecast", why: "several chats: the workspace" },
    { layout: "adopted" as const, node: "node-chat", other: "node-ws", name: "Revenue model", why: "a workspace of one: the chat" },
    { layout: "missing" as const, node: "node-chat", other: "node-ws", name: "Revenue model", why: "a workspace this reader cannot read: the chat" },
  ])("shares $why", async ({ layout, node, other, name }) => {
    const paths = stub(layout);
    renderHarness();
    const key = screen.getByRole("button", { name: "Share" });
    await waitFor(() => expect(key).not.toBeDisabled());
    fireEvent.click(key);
    const dialog = await screen.findByRole("dialog");
    expect(dialog.textContent).toContain(name);
    // A chat or workspace runs on its owner's connections for whoever it is shared with.
    await waitFor(() => expect(dialog.textContent).toContain(SHARES_CONNECTIONS_NOTE));
    await waitFor(() => expect(paths.some((p) => p.endsWith(`/items/${node}/permissions`))).toBe(true));
    expect(paths.some((p) => p.includes(`/items/${other}`))).toBe(false);
  });
});
