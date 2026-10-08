// A throttled send, from the side the reader is on — and from the two sides
// they are NOT.
//
// `doSend` serves three callers, and only one of them is words in the composer:
// the reader's own send, the queue releasing a message when a turn ends, and
// the reopen prompt re-delivering one the daemon refused. Handing every refusal
// back to the field was wrong for two of the three, and the automatic retry the
// first of them arms was never cancelled — so pressing Send during the wait,
// which getting your words back invites, put the same message on the wire
// twice.
//
// Driven through the real store AND the real composer: the defects here are
// invisible from store state alone and obvious the moment a field is on screen.

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Composer } from "@alkera/ui";

import { ApiError } from "@/api/errors";
import { useChatStore } from "./chatStore";
import { readQueued } from "./queuedMessages";

function throttle(retryAfter: string): ApiError {
  return new ApiError(
    429,
    { detail: { code: "rate_limited", message: "You are sending messages very quickly." } },
    "the request to /api/v1/chats/c1/messages failed",
    new Headers({ "retry-after": retryAfter }),
  );
}

const sends: string[] = [];
let answer: (attempt: number) => Promise<unknown> = async () => ({});
/** Who the queue is written down for. */
const ME = { userId: "usr_dana", orgId: "org_a" };

vi.mock("./data", async (importOriginal) => {
  const real = await importOriginal<typeof import("./data")>();
  return {
    ...real,
    chatHost: () => ({ account: () => ({ email: null, webAppUrl: null, ...ME }) }),
    chatData: () => ({
      sendUserMessage: (_chatId: string, content: string) => {
        sends.push(content);
        return answer(sends.length - 1);
      },
      getChatTurns: () => [],
      getChatActivity: () => ({}),
      reopenChat: async () => undefined,
    }),
  };
});

const entry = () => useChatStore.getState().byId.c1;
const settle = async (): Promise<void> => {
  await vi.advanceTimersByTimeAsync(0);
};

/** The composer as `ChatSurface` wires it: the store's returned draft is the
 *  draft prop, and pressing Send is the store's own `send`. Nothing about the
 *  throttle is re-implemented here. */
function ChatField() {
  const returnedDraft = useChatStore((state) => state.byId.c1?.returnedDraft ?? null);
  return (
    <Composer
      modes={[{ value: "ask", label: "Ask" }]}
      mode="ask"
      models={[{ value: "m1", label: "M1" }]}
      model="m1"
      onModelChange={vi.fn()}
      efforts={[{ value: "low", label: "Low", bars: 1 as const }]}
      effort="low"
      draft={returnedDraft ?? undefined}
      onSend={(text: string) => useChatStore.getState().send("c1", text)}
    />
  );
}

const field = (): HTMLTextAreaElement => screen.getByLabelText("Message Databench");
const pressSend = (): void => {
  const button = screen.getByRole("button", { name: /send/i });
  button.click();
};

beforeEach(() => {
  vi.useFakeTimers();
  // The retry's spread is pinned so the wait is exactly what the server named.
  vi.spyOn(Math, "random").mockReturnValue(0);
  sends.length = 0;
  useChatStore.setState({ byId: {} });
  localStorage.clear();
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("the reader's own send during the wait", () => {
  it("cancels the armed retry — one message per intent, never two", async () => {
    answer = async () => {
      throw throttle("3");
    };
    render(<ChatField />);
    useChatStore.getState().send("c1", "impatient");
    await settle();
    // The words are back in the field, which is the invitation to press Send.
    expect(field().value).toBe("impatient");
    expect(sends).toEqual(["impatient"]);

    // The reader takes it. From here the refusals stop, so a surviving timer
    // would show up as a third send.
    answer = async () => ({ id: "m1" });
    pressSend();
    await settle();
    expect(sends).toEqual(["impatient", "impatient"]);

    // Past when the armed retry would have fired.
    await vi.advanceTimersByTimeAsync(10_000);
    expect(sends).toEqual(["impatient", "impatient"]);
    expect(entry().optimistic).toHaveLength(1);
  });

  it("re-sends now and disarms, so the wait never adds a second copy", async () => {
    answer = async () => {
      throw throttle("30");
    };
    render(<ChatField />);
    useChatStore.getState().send("c1", "twice over");
    await settle();

    answer = async () => ({ id: "m1" });
    pressSend();
    await settle();
    await vi.advanceTimersByTimeAsync(60_000);

    expect(sends).toEqual(["twice over", "twice over"]);
  });

  it("is cancelled by Stop as well — a retry is a send that goes by itself", async () => {
    answer = async () => {
      throw throttle("3");
    };
    useChatStore.getState().send("c1", "held");
    await settle();
    useChatStore.getState().cancel("c1");
    await vi.advanceTimersByTimeAsync(10_000);

    expect(sends).toEqual(["held"]);
  });
});

describe("the field after an automatic retry is taken", () => {
  it("is empty, with the message in the transcript", async () => {
    answer = async (attempt) => {
      if (attempt === 0) throw throttle("3");
      return { id: "m1" };
    };
    render(<ChatField />);
    useChatStore.getState().send("c1", "the words");
    await settle();
    expect(field().value).toBe("the words");

    await vi.advanceTimersByTimeAsync(3_000);
    await settle();

    expect(sends).toEqual(["the words", "the words"]);
    // The words became a message, so the copy of them in the field goes.
    expect(field().value).toBe("");
    expect(entry().optimistic).toHaveLength(1);
  });

  it("keeps a NEWER refusal's words — a stale success does not clear them", async () => {
    answer = async () => {
      throw throttle("3");
    };
    useChatStore.getState().send("c1", "first");
    await settle();
    // A different send is taken while the first one's words are in the field.
    answer = async () => ({ id: "m1" });
    useChatStore.getState().send("c1", "second");
    await settle();

    expect(entry().returnedDraft?.text).toBe("first");
  });
});

describe("a refused send the reader is not looking at", () => {
  it("puts the queued row back rather than painting it over the live draft", async () => {
    answer = async () => {
      throw throttle("3");
    };
    render(<ChatField />);
    useChatStore.setState({
      byId: {
        c1: {
          ...useChatStore.getState().byId.c1,
          base: [],
          optimistic: [],
          baseUserCount: 0,
          sending: false,
          loading: false,
          error: null,
          staleSend: null,
          sendRefusal: null,
          returnedDraft: null,
          released: false,
          queued: [{ id: "q1", text: "the queued one" }],
          queuedBehind: null,
        },
      },
    });
    useChatStore.getState().sendQueued("c1", "q1");
    await settle();

    expect(sends).toEqual(["the queued one"]);
    // The reader's field is untouched — they were typing something else.
    expect(field().value).toBe("");
    expect(entry().returnedDraft).toBeNull();
    // The row is back, and back under its own id, held until the reader says so.
    expect(entry().queued).toHaveLength(1);
    expect(entry().queued[0]?.id).toBe("q1");
    expect(entry().queued[0]?.text).toBe("the queued one");
    expect(entry().queued[0]?.restored).toBe(true);
    // And written down, so a reload does not lose it.
    expect(readQueued(ME, "c1").map((held) => held.text)).toEqual(["the queued one"]);
  });

  // The third caller — the reopen prompt's resend — routes through the same
  // intent and keeps its refusal record instead of touching the field. Driving
  // it end to end needs a fuller reopen/refresh fake than belongs in this file;
  // what is pinned here is that NOTHING but the composer's own send writes the
  // draft, which the two cases above cover from both sides.
});
