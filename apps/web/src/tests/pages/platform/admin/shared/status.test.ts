import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { tokenGrade } from "@/pages/platform/admin/shared/status";

// A proxy token's grade decides which actions a registrar can take (only an active token rotates /
// revokes) and which status chip shows. The order of precedence is the invariant: revoked beats an
// elapsed expiry beats active. Pinned against a frozen clock so "expired" is deterministic.

const FROZEN_NOW = new Date("2026-06-30T12:00:00Z");

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(FROZEN_NOW);
});
afterEach(() => {
  vi.useRealTimers();
});

describe("tokenGrade", () => {
  it("is active when neither revoked nor expired", () => {
    expect(tokenGrade({ revoked_at: null, expires_at: null })).toBe("active");
  });

  it("is active when the expiry is in the future", () => {
    expect(tokenGrade({ revoked_at: null, expires_at: "2027-01-01T00:00:00Z" })).toBe("active");
  });

  it("is expired once the expiry has elapsed", () => {
    expect(tokenGrade({ revoked_at: null, expires_at: "2026-06-01T00:00:00Z" })).toBe("expired");
  });

  it("is revoked even when also expired (revocation wins)", () => {
    expect(tokenGrade({ revoked_at: "2026-05-01T00:00:00Z", expires_at: "2026-06-01T00:00:00Z" })).toBe("revoked");
  });

  it("is revoked even when the expiry is still in the future", () => {
    expect(tokenGrade({ revoked_at: "2026-06-29T00:00:00Z", expires_at: "2027-01-01T00:00:00Z" })).toBe("revoked");
  });

  it("flips active → expired exactly at the expiry boundary as the clock advances past it", () => {
    const token = { revoked_at: null, expires_at: "2026-06-30T18:00:00Z" };
    expect(tokenGrade(token)).toBe("active"); // 12:00, before 18:00
    vi.setSystemTime(new Date("2026-06-30T18:00:01Z"));
    expect(tokenGrade(token)).toBe("expired"); // one second past 18:00
  });
});
