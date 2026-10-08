import { QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Item } from "@/api/files";
import { createQueryClient } from "@/api/queryClient";
import { FilesActions } from "@/pages/workspace/files/FilesActions";
import { RightPane, briefOf, describeSharing, storeKeyOf } from "@/pages/workspace/files/RightPane";

// The pane against a stubbed drive: the two reads behind it (effective permissions
// and versions) come back over `fetch`, so what is asserted is the sentence the pane
// painted from the wire, and — for the sharing summary — that the request really
// asked for the *effective* grants, since an ancestor's grant is the whole point.
// The versions surface itself is pinned in `fileVersions.test.tsx`.

const DRIVE = "drv_1";

const FILE: Item = {
  id: "nd_a",
  ino: 41_005,
  driveId: DRIVE,
  kind: "file",
  name: "quarterly.csv",
  nameDisplay: "quarterly.csv",
  nameEncoding: "utf-8",
  pathBytes: "",
  parentId: "nd_parent",
  path: "/home/dana/quarterly.csv",
  etag: "e1",
  ctag: "c1",
  attrs: {
    owner: "dana",
    mtime: "2026-03-04T10:00:00Z",
    birthtime: "2026-01-09T08:00:00Z",
    metadata: { modifiedBy: "morgan" },
  },
  file: { size: 2_048, content_hash: "sha256:abc123", store_key: "blobs/ab/abc123" },
  ownerName: "Dana Okafor",
  stale: false,
  locked: false,
  held: false,
  shared: true,
  trashed: false,
} as unknown as Item;

let calls: string[] = [];

function stubReads(): void {
  calls = [];
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL) => {
      const raw =
        typeof input === "string" ? input : input instanceof Request ? input.url : input.toString();
      calls.push(raw);
      const url = new URL(raw, "http://localhost");
      // The share dialog the pane opens reads the org's people and teams; both
      // answer lists, and a non-list would crash the picker rather than fail a
      // pin.
      const listed = url.pathname.endsWith("/org/members") || url.pathname.endsWith("/teams");
      const grants = {
        value: [
          {
            principal: { kind: "user", id: "dana" },
            principalName: "Dana Okafor",
            role: "owner",
            roleLabel: "Owner",
            origin: "direct",
            grantingNodeId: FILE.id,
          },
          {
            principal: { kind: "team", id: "analytics" },
            principalName: "Analytics",
            role: "reader",
            roleLabel: "Can view",
            origin: "inherited",
            grantingNodeId: "nd_parent",
          },
        ],
      };
      const body = listed
        ? []
        : url.pathname.endsWith("/permissions")
          ? grants
          : { versions: [{ id: "v1" }, { id: "v2" }, { id: "v3" }] };
      return Promise.resolve(
        new Response(JSON.stringify(body), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
      );
    }),
  );
}

