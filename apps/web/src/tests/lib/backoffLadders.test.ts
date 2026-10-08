// Every client-side doubling wait, as the attempt -> milliseconds table each
// call site arms.
//
// The sites share one shape (a floor doubled per attempt, held at a cap) but
// each picks its own floor, cap, exponent offset and jitter. These tables pin
// what each one actually waits, so moving a site onto the shared primitive is
// provably a move and not a retune.

import { describe, expect, it } from "vitest";

import { ApiError } from "@/api/errors";
import { queryRetryDelay } from "@/api/retry";
import { ladderDelay, retryLadder } from "@/lib/limits";
import { historyRetryDelayMs } from "@/pages/workspace/chat/controller/useChatTranscript";

/** A failure that names no wait of its own, so the site's own ladder decides. */
const unnamed = new ApiError(503, { error: { code: "unavailable", message: "busy" } }, "busy");

describe("ladderDelay", () => {
  it("climbs a ladder's rungs from its floor and holds at its cap", () => {
    const ladder = retryLadder(250, 1_000);
    expect([0, 1, 2, 3, 4].map((rung) => ladderDelay(rung, ladder))).toEqual([250, 500, 1_000, 1_000, 1_000]);
  });
});

describe("the portal's read ladder (queryRetryDelay)", () => {
  it.each([
    { attempt: 0, low: 1_000, high: 1_300 },
    { attempt: 1, low: 2_000, high: 2_600 },
    { attempt: 2, low: 4_000, high: 5_200 },
    { attempt: 3, low: 8_000, high: 10_400 },
    { attempt: 4, low: 16_000, high: 20_800 },
    { attempt: 5, low: 30_000, high: 39_000 },
    { attempt: 12, low: 30_000, high: 39_000 },
  ])("waits $low..$high ms before retry $attempt", ({ attempt, low, high }) => {
    expect(queryRetryDelay(attempt, unnamed, () => 0)).toBe(low);
    expect(queryRetryDelay(attempt, null, () => 0)).toBe(low);
    expect(queryRetryDelay(attempt, unnamed, () => 1)).toBe(high);
  });
});

describe("the earlier-messages ladder (historyRetryDelayMs)", () => {
  it.each([
    { attempt: 0, base: 1_000 },
    { attempt: 1, base: 2_000 },
    { attempt: 2, base: 4_000 },
    { attempt: 3, base: 8_000 },
    { attempt: 4, base: 16_000 },
    { attempt: 5, base: 30_000 },
    { attempt: 40, base: 30_000 },
    { attempt: 2.7, base: 4_000 },
  ])("waits $base ms plus up to one floor of jitter before retry $attempt", ({ attempt, base }) => {
    expect(historyRetryDelayMs(attempt, 0)).toBe(base);
    expect(historyRetryDelayMs(attempt, 0.5)).toBe(base + 500);
  });
});
