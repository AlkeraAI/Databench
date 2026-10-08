// The wait between two attempts at the page above. The sentinel that asks for
// it re-arms as soon as a request settles, so this ladder is the only thing
// between a refusing server and one request per round trip for the life of the
// tab.

import { describe, expect, it } from "vitest";

import { HISTORY_RETRY_ATTEMPTS, HISTORY_RETRY_CAP_MS, HISTORY_RETRY_FLOOR_MS } from "@/lib/limits";
import { historyRetryDelayMs } from "@/pages/workspace/chat/controller/useChatTranscript";

describe("historyRetryDelayMs", () => {
  it("doubles from the floor and never drops back", () => {
    const waits = [0, 1, 2, 3].map((attempt) => historyRetryDelayMs(attempt, 0));
    expect(waits).toEqual([
      HISTORY_RETRY_FLOOR_MS,
      HISTORY_RETRY_FLOOR_MS * 2,
      HISTORY_RETRY_FLOOR_MS * 4,
      HISTORY_RETRY_FLOOR_MS * 8,
    ]);
  });

  it("reaches the cap on a rung the ladder actually arms", () => {
    // A cap the attempt count never climbs to is a number that documents a
    // wait nothing waits, so the two are derived together.
    const armed = Array.from({ length: HISTORY_RETRY_ATTEMPTS - 1 }, (_, n) =>
      historyRetryDelayMs(n, 0),
    );
    expect(armed).toContain(HISTORY_RETRY_CAP_MS);
    expect(Math.max(...armed)).toBe(HISTORY_RETRY_CAP_MS);
  });

  it("tops out at the cap however long the outage lasts", () => {
    for (const attempt of [8, 20, 200, 10_000]) {
      expect(historyRetryDelayMs(attempt, 0)).toBe(HISTORY_RETRY_CAP_MS);
      expect(historyRetryDelayMs(attempt, 0.9)).toBeLessThanOrEqual(
        HISTORY_RETRY_CAP_MS + HISTORY_RETRY_FLOOR_MS,
      );
    }
  });

  it("spreads a fleet of tabs across one floor rather than bringing them back in step", () => {
    const low = historyRetryDelayMs(2, 0);
    const high = historyRetryDelayMs(2, 0.999);
    expect(high - low).toBeGreaterThan(HISTORY_RETRY_FLOOR_MS * 0.9);
    expect(high - low).toBeLessThan(HISTORY_RETRY_FLOOR_MS);
  });

  it("never returns less than the floor, whatever it is handed", () => {
    for (const jitter of [-5, 0, 1, 50, Number.NaN]) {
      expect(historyRetryDelayMs(0, jitter)).toBeGreaterThanOrEqual(HISTORY_RETRY_FLOOR_MS);
    }
    expect(historyRetryDelayMs(-3, 0)).toBe(HISTORY_RETRY_FLOOR_MS);
    // A wait is armed on a timer, and `setTimeout(fn, NaN)` fires at once.
    expect(historyRetryDelayMs(Number.NaN, 0)).toBe(HISTORY_RETRY_FLOOR_MS);
    expect(historyRetryDelayMs(Number.POSITIVE_INFINITY, 0)).toBe(HISTORY_RETRY_FLOOR_MS);
  });
});
