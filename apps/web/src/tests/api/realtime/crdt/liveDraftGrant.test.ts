// A role change reaches the chat's live draft as a fresh `subscribed` frame.
//
// The server re-sends the channel's grant the moment an owner raises or
// lowers a reader's role. The draft reports that change, and only that: the
// grant the channel was opened with is the reader's role as it already was,
// not a change to it, and reporting it would re-read the composer's rows on
// every chat open.

import { afterEach, describe, expect, it } from "vitest";

import { acquireLiveDraft, closeAllLiveDrafts } from "@/api/realtime/crdt/liveDraft";

import { FakeSocket, LiveServer, loroNode } from "./liveServer";

afterEach(() => {
  closeAllLiveDrafts();
});

/** Open the draft for a reader the server first answers with `canWrite`, and
 *  carry it until the channel is live. */
async function open(canWrite: boolean): Promise<{ socket: FakeSocket; channel: string; grants: boolean[] }> {
  const server = new LiveServer();
  const socket = server.socket("bea");
  const held = acquireLiveDraft("c1", { socket, loadLoro: () => Promise.resolve(loroNode), hueOf: () => 0, account: null });
  const grants: boolean[] = [];
  held.draft.onGrantChange((granted) => grants.push(granted));
  const channel = [...socket.held][0]!;
  // The first answer to the subscribe is the role the reader opens with.
  const first = socket.inbox[0];
  if (first?.t === "subscribed") first.can_write = canWrite;
  await socket.deliver();
  await socket.deliver();
  return { socket, channel, grants };
}

describe("LiveDraft.onGrantChange", () => {
  it("reports a reader raised to edit after the channel opened read-only", async () => {
    const { socket, channel, grants } = await open(false);
    expect(grants).toEqual([]);
    socket.inbox.push({ t: "subscribed", channel, can_write: true });
    await socket.deliver();
    expect(grants).toEqual([true]);
  });

  it("does not report the grant an editor opens with, only a later change to it", async () => {
    const { socket, channel, grants } = await open(true);
    expect(grants).toEqual([]);
    socket.inbox.push({ t: "subscribed", channel, can_write: false });
    await socket.deliver();
    expect(grants).toEqual([false]);
  });

  it("does not report a grant re-sent unchanged", async () => {
    const { socket, channel, grants } = await open(true);
    socket.inbox.push({ t: "subscribed", channel, can_write: true });
    await socket.deliver();
    expect(grants).toEqual([]);
  });
});
