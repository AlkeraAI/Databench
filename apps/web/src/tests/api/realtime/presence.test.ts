// Presence on a channel: join once the subscription is confirmed (and again after every
// reconnect), heartbeat on a cadence, leave on release, and a roster that follows the server's
// roster and deltas. A viewer is a person, not a tab — except the reader's own tab, which is
// the one peer that never counts.

import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { setRealtimeClientForTests } from "@/api/realtime/client";
import {
  cursorSender,
  initialsOf,
  joinPresence,
  useRealtimePeer,
  viewerName,
  viewerNames,
  viewersAcross,
  type PresencePeer,
} from "@/api/realtime/presence";
import { WsClient } from "@/api/realtime/wsClient";

import { advance, socketFactory, ticketMinter } from "./fakeWebSocket";

const CHANNEL = "doc:artifact:0f2b6c1e-6d1a-4a6d-9f1e-2c3b4a5d6e7f";
const peer = (peer_id: string, user_id: string): PresencePeer => ({ peer_id, user_id, last_seen_at: "2026-09-05T12:00:00Z" });

let sockets: ReturnType<typeof socketFactory>;
let client: WsClient;
let rosters: PresencePeer[][];
let release: (() => void) | null;

async function connect(peerId = "p:1") {
  client.start();
  await advance(0);
  sockets.last().welcome(peerId);
  await advance(0);
}

beforeEach(() => {
  vi.useFakeTimers();
  sockets = socketFactory();
  client = new WsClient({
    mintTicket: ticketMinter().mint,
    socketUrl: (path) => `ws://api.test${path}`,
    factory: sockets.factory,
    backoff: { jitter: () => 0 },
    // The socket's own liveness ping is out of scope here: nobody answers it, so a real
    // cadence would close the socket under a minute-long presence test.
    heartbeatMs: 60 * 60_000,
  });
  rosters = [];
  release = null;
});

afterEach(() => {
  release?.();
  client.stop();
  vi.useRealTimers();
});

