// Who is in a workspace: the union of its chats' rosters, watched without
// joining any of them, one entry per person.

import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { setRealtimeClientForTests } from "@/api/realtime/client";
import { joinPresence, observePresence, type PresencePeer } from "@/api/realtime/presence";
import { WsClient } from "@/api/realtime/wsClient";
import {
  WORKSPACE_ROSTER,
  useWorkspaceViewers,
  workspaceViewersOf,
} from "@/pages/workspace/workspaces/useWorkspacePresence";

import { advance, socketFactory, ticketMinter } from "../../../api/realtime/fakeWebSocket";

const A = "doc:chat:aaaaaaaa-0000-4000-8000-000000000001";
const peer = (peer_id: string, user_id: string, display_name = ""): PresencePeer => ({
  peer_id,
  user_id,
  display_name,
  last_seen_at: "2026-10-04T12:00:00Z",
});

let sockets: ReturnType<typeof socketFactory>;
let client: WsClient;

async function connect(peerId = "p:self") {
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
    heartbeatMs: 60 * 60_000,
  });
});

afterEach(() => {
  setRealtimeClientForTests(null);
  client.stop();
  vi.useRealTimers();
});

describe("observePresence", () => {
  it("reads the roster and its deltas without ever joining the channel", async () => {
    await connect();
    const rosters: PresencePeer[][] = [];
    const release = observePresence(client, A, (peers) => rosters.push(peers));
    const socket = sockets.last();
    socket.serverSend({ t: "subscribed", channel: A, can_write: false });
    socket.serverSend({ t: "presence", channel: A, event: "roster", peers: [peer("p:2", "u2")] });
    socket.serverSend({ t: "presence", channel: A, event: "join", peers: [peer("p:3", "u3")] });
    socket.serverSend({ t: "presence", channel: A, event: "leave", peers: [peer("p:2", "u2")] });
    expect(rosters.at(-1)?.map((p) => p.peer_id)).toEqual(["p:3"]);
    // Watching is not being there: no join, no heartbeat, no leave.
    expect(socket.framesOf("presence.join")).toHaveLength(0);
    await advance(120_000);
    expect(socket.framesOf("presence.heartbeat")).toHaveLength(0);
    release();
    expect(socket.framesOf("presence.leave")).toHaveLength(0);
    expect(socket.framesOf("unsubscribe").map((f) => f.channel)).toEqual([A]);
  });

  it("asks for a roster on a channel this tab already holds", async () => {
    await connect();
    const joined = joinPresence(client, A, () => undefined);
    const socket = sockets.last();
    expect(socket.framesOf("subscribe")).toHaveLength(1);
    const release = observePresence(client, A, () => undefined);
    // The held subscription would send nothing new, so the watcher asks again.
    expect(socket.framesOf("subscribe")).toHaveLength(2);
    release();
    // The chat page still holds the channel: releasing the watcher leaves it.
    expect(socket.framesOf("unsubscribe")).toHaveLength(0);
    joined();
  });
});

describe("workspaceViewersOf", () => {
  it("is one person per user across every chat and tab, without this tab", () => {
    const rosters = new Map<string, PresencePeer[]>([
      ["chat-1", [peer("p:self", "u-me"), peer("p:2", "u-dana", ""), peer("p:9", "u-me")]],
      ["chat-2", [peer("p:3", "u-dana", "Dana Ruiz"), peer("p:4", "u-li", "Li Wei")]],
    ]);
    const viewers = workspaceViewersOf(rosters, "p:self");
    expect(viewers.map((v) => v.userId)).toEqual(["u-dana", "u-me", "u-li"]);
    const dana = viewers.find((v) => v.userId === "u-dana");
    // The name comes from whichever tab carried one.
    expect(dana?.name).toBe("Dana Ruiz");
    expect(dana?.chatIds).toEqual(["chat-1", "chat-2"]);
    // The reader's OTHER window is company; this tab is not.
    expect(viewers.find((v) => v.userId === "u-me")?.chatIds).toEqual(["chat-1"]);
  });

  it("counts someone on the workspace's own roster without putting them in a chat", () => {
    const rosters = new Map<string, PresencePeer[]>([
      [WORKSPACE_ROSTER, [peer("p:self", "u-me"), peer("p:5", "u-ola", "Ola Ade"), peer("p:3", "u-dana")]],
      ["chat-2", [peer("p:3", "u-dana", "Dana Ruiz")]],
    ]);
    const viewers = workspaceViewersOf(rosters, "p:self");
    expect(viewers.find((v) => v.userId === "u-ola")?.chatIds).toEqual([]);
    expect(viewers.find((v) => v.userId === "u-dana")?.chatIds).toEqual(["chat-2"]);
    expect(viewers.some((v) => v.userId === "u-me")).toBe(false);
  });

  it("is nobody when only this tab is in the workspace", () => {
    expect(workspaceViewersOf(new Map([["chat-1", [peer("p:self", "u-me")]]]), "p:self")).toEqual([]);
  });
});

describe("useWorkspaceViewers", () => {
  const W = "doc:workspace:11111111-1111-4111-8111-111111111111";
  const C = "doc:chat:chat-1";

  it("joins the workspace's own channel, watches its chats, and names who else is there", async () => {
    setRealtimeClientForTests(client);
    const hook = renderHook(() => useWorkspaceViewers("11111111-1111-4111-8111-111111111111", ["chat-1"]));
    await act(async () => {
      await advance(0);
    });
    await act(async () => {
      sockets.last().welcome("p:self");
      await advance(0);
    });
    const socket = sockets.last();
    expect(socket.framesOf("subscribe").map((f) => f.channel)).toEqual(expect.arrayContaining([W, C]));
    await act(async () => {
      socket.serverSend({ t: "subscribed", channel: W, can_write: false });
      socket.serverSend({ t: "subscribed", channel: C, can_write: false });
      await advance(0);
    });
    // Being on the workspace is being in it: the workspace is joined, the
    // chat is only watched.
    expect(socket.framesOf("presence.join").map((f) => f.channel)).toEqual([W]);
    await act(async () => {
      socket.serverSend({
        t: "presence",
        channel: W,
        event: "roster",
        peers: [peer("p:self", "u-me"), peer("p:7", "u-ola", "Ola Ade")],
      });
      socket.serverSend({ t: "presence", channel: C, event: "roster", peers: [peer("p:8", "u-dana", "Dana Ruiz")] });
      await advance(0);
    });
    expect(hook.result.current.map((v) => [v.name, v.chatIds])).toEqual([
      ["Dana Ruiz", ["chat-1"]],
      ["Ola Ade", []],
    ]);
    hook.unmount();
    expect(socket.framesOf("presence.leave").map((f) => f.channel)).toEqual([W]);
  });
});