function mount(props: Partial<React.ComponentProps<typeof RightPane>> = {}) {
  const client = createQueryClient({ retry: false });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <RightPane driveId={DRIVE} item={FILE} {...props} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

/** The value cell next to a field label. */
function fieldValue(label: string): string {
  const term = screen.getByText(label);
  const value = term.nextElementSibling;
  return value?.textContent ?? "";
}

beforeEach(stubReads);
afterEach(() => vi.unstubAllGlobals());

describe("the right pane", () => {
  it("names the file's own facts", async () => {
    mount();
    expect(screen.getByRole("heading", { name: "quarterly.csv" })).toBeInTheDocument();
    expect(fieldValue("Kind")).toBe("CSV file");
    expect(fieldValue("Size")).toBe("2 KB");
    // The pane reads the owner through the same cell the listing does: the
    // label the server resolved, never the id.
    expect(fieldValue("Owner")).toBe("Dana Okafor");
    expect(fieldValue("Path")).toBe("/home/dana/quarterly.csv");
    expect(fieldValue("Hash")).toBe("sha256:abc123");
    // The actor rides the Modified line; Created has none.
    expect(fieldValue("Modified")).toContain("morgan");
    expect(fieldValue("Created")).not.toContain("morgan");
    expect(fieldValue("Created")).not.toBe("—");

    // The count is read from the server and is the way into the history. Not a
    // link: there is no versions route, and the history is read against the
    // node's live version rather than a page's url.
    await waitFor(() => expect(fieldValue("Versions")).toBe("3 versions"));
    expect(calls.some((url) => url.includes("/versions"))).toBe(true);
    expect(screen.queryByRole("link", { name: /version/ })).not.toBeInTheDocument();
  });

  it("summarizes who can access it, from the effective grants", async () => {
    mount();
    await waitFor(() => expect(screen.getByText(/Analytics/)).toBeInTheDocument());
    const lines = within(screen.getByRole("list")).getAllByRole("listitem");
    // The names the server resolved, and the rungs as the product spells them —
    // neither a uuid nor the internal role word reaches the pane.
    expect(lines.map((line) => line.textContent)).toEqual([
      "Dana Okafor · Owner",
      "Analytics · Can view · via a parent folder",
    ]);
    // The inherited half only exists because the read asked for it.
    expect(
      calls.some((url) => url.includes("/permissions") && url.includes("effective=true")),
    ).toBe(true);
  });

  it("keeps the store key and ino behind the developer toggle", async () => {
    const user = userEvent.setup();
    mount();
    expect(screen.queryByText("Store key")).not.toBeInTheDocument();
    expect(screen.queryByText("blobs/ab/abc123")).not.toBeInTheDocument();

    const toggle = screen.getByRole("button", { name: "Developer details" });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    await user.click(toggle);

    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(fieldValue("Store key")).toBe("blobs/ab/abc123");
    expect(fieldValue("ino")).toBe("41005");

    await user.click(toggle);
    expect(screen.queryByText("blobs/ab/abc123")).not.toBeInTheDocument();
  });

  it("shows the path the server sends when it did not resolve a display path", () => {
    // `path` is null on the wire; `pathBytes` carries the readable path. The row read a dash.
    mount({
      item: { ...FILE, path: null, pathBytes: "/home/dana/quarterly.csv" } as unknown as Item,
    });
    expect(fieldValue("Path")).toBe("/home/dana/quarterly.csv");
  });

  it.each([
    ["the server sent the empty string", ""],
    ["the server sent nothing at all", null],
  ])("says there is no path when %s", (_label, pathBytes) => {
    // A node at the top of a drive has nothing above it, and the server spells
    // that as the empty string. A field that renders it is a blank cell, which
    // reads as a value that failed to load rather than as one there isn't.
    mount({ item: { ...FILE, path: null, pathBytes } as unknown as Item });
    expect(fieldValue("Path")).toBe("—");
  });

  it("offers no Rename of its own — a row renames from F2 or the menu", () => {
    mount();
    expect(screen.getByRole("heading", { name: "quarterly.csv" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Rename" })).not.toBeInTheDocument();
    expect(screen.queryByRole("textbox", { name: "New name" })).not.toBeInTheDocument();
  });

  it("keeps Developer details out of a production build", () => {
    // The store key and the inode are developer facts. Vitest runs as a dev build, so
    // the control is there by default; a production build (DEV false) never renders it.
    vi.stubEnv("DEV", false);
    try {
      mount();
      expect(screen.getByRole("heading", { name: "quarterly.csv" })).toBeInTheDocument();
      expect(screen.queryByRole("button", { name: "Developer details" })).not.toBeInTheDocument();
      expect(screen.queryByText("blobs/ab/abc123")).not.toBeInTheDocument();
    } finally {
      vi.unstubAllEnvs();
    }
  });

  it("says what is selected instead of showing one item's facts", () => {
    mount({ selectedCount: 3 });
    expect(screen.getByText("3 items selected")).toBeInTheDocument();
    expect(screen.queryByText("Hash")).not.toBeInTheDocument();
  });

  it("asks for a selection when there is none", () => {
    mount({ item: undefined });
    expect(screen.getByText("Select an item to see its details.")).toBeInTheDocument();
  });

  it("shows a folder's aggregate size and no hash", () => {
    mount({
      item: {
        ...FILE,
        kind: "folder",
        name: "reports",
        nameDisplay: "reports",
        file: null,
        dirStats: { total_bytes: 5_000_000, direct_children: 12 },
      } as unknown as Item,
    });
    expect(fieldValue("Size")).toBe("5 MB");
    expect(fieldValue("Hash")).toBe("—");
  });
});

describe("the pane's pure readers", () => {
  it("marks a grant as inherited only when it came from another node", () => {
    expect(
      describeSharing(
        {
          value: [
            {
              principal: { kind: "user", id: "dana" },
              principalName: "Dana Okafor",
              role: "owner",
              roleLabel: "Owner",
              origin: "direct",
              grantingNodeId: "nd_a",
            },
            {
              principal: { kind: "user", id: "sam" },
              principalName: "Sam Reyes",
              role: "editor",
              origin: "inherited",
              grantingNodeId: "nd_up",
            },
            {
              // A principal the server could not resolve keeps its id, and a rung
              // outside the shipped ladder keeps its own spelling.
              principal: { kind: "user", id: "kim" },
              role: "reader",
              roleLabel: "Can view",
              origin: "direct",
              grantingNodeId: null,
            },
          ],
        } as Parameters<typeof describeSharing>[0],
        "nd_a",
      ),
    ).toEqual([
      "Dana Okafor · Owner",
      "Sam Reyes · editor · via a parent folder",
      "kim · Can view",
    ]);
  });

  it("reads the store key under either spelling, and nothing when the facet has none", () => {
    expect(storeKeyOf({ file: { store_key: "a/b" } } as unknown as Item)).toBe("a/b");
    expect(storeKeyOf({ file: { storeKey: "c/d" } } as unknown as Item)).toBe("c/d");
    expect(storeKeyOf({ file: { store_key: "" } } as unknown as Item)).toBeNull();
    expect(storeKeyOf({ file: null } as unknown as Item)).toBeNull();
  });
});

/** A folder that is also a page: the pane's two doors read off this, never off
 *  the node's kind. */
function folderObject(over: Record<string, unknown>): Item {
  return {
    ...FILE,
    id: "nd_obj",
    kind: "folder",
    file: null,
    ...over,
  } as unknown as Item;
}

const CHAT = folderObject({
  name: "3952c9e2.alkerachat",
  nameDisplay: "3952c9e2.alkerachat",
  object: { type: "chat", id: "cht_9", title: "Q3 review", web_url: "/chat/cht_9" },
});

const TEMPLATE = folderObject({
  name: "Monthly revenue.alkerachat.template",
  nameDisplay: "Monthly revenue.alkerachat.template",
  object: {
    type: "chat_template",
    id: "tpl_4",
    title: "Monthly revenue",
    web_url: "/templates/tpl_4",
    metadata: { brief: "Pull revenue by region for the month." },
  },
});

const WORKSPACE = folderObject({
  name: "Pricing.alkeraworkspace",
  nameDisplay: "Pricing.alkeraworkspace",
  object: { type: "workspace", id: "ws_3", title: "Pricing", web_url: "/workspaces/ws_3" },
});

describe("the pane's two doors", () => {
  it("names the page for what a workspace is, and offers its files", async () => {
    const onOpenChat = vi.fn();
    const onViewFiles = vi.fn();
    mount({ item: WORKSPACE, onOpenChat, onViewFiles });

    const ways = screen.getByRole("group", { name: "Open" });
    await userEvent.click(within(ways).getByRole("button", { name: "Open workspace" }));
    await userEvent.click(within(ways).getByRole("button", { name: "Browse files" }));
    expect(onOpenChat).toHaveBeenCalledWith(WORKSPACE);
    expect(onViewFiles).toHaveBeenCalledWith(WORKSPACE);
  });

  it("names the page for what a chat is, and offers its files", async () => {
    const onOpenChat = vi.fn();
    const onViewFiles = vi.fn();
    mount({ item: CHAT, onOpenChat, onViewFiles });

    const ways = screen.getByRole("group", { name: "Open" });
    await userEvent.click(within(ways).getByRole("button", { name: "Open chat" }));
    await userEvent.click(within(ways).getByRole("button", { name: "Browse files" }));
    expect(onOpenChat).toHaveBeenCalledWith(CHAT);
    expect(onViewFiles).toHaveBeenCalledWith(CHAT);
  });

  it("names the page for what a template is, and offers its files", async () => {
    const onOpenChat = vi.fn();
    const onViewFiles = vi.fn();
    mount({ item: TEMPLATE, onOpenChat, onViewFiles });

    const ways = screen.getByRole("group", { name: "Open" });
    await userEvent.click(within(ways).getByRole("button", { name: "Open template" }));
    await userEvent.click(within(ways).getByRole("button", { name: "Browse files" }));
    expect(onOpenChat).toHaveBeenCalledWith(TEMPLATE);
    expect(onViewFiles).toHaveBeenCalledWith(TEMPLATE);
  });

  it("offers no doors on an ordinary row, which already is its contents", () => {
    mount();
    expect(screen.queryByRole("group", { name: "Open" })).toBeNull();
  });
});

describe("what the pane says about deleting a chat", () => {
  it("says the folder holds the chat's FILES and the chat goes from its own page", () => {
    mount({ item: CHAT });
    expect(screen.getByText(/the chat itself is deleted from the chat page/i)).toBeInTheDocument();
  });

  it("says nothing of the sort on a template or an ordinary row", () => {
    const { unmount } = mount({ item: TEMPLATE });
    expect(screen.queryByText(/deleted from the chat page/i)).toBeNull();
    unmount();
    mount();
    expect(screen.queryByText(/deleted from the chat page/i)).toBeNull();
  });
});

describe("a template's brief", () => {
  it("shows the brief with a link to where it is edited", () => {
    mount({ item: TEMPLATE });
    expect(fieldValue("Brief")).toContain("Pull revenue by region for the month.");
    // Onto the template's own page, which is where a brief is written: the pane
    // shows it and does not become a second editor for it.
    expect(screen.getByRole("link", { name: "Edit" })).toHaveAttribute("href", "/templates/tpl_4");
  });

  it("says a template with no brief has none, and still offers the page", () => {
    mount({
      item: folderObject({
        object: { type: "chat_template", id: "tpl_5", web_url: "/templates/tpl_5" },
      }),
    });
    expect(fieldValue("Brief")).toContain("No brief yet.");
    expect(screen.getByRole("link", { name: "Edit" })).toHaveAttribute("href", "/templates/tpl_5");
  });

  it("has no Brief field on a chat or an ordinary row", () => {
    const { unmount } = mount({ item: CHAT });
    expect(screen.queryByText("Brief")).toBeNull();
    unmount();
    mount();
    expect(screen.queryByText("Brief")).toBeNull();
  });

  it("reads a brief only when the facet carries one worth showing", () => {
    expect(briefOf({ object: { metadata: { brief: "do the thing" } } } as unknown as Item)).toBe(
      "do the thing",
    );
    expect(briefOf({ object: { metadata: { brief: "   " } } } as unknown as Item)).toBeNull();
    expect(briefOf({ object: { metadata: {} } } as unknown as Item)).toBeNull();
    expect(briefOf({ object: null } as unknown as Item)).toBeNull();
  });
});

/** The pane wired to the page's one action layer, as the Files page wires it:
 *  the pane is a second place a person works from, not a second product, so its
 *  openers hand straight to the menu the list already builds. */
function mountWired(row: Item = FILE) {
  const client = createQueryClient({ retry: false });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <FilesActions canWriteHere driveId={DRIVE} currentFolderId="nd_parent" selection={[row]}>
          {(actions) => (
            <RightPane
              driveId={DRIVE}
              item={row}
              triggerProps={actions.triggerProps}
              openMenuAt={actions.openMenuAt}
            />
          )}
        </FilesActions>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

/** Every row of the open menu, by the id the page dispatches. */
function openMenuRows(): string[] {
  return within(screen.getByRole("menu"))
    .getAllByRole("menuitem")
    .map((row) => row.getAttribute("data-item") ?? "");
}

describe("the pane's way into the row menu", () => {
  it("opens the same menu from Shift+F10 on the pane and from its … button", async () => {
    const user = userEvent.setup();
    mountWired();

    fireEvent.keyDown(screen.getByRole("complementary", { name: "Details" }), {
      key: "F10",
      shiftKey: true,
    });
    const byKeyboard = openMenuRows();
    expect(byKeyboard).toContain("share");
    expect(byKeyboard).toContain("copy-link");

    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("menu")).toBeNull());

    await user.click(screen.getByRole("button", { name: "Actions for quarterly.csv" }));
    expect(openMenuRows()).toEqual(byKeyboard);
  });

  it("names the button for the row a person has been reading", () => {
    // A chat is stored as a uuid-named folder; the heading shows its title, and
    // so does the control beside it.
    mountWired(CHAT);
    expect(screen.getByRole("button", { name: "Actions for Q3 review" })).toHaveAttribute(
      "aria-haspopup",
      "menu",
    );
  });

  it("offers no … button when nothing wired a menu to it", () => {
    mount();
    expect(screen.queryByRole("button", { name: /^Actions for/ })).toBeNull();
  });
});

describe("the pane's own Share", () => {
  it("titles the dialog with the chat's title, not the folder it is stored as", async () => {
    const user = userEvent.setup();
    const shareable = {
      ...CHAT,
      capabilities: { can_read: true, can_share: true, refusals: {} },
    } as unknown as Item;
    mount({ item: shareable });

    await user.click(screen.getByRole("button", { name: "Share" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText("Share “Q3 review”")).toBeInTheDocument();
    expect(within(dialog).queryByText(/alkerachat/)).toBeNull();
  });
});
