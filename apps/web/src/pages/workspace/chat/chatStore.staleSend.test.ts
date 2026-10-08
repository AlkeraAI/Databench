// The stale-session prompt's state machine. When the daemon refuses a send
// because it no longer holds the chat's session, the store holds the message so
// the user's one action can re-open and deliver it. Every other rejection leaves
// the prompt untouched, and nothing re-opens until the user asks.

import { afterEach, describe, expect, it, vi } from "vitest";

const { ds } = vi.hoisted(() => ({
  ds: {
    getChatTurns: vi.fn(async () => [] as unknown[]),
    sendUserMessage: vi.fn(async () => ({ id: "m1", role: "user", content: "x" })),
    reopenChat: vi.fn(async () => undefined),
    subscribeChat: vi.fn(() => () => {}),
  },
}));

// Partial mock: the store runs against the REAL isSessionNotOpen, so the
// recognition rule is exercised here instead of being restated by a stub.
vi.mock("./data", async (importOriginal) => ({
  ...(await importOriginal<typeof import("./data")>()),
  chatData: () => ds,
}));

import { useChatStore } from "./chatStore";

const OPTS = { model: { id: "m", displayName: "M", wire: "anthropic" as const, efforts: [], defaultEffort: null }, effort: "high" };
const REFUSED = { code: -32002, message: "session c1 is not open" };

const flush = async (): Promise<void> => {
  for (let i = 0; i < 5; i += 1) await Promise.resolve();
};

/** Send a message the daemon rejects with `err`, and settle the failure path. */
async function sendRejectedWith(err: unknown): Promise<void> {
  ds.sendUserMessage.mockRejectedValueOnce(err);
  useChatStore.getState().send("c1", "Hi", OPTS);
  await flush();
}

const entry = () => useChatStore.getState().byId.c1;

afterEach(() => {
  vi.clearAllMocks();
  useChatStore.setState({ byId: {} });
});

describe("chatStore stale-session prompt", () => {
  it("holds the refused message and its options for an unchanged retry", async () => {
    await sendRejectedWith(REFUSED);

    expect(entry().staleSend).toEqual({ text: "Hi", opts: OPTS });
    // The failure path also refetches the transcript; that must not erase the hold.
    expect(ds.getChatTurns).toHaveBeenCalledWith("c1");
    expect(entry().staleSend).toEqual({ text: "Hi", opts: OPTS });
  });

  it("never re-opens on its own -- the refusal only raises the prompt", async () => {
    await sendRejectedWith(REFUSED);

    expect(ds.reopenChat).not.toHaveBeenCalled();
    expect(ds.sendUserMessage).toHaveBeenCalledTimes(1);
  });

  it.each([
    ["the auth rejection, which the login panel owns", { code: -32001, message: "AUTH_REQUIRED" }],
    ["a generic daemon failure", { code: -32000, message: "boom" }],
    ["a transport Error carrying no code", new Error("socket closed by the fake")],
  ])("leaves the prompt down for %s", async (_label, err) => {
    await sendRejectedWith(err);

    expect(entry().staleSend).toBeNull();
    expect(ds.reopenChat).not.toHaveBeenCalled();
  });

  const clearingActions: { label: string; act: () => (() => void) | void; sends: number }[] = [
    { label: "a new send starts", act: () => useChatStore.getState().send("c1", "Something else"), sends: 1 },
    { label: "the chat is opened again", act: () => useChatStore.getState().open("c1"), sends: 0 },
    { label: "the user dismisses it", act: () => useChatStore.getState().dismissStaleSend("c1"), sends: 0 },
  ];

  it.each(clearingActions)("clears the prompt when $label, and re-opens nothing", async ({ act, sends }) => {
    await sendRejectedWith(REFUSED);
    ds.sendUserMessage.mockClear();

    const cleanup = act();

    // Synchronously: a prompt that outlives the action it answers is a prompt
    // the user can click twice.
    expect(entry().staleSend).toBeNull();
    await flush();
    expect(entry().staleSend).toBeNull();
    expect(ds.reopenChat).not.toHaveBeenCalled();
    expect(ds.sendUserMessage).toHaveBeenCalledTimes(sends);
    if (typeof cleanup === "function") cleanup();
  });

  it("re-opens BEFORE resending, and resends the same message and options", async () => {
    await sendRejectedWith(REFUSED);
    ds.sendUserMessage.mockClear();

    useChatStore.getState().reopenAndResend("c1");
    await flush();

    expect(ds.reopenChat).toHaveBeenCalledWith("c1");
    expect(ds.sendUserMessage).toHaveBeenCalledWith("c1", "Hi", { ...OPTS, clientId: expect.any(String) });
    // A resend that raced an unopened chat would be refused all over again.
    expect(ds.reopenChat.mock.invocationCallOrder[0]).toBeLessThan(
      ds.sendUserMessage.mock.invocationCallOrder[0],
    );
    expect(entry().staleSend).toBeNull();
    expect(entry().sending).toBe(true);
  });

  it("does nothing when asked to resend with nothing held", async () => {
    useChatStore.getState().reopenAndResend("c1");
    await flush();

    expect(ds.reopenChat).not.toHaveBeenCalled();
    expect(ds.sendUserMessage).not.toHaveBeenCalled();
  });

});
