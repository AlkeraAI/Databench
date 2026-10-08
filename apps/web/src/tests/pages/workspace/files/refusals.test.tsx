/**
 * A refused Files action is SHOWN.
 *
 * Every write here answers 4xx and changes nothing, so the only thing that tells a
 * person the difference between "refused" and "broken" is the sentence on screen. The
 * pins drive the real component through the real hooks with `fetch` answering the four
 * refusals a customer actually hits — a lease, a stale precondition, the quota and a
 * plain denial — and assert the sentence, that the listing was not touched, and that
 * the next write that lands clears it.
 */

import { QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { Item } from "@/api/files";
import { enterOrg, forgetActiveOrg } from "@/api/activeOrg";
import { createQueryClient } from "@/api/queryClient";
import { FilesActions, type FilesActionsApi } from "@/pages/workspace/files/FilesActions";
import { RenameInline } from "@/pages/workspace/files/RenameInline";

const DRIVE = "dr_1";

function item(overrides: Partial<Item> = {}): Item {
  return {
    id: "nd_1",
    name: "notes.md",
    nameDisplay: "notes.md",
    kind: "file",
    etag: "7",
    parentId: "nd_root",
    // Every capability, as the server sends them to an owner: the page asks the
    // menu before it runs a write, and a row with none would be refused.
    capabilities: {
      can_read: true,
      can_write: true,
      can_share: true,
      can_delete: true,
      can_rename: true,
      can_download: true,
    },
    ...overrides,
  } as Item;
}

/** The lease facet as the wire carries it — the page reads the holder off the item. */
const LEASED = item({
  id: "nd_leased",
  name: "designs",
  nameDisplay: "designs",
  kind: "folder",
  lease: {
    holder: "Dana",
    machine: "dana-mbp",
    purpose: "mount",
    mine: false,
  },
} as Partial<Item>);

interface Refusal {
  status: number;
  code: string;
  message?: string;
}

/** Answer every write with `refusal`, then with 200 once `succeedAfter` have been made.
 *  Returns the write requests seen, so a pin can prove nothing else was sent. */
function stubWrites(refusal: Refusal, succeedAfter = Number.POSITIVE_INFINITY) {
  const seen: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const request = input instanceof Request ? input : new Request(String(input), init);
      if (request.method === "GET") {
        return new Response(JSON.stringify({ entries: [] }), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      }
      seen.push(`${request.method} ${new URL(request.url, "http://x").pathname}`);
      if (seen.length > succeedAfter) {
        return new Response(
          JSON.stringify({ id: "op_1", driveId: DRIVE, kind: "trash", state: "done" }),
          { status: 200, headers: { "content-type": "application/json" } },
        );
      }
      return new Response(
        JSON.stringify({ code: refusal.code, message: refusal.message ?? "" }),
        { status: refusal.status, headers: { "content-type": "application/json" } },
      );
    }),
  );
  return seen;
}

let api: FilesActionsApi | null = null;

/** The rows the page believes it is showing; a refused write must not change them. */
function renderActions(selection: readonly Item[]) {
  const client = createQueryClient();
  const moved: string[] = [];
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <FilesActions canWriteHere
          driveId={DRIVE}
          currentFolderId="nd_root"
          selection={selection}
          onOperation={(report) => moved.push(report.label)}
        >
          {(actions) => {
            api = actions;
            return (
              <ul>
                {selection.map((row) => (
                  <li key={row.id} data-testid={`row-${row.id}`}>
                    {row.nameDisplay}
                  </li>
                ))}
              </ul>
            );
          }}
        </FilesActions>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { moved };
}