describe("joinPresence", () => {
  it("subscribes, joins only once the server confirms the channel, and reads the roster", async () => {
    await connect();
    release = joinPresence(client, CHANNEL, (peers) => rosters.push(peers));
    const socket = sockets.last();
    expect(socket.framesOf("subscribe").map((f) => f.channel)).toEqual([CHANNEL]);
    expect(socket.framesOf("presence.join")).toHaveLength(0);
    socket.serverSend({ t: "subscribed", channel: CHANNEL, can_write: false });
    expect(socket.framesOf("presence.join").map((f) => f.channel)).toEqual([CHANNEL]);
    socket.serverSend({ t: "presence", channel: CHANNEL, event: "roster", peers: [peer("p:1", "u1"), peer("p:2", "u2")] });
    expect(rosters.at(-1)).toEqual([peer("p:1", "u1"), peer("p:2", "u2")]);
  });

  it("applies joins, heartbeats and leaves as deltas, sorted by peer id", async () => {
    await connect();
    release = joinPresence(client, CHANNEL, (peers) => rosters.push(peers));
    const socket = sockets.last();
    socket.serverSend({ t: "subscribed", channel: CHANNEL, can_write: false });
    socket.serverSend({ t: "presence", channel: CHANNEL, event: "roster", peers: [peer("p:1", "u1")] });
    socket.serverSend({ t: "presence", channel: CHANNEL, event: "join", peers: [peer("p:3", "u3")] });
    expect(rosters.at(-1)?.map((p) => p.peer_id)).toEqual(["p:1", "p:3"]);
    socket.serverSend({ t: "presence", channel: CHANNEL, event: "join", peers: [peer("p:2", "u2")] });
    expect(rosters.at(-1)?.map((p) => p.peer_id)).toEqual(["p:1", "p:2", "p:3"]);
    socket.serverSend({
      t: "presence",
      channel: CHANNEL,
      event: "heartbeat",
      peers: [{ ...peer("p:2", "u2"), last_seen_at: "2026-09-05T12:01:00Z" }],
    });
    expect(rosters.at(-1)?.find((p) => p.peer_id === "p:2")?.last_seen_at).toBe("2026-09-05T12:01:00Z");
    socket.serverSend({ t: "presence", channel: CHANNEL, event: "leave", peers: [peer("p:1", "u1")] });
    expect(rosters.at(-1)?.map((p) => p.peer_id)).toEqual(["p:2", "p:3"]);
    // A fresh roster replaces everything.
    socket.serverSend({ t: "presence", channel: CHANNEL, event: "roster", peers: [peer("p:9", "u9")] });
    expect(rosters.at(-1)?.map((p) => p.peer_id)).toEqual(["p:9"]);
  });

  it("ignores presence on another channel", async () => {
    await connect();
    release = joinPresence(client, CHANNEL, (peers) => rosters.push(peers));
    sockets.last().serverSend({ t: "subscribed", channel: "doc:artifact:other", can_write: false });
    sockets.last().serverSend({ t: "presence", channel: "doc:artifact:other", event: "roster", peers: [peer("p:1", "u1")] });
    expect(sockets.last().framesOf("presence.join")).toHaveLength(0);
    expect(rosters).toEqual([]);
  });

  it("heartbeats on the cadence after joining, and not before", async () => {
    await connect();
    release = joinPresence(client, CHANNEL, () => undefined, { heartbeatMs: 20_000 });
    const socket = sockets.last();
    await advance(60_000);
    expect(socket.framesOf("presence.heartbeat")).toHaveLength(0);
    socket.serverSend({ t: "subscribed", channel: CHANNEL, can_write: false });
    await advance(19_999);
    expect(socket.framesOf("presence.heartbeat")).toHaveLength(0);
    await advance(1);
    expect(socket.framesOf("presence.heartbeat")).toHaveLength(1);
    await advance(40_000);
    expect(socket.framesOf("presence.heartbeat")).toHaveLength(3);
    expect(socket.framesOf("presence.heartbeat").every((f) => f.channel === CHANNEL)).toBe(true);
  });

  it("re-joins after a reconnect, and a dropped socket empties the roster meanwhile", async () => {
    await connect();
    release = joinPresence(client, CHANNEL, (peers) => rosters.push(peers), { heartbeatMs: 20_000 });
    const first = sockets.last();
    first.serverSend({ t: "subscribed", channel: CHANNEL, can_write: false });
    first.serverSend({ t: "presence", channel: CHANNEL, event: "roster", peers: [peer("p:1", "u1"), peer("p:2", "u2")] });
    first.serverClose(1006);
    await advance(0);
    expect(rosters.at(-1)).toEqual([]);
    await advance(2_000);
    const second = sockets.last();
    second.welcome("p:5");
    second.serverSend({ t: "subscribed", channel: CHANNEL, can_write: false });
    expect(second.framesOf("presence.join").map((f) => f.channel)).toEqual([CHANNEL]);
    await advance(20_000);
    expect(second.framesOf("presence.heartbeat")).toHaveLength(1);
    expect(first.framesOf("presence.heartbeat")).toHaveLength(0);
  });

  it("release leaves the channel, stops the heartbeat and releases the subscription", async () => {
    await connect();
    release = joinPresence(client, CHANNEL, (peers) => rosters.push(peers), { heartbeatMs: 20_000 });
    const socket = sockets.last();
    socket.serverSend({ t: "subscribed", channel: CHANNEL, can_write: false });
    release();
    release(); // idempotent
    release = null;
    expect(socket.framesOf("presence.leave").map((f) => f.channel)).toEqual([CHANNEL]);
    expect(socket.framesOf("unsubscribe").map((f) => f.channel)).toEqual([CHANNEL]);
    await advance(60_000);
    expect(socket.framesOf("presence.heartbeat")).toHaveLength(0);
    socket.serverSend({ t: "presence", channel: CHANNEL, event: "join", peers: [peer("p:7", "u7")] });
    expect(rosters).toEqual([]);
  });

  it("a second holder of the channel does not unsubscribe it when presence is released", async () => {
    await connect();
    const keep = client.subscribe(CHANNEL);
    release = joinPresence(client, CHANNEL, () => undefined);
    release();
    release = null;
    expect(sockets.last().framesOf("unsubscribe")).toHaveLength(0);
    keep();
    expect(sockets.last().framesOf("unsubscribe")).toHaveLength(1);
  });
});

