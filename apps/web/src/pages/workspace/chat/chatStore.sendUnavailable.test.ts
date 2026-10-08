// A send the server could not take: a 5xx (a database stall answers 503 with a
// Retry-After), or no answer at all.
//
// The message is never dropped. Its bubble goes (it was not recorded, so a
// bubble reading "Waiting for a machine…" under it would be a lie), and the
// words become a queue row marked "Not sent" that is written down in this
// browser, so a reload or a closed tab keeps them. The row is sent again on a
// climbing backoff that honours the server's Retry-After, under ONE client id,
// so a retry of a message whose answer was lost is recorded once. When the
// retries are spent the row stays with a Retry of its own. A send still on the
// wire when the page went away comes back as such a row too.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "@/api/errors";
import {
  SEND_UNAVAILABLE_RETRIES,
  isSendUnavailable,
  sendUnavailableWaitMs,
  useChatStore,
} from "./chatStore";
import { readQueued } from "./queuedMessages";

function stalled(retryAfter?: string): ApiError {
  return new ApiError(
    503,
    { detail: { code: "db_lock_timeout", message: "The database is busy" } },
    "the request to /api/v1/chats/c1/messages failed",
    new Headers(retryAfter === undefined ? {} : { "retry-after": retryAfter }),
  );
}

/** Who the queue and the sends on the wire are written down for. */
const ME = { userId: "usr_dana", orgId: "org_a" };

const sends: { content: string; clientId: string | undefined }[] = [];
let answer: (attempt: number) => Promise<unknown> = async () => ({});

vi.mock("./data", async (importOriginal) => {
  const real = await importOriginal<typeof import("./data")>();
  return {
    ...real,
    chatHost: () => ({ account: () => ({ email: null, webAppUrl: null, ...ME }) }),
    chatData: () => ({
      sendUserMessage: (_chatId: string, content: string, opts?: { clientId?: string }) => {
        sends.push({ content, clientId: opts?.clientId });
        return answer(sends.length - 1);
      },
      getChatTurns: async () => [],
      getChatActivity: () => ({}),
      subscribeChat: () => () => {},
    }),
  };
});

const entry = () => useChatStore.getState().byId.c1;
const settle = async (): Promise<void> => {
  await vi.advanceTimersByTimeAsync(0);
};
/** A fresh page: the store as a new module holds it, the browser's storage kept. */
const reload = (): void => {
  // A page that goes away takes its timers with it.
  vi.clearAllTimers();
  useChatStore.setState({ byId: {} });
};
const never = (): Promise<unknown> => new Promise(() => {});

beforeEach(() => {
  vi.useFakeTimers();
  vi.spyOn(Math, "random").mockReturnValue(0);
  sends.length = 0;
  window.localStorage.clear();
  useChatStore.setState({ byId: {} });
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
  window.localStorage.clear();
});

describe("isSendUnavailable", () => {
  it.each([
    ["a 503", stalled(), true],
    ["a 500", new ApiError(500, null), true],
    ["no answer at all", new TypeError("Failed to fetch"), true],
    ["a 429, which is the limiter's own path", new ApiError(429, null), false],
    ["a refusal", new ApiError(403, { code: "forbidden" }), false],
    ["a bad request", new ApiError(422, null), false],
  ])("%s: %s", (_why, err, expected) => {
    expect(isSendUnavailable(err)).toBe(expected);
  });
});