afterEach(() => {
  api = null;
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("a refused Files action", () => {
  it("names the lease holder when a trash is refused, and leaves the row where it was", async () => {
    const writes = stubWrites({ status: 409, code: "files.leased" });
    const { moved } = renderActions([LEASED]);

    api?.run("trash");

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Dana has this folder for local use.");
    expect(alert).toHaveTextContent("dana-mbp");
    expect(alert.getAttribute("data-code")).toBe("files.leased");
    // The write was attempted, refused, and reported nothing to the undo stack.
    expect(writes).toHaveLength(1);
    expect(moved).toEqual([]);
    expect(screen.getByTestId("row-nd_leased")).toBeInTheDocument();
  });

  it.each([
    [
      "a stale precondition",
      { status: 412, code: "files.precondition_failed", message: "node 3f2a is not at etag 7" },
      "Someone changed this while you were working.",
    ],
    [
      "a drive over its limit",
      { status: 409, code: "files.frozen", message: "drive 3f2a is over_quota" },
      "This drive is over its storage limit.",
    ],
    [
      "the storage quota",
      { status: 507, code: "files.quota_bytes", message: "2.0 TB of 2.0 TB used." },
      "Your organization is out of storage.",
    ],
    [
      "a denial",
      { status: 403, code: "files.forbidden", message: "" },
      "You do not have permission to do that.",
    ],
  ] as const)("shows a sentence for %s", async (_name, refusal, sentence) => {
    stubWrites(refusal);
    renderActions([item()]);

    // Starring is retired; a rename-free write with the same refusal surface.
    api?.run("trash");

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(sentence);
    // The customer never reads the server's own wording for these.
    expect(alert.textContent ?? "").not.toMatch(/etag|over_quota|3f2a/);
  });

  it("clears the refusal once a later write lands", async () => {
    stubWrites({ status: 409, code: "files.leased" }, 1);
    const { moved } = renderActions([item()]);

    api?.run("trash");
    expect(await screen.findByRole("alert")).toBeInTheDocument();

    api?.run("trash");
    await waitFor(() => expect(screen.queryByRole("alert")).toBeNull());
    expect(moved).toEqual(["Moved notes.md to trash"]);
  });

  it("shows the refusal on the copy route too, not only on the trash", async () => {
    const writes = stubWrites({ status: 409, code: "files.exists" });
    const { moved } = renderActions([item()]);

    api?.run("duplicate");

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Something here already has that name.");
    expect(alert.getAttribute("data-code")).toBe("files.exists");
    expect(writes).toEqual(["POST /api/v1/files/drives/dr_1/items/nd_1/copy"]);
    expect(moved).toEqual([]);
  });
});

// The three surfaces that own their own mutation, so the seam above never sees
// their refusals. None may change nothing and say nothing.

describe("a refused rename", () => {
  it("shows the sentence and keeps the editor open with what was typed", async () => {
    const writes = stubWrites({ status: 409, code: "files.exists" });
    const client = createQueryClient();
    const done = vi.fn();
    render(
      <QueryClientProvider client={client}>
        <MemoryRouter>
          <RenameInline driveId={DRIVE} item={item()} onDone={done} />
        </MemoryRouter>
      </QueryClientProvider>,
    );

    const field = screen.getByRole("textbox", { name: "New name" });
    await userEvent.clear(field);
    await userEvent.type(field, "taken.md{Enter}");

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Something here already has that name.");
    expect(alert.getAttribute("data-code")).toBe("files.exists");
    // The editor is still there, still holding the typed name, and nobody was
    // told the rename was finished.
    expect(screen.getByRole("textbox", { name: "New name" })).toHaveValue("taken.md");
    expect(done).not.toHaveBeenCalled();
    expect(writes).toEqual(["PATCH /api/v1/files/drives/dr_1/items/nd_1"]);
  });
});

/** The clipboard the page writes through. jsdom ships none. */
function clipboard(writeText: unknown): void {
  Object.defineProperty(globalThis.navigator, "clipboard", {
    value: writeText === undefined ? undefined : { writeText },
    configurable: true,
  });
}

/** Answer every read with `node`, so a dialog the menu opens has a row to read.
 *  Nothing here writes — the two cases below send no request at all. */
function stubNode(node: Record<string, unknown>): void {
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const request = input instanceof Request ? input : new Request(String(input), init);
      const path = new URL(request.url, "http://x").pathname;
      const body = path.endsWith("/permissions")
        ? { value: [] }
        : path.endsWith("/org/members") || path.endsWith("/teams")
          ? []
          : node;
      return Promise.resolve(
        new Response(JSON.stringify(body), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
      );
    }),
  );
}