describe("a peer nobody hears from", () => {
  const LIMITS = { frames_per_window: 200, bytes_per_window: 4194304, window_seconds: 10, max_frame_bytes: 2162688 };

  async function joined(ttlSeconds: number | null) {
    client.start();
    await advance(0);
    sockets.last().welcome("p:1", ttlSeconds === null ? LIMITS : { ...LIMITS, presence_ttl_seconds: ttlSeconds });
    await advance(0);
    release = joinPresence(client, CHANNEL, (peers) => rosters.push(peers), { heartbeatMs: 60 * 60_000 });
    const socket = sockets.last();
    socket.serverSend({ t: "subscribed", channel: CHANNEL, can_write: false });
    socket.serverSend({ t: "presence", channel: CHANNEL, event: "roster", peers: [peer("p:1", "u1"), peer("p:ghost", "u2")] });
    return socket;
  }

  it("is dropped once the server's presence TTL passes with no leave", async () => {
    const socket = await joined(45);
    await advance(30_000);
    socket.serverSend({ t: "presence", channel: CHANNEL, event: "heartbeat", peers: [peer("p:1", "u1")] });
    expect(rosters.at(-1)?.map((p) => p.peer_id)).toEqual(["p:1", "p:ghost"]);
    await advance(20_000);
    expect(rosters.at(-1)?.map((p) => p.peer_id)).toEqual(["p:1"]);
  });

  it("stays while it keeps heartbeating", async () => {
    const socket = await joined(45);
    for (let i = 0; i < 6; i += 1) {
      await advance(20_000);
      socket.serverSend({
        t: "presence",
        channel: CHANNEL,
        event: "heartbeat",
        peers: [peer("p:1", "u1"), peer("p:ghost", "u2")],
      });
    }
    expect(rosters.at(-1)?.map((p) => p.peer_id)).toEqual(["p:1", "p:ghost"]);
  });

  it("is kept by a server that names no TTL, as before", async () => {
    await joined(null);
    await advance(10 * 60_000);
    expect(rosters.at(-1)?.map((p) => p.peer_id)).toEqual(["p:1", "p:ghost"]);
  });
});

describe("the people behind a roster", () => {
  const viewerCount = (...args: Parameters<typeof viewerNames>): number => viewerNames(...args).length;

  it("counts people, not tabs", () => {
    expect(viewerCount([], "p:me")).toBe(0);
    expect(viewerCount([peer("p:1", "u1")], "p:me")).toBe(1);
    expect(viewerCount([peer("p:1", "u1"), peer("p:2", "u1")], "p:me")).toBe(1);
    expect(viewerCount([peer("p:1", "u1"), peer("p:2", "u2"), peer("p:3", "u1")], "p:me")).toBe(2);
  });

  it("leaves THIS tab out and no other tab of the same person", () => {
    // Alone in one window: nobody else is watching.
    expect(viewerCount([peer("p:me", "u1")], "p:me")).toBe(0);
    // The same person's second window IS somebody else's screen — counted, once.
    expect(viewerCount([peer("p:me", "u1"), peer("p:2", "u1")], "p:me")).toBe(1);
    expect(viewerCount([peer("p:me", "u1"), peer("p:2", "u1"), peer("p:3", "u1")], "p:me")).toBe(1);
    // A colleague is counted next to that second window, not instead of it.
    expect(viewerCount([peer("p:me", "u1"), peer("p:2", "u1"), peer("p:9", "u2")], "p:me")).toBe(2);
  });

  it("subtracts nothing for a socket the server has not named yet", () => {
    expect(viewerCount([peer("p:1", "u1"), peer("p:2", "u2")], null)).toBe(2);
  });

  it("names each of them the way every surface that draws one does", () => {
    const named = { ...peer("p:2", "u2"), display_name: "Bo Chen", email: "bo@acme.test" };
    const addressed = { ...peer("p:3", "u3"), email: "cleo@acme.test" };
    // A name, an address where the server resolved no name, and neither.
    expect(viewerNames([named, addressed, peer("p:4", "u4")], "p:me")).toEqual([
      "Bo Chen",
      "cleo@acme.test",
      "Someone",
    ]);
    // The reader's own other window says whose it is instead of their name.
    expect(viewerNames([named], "p:me", "u2")).toEqual(["You (another window)"]);
  });

  it("takes a person's name off whichever of their tabs carries one", () => {
    const anonymous = peer("p:7", "u2");
    const named = { ...peer("p:8", "u2"), display_name: "Bo Chen" };
    expect(viewerNames([anonymous, named], "p:me")).toEqual(["Bo Chen"]);
  });
});

