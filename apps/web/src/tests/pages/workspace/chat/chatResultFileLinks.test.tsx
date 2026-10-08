// A result the agent wrote out to a file and then named by its handle opens that
// file in the browser, through the real chat-files port walking the chat's
// folder in Files.
//
// Verbatim from a real chat: the agent ran `blob.materialize` on its query
// result (the tool answered with the box's absolute path), then ended its reply
// with `[Active prompts by category/locale](blob:<handle>)` on its own line. The
// result itself stayed on the machine that made it, so the web drew a chip that
// did nothing; the CSV sat in the Files pane beside it.

import { QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { ConversationTurn, ToolConversationPart } from "@alkera/chat-model";
import { ChatFilesProvider, Prose, type ChatFileRef } from "@alkera/ui";

import { createQueryClient } from "@/api/queryClient";
import { materializedResults } from "@/pages/workspace/chat/blobModel";
import { useChatFilesWiring } from "@/pages/workspace/chat/chatFilesWiring";
import { installChatRuntime, resetChatRuntime, type ChatDataSource, type ChatHost } from "@/pages/workspace/chat/data";
import { cloudChatFiles } from "@/pages/workspace/chat/data/chatFiles";

const CHAT = "d6d800e9-5bce-4d9f-b0e0-dd5367ea4298";
const ROOT = "chat-node";
const DRIVE = "drive-1";
const HANDLE = "f087b00a98f9897e0e5fa09b733e70f6eb94dbc66fe370bbb57c70197446d653";
const BOX_PATH = `/opt/alkera-work/.alkera/chats/${CHAT}/scratch/result-f087b00a.csv`;
const LABEL = "Active prompts by category/locale";
const REPLY = [
  "Full breakdown (80 category × locale pairs):",
  "",
  `[${LABEL}](blob:${HANDLE})`,
  "",
  "Want it split differently — e.g. one bar per category, or filtered to a single customer?",
].join("\n");

type Row = { id: string; kind: "file" | "folder"; name: string; driveId: string; etag: string };
const row = (id: string, kind: Row["kind"], name: string): Row => ({ id, kind, name, driveId: DRIVE, etag: "v1" });

/** The chat folder as Files holds it: the working folder `scratch/` with the
 *  materialized CSV, the chart, and the bash tool's spill folder. */
const TREE: Record<string, Row[]> = {
  [ROOT]: [row("f-work", "folder", "scratch")],
  "f-work": [
    row("n-png", "file", "active-prompts-by-category-locale.png"),
    row("n-csv", "file", "result-f087b00a.csv"),
    row("f-tool", "folder", "tool-output"),
  ],
};

function serveFiles(): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string | URL | Request) => {
      const href = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
      const url = new URL(href);
      const json = (body: unknown) =>
        new Response(JSON.stringify(body), { status: 200, headers: { "content-type": "application/json" } });
      if (url.pathname === `/api/v1/chats/${CHAT}`) return json({ id: CHAT, title: "t", files_node_id: ROOT });
      if (url.pathname === "/api/v1/files/drives") return json({ id: DRIVE });
      const children = /\/items\/([^/]+)\/children$/.exec(url.pathname);
      if (children) return json({ value: TREE[children[1]] ?? [], nextMarker: null });
      return new Response("{}", { status: 404 });
    }),
  );
}

/** The `blob.materialize` call as the transcript carries it: inside the MCP
 *  `call_tool` envelope, its result a JSON string. */
function materialize(state: ToolConversationPart["state"], handle = HANDLE, path = BOX_PATH): ToolConversationPart {
  return {
    id: `tool-${handle.slice(0, 4)}-${state}`,
    kind: "tool",
    callId: `call-${handle.slice(0, 4)}`,
    name: "alkera_call_tool",
    state,
    input: { name: "blob.materialize", args: { path: "active_prompts_by_cat_locale.csv", format: "csv", handle } },
    output:
      state === "completed"
        ? JSON.stringify({ result: { path, format: "csv", row_count: 80, char_count: 0, columns: ["category"], bytes: 1905 } })
        : null,
  };
}

const turn = (parts: ConversationTurn["parts"]): ConversationTurn => ({ id: "t1", author: "assistant", parts });

afterEach(() => {
  cleanup();
  resetChatRuntime();
  vi.unstubAllGlobals();
});

describe("materializedResults", () => {
  it("maps each result the agent wrote out to the path the tool reported", () => {
    expect(materializedResults([turn([materialize("completed")])])).toEqual(new Map([[HANDLE, BOX_PATH]]));
  });

  it("keeps the latest file when a result is written out twice", () => {
    const later = `/opt/alkera-work/.alkera/chats/${CHAT}/scratch/again.csv`;
    const files = materializedResults([turn([materialize("completed")]), turn([materialize("completed", HANDLE, later)])]);
    expect(files.get(HANDLE)).toBe(later);
  });

  it.each([
    ["a call still running", materialize("running")],
    ["a call that failed", { ...materialize("completed"), state: "error" as const }],
    ["another tool", { ...materialize("completed"), input: { name: "blob.info", args: { handle: HANDLE } } }],
    ["a result with no path", { ...materialize("completed"), output: JSON.stringify({ result: { format: "csv" } }) }],
  ])("names no file for %s", (_case, part) => {
    expect(materializedResults([turn([part])]).size).toBe(0);
  });
});

function Transcript({ turns, reveal, children }: { turns: ConversationTurn[]; reveal: (ref: ChatFileRef) => void; children: ReactNode }) {
  const { resolver } = useChatFilesWiring(CHAT, undefined, { revealItem: reveal });
  return (
    <ChatFilesProvider resolver={resolver} chatId={CHAT} resultFiles={materializedResults(turns)}>
      {children}
    </ChatFilesProvider>
  );
}

function renderReply(turns: ConversationTurn[], reveal: (ref: ChatFileRef) => void) {
  serveFiles();
  installChatRuntime({
    source: { chatFiles: cloudChatFiles() } as unknown as ChatDataSource,
    host: {} as unknown as ChatHost,
  });
  return render(
    <QueryClientProvider client={createQueryClient({ retry: false })}>
      <MemoryRouter>
        <Transcript turns={turns} reveal={reveal}>
          <Prose content={REPLY} onResourceOpen={vi.fn()} />
        </Transcript>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("the reply's result link on the web", () => {
  it("opens the CSV the result was written out to, in the Files pane", async () => {
    const reveal = vi.fn();
    renderReply([turn([materialize("completed")])], reveal);

    const link = await screen.findByRole("button", { name: LABEL });
    await waitFor(() => expect(link.getAttribute("title")).toBe("result-f087b00a.csv (in scratch)"));
    fireEvent.click(link);
    expect(reveal).toHaveBeenCalledWith({
      nodeId: "n-csv",
      parentId: "f-work",
      name: "result-f087b00a.csv",
      path: "scratch/result-f087b00a.csv",
    });
  });

  it("is its label, not a chip, when the result was never written to a file", async () => {
    const { container } = renderReply([turn([])], vi.fn());
    expect(screen.getByText(LABEL)).toBeInTheDocument();
    expect(container.querySelector("button")).toBeNull();
  });
});
