import { QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { RealtimeEventFrame } from "@/api/events/eventMap";
import { publishFrame, resetFrameBus } from "@/api/events/frameBus";
import type { Item } from "@/api/files";
import { createQueryClient } from "@/api/queryClient";
import { buildContextMenuItems, type MenuActionId } from "@/pages/workspace/files/contextMenuItems";
import { FileVersionsDialog } from "@/pages/workspace/files/FileVersionsDialog";
import { RightPane } from "@/pages/workspace/files/RightPane";

// A file's history, and the way back to one of its versions.
//
// The upload prompt promises that replacing a file keeps what was there, so the
// product owes the reader somewhere to go and get it. The stub below is a small
// server rather than a canned answer on purpose: a restore APPENDS the old bytes
// as a new head, so what is asserted after a click is the list the server would
// then answer — which a mock that only counted calls could never show.

const DRIVE = "drv_1";
const NODE = "nd_a";

interface WireVersion {
  id: string;
  seq: number;
  size: number;
  createdAt: string;
  isHead?: boolean;
}

interface Call {
  method: string;
  url: string;
  headers: Record<string, string>;
}

let calls: Call[] = [];
/** The file's whole history as the server holds it, oldest first. Mutated by a
 *  restore so the re-read that follows one answers what actually happened. */
let history: WireVersion[] = [];
/** The node's version counter, bumped by a restore the way a real write bumps it. */
let etag = "7";
/** Non-null makes the next restore answer this instead of doing the write. */
let restoreRefusal: { status: number; body: unknown } | null = null;
/** Non-null makes the versions read answer this instead of the history. */
let readRefusal: { status: number; body: unknown } | null = null;
let capabilities: Record<string, unknown> = {
  can_read: true,
  can_write: true,
  can_share: true,
  refusals: {},
};

function version(seq: number, size: number, over: Partial<WireVersion> = {}): WireVersion {
  return {
    id: `v${seq}`,
    seq,
    size,
    createdAt: `2026-0${seq}-0${seq}T09:00:00Z`,
    ...over,
  };
}

/** What the route does: the old bytes come back as a NEW version on top, so the
 *  history only ever grows and the node moves to a version it has never had. */
function appendRestored(sourceId: string): void {
  const source = history.find((row) => row.id === sourceId);
  if (source === undefined) return;
  for (const row of history) row.isHead = false;
  const seq = history.length + 1;
  history.push({
    id: `v${seq}`,
    seq,
    size: source.size,
    createdAt: "2026-06-06T09:00:00Z",
    isHead: true,
  });
  etag = String(Number(etag) + 1);
}

function node(): Record<string, unknown> {
  return {
    id: NODE,
    ino: 41_005,
    driveId: DRIVE,
    kind: "file",
    name: "quarterly.csv",
    nameDisplay: "quarterly.csv",
    parentId: "nd_parent",
    path: "/home/dana/quarterly.csv",
    etag,
    ctag: "c1",
    attrs: { owner: "dana", mtime: "2026-03-04T10:00:00Z", metadata: {} },
    file: { size: 4_096, content_hash: "sha256:abc123" },
    capabilities,
  };
}

function stub(): void {
  calls = [];
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      // The typed client hands `fetch` a built `Request`, so the method and the
      // headers live on IT rather than on an init object.
      const asRequest = input instanceof Request ? input : null;
      const raw = asRequest ? asRequest.url : String(input);
      const method = (init?.method ?? asRequest?.method ?? "GET").toUpperCase();
      const headers = Object.fromEntries(
        new Headers((init?.headers as HeadersInit | undefined) ?? asRequest?.headers).entries(),
      );
      calls.push({ method, url: raw, headers });
      const url = new URL(raw, "http://localhost");

      const answer = (status: number, payload: unknown): Promise<Response> =>
        Promise.resolve(
          new Response(payload === null ? null : JSON.stringify(payload), {
            status,
            headers: { "content-type": "application/json" },
          }),
        );

      if (url.pathname.endsWith("/restore")) {
        if (restoreRefusal !== null) return answer(restoreRefusal.status, restoreRefusal.body);
        const parts = url.pathname.split("/");
        appendRestored(parts[parts.length - 2] ?? "");
        return answer(200, node());
      }
      if (url.pathname.endsWith("/versions")) {
        if (readRefusal !== null) return answer(readRefusal.status, readRefusal.body);
        return answer(200, { versions: history });
      }
      if (url.pathname.endsWith("/permissions")) {
        return answer(200, {
          value: [
            {
              principal: { kind: "user", id: "dana" },
              principalName: "Dana Okafor",
              role: "owner",
              origin: "direct",
              grantingNodeId: NODE,
            },
          ],
        });
      }
      if (url.pathname.includes("/items/")) return answer(200, node());
      if (url.pathname.endsWith("/org/members") || url.pathname.endsWith("/teams")) {
        return answer(200, []);
      }
      return answer(200, {});
    }),
  );
}

