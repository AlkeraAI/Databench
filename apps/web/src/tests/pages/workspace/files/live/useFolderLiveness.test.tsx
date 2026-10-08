// Clicking into a leased folder mounts its contents live.
//
// Every assertion here counts REAL refetches: each folder in play is a mounted
// query with a counting `queryFn`, so "invalidates exactly that folder" means
// that folder's reader ran again and its sibling's did not. A frame for a
// folder nobody has open must cost nothing at all, twenty frames in a quarter
// second must cost one pass, and with the stream down the poll — not the
// stream — has to be what keeps the open folders moving.

import { useEffect } from "react";
import { QueryClientProvider, useQuery } from "@tanstack/react-query";
import { act, cleanup, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { RealtimeEventFrame } from "@/api/events/eventMap";
import { publishFrame, resetFrameBus } from "@/api/events/frameBus";
import { resetRealtimeStatus, useRealtimeStatus } from "@/api/events/status";
import type { Item } from "@/api/files";
import { keys } from "@/api/keys";
import { createQueryClient } from "@/api/queryClient";
import { gatedFetch, resetReadGate } from "@/api/readGate";
import {
  countOnBox,
  LIVE_POLL_MS,
  MAX_HELD_LEASE_FRAMES,
  useFolderLiveness,
} from "@/pages/workspace/files/live/useFolderLiveness";
import { filesLiveFact } from "@/tests/fixtures/statusFacts";

const ORG = "22222222-2222-2222-2222-222222222222";
const DRIVE = "drive-1";
const ROOT = "root-node";
const OPEN_CHILD = "open-child";
const CLOSED_CHILD = "closed-child";
/** The leased folder the listed one sits under: a chat's folder, say. */
const LEASED_ABOVE = "chat-folder";

const nodeFrame = (
  parentId: string | undefined,
  entityId = "written-file",
): RealtimeEventFrame => ({
  type: "file_node.changed",
  entity: "file_node",
  entity_id: entityId,
  version: 1,
  org_id: ORG,
  drive_id: DRIVE,
  ...(parentId === undefined ? {} : { parent_id: parentId }),
});

const leaseFrame = (leaseNodeId: string): RealtimeEventFrame => ({
  type: "file_lease.changed",
  entity: "file_lease",
  entity_id: leaseNodeId,
  version: 4,
  org_id: ORG,
  drive_id: DRIVE,
  lease_node_id: leaseNodeId,
});

const leasedRoot = (over: Partial<Item> = {}): Item =>
  ({
    id: ROOT,
    name: "chat files",
    kind: "folder",
    etag: "e1",
    ctag: "c1",
    stale: false,
    locked: false,
    held: false,
    shared: false,
    starred: false,
    trashed: false,
    lease: {
      holder: "Ada",
      machine: "the box",
      live: true,
      status: filesLiveFact("live"),
      pending: 2,
      since: "2026-09-16T10:00:00Z",
      last_sync_at: "2026-09-16T10:05:00Z",
      expires_at: "2100-01-01T00:00:00Z",
    },
    ...over,
  }) as unknown as Item;

interface Counters {
  root: number;
  open: number;
  closed: number;
}

/** A harness whose folders are real mounted queries, so an invalidation that
 *  does not reach one shows up as a refetch that did not happen. */
function Harness({
  counters,
  openFolderIds,
  onView,
  holderReady,
}: {
  counters: Counters;
  openFolderIds: string[];
  onView?: (view: ReturnType<typeof useFolderLiveness>) => void;
  holderReady?: boolean;
}) {
  useQuery({
    queryKey: keys.files.childrenOf(DRIVE, OPEN_CHILD),
    queryFn: () => {
      counters.open += 1;
      return [];
    },
  });
  useQuery({
    queryKey: keys.files.childrenOf(DRIVE, CLOSED_CHILD),
    queryFn: () => {
      counters.closed += 1;
      return [];
    },
  });
  const view = useFolderLiveness(DRIVE, ROOT, openFolderIds, 0, holderReady);
  useEffect(() => {
    onView?.(view);
  });
  return <span data-testid="state">{view.liveness.state}</span>;
}

async function mount(
  openFolderIds: string[],
  rootItem: Item = leasedRoot(),
  /** Holds the root's first read until it settles, when a case needs frames
   *  to land before the hook knows which lease it lives under. */
  firstRootRead?: Promise<void>,
) {
  const counters: Counters = { root: 0, open: 0, closed: 0 };
  // The root's own item is the one read that goes through the real files hook,
  // so it is counted where it really happens: at the network.
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
      if (url.includes(ROOT)) {
        counters.root += 1;
        if (counters.root === 1 && firstRootRead !== undefined) await firstRootRead;
      }
      return new Response(JSON.stringify(rootItem), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    }),
  );
  const qc = createQueryClient({ retry: false });
  let view: ReturnType<typeof useFolderLiveness> | undefined;
  const rendered = render(
    <QueryClientProvider client={qc}>
      <Harness counters={counters} openFolderIds={openFolderIds} onView={(v) => (view = v)} />
    </QueryClientProvider>,
  );
  await act(async () => {
    await vi.advanceTimersByTimeAsync(0);
  });
  return { counters, qc, rendered, view: () => view };
}

