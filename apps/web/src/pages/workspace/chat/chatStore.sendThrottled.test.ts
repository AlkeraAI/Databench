// A send the rate limiter refused.
//
// Five tabs of one reader hitting Send inside the same second is an ordinary
// thing to do, and the limiter answers the later ones 429. Before this, a 429
// fell through every arm of the send's rejection handler: the optimistic bubble
// stayed in the transcript (a message the colleague on the other side could
// never see), `sending` flipped off, the composer had already cleared the field,
// and the reader was told nothing. The words were simply gone.
//
// So: the bubble is withdrawn, the words come back to the field, a line says
// what happened, and the send is re-offered ONCE on the wait the server named.
// A second refusal is not re-armed — the limiter is asking for a rest, and a
// client that keeps re-sending spends the budget it was just told it is out of.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "@/api/errors";
import { isSendThrottled, sendThrottleWaitMs, useChatStore } from "./chatStore";

/** A real 429 as the transport builds one: the server's envelope parsed by the
 *  real error type, with `Retry-After` read off real headers. Nothing here is a
 *  hand-shaped rejection that happens to match what the code looks for. */
function throttle(retryAfter?: string): ApiError {
  return new ApiError(
    429,
    {
      detail: {
        code: "rate_limited",
        message: "You are sending messages very quickly. Please pause for a moment.",
      },
    },
    "the request to /api/v1/chats/c1/messages failed",
    new Headers(retryAfter === undefined ? {} : { "retry-after": retryAfter }),
  );
}

const sends: string[] = [];
let answer: (attempt: number) => Promise<unknown> = async () => ({});

vi.mock("./data", async (importOriginal) => {
  const real = await importOriginal<typeof import("./data")>();
  return {
    ...real,
    chatData: () => ({
      sendUserMessage: (_chatId: string, content: string) => {
        sends.push(content);
        return answer(sends.length - 1);
      },
      getChatTurns: () => [],
      getChatActivity: () => ({}),
    }),
  };
});

const entry = () => useChatStore.getState().byId.c1;

/** Let the floated `.then` handlers run without advancing the fake clock. */
const settle = async (): Promise<void> => {
  await vi.advanceTimersByTimeAsync(0);
};

beforeEach(() => {
  vi.useFakeTimers();
  // The retry's spread is pinned so the wait is exactly what the server named.
  vi.spyOn(Math, "random").mockReturnValue(0);
  sends.length = 0;
  useChatStore.setState({ byId: {} });
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("a send the limiter refuses", () => {
  it("withdraws the bubble, hands the words back, and says the wait the server named", async () => {
    answer = async () => {
      throw throttle("7");
    };
    useChatStore.getState().send("c1", "the message that must not be lost");
    // The bubble is up synchronously — that is the optimistic send working.
    expect(entry().optimistic).toHaveLength(1);
    await settle();

    expect(entry().optimistic).toHaveLength(0);
    expect(entry().sending).toBe(false);
    expect(entry().sendRefusal).toBe("Sending too quickly. Retrying in 7 seconds.");
    expect(entry().returnedDraft?.text).toBe("the message that must not be lost");
  });

  it("re-sends once on the server's wait, and not before it", async () => {
    answer = async (attempt) => {
      if (attempt === 0) throw throttle("7");
      return { id: "m1", role: "user", content: "hi" };
    };
    useChatStore.getState().send("c1", "hi");
    await settle();
    expect(sends).toHaveLength(1);

    // Nothing goes out during the wait the server asked for.
    await vi.advanceTimersByTimeAsync(6_900);
    expect(sends).toHaveLength(1);

    await vi.advanceTimersByTimeAsync(200);
    expect(sends).toEqual(["hi", "hi"]);
    // The retry landed, so the line and the withdrawn bubble are gone.
    expect(entry().sendRefusal).toBeNull();
  });

  it("leaves the words, the line and a live Send after a SECOND refusal", async () => {
    answer = async () => {
      throw throttle("2");
    };
    useChatStore.getState().send("c1", "twice refused");
    await settle();
    await vi.advanceTimersByTimeAsync(2_000);
    await settle();

    expect(sends).toEqual(["twice refused", "twice refused"]);
    // The automatic retry is spent: no third attempt, however long we wait.
    await vi.advanceTimersByTimeAsync(120_000);
    expect(sends).toHaveLength(2);

    expect(entry().sendRefusal).toBe("Sending too quickly. Try again in a moment.");
    expect(entry().returnedDraft?.text).toBe("twice refused");
    // Nothing is in flight, so the composer's Send is live again.
    expect(entry().sending).toBe(false);
    expect(entry().optimistic).toHaveLength(0);
  });

  it("gives each refusal its own stamp, so a second one still reaches the field", async () => {
    answer = async () => {
      throw throttle("1");
    };
    useChatStore.getState().send("c1", "first");
    await settle();
    const first = entry().returnedDraft;
    await vi.advanceTimersByTimeAsync(1_000);
    await settle();

    expect(entry().returnedDraft?.at).toBeGreaterThan(first?.at ?? 0);
  });
});

describe("the wait a throttled send takes", () => {
  it.each([
    ["the server's own seconds", "7", 7_000],
    ["a header the cap says is a mistake", "9999", 60_000],
    // A proxy that spells the header badly must not license an instant re-send
    // of the burst that was just refused.
    ["a zero", "0", 1_000],
    ["a negative", "-5", 1_000],
    ["an unparseable one", "soon", 1_000],
  ])("reads %s as %sms", (_name, header, expected) => {
    expect(sendThrottleWaitMs(throttle(header as string), () => 0)).toBe(expected);
  });

  it("falls back to the backoff floor when the server named no wait", () => {
    expect(sendThrottleWaitMs(throttle(), () => 0)).toBe(1_000);
  });
});

describe("what counts as throttled", () => {
  it.each([
    [429, true],
    [403, false],
    [409, false],
    [500, false],
  ])("a %i is %s", (status, expected) => {
    expect(isSendThrottled(new ApiError(status, null))).toBe(expected);
  });

  it("is not tripped by a plain transport failure", () => {
    expect(isSendThrottled(new Error("network down"))).toBe(false);
    expect(isSendThrottled(null)).toBe(false);
  });
});

describe("the spread on a throttled send's wait", () => {
  it("adds up to a third of the wait and never subtracts from it", () => {
    // Five tabs of one reader are handed the same `Retry-After`; without the
    // spread all five retry in the same millisecond and are refused again.
    expect(sendThrottleWaitMs(throttle("10"), () => 0)).toBe(10_000);
    expect(sendThrottleWaitMs(throttle("10"), () => 1)).toBe(13_000);
    expect(sendThrottleWaitMs(throttle("10"), () => 0.5)).toBe(11_500);
  });
});
