// The realtime socket client's contract, driven with a fake WebSocket and a scripted ticket
// minter under fake timers: the ticket travels only in the subprotocol header and a fresh one
// is minted per connection; "connected" is the server's welcome, not the open event; held
// channels are re-subscribed after every welcome; each close code gets the reaction the wire
// contract asks for; and the liveness ping closes a socket the server stopped answering.

import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from "vitest";

import { apiBaseUrl } from "@/api/client";
import type { RealtimeStatus } from "@/api/events/status";
import {
  CLOSE_CODES,
  CLOSE_PONG_TIMEOUT,
  WS_SUBPROTOCOL,
  WS_TICKET_SUBPROTOCOL_PREFIX,
  WsClient,
  defaultSocketUrl,
  parseServerFrame,
  type ServerFrame,
} from "@/api/realtime/wsClient";

import { advance, socketFactory, ticketMinter } from "./fakeWebSocket";

describe("defaultSocketUrl", () => {
  it("swaps the API base's http(s) for ws(s) and appends the ticket's path", () => {
    const expected = apiBaseUrl.replace(/^http/i, "ws") + "/api/v1/ws";
    expect(defaultSocketUrl("/api/v1/ws")).toBe(expected);
    expect(defaultSocketUrl("/api/v1/ws")).toMatch(/^wss?:\/\//);
    expect(defaultSocketUrl("/api/v1/ws")).not.toContain("?");
  });
});

describe("parseServerFrame", () => {
  it.each([
    [
      "welcome",
      {
        t: "welcome",
        peer_id: "p:1",
        server_time: "2026-09-05T12:00:00Z",
        instance: "i1",
        min_client_generation: 1,
        limits: {
          frames_per_window: 200,
          bytes_per_window: 4194304,
          window_seconds: 10,
          max_frame_bytes: 2162688,
          presence_ttl_seconds: 45,
        },
      },
    ],
    ["subscribed", { t: "subscribed", channel: "doc:artifact:a", can_write: true }],
    [
      "presence",
      {
        t: "presence",
        channel: "doc:artifact:a",
        event: "roster",
        peers: [{ peer_id: "p:2", user_id: "u2", last_seen_at: "2026-09-05T12:00:00Z" }],
      },
    ],
    ["reset", { t: "reset", reason: "overflow" }],
    ["error", { t: "error", code: "not_found", message: "no", channel: "doc:artifact:a" }],
    ["pong", { t: "pong" }],
    [
      "doc",
      {
        t: "doc",
        envelope: { doc_id: "a", doc_type: "artifact", epoch: 1, peer_id: "srv:0", seq: 0, kind: "ack", payload: {} },
      },
    ],
    ["publisher", { t: "publisher", channel: "doc:chat:c1", state: "gone", at: "2026-10-04T16:00:00Z" }],
  ])("parses a %s frame", (_tag, frame) => {
    expect(parseServerFrame(JSON.stringify(frame))).toEqual(frame);
  });

  it.each([
    ["an unknown state", { t: "publisher", channel: "doc:chat:c1", state: "asleep", at: "2026-10-04T16:00:00Z" }],
    ["no time", { t: "publisher", channel: "doc:chat:c1", state: "gone" }],
    ["no channel", { t: "publisher", state: "here", at: "2026-10-04T16:00:00Z" }],
  ])("drops a publisher frame with %s", (_why, frame) => {
    expect(parseServerFrame(JSON.stringify(frame))).toBeNull();
  });

  it.each([
    ["absent", undefined],
    ["not an object", 5],
    ["missing a term", { frames_per_window: 200, bytes_per_window: 1, window_seconds: 10 }],
    ["a zero term", { frames_per_window: 0, bytes_per_window: 1, window_seconds: 10, max_frame_bytes: 1 }],
    ["a negative term", { frames_per_window: 1, bytes_per_window: -1, window_seconds: 10, max_frame_bytes: 1 }],
    ["a string term", { frames_per_window: 1, bytes_per_window: 1, window_seconds: "10", max_frame_bytes: 1 }],
    ["a null term", { frames_per_window: 1, bytes_per_window: 1, window_seconds: null, max_frame_bytes: 1 }],
  ])("reads a welcome's budget as none when it is %s", (_name, limits) => {
    const frame = parseServerFrame(
      JSON.stringify({ t: "welcome", peer_id: "p:1", server_time: "", instance: "i1", limits }),
    );
    expect(frame).toMatchObject({ t: "welcome", limits: null });
  });

  it.each(["chat", "artifact", "chat_draft", "file", "notebook"])("admits a %s envelope", (docType) => {
    const envelope = { doc_id: "a", doc_type: docType, epoch: 1, peer_id: "p:1", seq: 0, kind: "crdt", payload: {} };
    expect(parseServerFrame(JSON.stringify({ t: "doc", envelope }))).toEqual({ t: "doc", envelope });
  });

  it("drops an envelope for a document type this client does not know", () => {
    const envelope = { doc_id: "a", doc_type: "spreadsheet", epoch: 1, peer_id: "p:1", seq: 0, kind: "crdt", payload: {} };
    expect(parseServerFrame(JSON.stringify({ t: "doc", envelope }))).toBeNull();
  });

  it("fills a frame's optional fields with their neutral values", () => {
    expect(parseServerFrame(JSON.stringify({ t: "error", code: "x" }))).toEqual({
      t: "error",
      code: "x",
      message: "",
      channel: null,
    });
    expect(parseServerFrame(JSON.stringify({ t: "subscribed", channel: "c" }))).toEqual({
      t: "subscribed",
      channel: "c",
      can_write: false,
    });
  });

  it("drops malformed presence peers and keeps the rest", () => {
    const parsed = parseServerFrame(
      JSON.stringify({
        t: "presence",
        channel: "c",
        event: "join",
        peers: [{ peer_id: "p:1", user_id: "u1", last_seen_at: "t" }, { peer_id: 7 }, "x", null],
      }),
    ) as Extract<ServerFrame, { t: "presence" }>;
    expect(parsed.peers).toEqual([{ peer_id: "p:1", user_id: "u1", last_seen_at: "t" }]);
  });

  it.each([
    ["non-string data", 42],
    ["invalid json", "{not json"],
    ["no tag", JSON.stringify({ channel: "c" })],
    ["an unknown tag from a newer server", JSON.stringify({ t: "typing", channel: "c" })],
    ["a welcome without a peer id", JSON.stringify({ t: "welcome" })],
    ["a doc frame without an envelope", JSON.stringify({ t: "doc" })],
    ["a doc frame whose envelope names an unknown doc type", JSON.stringify({ t: "doc", envelope: { doc_id: "a", doc_type: "sheet", epoch: 1, peer_id: "p", seq: 0, kind: "op", payload: {} } })],
    ["a presence frame with an unknown event", JSON.stringify({ t: "presence", channel: "c", event: "wave", peers: [] })],
  ])("returns null for %s", (_name, data) => {
    expect(parseServerFrame(data)).toBeNull();
  });
});

describe("WsClient", () => {
  let sockets: ReturnType<typeof socketFactory>;
  let minter: ReturnType<typeof ticketMinter>;
  let statuses: RealtimeStatus[];
  let onUnauthorized: Mock<() => void>;
  let log: Mock<(message: string, detail?: unknown) => void>;

  const build = (over: Partial<ConstructorParameters<typeof WsClient>[0]> = {}) =>
    new WsClient({
      mintTicket: minter.mint,
      socketUrl: (path) => `ws://api.test${path}`,
      factory: sockets.factory,
      onStatus: (s) => statuses.push(s),
      onUnauthorized,
      log,
      backoff: { jitter: () => 0 },
      ...over,
    });

  beforeEach(() => {
    vi.useFakeTimers();
    sockets = socketFactory();
    minter = ticketMinter();
    statuses = [];
    onUnauthorized = vi.fn();
    log = vi.fn();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  /** Start and complete one handshake. */
  async function connected(client: WsClient, peerId = "p:1") {
    client.start();
    await advance(0);
    sockets.last().welcome(peerId);
    await advance(0);
  }

  it("hands every welcome's minimum client generation to its handler, 0 from a server that sends none", async () => {
    const seen: number[] = [];
    const client = build({ onMinClientGeneration: (min) => seen.push(min) });
    client.start();
    await advance(0);
    sockets.last().open();
    sockets.last().serverSend({
      t: "welcome",
      peer_id: "p:1",
      server_time: "2026-09-05T12:00:00Z",
      instance: "i1",
      min_client_generation: 7,
    });
    await advance(0);
    sockets.last().serverClose(1006);
    await advance(10_000);
    sockets.last().welcome("p:2");
    await advance(0);
    expect(seen).toEqual([7, 0]);
  });

  it("mints a ticket and opens the socket offering the version and the ticket as subprotocols — never in the url", async () => {
    const client = build();
    client.start();
    await advance(0);
    expect(minter.minted).toEqual(["T1"]);
    const socket = sockets.last();
    expect(socket.url).toBe("ws://api.test/api/v1/ws");
    expect(socket.protocols).toEqual([WS_SUBPROTOCOL, `${WS_TICKET_SUBPROTOCOL_PREFIX}T1`]);
    expect(socket.url).not.toContain("?");
    expect(socket.url).not.toContain("T1");
  });

  it("is connected on the server's welcome, not on the open event, and learns its peer id there", async () => {
    const client = build();
    client.start();
    await advance(0);
    expect(client.status).toBe("connecting");
    sockets.last().open();
    expect(client.status).toBe("connecting");
    expect(client.peerId).toBeNull();
    sockets.last().serverSend({ t: "welcome", peer_id: "p:9", server_time: "t", instance: "i" });
    expect(client.status).toBe("connected");
    expect(client.peerId).toBe("p:9");
    expect(statuses).toEqual(["connecting", "connected"]);
  });

  it("every reconnect mints a new ticket (a ticket is burned on first use)", async () => {
    const client = build();
    await connected(client);
    sockets.last().serverClose(1006);
    await advance(2_000);
    expect(minter.minted).toEqual(["T1", "T2"]);
    expect(sockets.sockets).toHaveLength(2);
    expect(sockets.last().protocols[1]).toBe(`${WS_TICKET_SUBPROTOCOL_PREFIX}T2`);
  });

  it("re-subscribes every held channel after each welcome, in order, and fires onOpen after that", async () => {
    const client = build();
    const seen: string[] = [];
    client.onOpen((peerId) => seen.push(`open:${peerId}:${sockets.last().framesOf("subscribe").length}`));
    client.subscribe("doc:artifact:a");
    client.subscribe("doc:chat:b");
    await connected(client, "p:1");
    expect(sockets.last().framesOf("subscribe").map((f) => f.channel)).toEqual(["doc:artifact:a", "doc:chat:b"]);
    expect(seen).toEqual(["open:p:1:2"]);

    sockets.last().serverClose(1006);
    await advance(2_000);
    sockets.last().welcome("p:2");
    expect(sockets.last().framesOf("subscribe").map((f) => f.channel)).toEqual(["doc:artifact:a", "doc:chat:b"]);
    expect(seen).toEqual(["open:p:1:2", "open:p:2:2"]);
  });

  it("subscriptions are ref-counted: one release keeps the channel, the last one unsubscribes", async () => {
    const client = build();
    await connected(client);
    const releaseA = client.subscribe("doc:artifact:a");
    const releaseB = client.subscribe("doc:artifact:a");
    expect(sockets.last().framesOf("subscribe")).toHaveLength(1);
    releaseA();
    releaseA(); // idempotent
    expect(sockets.last().framesOf("unsubscribe")).toHaveLength(0);
    expect(client.channels).toEqual(["doc:artifact:a"]);
    releaseB();
    expect(sockets.last().framesOf("unsubscribe").map((f) => f.channel)).toEqual(["doc:artifact:a"]);
    expect(client.channels).toEqual([]);
  });

  it("send is refused before the welcome and honoured after it", async () => {
    const client = build();
    client.start();
    await advance(0);
    expect(client.send({ t: "ping" })).toBe(false);
    sockets.last().open();
    expect(client.send({ t: "ping" })).toBe(false);
    sockets.last().serverSend({ t: "welcome", peer_id: "p:1", server_time: "t", instance: "i" });
    expect(client.send({ t: "ping" })).toBe(true);
    expect(sockets.last().framesOf("ping")).toHaveLength(1);
  });

  it("delivers server frames to listeners, keeps welcome and pong to itself, and drops garbage", async () => {
    const client = build();
    const frames: ServerFrame[] = [];
    client.onFrame((f) => frames.push(f));
    await connected(client);
    const socket = sockets.last();
    socket.serverSend({ t: "subscribed", channel: "c", can_write: true });
    socket.serverSend({ t: "pong" });
    socket.serverSend("not json");
    socket.serverSend({ t: "typing" });
    socket.serverSend({ t: "reset", reason: "overflow" });
    expect(frames.map((f) => f.t)).toEqual(["subscribed", "reset"]);
  });

  it("pings on the heartbeat cadence and a missed pong closes the socket and reconnects on the floor", async () => {
    const client = build({ heartbeatMs: 20_000, pongTimeoutMs: 45_000 });
    await connected(client);
    const first = sockets.last();
    await advance(19_999);
    expect(first.framesOf("ping")).toHaveLength(0);
    await advance(1);
    expect(first.framesOf("ping")).toHaveLength(1);
    first.serverSend({ t: "pong" });
    await advance(20_000);
    expect(first.framesOf("ping")).toHaveLength(2);
    // No pong this time: 45 s after the ping the client gives up on the socket.
    await advance(44_999);
    expect(first.closedWith).toBeNull();
    await advance(1);
    expect(first.closedWith?.code).toBe(CLOSE_PONG_TIMEOUT);
    expect(client.status).toBe("reconnecting");
    await advance(2_000);
    expect(sockets.sockets).toHaveLength(2);
    expect(minter.minted).toHaveLength(2);
  });

  it("close 4401 re-mints once immediately, then backs off", async () => {
    const client = build();
    await connected(client);
    sockets.last().serverClose(CLOSE_CODES.UNAUTHORIZED);
    await advance(0);
    expect(sockets.sockets).toHaveLength(2);
    expect(minter.minted).toHaveLength(2);
    sockets.last().serverClose(CLOSE_CODES.UNAUTHORIZED);
    await advance(0);
    expect(sockets.sockets).toHaveLength(2);
    await advance(2_000);
    expect(sockets.sockets).toHaveLength(3);
  });

  it("a welcome that LASTS re-arms the single immediate re-mint", async () => {
    const client = build({ stableAfterMs: 60_000 });
    await connected(client);
    sockets.last().serverClose(CLOSE_CODES.UNAUTHORIZED);
    await advance(0);
    sockets.last().welcome("p:2");
    await advance(60_000); // the connection held: this one counts as a recovery
    sockets.last().serverClose(CLOSE_CODES.UNAUTHORIZED);
    await advance(0);
    expect(sockets.sockets).toHaveLength(3);
  });

  it("a welcome that does NOT last leaves the single re-mint spent", async () => {
    // The accept-then-drop loop: were the handshake alone enough to re-arm it, every cycle
    // would mint a fresh ticket and reconnect immediately, forever.
    const client = build({ stableAfterMs: 60_000 });
    await connected(client);
    sockets.last().serverClose(CLOSE_CODES.UNAUTHORIZED);
    await advance(0);
    sockets.last().welcome("p:2");
    sockets.last().serverClose(CLOSE_CODES.UNAUTHORIZED);
    await advance(0);
    expect(sockets.sockets).toHaveLength(2); // no immediate re-mint
    await advance(2_000);
    expect(sockets.sockets).toHaveLength(3); // it backed off instead
  });

  it("close 4403 reports down, logs, and never retries (a misconfigured origin)", async () => {
    const client = build();
    await connected(client);
    sockets.last().serverClose(CLOSE_CODES.ORIGIN_FORBIDDEN);
    expect(client.status).toBe("down");
    expect(log).toHaveBeenCalledTimes(1);
    await advance(10 * 60_000);
    expect(sockets.sockets).toHaveLength(1);
    expect(minter.minted).toHaveLength(1);
  });

  it("close 4408 re-mints and reconnects at once", async () => {
    const client = build();
    await connected(client);
    sockets.last().serverClose(CLOSE_CODES.SESSION_EXPIRED);
    await advance(0);
    expect(sockets.sockets).toHaveLength(2);
    expect(client.status).toBe("reconnecting");
    sockets.last().welcome("p:2");
    expect(client.status).toBe("connected");
  });

  it("a second 4408 backs off instead of re-minting again at once", async () => {
    // A fresh ticket is the right answer to an expired session exactly once. A mint that
    // keeps succeeding against a server that keeps closing 4408 is otherwise an unthrottled
    // mint-and-handshake loop on the transport whose whole backoff exists to prevent one.
    const client = build();
    await connected(client);
    sockets.last().serverClose(CLOSE_CODES.SESSION_EXPIRED);
    await advance(0);
    expect(sockets.sockets).toHaveLength(2);
    sockets.last().serverClose(CLOSE_CODES.SESSION_EXPIRED);
    await advance(0);
    expect(sockets.sockets).toHaveLength(2);
    await advance(2_000);
    expect(sockets.sockets).toHaveLength(3);
    // And it keeps backing off: the third wait is longer than the second.
    sockets.last().serverClose(CLOSE_CODES.SESSION_EXPIRED);
    await advance(2_000);
    expect(sockets.sockets).toHaveLength(3);
    await advance(2_000);
    expect(sockets.sockets).toHaveLength(4);
  });

  it("a welcome that LASTS re-arms the single immediate 4408 re-mint", async () => {
    const client = build({ stableAfterMs: 60_000 });
    await connected(client);
    sockets.last().serverClose(CLOSE_CODES.SESSION_EXPIRED);
    await advance(0);
    sockets.last().welcome("p:2");
    await advance(60_000);
    sockets.last().serverClose(CLOSE_CODES.SESSION_EXPIRED);
    await advance(0);
    expect(sockets.sockets).toHaveLength(3);
  });

  it("close 4413 logs and backs off", async () => {
    const client = build();
    await connected(client);
    sockets.last().serverClose(CLOSE_CODES.FRAME_TOO_LARGE);
    expect(log).toHaveBeenCalledTimes(1);
    await advance(1_999);
    expect(sockets.sockets).toHaveLength(1);
    await advance(1);
    expect(sockets.sockets).toHaveLength(2);
  });

  it("close 4503 backs off and comes back on a fresh ticket, never in a tight loop", async () => {
    // The replica that answered cannot deliver: it runs without the outbox listener every
    // cross-replica frame arrives through, so it refuses the socket at the handshake. The
    // client's job is to land somewhere else, and a refusal that costs nothing to produce
    // is exactly the one a client must not spin on -- it would hammer every replica in the
    // pool. So: wait, then re-mint (the refused ticket was burned on first use).
    const client = build();
    await connected(client);
    sockets.last().serverClose(CLOSE_CODES.UNAVAILABLE);
    expect(client.status).toBe("reconnecting");
    await advance(0);
    expect(sockets.sockets).toHaveLength(1);
    await advance(1_999);
    expect(sockets.sockets).toHaveLength(1);
    await advance(1);
    expect(sockets.sockets).toHaveLength(2);
    expect(minter.minted).toEqual(["T1", "T2"]);
    expect(sockets.last().protocols[1]).toBe(`${WS_TICKET_SUBPROTOCOL_PREFIX}T2`);

    // Refused again — a whole pool can be mid-rollout. The wait grows rather than repeating.
    sockets.last().serverClose(CLOSE_CODES.UNAVAILABLE);
    await advance(3_999);
    expect(sockets.sockets).toHaveLength(2);
    await advance(1);
    expect(sockets.sockets).toHaveLength(3);

    // A replica that answers and drops us again is NOT a recovery: the wait keeps growing
    // rather than returning to the floor (never reset the backoff on a connect).
    sockets.last().welcome("p:2");
    expect(client.status).toBe("connected");
    sockets.last().serverClose(CLOSE_CODES.UNAVAILABLE);
    await advance(7_999);
    expect(sockets.sockets).toHaveLength(3);
    await advance(1); // +8 s - the third attempt's delay, not the floor
    expect(sockets.sockets).toHaveLength(4);
    // Nothing here is an operator's problem: a replica out of the rotation is routine.
    expect(log).not.toHaveBeenCalled();
  });

  it.each([1006, CLOSE_CODES.TOO_MANY, CLOSE_CODES.SERVER_RESET, CLOSE_CODES.NOT_FOUND])(
    "close %i backs off exponentially and reports down after the grace window",
    async (code) => {
      const client = build();
      await connected(client);
      sockets.last().serverClose(code);
      expect(client.status).toBe("reconnecting");
      await advance(2_000); // attempt 2 at +2 s
      expect(sockets.sockets).toHaveLength(2);
      sockets.last().serverClose(code);
      await advance(3_000); // +5 s since the first close: down
      expect(client.status).toBe("down");
      await advance(1_000); // attempt 3 at +4 s after the second close
      expect(sockets.sockets).toHaveLength(3);
      sockets.last().serverClose(code);
      await advance(7_999);
      expect(sockets.sockets).toHaveLength(3);
      await advance(1); // +8 s
      expect(sockets.sockets).toHaveLength(4);
    },
  );

  it("a connection that HOLDS returns the next reconnect to the floor", async () => {
    // The other half of the rule: a real recovery - a socket that stayed welcomed - does
    // start over, or a long-lived session would creep to the cap over a day of blips.
    const client = build({ stableAfterMs: 60_000 });
    await connected(client);
    sockets.last().serverClose(CLOSE_CODES.SERVER_RESET);
    await advance(2_000);
    sockets.last().serverClose(CLOSE_CODES.SERVER_RESET);
    await advance(4_000); // the second attempt waited twice as long
    expect(sockets.sockets).toHaveLength(3);

    sockets.last().welcome("p:3");
    await advance(60_000);
    sockets.last().serverClose(CLOSE_CODES.SERVER_RESET);
    await advance(1_999);
    expect(sockets.sockets).toHaveLength(3);
    await advance(1); // back on the 2 s floor
    expect(sockets.sockets).toHaveLength(4);
  });

  it("onClose listeners hear the code of every close the client did not ask for", async () => {
    const client = build();
    const codes: number[] = [];
    client.onClose(({ code }) => codes.push(code));
    await connected(client);
    sockets.last().serverClose(1006);
    await advance(2_000);
    sockets.last().welcome("p:2");
    sockets.last().serverClose(CLOSE_CODES.SESSION_EXPIRED);
    await advance(0);
    client.stop();
    expect(codes).toEqual([1006, CLOSE_CODES.SESSION_EXPIRED]);
  });

  it("a ticket refused with 401 hands the user to login and opens nothing", async () => {
    minter.refuseNextUnauthorized();
    const client = build();
    client.start();
    await advance(0);
    expect(onUnauthorized).toHaveBeenCalledTimes(1);
    expect(client.status).toBe("idle");
    expect(sockets.sockets).toHaveLength(0);
    await advance(10 * 60_000);
    expect(minter.mint).toHaveBeenCalledTimes(1);
  });

  it("a ticket mint that fails otherwise backs off and mints again", async () => {
    minter.refuseNext(new Error("gateway timeout"));
    const client = build();
    client.start();
    await advance(0);
    expect(client.status).toBe("reconnecting");
    expect(sockets.sockets).toHaveLength(0);
    await advance(2_000);
    expect(minter.mint).toHaveBeenCalledTimes(2);
    expect(sockets.sockets).toHaveLength(1);
  });

  it("a factory that throws is a failure with a retry, not an exception", async () => {
    const client = build({
      factory: vi.fn().mockImplementationOnce(() => {
        throw new Error("SecurityError");
      }).mockImplementation(sockets.factory),
    });
    client.start();
    await advance(0);
    expect(client.status).toBe("reconnecting");
    await advance(2_000);
    expect(sockets.sockets).toHaveLength(1);
  });

  it("stop closes the socket with 1000, clears every timer and returns to idle", async () => {
    const client = build();
    await connected(client);
    const socket = sockets.last();
    client.stop();
    expect(socket.closedWith?.code).toBe(1000);
    expect(client.status).toBe("idle");
    expect(client.peerId).toBeNull();
    await advance(10 * 60_000);
    expect(minter.mint).toHaveBeenCalledTimes(1);
    expect(sockets.sockets).toHaveLength(1);
  });

  it("stop during a backoff cancels the reconnect", async () => {
    const client = build();
    await connected(client);
    sockets.last().serverClose(1006);
    client.stop();
    await advance(10 * 60_000);
    expect(sockets.sockets).toHaveLength(1);
    expect(statuses).toEqual(["connecting", "connected", "reconnecting", "idle"]);
  });

  it("start is idempotent while running and restarts after a stop", async () => {
    const client = build();
    client.start();
    client.start();
    await advance(0);
    expect(minter.mint).toHaveBeenCalledTimes(1);
    client.stop();
    client.start();
    await advance(0);
    expect(minter.mint).toHaveBeenCalledTimes(2);
  });

  it("without a WebSocket in the environment the client reports down once and never mints", () => {
    const client = build({ factory: null });
    client.start();
    expect(client.status).toBe("down");
    expect(statuses).toEqual(["down"]);
    expect(minter.mint).not.toHaveBeenCalled();
  });
});
