// Every way an agent writes a link to a file in its chat folder opens that
// file, and a link that names nothing this shell can open is its label in plain
// words — never a control that answers a click with nothing.
//
// The reply below is verbatim from a staging chat: the agent wrote its query
// result out to `scratch/result-f087b00a.csv` with `blob.materialize` and then
// named the result by its handle. On the web the result itself is on the
// machine that made it, so the transcript showed a chip that did nothing.

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { Prose } from "../../../chat/prose";
import { ReferenceStoreProvider } from "../../../resources";
import { ChatFilesProvider, type ChatFileRef, type ChatFilesResolver } from "./ChatFiles";
import { Markdown } from "./Markdown";
import { chatRelativePath } from "./chatPaths";
import sharedCases from "../../../../../api-core/tests/fixtures/chat_paths/cases.json";

afterEach(cleanup);

interface SharedCases {
  chat_id: string;
  cases: { id: string; target: string; path: string | null }[];
}

// The same table `alkera_core.chat_paths.chat_path` is pinned against.
const SHARED: SharedCases = sharedCases;

describe("the chat path rule, case for case with the box's", () => {
  it.each(SHARED.cases.map((c) => [c.id, c.target, c.path] as const))("%s", (_id, target, path) => {
    expect(chatRelativePath(target, SHARED.chat_id)).toBe(path);
  });

  it("does not take a box path as the chat's without knowing which chat it is", () => {
    const boxPath = `/opt/alkera-work/.alkera/chats/${SHARED.chat_id}/scratch/result-f087b00a.csv`;
    expect(chatRelativePath(boxPath)).toBeNull();
  });
});

const CHAT = SHARED.chat_id;
const HANDLE = "f087b00a98f9897e0e5fa09b733e70f6eb94dbc66fe370bbb57c70197446d653";
const BOX_CSV = `/opt/alkera-work/.alkera/chats/${CHAT}/scratch/result-f087b00a.csv`;
const LABEL = "Active prompts by category/locale";

const CSV_IN_WORKING: ChatFileRef = {
  nodeId: "nd_csv",
  parentId: "nd_scratch",
  name: "result-f087b00a.csv",
  path: "result-f087b00a.csv",
};
const CSV_FROM_CHAT_ROOT: ChatFileRef = { ...CSV_IN_WORKING, path: "scratch/result-f087b00a.csv" };
const SPACED: ChatFileRef = { nodeId: "nd_spaced", parentId: "nd_scratch", name: "Active prompts.csv", path: "Active prompts.csv" };

/** The web's resolver: it looks paths up in the chat folder and reveals what it
 *  finds in the Files pane. */
function webResolver(): ChatFilesResolver {
  const refs: Record<string, ChatFileRef> = {
    "result-f087b00a.csv": CSV_IN_WORKING,
    "scratch/result-f087b00a.csv": CSV_FROM_CHAT_ROOT,
    "Active prompts.csv": SPACED,
  };
  return {
    resolveUrl: vi.fn(async () => null),
    locate: vi.fn(async (path: string) => refs[path] ?? null),
    reveal: vi.fn(),
    openPath: vi.fn(),
  };
}

function reply(link: string): string {
  return ["Full breakdown (80 category × locale pairs):", "", link, "", "Want it split differently?"].join("\n");
}

describe("a reply linking the result file, in every shape the agent writes it", () => {
  it.each([
    ["the result handle it wrote the file from", `[${LABEL}](blob:${HANDLE})`, CSV_FROM_CHAT_ROOT],
    ["the result handle, mid-sentence", `see [${LABEL}](blob:${HANDLE}) for all 80`, CSV_FROM_CHAT_ROOT],
    ["the box's absolute path", `[${LABEL}](${BOX_CSV})`, CSV_FROM_CHAT_ROOT],
    ["the path relative to its working folder", `[${LABEL}](result-f087b00a.csv)`, CSV_IN_WORKING],
    ["a dot-slash path", `[${LABEL}](./result-f087b00a.csv)`, CSV_IN_WORKING],
    ["a percent-escaped name", `[${LABEL}](Active%20prompts.csv)`, SPACED],
  ])("opens the file when it names it by %s", async (_shape, link, ref) => {
    const resolver = webResolver();
    render(
      <ChatFilesProvider resolver={resolver} chatId={CHAT} resultFiles={new Map([[HANDLE, BOX_CSV]])}>
        {/* The web passes its (no-op) resource routing too; the chat file wins. */}
        <Prose content={reply(link)} onResourceOpen={vi.fn()} />
      </ChatFilesProvider>,
    );
    const button = await screen.findByRole("button", { name: LABEL });
    await waitFor(() => expect(button.className).toContain("chat-file-ref--live"));
    fireEvent.click(button);
    expect(resolver.reveal).toHaveBeenCalledWith(ref);
  });
});