beforeEach(() => {
  etag = "7";
  restoreRefusal = null;
  readRefusal = null;
  capabilities = { can_read: true, can_write: true, can_share: true, refusals: {} };
  // Three sizes no one of which reads as a substring of another, so a row that
  // drew the wrong version's size cannot pass by accident.
  history = [version(1, 512), version(2, 2_048), version(3, 3_000_000, { isHead: true })];
  stub();
});
afterEach(() => vi.unstubAllGlobals());

function mountDialog(props: Partial<React.ComponentProps<typeof FileVersionsDialog>> = {}) {
  // createQueryClient, never a bare QueryClient: the invalidation policy that
  // makes a landed restore re-read the history lives on it.
  const client = createQueryClient({ retry: false });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <FileVersionsDialog
          driveId={DRIVE}
          nodeId={NODE}
          open
          onClose={() => undefined}
          {...props}
        />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

/** Every row of the history, in the order the dialog drew them. */
async function rows(): Promise<HTMLElement[]> {
  const list = await screen.findByRole("list", { name: "Versions" });
  return within(list).getAllByRole("listitem");
}

/** The drive saying a node changed, as a live document's write-back says it. */
function changed(entityId: string): RealtimeEventFrame {
  return {
    type: "file_node.changed",
    entity: "file_node",
    entity_id: entityId,
    version: 9,
    org_id: "org_1",
    drive_id: DRIVE,
    parent_id: "nd_parent",
    reason: "live_doc_saved",
  } as RealtimeEventFrame;
}

function restoreCalls(): Call[] {
  return calls.filter((call) => call.method === "POST" && call.url.endsWith("/restore"));
}

describe("the versions dialog", () => {
  it("lists the file's whole history newest first", async () => {
    mountDialog();
    const listed = await rows();
    expect(listed.map((row) => row.textContent)).toEqual([
      expect.stringContaining("Version 3"),
      expect.stringContaining("Version 2"),
      expect.stringContaining("Version 1"),
    ]);
  });

  it("carries each version's own size", async () => {
    mountDialog();
    const listed = await rows();
    // Per row, not "somewhere on screen": a list that drew one version's size on
    // every line would pass a looser assertion and mislead every reader.
    expect(listed[0]?.textContent).toContain("3 MB");
    expect(listed[1]?.textContent).toContain("2 KB");
    expect(listed[2]?.textContent).toContain("512 B");
  });

  it("says when each version was written, and nothing when the server sent no date", async () => {
    history = [version(1, 512, { createdAt: "" }), version(2, 2_048, { isHead: true })];
    mountDialog();
    const listed = await rows();
    expect(listed[0]?.textContent).toMatch(/2026/);
    // A row whose date the formatter cannot read shows the dash rather than an
    // invalid one, which is how a reader tells "no date" from "a wrong date".
    expect(listed[1]?.textContent).toContain("—");
    expect(listed[1]?.textContent).not.toMatch(/2026/);
  });

  it("says who wrote each version: the person, the agent, both, or nothing it was not told", async () => {
    history = [
      version(1, 512, { author: "Dana Ruiz" } as Partial<WireVersion>),
      version(2, 600, { author: "Dana Ruiz", machine: "box-7" } as Partial<WireVersion>),
      version(3, 700, { machine: "box-7" } as Partial<WireVersion>),
      version(4, 800, { isHead: true }),
    ];
    mountDialog();
    const shown = (await rows()).map((row) => row.querySelector(".alk-files-versions__meta")?.textContent ?? "");
    expect(shown[3]).toMatch(/· Dana Ruiz$/);
    expect(shown[2]).toMatch(/· Dana Ruiz, Agent$/);
    expect(shown[1]).toMatch(/· Agent$/);
    expect(shown[0]).not.toMatch(/Dana|Agent/);
  });

  it("tells apart two versions written minutes apart on one day", async () => {
    history = [
      version(1, 512, { createdAt: "2026-10-04T09:01:00Z" }),
      version(2, 2_048, { createdAt: "2026-10-04T09:07:00Z", isHead: true }),
    ];
    mountDialog();
    const listed = await rows();
    const shown = listed.map((row) => row.querySelector(".alk-files-versions__meta")?.textContent);
    expect(shown[0]).not.toEqual(shown[1]);
  });

  it("re-reads the history when the drive says the file changed while it is open", async () => {
    resetFrameBus();
    mountDialog();
    expect(await rows()).toHaveLength(3);
    history.push(version(4, 77, { createdAt: "2026-10-04T09:00:00Z" }));
    act(() => {
      publishFrame(changed("nd_someone_else"));
    });
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(await rows()).toHaveLength(3);
    act(() => {
      publishFrame(changed(NODE));
    });
    await waitFor(async () => expect(await rows()).toHaveLength(4));
    resetFrameBus();
  });

  it("does not offer to restore the version the file is already at", async () => {
    mountDialog();
    const listed = await rows();
    expect(within(listed[0] as HTMLElement).getByText("Current")).toBeInTheDocument();
    expect(within(listed[0] as HTMLElement).queryByRole("button")).toBeNull();
    expect(screen.getAllByRole("button", { name: /^Restore version/ })).toHaveLength(2);
  });

  it("marks and offers versions with the UI library's pill and buttons", async () => {
    mountDialog();
    const listed = await rows();
    const current = within(listed[0] as HTMLElement).getByText("Current");
    expect(current.closest(".alk-pill")).not.toBeNull();
    for (const button of screen.getAllByRole("button", { name: /^Restore version/ })) {
      expect(button).toHaveClass("alk-btn");
    }
  });

  it("treats the newest version as the current one when the server flags none", async () => {
    // `isHead` is an optional field on the wire. A node points at its newest
    // version by construction, so an answer that flags nothing still has one.
    history = [version(1, 512), version(2, 2_048), version(3, 3_000_000)];
    mountDialog();
    const listed = await rows();
    expect(within(listed[0] as HTMLElement).getByText("Current")).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: /^Restore version/ })).toHaveLength(2);
  });

  it("restores the version that was clicked and shows what the server did", async () => {
    const user = userEvent.setup();
    mountDialog();
    await rows();

    await user.click(screen.getByRole("button", { name: "Restore version 1" }));

    await waitFor(() => expect(restoreCalls()).toHaveLength(1));
    const sent = restoreCalls()[0];
    expect(sent?.url).toContain(`/drives/${DRIVE}/items/${NODE}/versions/v1/restore`);
    // Every Files write says which version it believed it was changing and
    // carries a key the server can replay an answer under.
    expect(sent?.headers["if-match"]).toBe("7");
    expect(sent?.headers["idempotency-key"]).toBeTruthy();

    // The proof the write landed is the history the server now answers: the old
    // bytes came back as a NEW head, so there are four rows and the top one is it.
    await waitFor(async () => expect(await rows()).toHaveLength(4));
    const listed = await rows();
    expect(listed[0]?.textContent).toContain("Version 4");
    expect(within(listed[0] as HTMLElement).getByText("Current")).toBeInTheDocument();
    // 512 B is what version 1 held, and what the restore put back on top.
    expect(listed[0]?.textContent).toContain("512 B");
    expect(within(listed[3] as HTMLElement).getByRole("button")).toBeInTheDocument();
  });

  it("fences the second restore on the version the first one produced", async () => {
    const user = userEvent.setup();
    mountDialog();
    await rows();

    await user.click(screen.getByRole("button", { name: "Restore version 1" }));
    await waitFor(async () => expect(await rows()).toHaveLength(4));
    await user.click(screen.getByRole("button", { name: "Restore version 2" }));

    await waitFor(() => expect(restoreCalls()).toHaveLength(2));
    // The etag the dialog opened with is spent the moment the first restore
    // lands; sending it again is the refusal this guards against.
    expect(restoreCalls()[1]?.headers["if-match"]).toBe("8");
  });

  it("says what the server said when a restore is refused, and changes nothing", async () => {
    const user = userEvent.setup();
    restoreRefusal = {
      status: 412,
      body: { code: "files.precondition_failed", message: "This item changed" },
    };
    mountDialog();
    await rows();

    await user.click(screen.getByRole("button", { name: "Restore version 1" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Someone changed this while you were working.");
    expect(await rows()).toHaveLength(3);
  });

  it("refuses to restore for a reader, in the server's own words", async () => {
    capabilities = {
      can_read: true,
      can_write: false,
      refusals: { write: "This file was shared with you to read." },
    };
    mountDialog();
    const listed = await rows();
    const button = within(listed[1] as HTMLElement).getByRole("button");
    // The reason, not just the disabled state: the button is held while the node
    // read is still out too, so only the server's own sentence proves the
    // capability was what refused it.
    await waitFor(() =>
      expect(button).toHaveAttribute("title", "This file was shared with you to read."),
    );
    expect(button).toBeDisabled();
  });

  it("says a file with one version has only the one", async () => {
    history = [version(1, 512, { isHead: true })];
    mountDialog();
    expect(await screen.findByText("This file has only one version.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Restore version/ })).toBeNull();
  });

  it("says the history is loading before the read lands", () => {
    mountDialog();
    expect(screen.getByText("Loading…")).toBeInTheDocument();
  });

  it("says so when the history cannot be read", async () => {
    readRefusal = { status: 500, body: { code: "files.internal", message: "no" } };
    mountDialog();
    expect(
      await screen.findByText("This file’s versions could not be read."),
    ).toBeInTheDocument();
  });

  it("titles itself with the name the reader knows the file by", async () => {
    mountDialog({ subjectName: "Q3 review" });
    expect(await screen.findByText("Versions of “Q3 review”")).toBeInTheDocument();
  });

  it("reads nothing while it is closed", () => {
    mountDialog({ open: false });
    expect(calls.some((call) => call.url.includes("/versions"))).toBe(false);
  });
});

const FILE: Item = {
  ...node(),
  stale: false,
  locked: false,
  held: false,
  shared: false,
  trashed: false,
} as unknown as Item;

const FOLDER = { ...FILE, id: "nd_f", kind: "folder", file: null } as unknown as Item;

function mountPane(item: Item = FILE) {
  const client = createQueryClient({ retry: false });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <RightPane driveId={DRIVE} item={item} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("the details pane's Versions field", () => {
  it("counts the file's versions and opens the history from the count", async () => {
    const user = userEvent.setup();
    mountPane();

    const control = await screen.findByRole("button", { name: "3 versions" });
    await user.click(control);

    const listed = await rows();
    expect(listed).toHaveLength(3);
    expect(listed[0]?.textContent).toContain("Version 3");
  });

  it("counts one version in the singular", async () => {
    history = [version(1, 512, { isHead: true })];
    mountPane();
    expect(await screen.findByRole("button", { name: "1 version" })).toBeInTheDocument();
  });

  it("has no Versions field on a folder, and asks the server for none", async () => {
    mountPane(FOLDER);
    await waitFor(() => expect(screen.getByText(/Dana Okafor/)).toBeInTheDocument());
    expect(screen.queryByText("Versions")).toBeNull();
    expect(calls.some((call) => call.url.includes("/versions"))).toBe(false);
  });
});

function menu(targets: readonly Item[], onAction = vi.fn()) {
  return buildContextMenuItems({
    platform: "mac",
    targets,
    currentFolderId: "nd_parent",
    canWriteHere: true,
    clipboard: { mode: null, items: [], sourceFolderId: null },
    onAction,
  });
}

function rowFor(targets: readonly Item[], id: MenuActionId, onAction = vi.fn()) {
  return menu(targets, onAction).find((row) => row.id === id);
}

const CHAT = {
  ...FOLDER,
  id: "nd_chat",
  name: "Q3 review.alkerachat",
  nameDisplay: "Q3 review.alkerachat",
  object: { type: "chat", id: "cht_9", title: "Q3 review", web_url: "/chat/cht_9" },
} as unknown as Item;

describe("Versions on a Files row", () => {
  it("is offered on a file", () => {
    // `disabled` alone would read as "fine" on a row that is not in the menu at
    // all, so the row has to be found before it is asked anything.
    const row = rowFor([FILE], "versions");
    expect(row?.label).toBe("Versions");
    expect(row?.disabled).toBeUndefined();
  });

  it.each([
    ["a plain folder", FOLDER],
    ["a chat", CHAT],
  ])("is not in the menu on %s: only a file keeps versions", (_label, target) => {
    expect(rowFor([target], "versions")).toBeUndefined();
  });

  it("is not in the menu on a plural selection: a history is one file's", () => {
    expect(rowFor([FILE, FILE], "versions")).toBeUndefined();
  });

  it("refuses a file the caller cannot read, in the server's own words", () => {
    const sealed = {
      ...FILE,
      capabilities: { can_read: false, refusals: { read: "This file is not shared with you." } },
    } as unknown as Item;
    expect(rowFor([sealed], "versions")?.disabled).toBe("This file is not shared with you.");
  });

  it("dispatches the action with the row it was opened over", () => {
    const onAction = vi.fn();
    rowFor([FILE], "versions", onAction)?.onSelect?.();
    expect(onAction).toHaveBeenCalledWith("versions", [FILE]);
  });
});