describe("what a viewer is called", () => {
  it("prefers the name, falls back to the address, and gives up at Someone", () => {
    expect(viewerName({ userId: "u1", name: "Bo Chen", email: "bo@acme.test" })).toBe("Bo Chen");
    expect(viewerName({ userId: "u1", name: "  ", email: "bo@acme.test" })).toBe("bo@acme.test");
    expect(viewerName({ userId: "u1", name: "", email: "" })).toBe("Someone");
  });

  it("says a window is the reader's own rather than repeating their name at them", () => {
    expect(viewerName({ userId: "u1", name: "Dana", email: "d@acme.test" }, "u1")).toBe("You (another window)");
    expect(viewerName({ userId: "u2", name: "Dana", email: "d@acme.test" }, "u1")).toBe("Dana");
  });
});

describe("a caret on the roster", () => {
  const cursor = (offset: number) => ({ offset, anchor: offset, before: "ab", after: "cd" });

  it("lands on the peer that reported it and survives that peer's heartbeat", async () => {
    await connect();
    release = joinPresence(client, CHANNEL, (peers) => rosters.push(peers));
    const socket = sockets.last();
    socket.serverSend({ t: "subscribed", channel: CHANNEL, can_write: true });
    socket.serverSend({ t: "presence", channel: CHANNEL, event: "roster", peers: [peer("p:1", "u1"), peer("p:2", "u2")] });
    socket.serverSend({ t: "presence", channel: CHANNEL, event: "cursor", peers: [{ ...peer("p:2", "u2"), cursor: cursor(7) }] });
    expect(rosters.at(-1)?.find((p) => p.peer_id === "p:2")?.cursor).toEqual(cursor(7));
    expect(rosters.at(-1)?.find((p) => p.peer_id === "p:1")?.cursor ?? null).toBeNull();

    socket.serverSend({ t: "presence", channel: CHANNEL, event: "heartbeat", peers: [peer("p:2", "u2")] });
    expect(rosters.at(-1)?.find((p) => p.peer_id === "p:2")?.cursor).toEqual(cursor(7));

    socket.serverSend({ t: "presence", channel: CHANNEL, event: "cursor", peers: [{ ...peer("p:2", "u2"), cursor: cursor(3) }] });
    expect(rosters.at(-1)?.find((p) => p.peer_id === "p:2")?.cursor).toEqual(cursor(3));
  });

  it("goes with its peer when they leave, and never belongs to a peer the roster does not know", async () => {
    await connect();
    release = joinPresence(client, CHANNEL, (peers) => rosters.push(peers));
    const socket = sockets.last();
    socket.serverSend({ t: "subscribed", channel: CHANNEL, can_write: true });
    socket.serverSend({ t: "presence", channel: CHANNEL, event: "roster", peers: [peer("p:1", "u1"), peer("p:2", "u2")] });
    socket.serverSend({ t: "presence", channel: CHANNEL, event: "cursor", peers: [{ ...peer("p:9", "u9"), cursor: cursor(1) }] });
    expect(rosters.at(-1)?.map((p) => p.peer_id)).toEqual(["p:1", "p:2"]);

    socket.serverSend({ t: "presence", channel: CHANNEL, event: "cursor", peers: [{ ...peer("p:2", "u2"), cursor: cursor(5) }] });
    socket.serverSend({ t: "presence", channel: CHANNEL, event: "leave", peers: [peer("p:2", "u2")] });
    expect(rosters.at(-1)?.map((p) => p.peer_id)).toEqual(["p:1"]);
  });
});