describe("a send the server could not take", () => {
  it("withdraws the bubble and keeps the words as a written-down row marked not sent", async () => {
    answer = async () => {
      throw stalled("4");
    };
    useChatStore.getState().send("c1", "the message that must not be lost");
    await settle();
    expect(entry().optimistic).toHaveLength(0);
    expect(entry().sending).toBe(false);
    expect(entry().sendRefusal).toBe("Couldn't send. Retrying in 4 seconds.");
    // Held in ONE place: the row, not the composer as well.
    expect(entry().returnedDraft).toBeNull();
    expect(entry().queued).toEqual([
      expect.objectContaining({ text: "the message that must not be lost", unsent: true }),
    ]);
    expect(readQueued(ME, "c1").map((row) => row.text)).toEqual(["the message that must not be lost"]);
  });

  it("retries on the server's wait under one client id, and the row goes when it lands", async () => {
    answer = async (attempt) => {
      if (attempt < 2) throw stalled("3");
      return { id: "m1", role: "user", content: "hi" };
    };
    useChatStore.getState().send("c1", "hi");
    await settle();
    await vi.advanceTimersByTimeAsync(2_900);
    expect(sends).toHaveLength(1);
    await vi.advanceTimersByTimeAsync(200);
    await settle();
    await vi.advanceTimersByTimeAsync(3_100);
    await settle();
    expect(sends.map((s) => s.content)).toEqual(["hi", "hi", "hi"]);
    // One message, one identity: the server records it once even if an
    // earlier attempt reached it before its answer was lost.
    expect(new Set(sends.map((s) => s.clientId)).size).toBe(1);
    expect(sends[0]?.clientId).toBeTruthy();
    expect(entry().sendRefusal).toBeNull();
    expect(entry().queued).toEqual([]);
    expect(readQueued(ME, "c1")).toEqual([]);
  });

  it("climbs the wait when the server names none", () => {
    expect(sendUnavailableWaitMs(new TypeError("x"), 0, () => 0)).toBeLessThan(
      sendUnavailableWaitMs(new TypeError("x"), 2, () => 0),
    );
  });

  it("stops after its retries, keeps the row with a Retry, and the Retry is the same message", async () => {
    answer = async () => {
      throw stalled("1");
    };
    useChatStore.getState().send("c1", "still stalled");
    for (let i = 0; i <= SEND_UNAVAILABLE_RETRIES; i += 1) {
      await settle();
      await vi.advanceTimersByTimeAsync(60_000);
    }
    await settle();
    expect(sends).toHaveLength(SEND_UNAVAILABLE_RETRIES + 1);
    await vi.advanceTimersByTimeAsync(600_000);
    expect(sends).toHaveLength(SEND_UNAVAILABLE_RETRIES + 1);
    expect(entry().sendRefusal).toBeNull();
    expect(entry().sending).toBe(false);
    expect(entry().optimistic).toHaveLength(0);
    const [row] = entry().queued;
    expect(row).toMatchObject({ text: "still stalled", unsent: true });

    answer = async () => ({ id: "m1", role: "user", content: "still stalled" });
    useChatStore.getState().sendQueued("c1", row!.id);
    await settle();
    expect(sends).toHaveLength(SEND_UNAVAILABLE_RETRIES + 2);
    expect(sends.at(-1)?.clientId).toBe(sends[0]?.clientId);
    expect(entry().queued).toEqual([]);
  });

  it("sends nothing more once the reader takes the row back", async () => {
    answer = async () => {
      throw stalled("2");
    };
    useChatStore.getState().send("c1", "never mind");
    await settle();
    const [row] = entry().queued;
    useChatStore.getState().removeQueued("c1", row!.id);
    await vi.advanceTimersByTimeAsync(600_000);
    expect(sends).toHaveLength(1);
    expect(readQueued(ME, "c1")).toEqual([]);
  });

  it("keeps a later message behind the one that did not go, in the order they were sent", async () => {
    answer = async () => {
      throw stalled("30");
    };
    useChatStore.getState().send("c1", "first");
    await settle();
    useChatStore.getState().send("c1", "second");
    await settle();
    expect(sends).toHaveLength(1);
    expect(entry().queued.map((row) => row.text)).toEqual(["first", "second"]);
  });

  it("gives a fresh send of the same words a fresh identity", async () => {
    answer = async () => ({ id: "m", role: "user", content: "same" });
    useChatStore.getState().send("c1", "same");
    await settle();
    useChatStore.getState().send("c1", "same");
    await settle();
    expect(sends).toHaveLength(2);
    expect(sends[0]?.clientId).not.toBe(sends[1]?.clientId);
  });
});

describe("after a reload", () => {
  it("brings back a row that had not gone, and does not send it on its own", async () => {
    answer = async () => {
      throw stalled("5");
    };
    useChatStore.getState().send("c1", "typed before the reload");
    await settle();
    reload();
    useChatStore.getState().open("c1");
    await settle();
    await vi.advanceTimersByTimeAsync(600_000);
    expect(sends).toHaveLength(1);
    expect(entry().queued).toEqual([
      expect.objectContaining({ text: "typed before the reload", unsent: true }),
    ]);
  });

  it("brings back a send that was still on the wire when the page went away", async () => {
    answer = never;
    useChatStore.getState().send("c1", "sent as the tab closed");
    await settle();
    expect(sends).toHaveLength(1);
    reload();
    useChatStore.getState().open("c1");
    await settle();
    const [row] = entry().queued;
    expect(row).toMatchObject({ text: "sent as the tab closed", unsent: true });
    // Retried as the same message, so if the first one did land it is not
    // recorded twice.
    answer = async () => ({ id: "m1", role: "user", content: "sent as the tab closed" });
    useChatStore.getState().sendQueued("c1", row!.id);
    await settle();
    expect(sends[1]?.clientId).toBe(sends[0]?.clientId);
    expect(entry().queued).toEqual([]);
    // Nothing comes back on the next reload: the wire record was taken over.
    reload();
    useChatStore.getState().open("c1");
    await settle();
    expect(entry().queued).toEqual([]);
  });

  it("leaves nothing behind for a send the server took", async () => {
    answer = async () => ({ id: "m1", role: "user", content: "landed" });
    useChatStore.getState().send("c1", "landed");
    await settle();
    reload();
    useChatStore.getState().open("c1");
    await settle();
    expect(entry().queued).toEqual([]);
  });
});
