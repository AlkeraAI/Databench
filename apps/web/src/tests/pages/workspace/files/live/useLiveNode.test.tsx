// One node open in a tab, kept current by its own frames and nothing else.
//
// Counted at the network, so "re-fetches only when the node's frame arrives"
// means a request really went out for this node and did not for a neighbour's
// frame. The other half is `gone`: a node that has been trashed, or that the
// server no longer has, has to say so once — and settle, not retry a decision.

import { QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { RealtimeEventFrame } from "@/api/events/eventMap";
import { publishFrame, resetFrameBus } from "@/api/events/frameBus";
import { resetRealtimeStatus, useRealtimeStatus } from "@/api/events/status";
import type { Item } from "@/api/files";
import { createQueryClient } from "@/api/queryClient";
import { useLiveNode } from "@/pages/workspace/files/live/useLiveNode";

const ORG = "22222222-2222-2222-2222-222222222222";
const DRIVE = "drive-1";
const NODE = "node-1";

const item = (over: Partial<Item> = {}): Item =>
  ({
    id: NODE,
    name: "notes.md",
    kind: "file",
    etag: "etag-1",
    ctag: "ctag-1",
    stale: false,
    locked: false,
    held: false,
    shared: false,
    starred: false,
    trashed: false,
    ...over,
  }) as unknown as Item;

const nodeFrame = (entityId: string): RealtimeEventFrame => ({
  type: "file_node.changed",
  entity: "file_node",
  entity_id: entityId,
  version: 1,
  org_id: ORG,
  drive_id: DRIVE,
});

const leaseFrame = (leaseNodeId: string): RealtimeEventFrame => ({
  type: "file_lease.changed",
  entity: "file_lease",
  entity_id: leaseNodeId,
  version: 2,
  org_id: ORG,
  drive_id: DRIVE,
  lease_node_id: leaseNodeId,
});

interface Answer {
  status: number;
  body: unknown;
}

function Harness({ onView }: { onView: (view: ReturnType<typeof useLiveNode>) => void }) {
  const view = useLiveNode(DRIVE, NODE);
  onView(view);
  return <span data-testid="gone">{String(view.gone)}</span>;
}

async function mount(answers: Answer[]) {
  const calls: string[] = [];
  let index = 0;
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
      calls.push(url);
      const answer = answers[Math.min(index, answers.length - 1)];
      index += 1;
      return new Response(JSON.stringify(answer.body), {
        status: answer.status,
        headers: { "content-type": "application/json" },
      });
    }),
  );
  const qc = createQueryClient({ retry: false });
  let view: ReturnType<typeof useLiveNode> | undefined;
  const rendered = render(
    <QueryClientProvider client={qc}>
      <Harness onView={(v) => (view = v)} />
    </QueryClientProvider>,
  );
  await act(async () => {
    await vi.advanceTimersByTimeAsync(0);
  });
  return { calls, rendered, view: () => view };
}

beforeEach(() => {
  vi.useFakeTimers();
  resetRealtimeStatus();
  useRealtimeStatus.getState().setSse("connected");
});

afterEach(() => {
  resetFrameBus();
  cleanup();
  vi.unstubAllGlobals();
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("useLiveNode", () => {
  it("re-reads the node when its own frame arrives", async () => {
    const { calls } = await mount([{ status: 200, body: item() }]);
    const before = calls.length;

    act(() => publishFrame(nodeFrame(NODE)));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(50);
    });

    expect(calls.length).toBe(before + 1);
  });

  it("does not re-read for another node's frame", async () => {
    const { calls } = await mount([{ status: 200, body: item() }]);
    const before = calls.length;

    act(() => publishFrame(nodeFrame("somebody-else")));
    act(() => publishFrame(leaseFrame("another-root")));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(50);
    });

    expect(calls.length).toBe(before);
  });

  it("re-reads when the lease frame names this node", async () => {
    const { calls } = await mount([{ status: 200, body: item() }]);
    const before = calls.length;

    act(() => publishFrame(leaseFrame(NODE)));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(50);
    });

    expect(calls.length).toBe(before + 1);
  });

  it("reports the node's etag and what the machine is doing to it", async () => {
    const { view } = await mount([
      {
        status: 200,
        body: item({
          etag: "etag-9",
          live: { state: "uploading", box_size: 2048 },
        } as Partial<Item>),
      },
    ]);
    expect(view()?.etag).toBe("etag-9");
    expect(view()?.live?.state).toBe("uploading");
    expect(view()?.gone).toBe(false);
  });

  it("has no live facet when the node is not being written", async () => {
    const { view } = await mount([{ status: 200, body: item() }]);
    expect(view()?.live).toBeNull();
  });

  it("is gone when the node is in the trash", async () => {
    const { rendered, view } = await mount([{ status: 200, body: item({ trashed: true }) }]);
    expect(view()?.gone).toBe(true);
    expect(rendered.getByTestId("gone").textContent).toBe("true");
  });

  it("is gone after ONE 404 — the answer does not become false by asking again", async () => {
    const { calls, view } = await mount([
      { status: 404, body: { detail: { code: "files.not_found", message: "gone" } } },
    ]);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5_000);
    });

    expect(view()?.gone).toBe(true);
    expect(calls.length).toBe(1);
  });
});
