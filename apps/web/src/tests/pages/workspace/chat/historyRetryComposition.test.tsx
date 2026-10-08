// The retry ladder as the tape actually composes it: the hook that owns the
// attempt count, driven against a source that refuses, a source that resolves
// without progressing, and a source that says the chat is gone.
//
// The formula and the panel's `failed` rendering are pinned separately. What
// is pinned HERE is the thing the sweep found and nothing else defends: a page
// that will not come is asked for a BOUNDED number of times, the reader's own
// press starts the ladder over, and a settled answer stops it at one.

import { act, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "@/api/errors";
import { HISTORY_RETRY_ATTEMPTS } from "@/lib/limits";
import type { ChatDataSource } from "@/pages/workspace/chat/data/ChatDataSource";
import {
  createBrowserChatHost,
  installChatRuntime,
  resetChatRuntime,
} from "@/pages/workspace/chat/data";
import {
  useTranscriptHistory,
  type TranscriptHistory,
} from "@/pages/workspace/chat/controller/useChatTranscript";

const CHAT = "c1";

/** A source that always says there is a page above, and answers `answer()`. */
function source(answer: () => Promise<boolean | void>): { source: ChatDataSource; calls: () => number } {
  let calls = 0;
  const impl = {
    caps: { opencodeActive: false, modelCatalog: false },
    transcriptHistory: () => ({ hasOlder: true, loading: false }),
    loadOlderTurns: async () => {
      calls += 1;
      return answer();
    },
    releaseOlderTurns: () => undefined,
    subscribeChat: () => () => undefined,
  };
  return { source: impl as unknown as ChatDataSource, calls: () => calls };
}

/** Mount the hook alone and expose its latest value. The tape's sentinel is
 *  what calls `loadOlder` in the product; here the test plays that part, so
 *  the ladder is measured rather than the scroll geometry. */
function drive(): { read: () => TranscriptHistory; unmount: () => void } {
  let latest: TranscriptHistory | null = null;
  function Probe(): null {
    latest = useTranscriptHistory(CHAT);
    return null;
  }
  const view = render(<Probe />);
  return {
    read: () => {
      if (!latest) throw new Error("the hook never rendered");
      return latest;
    },
    unmount: view.unmount,
  };
}

/** Ask the way the sentinel does — on every render where nothing is in flight
 *  and the tape has not been told to stop — for `ticks` rounds of one minute,
 *  which is past every rung of the ladder. */
async function sentinel(read: () => TranscriptHistory, ticks: number): Promise<void> {
  for (let i = 0; i < ticks; i += 1) {
    const history = read();
    if (history.hasOlder && !history.loading && !history.failed) {
      await act(async () => {
        history.loadOlder();
        await Promise.resolve();
      });
    }
    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000);
    });
  }
}

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
  resetChatRuntime();
});

describe("the tape's retry ladder", () => {
  it("stops asking for a page that keeps failing, and offers the reader the retry", async () => {
    const refusing = source(() => Promise.reject(new ApiError(500, null)));
    installChatRuntime({ source: refusing.source, host: createBrowserChatHost() });
    const probe = drive();

    await sentinel(probe.read, 12);

    expect(refusing.calls()).toBe(HISTORY_RETRY_ATTEMPTS);
    expect(probe.read().failed).toBe(true);
    expect(probe.read().hasOlder).toBe(true);
    probe.unmount();
  });

  it("treats a page that resolves without bringing anything as the same standstill", async () => {
    // The shape a lost paging cursor produces: the request settles, nothing
    // moves, and the sentinel would otherwise re-arm on the very next render.
    const standstill = source(() => Promise.resolve(false));
    installChatRuntime({ source: standstill.source, host: createBrowserChatHost() });
    const probe = drive();

    await sentinel(probe.read, 12);

    expect(standstill.calls()).toBe(HISTORY_RETRY_ATTEMPTS);
    expect(probe.read().failed).toBe(true);
    probe.unmount();
  });

  it("starts the ladder over when the reader presses Retry", async () => {
    const refusing = source(() => Promise.reject(new ApiError(503, null)));
    installChatRuntime({ source: refusing.source, host: createBrowserChatHost() });
    const probe = drive();

    await sentinel(probe.read, 12);
    expect(refusing.calls()).toBe(HISTORY_RETRY_ATTEMPTS);

    await act(async () => {
      probe.read().loadOlder();
      await Promise.resolve();
    });
    await sentinel(probe.read, 12);

    expect(refusing.calls()).toBe(HISTORY_RETRY_ATTEMPTS * 2);
    probe.unmount();
  });

  it("stops at one attempt, with nothing above, when the chat is gone", async () => {
    const missing = source(() => Promise.reject(new ApiError(404, null)));
    installChatRuntime({ source: missing.source, host: createBrowserChatHost() });
    const probe = drive();

    await sentinel(probe.read, 12);

    expect(missing.calls()).toBe(1);
    expect(probe.read().hasOlder).toBe(false);
    expect(probe.read().failed).toBe(false);
    probe.unmount();
  });

  it("goes back to asking freely after a page finally lands", async () => {
    let refuse = true;
    const flaky = source(() => (refuse ? Promise.reject(new ApiError(500, null)) : Promise.resolve(true)));
    installChatRuntime({ source: flaky.source, host: createBrowserChatHost() });
    const probe = drive();

    await sentinel(probe.read, 3);
    const duringOutage = flaky.calls();
    expect(duringOutage).toBeGreaterThan(0);
    expect(duringOutage).toBeLessThan(HISTORY_RETRY_ATTEMPTS);

    refuse = false;
    await sentinel(probe.read, 2);
    expect(probe.read().failed).toBe(false);

    // And the ladder is back at its first rung: a fresh outage gets the full count.
    refuse = true;
    await sentinel(probe.read, 12);
    expect(flaky.calls()).toBeGreaterThanOrEqual(duringOutage + 2 + HISTORY_RETRY_ATTEMPTS);
    probe.unmount();
  });
});