/** A chat, as the drive stores one: a folder whose name is a uuid nobody typed
 *  and whose title is the only thing a person has been reading. */
const CHAT = item({
  id: "nd_chat",
  kind: "folder",
  name: "3952c9e2.alkerachat",
  nameDisplay: "3952c9e2.alkerachat",
  object: {
    type: "chat",
    id: "cht_9",
    title: "Q3 review",
    web_url: "https://app.alkera.test/chat/cht_9",
  },
} as unknown as Partial<Item>);

describe("copying a row's link", () => {
  it("says the link is copied once the clipboard actually has it", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    clipboard(writeText);
    stubNode({});
    renderActions([item()]);

    await act(async () => {
      api?.run("copy-link");
    });

    expect(writeText).toHaveBeenCalledWith(`${window.location.origin}/files/nd_1`);
    expect(screen.getByRole("status")).toHaveTextContent("Link copied");
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("says it was not copied when the browser refuses, and claims nothing", async () => {
    clipboard(vi.fn().mockRejectedValue(new Error("denied")));
    stubNode({});
    renderActions([item()]);

    await act(async () => {
      api?.run("copy-link");
    });

    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent("The link was not copied.");
    expect(alert.getAttribute("data-code")).toBe("files.link_not_copied");
    expect(screen.queryByRole("status")).toBeNull();
  });

  it("copies a chat's conversation link, not the folder it is stored as", async () => {
    // A chat row is a page and a folder at once, and the page is what a person
    // means by "the chat". Copying the folder address would hand somebody the
    // file listing when they were promised the conversation.
    const writeText = vi.fn().mockResolvedValue(undefined);
    clipboard(writeText);
    stubNode({});
    renderActions([CHAT]);

    await act(async () => {
      api?.run("copy-link");
    });

    expect(writeText).toHaveBeenCalledWith("https://app.alkera.test/chat/cht_9");
  });

  it("names the org the link was copied in", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    clipboard(writeText);
    stubNode({});
    enterOrg("org_a");
    try {
      renderActions([item()]);
      await act(async () => {
        api?.run("copy-link");
      });
      expect(writeText).toHaveBeenCalledWith(`${window.location.origin}/files/nd_1?org=org_a`);
    } finally {
      forgetActiveOrg();
    }
  });

  it("drops the notice as soon as something else is asked for", async () => {
    clipboard(vi.fn().mockResolvedValue(undefined));
    stubWrites({ status: 409, code: "files.leased" });
    renderActions([item()]);

    await act(async () => {
      api?.run("copy-link");
    });
    expect(screen.getByRole("status")).toHaveTextContent("Link copied");

    api?.run("trash");
    await waitFor(() => expect(screen.queryByRole("status")).toBeNull());
  });
});

describe("the share dialog the menu opens", () => {
  it("is titled with the chat's title, not the folder the chat is stored as", async () => {
    stubNode(CHAT as unknown as Record<string, unknown>);
    renderActions([CHAT]);

    api?.run("share");

    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText("Share “Q3 review”")).toBeInTheDocument();
    expect(within(dialog).queryByText(/alkerachat/)).toBeNull();
  });

  it("is titled with a plain file's own name", async () => {
    stubNode(item() as unknown as Record<string, unknown>);
    renderActions([item()]);

    api?.run("share");

    expect(await screen.findByText("Share “notes.md”")).toBeInTheDocument();
  });
});