describe("cursorSender", () => {
  const cursor = (offset: number) => ({ offset, anchor: offset, before: "", after: "" });

  it("sends the first move at once, folds a burst into the latest position, and never repeats itself", async () => {
    await connect();
    const socket = sockets.last();
    const sender = cursorSender(client, CHANNEL, { throttleMs: 100 });
    sender.move(cursor(1));
    expect(socket.framesOf("presence.cursor").map((f) => f.cursor.offset)).toEqual([1]);
    sender.move(cursor(2));
    sender.move(cursor(3));
    sender.move(cursor(4));
    expect(socket.framesOf("presence.cursor").map((f) => f.cursor.offset)).toEqual([1]);
    await advance(100);
    expect(socket.framesOf("presence.cursor").map((f) => f.cursor.offset)).toEqual([1, 4]);
    await advance(100);
    sender.move(cursor(4));
    await advance(200);
    expect(socket.framesOf("presence.cursor").map((f) => f.cursor.offset)).toEqual([1, 4]);
    sender.stop();
  });

  it("drops what is pending when stopped", async () => {
    await connect();
    const socket = sockets.last();
    const sender = cursorSender(client, CHANNEL, { throttleMs: 100 });
    sender.move(cursor(1));
    sender.move(cursor(2));
    sender.stop();
    await advance(500);
    expect(socket.framesOf("presence.cursor").map((f) => f.cursor.offset)).toEqual([1]);
  });
});

describe("viewersAcross", () => {
  const named = (peer_id: string, user_id: string, extra: Partial<PresencePeer>): PresencePeer => ({
    ...peer(peer_id, user_id),
    ...extra,
  });

  it("is one person per user across rosters, with each place they were seen in once", () => {
    const viewers = viewersAcross(
      [
        ["a", [peer("p:me", "u1"), peer("p:2", "u2"), peer("p:3", "u2")]],
        [null, [peer("p:4", "u3"), peer("p:2", "u2")]],
        ["b", [peer("p:5", "u2")]],
        ["a", [peer("p:6", "u2")]],
      ],
      "p:me",
    );
    expect(viewers.map((v) => [v.userId, v.places])).toEqual([
      ["u2", ["a", "b"]],
      ["u3", []],
    ]);
  });

  it("takes the name, the address and the picture from whichever of a person's peers carries each", () => {
    const [viewer] = viewersAcross(
      [
        ["a", [named("p:1", "u1", { display_name: "  " }), named("p:2", "u1", { avatar_url: "https://pics.test/a.png" })]],
        ["b", [named("p:3", "u1", { display_name: "Ada Ling", email: " ada@acme.test " })]],
        ["c", [named("p:4", "u1", { display_name: "Not Ada", email: "other@acme.test", avatar_url: "https://pics.test/b.png" })]],
      ],
      null,
    );
    // The first value a peer carried wins; a later peer never overwrites it.
    expect(viewer).toEqual({
      userId: "u1",
      name: "Ada Ling",
      email: "ada@acme.test",
      avatarUrl: "https://pics.test/a.png",
      places: ["a", "b", "c"],
    });
  });
});

describe("initialsOf", () => {
  it.each([
    ["a two-part name", "Ada Ling", "AL"],
    ["a three-part name, first and last", "Ada M Ling", "AL"],
    ["a one-part name, its first two characters", "ada@acme.test", "ad"],
    ["surrounding and repeated spaces", "  Ada   Ling ", "AL"],
    ["nothing", "", ""],
    ["only spaces", "   ", ""],
  ])("takes %s", (_case, name, expected) => {
    expect(initialsOf(name)).toBe(expected);
  });
});

describe("useRealtimePeer", () => {
  it("holds the socket only while active, and follows the id a reconnect mints", async () => {
    setRealtimeClientForTests(client);
    const hook = renderHook(({ active }) => useRealtimePeer(active), { initialProps: { active: false } });
    expect(hook.result.current).toEqual({ client: null, peerId: null });
    // Inactive: nothing asked for the socket, so it never opened.
    expect(sockets.sockets).toHaveLength(0);

    hook.rerender({ active: true });
    await act(async () => {
      await advance(0);
    });
    expect(hook.result.current.client).toBe(client);
    expect(hook.result.current.peerId).toBeNull();
    await act(async () => {
      sockets.last().welcome("p:first");
      await advance(0);
    });
    expect(hook.result.current.peerId).toBe("p:first");

    // A dropped socket comes back under a new id, and this tab is told apart by it.
    await act(async () => {
      sockets.last().serverClose(1006);
      await advance(60_000);
    });
    await act(async () => {
      sockets.last().welcome("p:second");
      await advance(0);
    });
    expect(hook.result.current.peerId).toBe("p:second");

    hook.rerender({ active: false });
    expect(hook.result.current).toEqual({ client: null, peerId: null });
    hook.unmount();
    setRealtimeClientForTests(null);
  });
});

