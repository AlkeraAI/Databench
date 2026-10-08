// A person's pasted image and an agent's chart render the same way: each is a
// path relative to the chat's working directory, each is found back through
// the browser's real chat-files port and bought as the version the drive holds
// now, and each is shown in the one fixed-size frame — neither message carries
// a size, and nothing on the element sets one.

import { QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ChatFilesProvider, Markdown, OrderTicket } from "@alkera/ui";

import { createQueryClient } from "@/api/queryClient";
import { useChatFilesWiring } from "@/pages/workspace/chat/chatFilesWiring";
import { installChatRuntime, resetChatRuntime, type ChatDataSource, type ChatHost } from "@/pages/workspace/chat/data";
import { cloudChatFiles } from "@/pages/workspace/chat/data/chatFiles";

const CHAT = "11111111-1111-1111-1111-111111111111";
const ROOT = "chat-node";
const DRIVE = "drive-1";
const CONTENT = "http://files.localhost:8000";

type Row = { id: string; kind: "file" | "folder"; name: string; driveId: string; etag: string };
const row = (id: string, kind: Row["kind"], name: string): Row => ({ id, kind, name, driveId: DRIVE, etag: "v1" });

/** The chat folder as Files holds it: the working directory, the person's
 *  upload under `uploads/`, the agent's chart under `charts/`. */
const TREE: Record<string, Row[]> = {
  [ROOT]: [row("f-work", "folder", "scratch")],
  "f-work": [row("f-up", "folder", "uploads"), row("f-charts", "folder", "charts")],
  "f-up": [row("n-paste", "file", "paste-1-ab12.png")],
  "f-charts": [row("n-chart", "file", "q3.svg")],
};

function serveFiles(): { minted: string[] } {
  const minted: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string | URL | Request) => {
      const href = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
      if (href.startsWith(`${CONTENT}/c/`)) return new Response("bytes", { status: 200 });
      const url = new URL(href);
      const json = (body: unknown) => new Response(JSON.stringify(body), { status: 200, headers: { "content-type": "application/json" } });
      if (url.pathname === `/api/v1/chats/${CHAT}`) return json({ id: CHAT, title: "t", files_node_id: ROOT });
      if (url.pathname === "/api/v1/files/drives") return json({ id: DRIVE });
      const grant = /\/items\/([^/]+)\/content-grants$/.exec(url.pathname);
      if (grant) {
        minted.push(grant[1]);
        return json({ url: `${CONTENT}/c/${grant[1]}`, expiresAt: "2026-09-25T12:00:00Z", kind: "file", etag: "v1" });
      }
      const children = /\/items\/([^/]+)\/children$/.exec(url.pathname);
      if (children) return json({ value: TREE[children[1]] ?? [], nextMarker: null });
      return new Response("{}", { status: 404 });
    }),
  );
  return { minted };
}

let objectUrls = 0;
const realCreate = URL.createObjectURL;
const realRevoke = URL.revokeObjectURL;

afterEach(() => {
  cleanup();
  resetChatRuntime();
  vi.unstubAllGlobals();
  URL.createObjectURL = realCreate;
  URL.revokeObjectURL = realRevoke;
});

function Transcript({ children }: { children: ReactNode }) {
  const { resolver } = useChatFilesWiring(CHAT);
  return <ChatFilesProvider resolver={resolver}>{children}</ChatFilesProvider>;
}

describe("images in a chat", () => {
  it("renders the person's upload and the agent's chart through the resolver, in the fixed-size frame", async () => {
    URL.createObjectURL = () => `blob:image-${(objectUrls += 1)}`;
    URL.revokeObjectURL = () => undefined;
    const { minted } = serveFiles();
    installChatRuntime({
      source: { chatFiles: cloudChatFiles() } as unknown as ChatDataSource,
      host: {} as unknown as ChatHost,
    });

    render(
      <QueryClientProvider client={createQueryClient({ retry: false })}>
        <MemoryRouter>
          <Transcript>
            <OrderTicket text={"here is the export\n![Image 1](uploads/paste-1-ab12.png)"} at="now" />
            <Markdown content={"The quarter:\n\n![chart](charts/q3.svg)"} />
          </Transcript>
        </MemoryRouter>
      </QueryClientProvider>,
    );

    const upload = await waitFor(() => {
      const img = screen.getByRole("img", { name: "Image 1" });
      expect(img.tagName).toBe("IMG");
      return img as HTMLImageElement;
    });
    const chart = await waitFor(() => {
      const img = screen.getByRole("img", { name: "chart" });
      expect(img.tagName).toBe("IMG");
      return img as HTMLImageElement;
    });

    // Each was bought as the node the path names — the upload one folder down
    // under uploads/, the chart under charts/ — never by a URL in the message.
    expect([...minted].sort()).toEqual(["n-chart", "n-paste"]);
    for (const img of [upload, chart]) {
      expect(img.getAttribute("src")).toMatch(/^blob:image-/);
      expect(img.className).toBe("alk-chat-image__img");
      expect(img.closest("figure")?.className).toBe("alk-chat-image");
      // The frame decides the size; nothing on the element does.
      expect(img.hasAttribute("width")).toBe(false);
      expect(img.hasAttribute("height")).toBe(false);
      expect(img.getAttribute("style")).toBeNull();
    }
  });
});
