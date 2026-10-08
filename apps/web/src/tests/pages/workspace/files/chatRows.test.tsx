import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Item } from "@/api/files";
import { createQueryClient } from "@/api/queryClient";
import { FilesScreen } from "@/pages/workspace/files/FilesPage";
import { displayNameOf, iconHintFor, navIconFor } from "@/lib/files/columns";

// A chat in the tree is a `<Title>.alkerachat` FOLDER. The name was minted from
// whatever the chat was called the day it was made — a uuid when it was untitled —
// and renaming the chat never rewrites it. So the row must render the CHAT: the
// title the server reports today, under the same mark the Chat nav wears. The raw
// `.alkerachat` name is an implementation detail nobody should ever read.

function item(over: Partial<Item> = {}): Item {
  return {
    id: "nd_1",
    ino: 7,
    driveId: "dr_1",
    kind: "file",
    name: "report.csv",
    nameDisplay: "report.csv",
    nameEncoding: "utf-8",
    pathBytes: "/home/report.csv",
    path: "/home/report.csv",
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

/** A chat row that was untitled when it was created, so the folder
 *  carries the chat's uuid, and the chat has been given a name since. */
function chatRow(title: string): Item {
  return item({
    id: "nd_chat",
    kind: "folder",
    name: "3952c9e2-4d6a-4a01-9f53-000000000001.alkerachat",
    nameDisplay: "3952c9e2-4d6a-4a01-9f53-000000000001.alkerachat",
    object: { type: "chat", id: "cht_9", title, web_url: "/chat/cht_9" },
  } as unknown as Partial<Item>);
}

const PLAIN = item({ id: "nd_plain", kind: "folder", name: "papers", nameDisplay: "papers" });

function stubApi(children: Item[]): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = input instanceof Request ? input.url : String(input);
      const answer = (body: unknown) =>
        new Response(JSON.stringify(body), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      if (url.includes("/leases")) return answer([]);
      if (url.includes("/search")) return answer({ value: [], nextMarker: null });
      if (url.includes("/trash")) return answer({ entries: [], nextMarker: null });
      if (/\/files\/drives\/?(\?|$)/.test(url)) {
        return answer({ id: "dr_1", orgId: "or_1", rootId: "nd_root", quotaBytes: 0 });
      }
      if (url.includes("/items/nd_root/children")) return answer({ value: [HOME], nextMarker: null });
      if (url.includes("/children")) return answer({ value: children, nextMarker: null });
      if (url.includes("/permissions")) return answer({ value: [] });
      if (url.includes("/items/")) return answer(HOME);
      return answer({});
    }),
  );
}

function stubViewport(): void {
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
}

function mount(at = "/files/nd_home") {
  return render(
    <QueryClientProvider client={createQueryClient({ retry: false })}>
      <MemoryRouter initialEntries={[at]}>
        <Routes>
          <Route path="/files/:nodeId" element={<FilesScreen />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => stubViewport());
afterEach(() => vi.unstubAllGlobals());

describe("a chat folder in the listing", () => {
  it("reads as the chat's title, never as its `.alkerachat` name", async () => {
    stubApi([chatRow("Warehouse spike"), PLAIN]);
    mount();

    await waitFor(() => expect(screen.getByText("Warehouse spike")).toBeInTheDocument());
    // Nothing anywhere on the page spells the folder name.
    expect(screen.queryByText(/alkerachat/)).toBeNull();
    // A plain folder is untouched: it still reads as its own name.
    expect(screen.getByText("papers")).toBeInTheDocument();
  });

  it("shows the title the chat has NOW, so a rename lands on the next listing", async () => {
    stubApi([chatRow("Warehouse spike")]);
    const first = mount();
    await waitFor(() => expect(screen.getByText("Warehouse spike")).toBeInTheDocument());
    first.unmount();

    // The same node, the same folder name, a renamed chat.
    stubApi([chatRow("Redshift cold starts")]);
    mount();
    await waitFor(() => expect(screen.getByText("Redshift cold starts")).toBeInTheDocument());
    expect(screen.queryByText("Warehouse spike")).toBeNull();
  });
});

describe("how a row derives its name and its mark", () => {
  it("takes an object-backed row's name from the object, not the node", () => {
    expect(displayNameOf(chatRow("Warehouse spike"))).toBe("Warehouse spike");
    // An untitled chat's folder is named after its uuid: it reads as every other
    // surface reads it, never the uuid.
    expect(displayNameOf(chatRow(""))).toBe("Untitled chat");
    expect(displayNameOf(chatRow("   "))).toBe("Untitled chat");
    expect(displayNameOf(PLAIN)).toBe("papers");
  });

  it("gives a chat the Chat nav's own glyph and everything else the file sprite", () => {
    expect(navIconFor(chatRow("Warehouse spike"))).toBe("chats");
    expect(navIconFor(PLAIN)).toBeNull();
    // The facet is read before the kind: a chat FOLDER must not resolve the folder glyph.
    expect(iconHintFor(chatRow("Warehouse spike"))).toBe("chat");
    expect(iconHintFor(PLAIN)).toBe("folder");
  });
});
