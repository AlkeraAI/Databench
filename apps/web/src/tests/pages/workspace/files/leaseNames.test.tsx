/**
 * No lease sentence ever shows a reader an id.
 *
 * A box registers itself under its allocation uuid AND holds its own lease, so
 * the facet the browser gets carries that one uuid as both the holder and the
 * machine. Rendered straight, the badge read "8 In use by <uuid> on <uuid>" —
 * the same id twice, as if it were two parties, behind an avatar showing the
 * first character of the uuid. These pin the three shapes a lease arrives in
 * (a box holding a chat's folder, a person on their own machine, and neither
 * name resolved) and the rule that covers every shape: no uuid on screen.
 */

import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { Item, LeaseRow } from "@/api/files";
import { createQueryClient } from "@/api/queryClient";
import { LeaseBadge, LeaseFacetPane } from "@/pages/workspace/files/LeaseBadge";
import { MyLeases } from "@/pages/workspace/files/MyLeases";

/** The allocation uuid a box is both known by and holds its lease under. */
const BOX_ID = "807bf89a-464c-4867-8b08-6e020a9bd8a3";
const CHAT_ID = "a7a3a90b-1111-4222-8333-444455556666";

/** Any uuid, anywhere in a rendered string. */
const ANY_ID = /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i;

const NOW = new Date("2026-09-09T10:15:00.000Z");

function leased(lease: Record<string, unknown>): Item {
  return {
    id: "node-1",
    ino: 1,
    driveId: "drive-1",
    kind: "folder",
    name: "scratch",
    nameDisplay: "scratch",
    nameEncoding: "utf-8",
    pathBytes: "/home/admin/scratch",
    path: "/home/admin/scratch",
    etag: "etag-1",
    ctag: "ctag-1",
    stale: false,
    locked: false,
    held: false,
    lease: {
      holder: "",
      machine: "",
      holder_name: "",
      machine_name: "",
      chat_title: "",
      can_open_chat: false,
      purpose: "chat",
      since: "2026-09-09T10:12:00.000Z",
      expires_at: "2026-09-09T10:42:00.000Z",
      last_sync_at: "2026-09-09T10:14:48.000Z",
      mine: false,
      live: true,
      ...lease,
    },
    capabilities: { can_read: true } as Item["capabilities"],
  } as unknown as Item;
}

/** What the drive serves for a chat's folder held by the demo box: one uuid as
 *  holder and machine, the machine's name resolved, the chat's title resolved,
 *  and no holder name at all — the server resolves none. */
const boxHoldingAChat = () =>
  leased({
    holder: BOX_ID,
    machine: BOX_ID,
    machine_name: "demo-box",
    chat_id: CHAT_ID,
    chat_title: "Revenue model",
  });

function renderWithClient(ui: React.ReactElement) {
  return render(
    <QueryClientProvider client={createQueryClient()}>
      <MemoryRouter>{ui}</MemoryRouter>
    </QueryClientProvider>,
  );
}

/** The badge's whole rendered text, avatar included. */
function badgeText(): string {
  return screen.getByText(/In use/).closest(".alk-files-lease")?.textContent ?? "";
}

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  vi.setSystemTime(NOW);
  vi.stubGlobal(
    "fetch",
    vi.fn(
      async () =>
        new Response("[]", { status: 200, headers: { "content-type": "application/json" } }),
    ),
  );
});

describe("a folder a box holds for a chat", () => {
  it("names the chat and the machine once, and neither by id", () => {
    renderWithClient(<LeaseBadge item={boxHoldingAChat()} />);
    const text = badgeText();
    expect(text).toContain("In use by chat Revenue model");
    expect(text).not.toMatch(ANY_ID);
    // "by X on X" named one machine as if it were two parties.
    expect(text).not.toContain("demo-box on demo-box");
  });

  it("does not put a character of the uuid in front of the sentence", () => {
    renderWithClient(<LeaseBadge item={boxHoldingAChat()} />);
    // The avatar is the badge's first character; a uuid holder made it "8".
    expect(badgeText().startsWith("R")).toBe(true);
  });

  it("names the chat in the details pane's holder row", () => {
    renderWithClient(<LeaseFacetPane item={boxHoldingAChat()} />);
    const pane = screen.getByLabelText("Lease");
    expect(pane).toHaveTextContent("the chat Revenue model");
    expect(pane).toHaveTextContent("demo-box");
    expect(pane.textContent ?? "").not.toMatch(ANY_ID);
  });

  it("calls an untitled chat a chat rather than printing its id", () => {
    renderWithClient(<LeaseBadge item={leased({ chat_id: CHAT_ID, machine_name: "box-2" })} />);
    const text = badgeText();
    expect(text).toContain("In use by chat a chat");
    expect(text).not.toMatch(ANY_ID);
  });
});

describe("a folder a person holds", () => {
  it("names the person the server resolved, over the id the holder registered", () => {
    renderWithClient(
      <LeaseBadge
        item={leased({
          holder: "e0b5b6f2-0000-4000-8000-00000000abcd",
          holder_name: "Ana Ruiz",
          machine: "ana-mbp",
          purpose: "mount",
          chat_id: null,
        })}
      />,
    );
    const text = badgeText();
    expect(text).toContain("In use by Ana Ruiz");
    expect(text).not.toMatch(ANY_ID);
  });

  it("keeps the name a laptop registered itself under when it is not an id", () => {
    renderWithClient(
      <LeaseBadge item={leased({ holder: "Robin", machine: "MacBook Pro", chat_id: null })} />,
    );
    expect(badgeText()).toContain("In use by Robin");
  });
});

describe("a lease whose parties the drive cannot name", () => {
  it("says a neutral noun for each, never the id", () => {
    renderWithClient(<LeaseBadge item={leased({ holder: BOX_ID, machine: BOX_ID })} />);
    const text = badgeText();
    expect(text).toContain("In use by someone");
    expect(text).not.toMatch(ANY_ID);
  });

  it("says the machine once when the holder is the machine", () => {
    renderWithClient(
      <LeaseBadge
        item={leased({
          holder: BOX_ID,
          machine: BOX_ID,
          holder_name: "demo-box",
          machine_name: "demo-box",
        })}
      />,
    );
    expect(badgeText()).toContain("In use on demo-box");
  });
});

describe("my leases", () => {
  it("does not list a machine by the uuid a box registered itself under", async () => {
    const rows: LeaseRow[] = [
      {
        nodeId: "node-1",
        epoch: 1,
        machine: BOX_ID,
        purpose: "chat",
        since: "2026-09-09T10:12:00.000Z",
        expiresAt: "2026-09-09T10:42:00.000Z",
        lastSyncAt: "2026-09-09T10:14:48.000Z",
      },
    ];
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async () =>
          new Response(JSON.stringify(rows), {
            status: 200,
            headers: { "content-type": "application/json" },
          }),
      ),
    );
    renderWithClient(<MyLeases driveId="drive-1" />);
    const list = await screen.findByLabelText("My leases");
    expect(list).toHaveTextContent("the workspace machine");
    expect(list.textContent ?? "").not.toMatch(ANY_ID);
  });
});