/** Arm the real read gate the way the server arms it: one gated read answered
 *  429. Nothing here reaches into the gate's own state, so the test still fails
 *  if the poll stops consulting it. */
async function armReadPause(retryAfter: string): Promise<void> {
  const stubbed = globalThis.fetch;
  globalThis.fetch = (() =>
    Promise.resolve(
      new Response("{}", { status: 429, headers: { "retry-after": retryAfter } }),
    )) as typeof fetch;
  await gatedFetch("https://example.test/refused");
  globalThis.fetch = stubbed;
}

beforeEach(() => {
  vi.useFakeTimers();
  resetRealtimeStatus();
  resetReadGate();
  // The stream is delivering unless a test says otherwise.
  useRealtimeStatus.getState().setSse("connected");
});

afterEach(() => {
  resetFrameBus();
  resetReadGate();
  cleanup();
  vi.unstubAllGlobals();
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("useFolderLiveness", () => {
  it("refetches exactly the open folder a frame names", async () => {
    const { counters } = await mount([ROOT, OPEN_CHILD]);
    const before = { ...counters };

    act(() => publishFrame(nodeFrame(OPEN_CHILD)));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(300);
    });

    expect(counters.open).toBe(before.open + 1);
    expect(counters.closed).toBe(before.closed);
    expect(counters.root).toBe(before.root);
  });

  it("ignores a frame for a folder nobody has open", async () => {
    const { counters } = await mount([ROOT, OPEN_CHILD]);
    const before = { ...counters };

    act(() => publishFrame(nodeFrame(CLOSED_CHILD)));
    // A frame with no parent at all is equally not this folder's business.
    act(() => publishFrame(nodeFrame(undefined)));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(300);
    });

    expect(counters).toEqual(before);
  });

  it("folds a burst of frames for one folder into a single refetch", async () => {
    const { counters } = await mount([ROOT, OPEN_CHILD]);
    const before = { ...counters };

    act(() => {
      for (let i = 0; i < 20; i += 1) publishFrame(nodeFrame(OPEN_CHILD, `file-${i}`));
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(300);
    });

    expect(counters.open).toBe(before.open + 1);
  });

  it("refreshes the leased folder's facet and every open listing on a lease frame", async () => {
    // The in-flight plane rides the root's item AND the rows under it: the
    // chip on a row is the plane's word about that row, and the states that
    // move without a save (uploading reported, left on the machine, a row
    // settled) are announced only by the lease frame. A folder nobody has
    // open is still not refetched — a deep write costs nothing.
    const { counters } = await mount([ROOT, OPEN_CHILD]);
    const before = { ...counters };

    act(() => publishFrame(leaseFrame(ROOT)));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(300);
    });

    expect(counters.root).toBe(before.root + 1);
    expect(counters.open).toBe(before.open + 1);
    expect(counters.closed).toBe(before.closed);
  });

  it("refreshes a subfolder of a leased folder on the frames of the lease above it", async () => {
    // The Files page lists whatever folder the reader walked into, and the
    // lease a box takes is on the chat's folder several levels up. The facet
    // every node under it carries names that folder, and it is the one the
    // server's lease frames name, so a listed subfolder hears them too.
    const { counters } = await mount(
      [ROOT, OPEN_CHILD],
      leasedRoot({ lease: { ...leasedRoot().lease, node_id: LEASED_ABOVE } } as Partial<Item>),
    );
    const before = { ...counters };

    act(() => publishFrame(leaseFrame(LEASED_ABOVE)));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(300);
    });

    expect(counters.root).toBe(before.root + 1);
    expect(counters.open).toBe(before.open + 1);
    expect(counters.closed).toBe(before.closed);
  });

  it("re-reads every open folder under the lease on a subtree frame", async () => {
    // A batch that touched more folders than the server names one by one says
    // so on the lease frame instead, and no node frame follows. Every open
    // listing under the lease may have moved; a folder nobody has open has not
    // been read, so it costs nothing.
    const { counters } = await mount(
      [ROOT, OPEN_CHILD],
      leasedRoot({ lease: { ...leasedRoot().lease, node_id: LEASED_ABOVE } } as Partial<Item>),
    );
    const before = { ...counters };

    act(() => publishFrame({ ...leaseFrame(LEASED_ABOVE), subtree: true }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(300);
    });

    expect(counters.root).toBe(before.root + 1);
    expect(counters.open).toBe(before.open + 1);
    expect(counters.closed).toBe(before.closed);
  });

  it("still hears a lease taken on the listed folder itself", async () => {
    // A lease can be taken on the folder on screen while its facet still names
    // the one above (or nothing): the frame for it is this listing's news.
    const { counters } = await mount(
      [ROOT, OPEN_CHILD],
      leasedRoot({ lease: { ...leasedRoot().lease, node_id: LEASED_ABOVE } } as Partial<Item>),
    );
    const before = { ...counters };

    act(() => publishFrame(leaseFrame(ROOT)));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(300);
    });

    expect(counters.root).toBe(before.root + 1);
    expect(counters.open).toBe(before.open + 1);
  });

  describe("a lease frame that lands before the root has been read", () => {
    const underChatFolder = (): Item =>
      leasedRoot({ lease: { ...leasedRoot().lease, node_id: LEASED_ABOVE } } as Partial<Item>);

    function gate(): { held: Promise<void>; release: () => void } {
      let release = (): void => undefined;
      const held = new Promise<void>((resolve) => {
        release = resolve;
      });
      return { held, release };
    }

    async function settle(): Promise<void> {
      await act(async () => {
        await vi.advanceTimersByTimeAsync(300);
      });
    }

    it("is applied once the root's read says the lease is its own", async () => {
      // Only the root's facet can say the lease above it is this listing's, so
      // until it lands the frame cannot be judged; dropping it left the rows
      // on the first read until the next save.
      const { held, release } = gate();
      const { counters } = await mount([ROOT, OPEN_CHILD], underChatFolder(), held);
      const before = { ...counters };

      act(() => publishFrame(leaseFrame(LEASED_ABOVE)));
      await settle();
      expect(counters.open).toBe(before.open);

      release();
      await settle();

      expect(counters.root).toBe(before.root + 1);
      expect(counters.open).toBe(before.open + 1);
      expect(counters.closed).toBe(before.closed);
    });

    it("is dropped once the root's read says the lease is somebody else's", async () => {
      const { held, release } = gate();
      const { counters } = await mount([ROOT, OPEN_CHILD], underChatFolder(), held);
      const before = { ...counters };

      act(() => publishFrame(leaseFrame("another-root")));
      release();
      await settle();

      // The root's one held read, and nothing a replay would have re-read.
      expect(counters).toEqual(before);
    });

    it("keeps only the latest frames per leased node, a bounded few", async () => {
      // A burst of other leases' frames pushes the oldest held frame out, and a
      // repeat of a node's frame counts as its latest, not a second entry.
      const { held, release } = gate();
      const { counters } = await mount([ROOT, OPEN_CHILD], underChatFolder(), held);
      const before = { ...counters };

      act(() => {
        publishFrame(leaseFrame(LEASED_ABOVE));
        for (let i = 0; i < MAX_HELD_LEASE_FRAMES; i += 1) publishFrame(leaseFrame(`other-${i}`));
      });
      release();
      await settle();
      expect(counters.open).toBe(before.open);
    });

    it("keeps a node's frame that was repeated after the burst", async () => {
      const { held, release } = gate();
      const { counters } = await mount([ROOT, OPEN_CHILD], underChatFolder(), held);
      const before = { ...counters };

      act(() => {
        publishFrame(leaseFrame(LEASED_ABOVE));
        for (let i = 0; i < MAX_HELD_LEASE_FRAMES - 1; i += 1) {
          publishFrame(leaseFrame(`other-${i}`));
        }
        publishFrame(leaseFrame(LEASED_ABOVE));
        publishFrame(leaseFrame("one-more"));
      });
      release();
      await settle();
      expect(counters.open).toBe(before.open + 1);
    });
  });

  it("ignores a lease frame for somebody else's folder", async () => {
    const { counters } = await mount([ROOT, OPEN_CHILD]);
    const before = { ...counters };

    act(() => publishFrame(leaseFrame("another-root")));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(300);
    });

    expect(counters).toEqual(before);
  });

  it("reports the stream down and polls the open folders while it is", async () => {
    const { counters, view } = await mount([ROOT, OPEN_CHILD]);
    expect(view()?.streamDown).toBe(false);

    await act(async () => {
      useRealtimeStatus.getState().setSse("down");
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(view()?.streamDown).toBe(true);

    const before = { ...counters };
    await act(async () => {
      await vi.advanceTimersByTimeAsync(LIVE_POLL_MS);
    });

    expect(counters.open).toBe(before.open + 1);
    expect(counters.root).toBe(before.root + 1);
    expect(counters.closed).toBe(before.closed);
  });

  it("stops polling while the server is refusing reads, and resumes after", async () => {
    // The poll is an interval, not a query, so nothing on the query layer holds
    // it back when the limiter refuses — and with the stream down it is the
    // drive's busiest read. It reads the gate itself.
    const { counters } = await mount([ROOT, OPEN_CHILD]);
    await act(async () => {
      useRealtimeStatus.getState().setSse("down");
      await vi.advanceTimersByTimeAsync(0);
    });

    await armReadPause("31");
    const before = { ...counters };
    await act(async () => {
      await vi.advanceTimersByTimeAsync(LIVE_POLL_MS * 2);
    });
    expect(counters).toEqual(before);

    // Past the pause the very next tick re-reads everything: nothing was lost
    // by skipping the ticks inside it.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(LIVE_POLL_MS);
    });
    expect(counters.root).toBe(before.root + 1);
    expect(counters.open).toBe(before.open + 1);
    expect(counters.closed).toBe(before.closed);
  });

  it("does not poll while the stream is delivering", async () => {
    const { counters } = await mount([ROOT, OPEN_CHILD]);
    const before = { ...counters };

    await act(async () => {
      await vi.advanceTimersByTimeAsync(LIVE_POLL_MS * 3);
    });

    expect(counters).toEqual(before);
  });

  it("refreshes the root and the open folders on demand", async () => {
    const { counters, view } = await mount([ROOT, OPEN_CHILD]);
    const before = { ...counters };

    await act(async () => {
      view()?.refresh();
      await vi.advanceTimersByTimeAsync(0);
    });

    expect(counters.root).toBe(before.root + 1);
    expect(counters.open).toBe(before.open + 1);
    expect(counters.closed).toBe(before.closed);
  });

  it("reads the root's lease as the liveness it reports", async () => {
    const live = await mount([ROOT]);
    expect(live.rendered.getByTestId("state").textContent).toBe("live");
    cleanup();

    const settled = await mount([ROOT], leasedRoot({ lease: null }));
    expect(settled.rendered.getByTestId("state").textContent).toBe("persisted");
  });
});