describe("a link this shell cannot open", () => {
  it.each([
    ["a result never written to a file", `[${LABEL}](blob:${"b".repeat(64)})`],
    ["another chat's file", `[${LABEL}](/opt/alkera-work/.alkera/chats/0a0b0c0d-1111-4222-8333-444455556666/a.csv)`],
    ["a system path", `[${LABEL}](/etc/passwd)`],
    ["a path climbing out of the chat", `[${LABEL}](../secrets.csv)`],
  ])("is its label in plain words on the web: %s", async (_case, link) => {
    const onResourceOpen = vi.fn();
    const { container } = render(
      <ChatFilesProvider resolver={webResolver()} chatId={CHAT} resultFiles={new Map([[HANDLE, BOX_CSV]])}>
        <Prose content={reply(link)} onResourceOpen={onResourceOpen} />
      </ChatFilesProvider>,
    );
    await waitFor(() => expect(screen.getByText(LABEL)).toBeInTheDocument());
    expect(screen.queryByRole("button", { name: new RegExp(LABEL) })).toBeNull();
    expect(container.querySelector("button")).toBeNull();
    fireEvent.click(screen.getByText(LABEL));
    expect(onResourceOpen).not.toHaveBeenCalled();
  });

  it("a result handle with no file and no way to open it is not the chip that looks like it opens", () => {
    const { container } = render(<Prose content={reply(`[${LABEL}](blob:${HANDLE})`)} />);
    expect(screen.getByText(LABEL)).toBeInTheDocument();
    expect(container.querySelector("button")).toBeNull();
  });

  it("a file link with no resolver and no routing is plain words, not a button", () => {
    const { container } = render(<Markdown content="[the report](report.csv)" />);
    expect(screen.getByText("the report")).toBeInTheDocument();
    expect(container.querySelector("button")).toBeNull();
  });
});

describe("the editor keeps what it can open", () => {
  it("a result it holds is still the rich card that opens it", () => {
    const onOpen = vi.fn();
    render(
      <ReferenceStoreProvider actions={{ onOpen }}>
        <Prose content={reply(`[${LABEL}](blob:${HANDLE})`)} />
      </ReferenceStoreProvider>,
    );
    fireEvent.click(screen.getByRole("button", { name: `Open ${LABEL} in the editor` }));
    expect(onOpen).toHaveBeenCalledWith({ handle: HANDLE, name: LABEL });
  });

  it("a path outside the chat still routes to the editor, which can open workspace files", () => {
    const onResourceOpen = vi.fn();
    const editor: ChatFilesResolver = { resolveUrl: vi.fn(async () => null), openPath: vi.fn() };
    render(
      <ChatFilesProvider resolver={editor} chatId={CHAT}>
        <Markdown content="[the model](/home/u/proj/models/orders.sql)" onResourceOpen={onResourceOpen} />
      </ChatFilesProvider>,
    );
    fireEvent.click(screen.getByRole("button", { name: "the model" }));
    expect(onResourceOpen).toHaveBeenCalledWith(expect.objectContaining({ path: "/home/u/proj/models/orders.sql" }));
  });

  it("a box path into this chat opens through the editor's own door as the chat-relative path", () => {
    const editor = { resolveUrl: vi.fn(async () => null), openPath: vi.fn() };
    render(
      <ChatFilesProvider resolver={editor} chatId={CHAT}>
        <Markdown content={`[${LABEL}](${BOX_CSV})`} onResourceOpen={vi.fn()} />
      </ChatFilesProvider>,
    );
    fireEvent.click(screen.getByRole("button", { name: LABEL }));
    expect(editor.openPath).toHaveBeenCalledWith("scratch/result-f087b00a.csv");
  });
});

describe("an image the agent names by the box's absolute path", () => {
  it("renders through the resolver as the chat-relative path when it is this chat's", async () => {
    const resolver: ChatFilesResolver = { resolveUrl: vi.fn(async () => "blob:bytes") };
    render(
      <ChatFilesProvider resolver={resolver} chatId={CHAT}>
        <Prose content={`![The chart](/opt/alkera-work/.alkera/chats/${CHAT}/scratch/chart.png)`} />
      </ChatFilesProvider>,
    );
    await waitFor(() => expect(screen.getByRole("img", { name: "The chart" }).tagName).toBe("IMG"));
    expect(resolver.resolveUrl).toHaveBeenCalledWith("scratch/chart.png");
  });

  it("asks for nothing when it is another chat's", async () => {
    const resolver: ChatFilesResolver = { resolveUrl: vi.fn(async () => "blob:bytes") };
    render(
      <ChatFilesProvider resolver={resolver} chatId={CHAT}>
        <Prose content="![The chart](/opt/alkera-work/.alkera/chats/other/scratch/chart.png)" />
      </ChatFilesProvider>,
    );
    expect(screen.getByRole("img", { name: "The chart" }).tagName).not.toBe("IMG");
    expect(resolver.resolveUrl).not.toHaveBeenCalled();
  });
});
