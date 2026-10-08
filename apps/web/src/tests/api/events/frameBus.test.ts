// The second reader of the event stream: the bus a surface subscribes to when it
// needs the frame itself, not just the cache slot the frame invalidates.
//
// Three things are proven here. The bus delivers to the subscribers whose
// predicate accepts a frame and to nobody else, survives a listener that throws
// and a listener that unsubscribes mid-pass, and stops delivering once
// unsubscribed. The realtime bridge really feeds it — a frame arriving on the
// scripted stream reaches a subscriber, and reaches it BEFORE the cache pass
// that follows. And the lease frame the bus exists for maps to the leased
// folder's own item, however the server spelled the id.

import { createElement } from "react";
import { QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { CurrentUser } from "@/api/auth";
import { EVENT_KEYS, type RealtimeEventFrame } from "@/api/events/eventMap";
import { publishFrame, resetFrameBus, subscribeFrames } from "@/api/events/frameBus";
import { RealtimeBridge } from "@/api/events/RealtimeBridge";
import { resetRealtimeStatus } from "@/api/events/status";
import { keys } from "@/api/keys";
import { createQueryClient } from "@/api/queryClient";

import { advance, openStream, scriptedFetch } from "./fakeSse";

const ORG = "22222222-2222-2222-2222-222222222222";

const USER = {
  id: "11111111-1111-1111-1111-111111111111",
  email: "ada@example.com",
  first_name: "Ada",
  last_name: "Lovelace",
  display_name: "Ada Lovelace",
  org_team_id: ORG,
  org_name: "Northwind Labs",
  org_role: "member",
  membership_count: 1,
  has_password: true,
  mfa_enabled: false,
  platform_role: null,
  platform_role_display: null,
  email_verified_at: "2026-07-30T00:00:00Z",
  email_verification_required: false,
  email_verification_deadline: null,
  verification_resend_available_at: null,
  created_at: "2026-07-30T00:00:00Z",
} satisfies CurrentUser;

const nodeFrame = (entity_id: string, extra: Record<string, string> = {}): RealtimeEventFrame => ({
  type: "file_node.changed",
  entity: "file_node",
  entity_id,
  version: 1,
  org_id: ORG,
  ...extra,
});

afterEach(() => {
  resetFrameBus();
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("the frame bus", () => {
  it("delivers a frame to the subscribers whose predicate accepts it, and to nobody else", () => {
    const mine: string[] = [];
    const theirs: string[] = [];
    subscribeFrames(
      (f) => f.entity_id === "node-a",
      (f) => mine.push(f.entity_id),
    );
    subscribeFrames(
      (f) => f.entity_id === "node-b",
      (f) => theirs.push(f.entity_id),
    );

    publishFrame(nodeFrame("node-a"));
    publishFrame(nodeFrame("node-c"));

    expect(mine).toEqual(["node-a"]);
    expect(theirs).toEqual([]);
  });

  it("stops delivering once unsubscribed", () => {
    const seen: string[] = [];
    const off = subscribeFrames(
      () => true,
      (f) => seen.push(f.entity_id),
    );
    publishFrame(nodeFrame("node-a"));
    off();
    publishFrame(nodeFrame("node-b"));
    expect(seen).toEqual(["node-a"]);
  });

  it("keeps delivering to the others when one listener throws", () => {
    const seen: string[] = [];
    subscribeFrames(
      () => true,
      () => {
        throw new Error("a broken surface");
      },
    );
    subscribeFrames(
      () => true,
      (f) => seen.push(f.entity_id),
    );
    // A predicate is the subscriber's code too, and may throw just as easily.
    subscribeFrames(
      () => {
        throw new Error("a broken predicate");
      },
      () => seen.push("never"),
    );

    expect(() => publishFrame(nodeFrame("node-a"))).not.toThrow();
    expect(seen).toEqual(["node-a"]);
  });

  it("does not call a subscriber another listener removed during the same frame", () => {
    const seen: string[] = [];
    let off = (): void => undefined;
    subscribeFrames(
      () => true,
      () => off(),
    );
    off = subscribeFrames(
      () => true,
      () => seen.push("second"),
    );

    publishFrame(nodeFrame("node-a"));
    expect(seen).toEqual([]);
  });
});

describe("the bridge feeding the bus", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.spyOn(Math, "random").mockReturnValue(0);
    resetRealtimeStatus();
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async () =>
          new Response(JSON.stringify(USER), {
            status: 200,
            headers: { "content-type": "application/json" },
          }),
      ),
    );
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("hands a frame off the live stream to the bus before it invalidates the cache", async () => {
    const script = scriptedFetch();
    const stream = openStream();
    script.answerStream(stream);

    const qc = createQueryClient({ retry: false });
    qc.setQueryData(keys.auth.me, USER);
    // A settled entry under the key the frame names: it goes stale only when the
    // cache pass runs, which is what orders the two against each other.
    qc.setQueryData(keys.files.item("node-a"), { id: "node-a" });

    const order: string[] = [];
    subscribeFrames(
      (f) => f.entity_id === "node-a",
      () => {
        const state = qc.getQueryState(keys.files.item("node-a"));
        order.push(state?.isInvalidated === true ? "bus-after-cache" : "bus-before-cache");
      },
    );

    render(
      createElement(
        QueryClientProvider,
        { client: qc },
        createElement(RealtimeBridge, { fetch: script.fetch }),
      ),
    );
    await act(async () => {
      await advance(0);
    });

    await act(async () => {
      stream.push(
        `id: 1\nevent: file_node.changed\ndata: ${JSON.stringify(nodeFrame("node-a"))}\n\n`,
      );
      await advance(300);
    });

    expect(order).toEqual(["bus-before-cache"]);
    expect(qc.getQueryState(keys.files.item("node-a"))?.isInvalidated).toBe(true);
  });

  it("does not publish a frame the parser rejected", async () => {
    const script = scriptedFetch();
    const stream = openStream();
    script.answerStream(stream);

    const qc = createQueryClient({ retry: false });
    qc.setQueryData(keys.auth.me, USER);

    const seen: RealtimeEventFrame[] = [];
    subscribeFrames(
      () => true,
      (f) => seen.push(f),
    );

    render(
      createElement(
        QueryClientProvider,
        { client: qc },
        createElement(RealtimeBridge, { fetch: script.fetch }),
      ),
    );
    await act(async () => {
      await advance(0);
    });

    await act(async () => {
      // A type this build does not know, and a body that is not JSON at all.
      stream.push(`id: 1\nevent: something.new\ndata: {"type":"something.new"}\n\n`);
      stream.push(`id: 2\nevent: file_node.changed\ndata: not-json\n\n`);
      await advance(300);
    });

    expect(seen).toEqual([]);
  });
});

describe("the lease frame's cache slots", () => {
  it("refreshes the leased folder's own item, named by the payload", () => {
    expect(
      EVENT_KEYS["file_lease.changed"]({
        type: "file_lease.changed",
        entity: "file_lease",
        entity_id: "lease-row",
        version: 7,
        org_id: ORG,
        lease_node_id: "root-node",
        drive_id: "drive-1",
      }),
    ).toEqual([[...keys.files.item("root-node")], [...keys.files.leasesAll]]);
  });

  it("falls back to the entity when the payload does not name the node", () => {
    expect(
      EVENT_KEYS["file_lease.changed"]({
        type: "file_lease.changed",
        entity: "file_lease",
        entity_id: "root-node",
        version: 7,
        org_id: ORG,
      }),
    ).toEqual([[...keys.files.item("root-node")], [...keys.files.leasesAll]]);
  });

  it("does not refresh the drive's folder listings — the node frames already did", () => {
    const named = EVENT_KEYS["file_lease.changed"]({
      type: "file_lease.changed",
      entity: "file_lease",
      entity_id: "root-node",
      version: 7,
      org_id: ORG,
    }).map((key) => JSON.stringify(key));
    expect(named).not.toContain(JSON.stringify([...keys.files.childrenAll]));
  });
});
