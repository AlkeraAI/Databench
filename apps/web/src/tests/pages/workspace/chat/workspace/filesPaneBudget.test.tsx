// What a chat's Files pane costs the server over a minute.
//
// With a box saving a file into the chat's folder every second, the pane made
// close to three hundred requests a minute: the event bridge and the pane each
// re-read the folder and the leased folder's item on every save and every
// lease report. Idle, it now asks nothing; busy, it re-reads each of those at
// most once per `MACHINE_REFRESH_MS`, and a person's own change (a rename) is
// still read at once.
//
// Driven through the real explorer and the real event bridge with `fetch`
// counted at the wire, on a fake clock.

import { QueryClientProvider, type QueryClient } from "@tanstack/react-query";
import { act, cleanup, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { createInvalidationScheduler, type RealtimeEventFrame } from "@/api/events/eventMap";
import { publishFrame, resetFrameBus } from "@/api/events/frameBus";
import { useRealtimeStatus } from "@/api/events/status";
import type { Item } from "@/api/files";
import { createQueryClient } from "@/api/queryClient";
import { MACHINE_REFRESH_MS } from "@/lib/limits";
import { tabKindFor, type WorkspaceCtx } from "@/pages/workspace/chat/workspace/tabKinds";
import { useWorkspaceStore } from "@/pages/workspace/chat/workspace/workspaceStore";

import "@/pages/workspace/chat/workspace/FilesTab";

const DRIVE = "drv_1";
const CHAT_NODE = "nd_chat";
const ROOT = "nd_root";
const TICKS = "nd_ticks";
const CTX: WorkspaceCtx = { chatId: "cht_1", driveId: DRIVE, rootNodeId: ROOT };

function item(over: Partial<Item> & { id: string; name: string }): Item {
  return {
    ino: 1,
    driveId: DRIVE,
    kind: "file",
    nameDisplay: over.name,
    nameEncoding: "utf-8",
    nameFlags: { windows_safe: true, macos_safe: true, display_warning: false },
    pathBytes: "",
    parentId: ROOT,
    path: null,
    etag: "1",
    ctag: "1",
    attrs: { mtime: "2026-09-01T10:00:00Z" },
    file: null,
    lease: null,
    live: null,
    stale: false,
    locked: false,
    capabilities: { can_write: true },
    trashed: false,
    ...over,
  } as unknown as Item;
}

const items: Record<string, Item> = {
  [CHAT_NODE]: item({
    id: CHAT_NODE,
    name: "c.alkerachat",
    kind: "folder",
    parentId: "nd_home",
    object: { id: "o", type: "chat", title: "Q3 review", metadata: { files_node_id: ROOT } } as Item["object"],
    lease: {
      holder: "Dana",
      machine: "box-1",
      machine_name: "box",
      live: true,
      inbound: true,
      pending: 0,
      node_id: CHAT_NODE,
      expires_at: new Date(Date.now() + 3_600_000).toISOString(),
    },
  } as Partial<Item> & { id: string; name: string }),
  [ROOT]: item({ id: ROOT, name: "scratch", kind: "folder", parentId: CHAT_NODE }),
};
let ticksEtag = 1;

let calls: string[] = [];

function stubWire(): void {
  calls = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = new URL(input instanceof Request ? input.url : String(input), "http://x");
      calls.push(url.pathname);
      const json = (body: unknown, status = 200) =>
        new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
      if (url.pathname.endsWith(`/items/${ROOT}/children`)) {
        const ticks = item({ id: TICKS, name: "ticks.log", etag: String(ticksEtag) });
        return json({ value: [ticks], nextMarker: null });
      }
      if (url.pathname.endsWith("/children")) return json({ value: [], nextMarker: null });
      const one = /\/items\/([^/?]+)$/.exec(url.pathname);
      if (one) {
        const found = items[decodeURIComponent(one[1] ?? "")];
        return found ? json(found) : json({ code: "files.not_found", message: "no" }, 404);
      }
      return json({});
    }),
  );
}