describe("useFolderLiveness after something that may have left the root stale", () => {
  const setOnline = (online: boolean): void => {
    vi.stubGlobal("navigator", { ...navigator, onLine: online });
    window.dispatchEvent(new Event(online ? "online" : "offline"));
  };

  it("re-reads the root once when the stream comes back, even from a brief drop", async () => {
    // A clean resume replays the events the stream missed, but a lease that
    // went offline and came back while nothing was saved emits none, so the
    // root is read once on the way back.
    const { counters } = await mount([ROOT]);
    await act(async () => {
      useRealtimeStatus.getState().setSse("reconnecting");
      await vi.advanceTimersByTimeAsync(0);
    });
    const before = counters.root;
    await act(async () => {
      useRealtimeStatus.getState().setSse("connected");
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(counters.root).toBe(before + 1);
    // Once: a stream that stays up is not read again.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(LIVE_POLL_MS * 2);
    });
    expect(counters.root).toBe(before + 1);
  });

  it("is down the moment the browser says it is offline, and re-reads when it is back", async () => {
    const { counters, view } = await mount([ROOT]);
    await act(async () => {
      setOnline(false);
      await vi.advanceTimersByTimeAsync(0);
    });
    // The stream itself still says connected: a dead socket is noticed only
    // when its stall timer fires, long after the network went.
    expect(useRealtimeStatus.getState().sse).toBe("connected");
    expect(view()?.streamDown).toBe(true);
    expect(view()?.offline).toBe(true);
    const before = counters.root;
    await act(async () => {
      setOnline(true);
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(view()?.streamDown).toBe(false);
    expect(view()?.offline).toBe(false);
    expect(counters.root).toBe(before + 1);
  });

  it("keeps re-reading a root whose lease reads offline, on the slow poll", async () => {
    const offline = leasedRoot();
    const lease = (offline as unknown as { lease: Record<string, unknown> }).lease;
    lease.served = "offline";
    lease.status = filesLiveFact("paused_silent");
    const { counters, view } = await mount([ROOT], offline);
    expect(view()?.liveness).toMatchObject({ state: "persisted", reason: "offline" });
    const before = counters.root;
    await act(async () => {
      await vi.advanceTimersByTimeAsync(LIVE_POLL_MS);
    });
    expect(counters.root).toBe(before + 1);
  });

  it("re-reads the root when the machine holding it changes reachability", async () => {
    const counters: Counters = { root: 0, open: 0, closed: 0 };
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
        if (url.includes(ROOT)) counters.root += 1;
        return new Response(JSON.stringify(leasedRoot()), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      }),
    );
    const qc = createQueryClient({ retry: false });
    const tree = (ready: boolean) => (
      <QueryClientProvider client={qc}>
        <Harness counters={counters} openFolderIds={[ROOT]} holderReady={ready} />
      </QueryClientProvider>
    );
    const rendered = render(tree(true));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    const before = counters.root;
    rendered.rerender(tree(false));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(counters.root).toBe(before + 1);
    rendered.rerender(tree(true));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(counters.root).toBe(before + 2);
  });
});

describe("countOnBox", () => {
  it("counts only the rows the machine is holding back", () => {
    const row = (state: string | null): Item =>
      ({ id: state ?? "none", live: state === null ? null : { state } }) as unknown as Item;
    expect(
      countOnBox([
        row("on_box"),
        row("deferred"),
        row("uploading"),
        row("writing"),
        row("inbound"),
        row(null),
      ]),
    ).toBe(2);
  });
});