function mount(client: QueryClient): void {
  const kind = tabKindFor("files");
  if (kind === undefined) throw new Error("the `files` tab kind was never registered");
  const Component = kind.Component;
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <Component tab={{ id: "files", kind: "files", name: "Files" }} ctx={CTX} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

/** One save by the box: the drive's node frame for the file, and the
 *  holder's report on its lease. */
function save(n: number): RealtimeEventFrame[] {
  ticksEtag = n + 2;
  return [
    {
      type: "file_node.changed",
      entity: "file_node",
      entity_id: TICKS,
      version: ticksEtag,
      org_id: "org",
      drive_id: DRIVE,
      parent_id: ROOT,
      reason: "live_saved",
    } as RealtimeEventFrame,
    {
      type: "file_lease.changed",
      entity: "file_lease",
      entity_id: CHAT_NODE,
      version: n + 2,
      org_id: "org",
      drive_id: DRIVE,
      lease_node_id: CHAT_NODE,
    } as RealtimeEventFrame,
  ];
}

const deliver = (scheduler: ReturnType<typeof createInvalidationScheduler>, frames: RealtimeEventFrame[]) =>
  act(() => {
    for (const frame of frames) {
      scheduler.push(frame);
      publishFrame(frame);
    }
    scheduler.flush();
  });

const listings = () => calls.filter((p) => p.endsWith(`/items/${ROOT}/children`)).length;

beforeEach(() => {
  ticksEtag = 1;
  stubWire();
  useRealtimeStatus.setState({ sse: "connected" });
  useWorkspaceStore.setState({ chats: {} });
});

afterEach(() => {
  cleanup();
  resetFrameBus();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("a chat's Files pane over a minute", () => {
  it("asks nothing while nothing happens", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    mount(createQueryClient({ retry: false }));
    await screen.findByText("ticks.log");
    await vi.advanceTimersByTimeAsync(2_000);
    const opened = calls.length;
    await vi.advanceTimersByTimeAsync(60_000);
    expect(calls.slice(opened)).toEqual([]);
  });

  it("re-reads the folder and the leased folder on the throttle while a box saves every second", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const client = createQueryClient({ retry: false });
    const scheduler = createInvalidationScheduler(client, { debounceMs: 0 });
    mount(client);
    await screen.findByText("ticks.log");
    await vi.waitFor(() => expect(calls.some((p) => p.endsWith(`/items/${CHAT_NODE}`))).toBe(true));
    await vi.advanceTimersByTimeAsync(2_000);
    const opened = calls.length;
    for (let n = 0; n < 60; n += 1) {
      deliver(scheduler, save(n));
      await vi.advanceTimersByTimeAsync(1_000);
    }
    const minute = calls.slice(opened);
    const perWindow = Math.ceil(60_000 / MACHINE_REFRESH_MS) + 1;
    const folder = minute.filter((p) => p.endsWith(`/items/${ROOT}/children`));
    const leased = minute.filter((p) => p.endsWith(`/items/${CHAT_NODE}`));
    // The saves are shown: the folder is read during the burst, not after it.
    expect(folder.length).toBeGreaterThan(1);
    expect(folder.length).toBeLessThanOrEqual(perWindow);
    expect(leased.length).toBeLessThanOrEqual(perWindow);
    // Nothing else the pane holds follows a box's saves.
    expect(minute.filter((p) => !folder.includes(p) && !leased.includes(p))).toEqual([]);
    expect(minute.length).toBeLessThanOrEqual(2 * perWindow);
    scheduler.dispose();
  });

  it("still reads a person's change to the folder at once, mid-burst", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const client = createQueryClient({ retry: false });
    const scheduler = createInvalidationScheduler(client, { debounceMs: 0 });
    mount(client);
    await screen.findByText("ticks.log");
    await vi.waitFor(() => expect(calls.some((p) => p.endsWith(`/items/${CHAT_NODE}`))).toBe(true));
    deliver(scheduler, save(0));
    await vi.advanceTimersByTimeAsync(1_000);
    deliver(scheduler, save(1));
    await vi.advanceTimersByTimeAsync(1_000);
    const before = listings();
    // A rename raises a node frame with no reason: read within the
    // coalescing window, not after the machine's.
    deliver(scheduler, [
      {
        type: "file_node.changed",
        entity: "file_node",
        entity_id: "nd_other",
        version: 5,
        org_id: "org",
        drive_id: DRIVE,
        parent_id: ROOT,
      } as RealtimeEventFrame,
    ]);
    await vi.advanceTimersByTimeAsync(1_000);
    expect(listings()).toBeGreaterThan(before);
    scheduler.dispose();
  });
});
