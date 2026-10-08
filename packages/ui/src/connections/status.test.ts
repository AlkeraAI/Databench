// The badge presentation: every badge has a label and a tone, and the words a
// person reads for each are pinned so the surfaces that show a connection cannot
// drift apart again. The server's badge derivation is checked against this
// vocabulary where that derivation lives.

import { describe, expect, it } from "vitest";

import {
  CONNECTION_BADGES,
  presentBadge,
  reauthLabel,
  verifiedAgo,
  type ConnectionBadge,
} from "./status";

const NOW = Date.parse("2026-09-14T12:00:00Z");

describe("the badge vocabulary", () => {
  it("is the twelve badges the words below pin", () => {
    expect(new Set(CONNECTION_BADGES)).toEqual(
      new Set([
        "connected",
        "stale",
        "verifying",
        "not_checked",
        "no_access",
        "unreachable",
        "error",
        "incomplete",
        "unsupported",
        "disabled",
        "muted",
        "needs_reauth",
      ]),
    );
  });

  it.each(CONNECTION_BADGES.map((b) => [b] as const))("%s has a label and a tone", (badge) => {
    const view = presentBadge({ badge }, NOW);
    expect(view.label.length).toBeGreaterThan(0);
    expect(["success", "neutral", "warning", "danger"]).toContain(view.tone);
  });

  it("names a badge it does not know instead of guessing", () => {
    const view = presentBadge({ badge: "quantum" }, NOW);
    expect(view.label).toBe("Unknown");
    expect(view.tone).toBe("neutral");
  });
});

describe("the words", () => {
  it.each([
    ["connected", "Connected", "success"],
    ["stale", "Connected", "neutral"],
    ["verifying", "Verifying…", "warning"],
    ["not_checked", "Not checked", "neutral"],
    ["no_access", "No access", "danger"],
    ["unreachable", "Unreachable", "danger"],
    ["error", "Error", "danger"],
    ["incomplete", "Ask your admin", "warning"],
    ["unsupported", "Update needed", "warning"],
    ["disabled", "Off", "neutral"],
    ["muted", "Hidden from agent", "neutral"],
  ] as const)("%s reads %s in %s", (badge, label, tone) => {
    const view = presentBadge({ badge: badge as ConnectionBadge }, NOW);
    expect(view.label).toBe(label);
    expect(view.tone).toBe(tone);
  });

  it("only Verifying… is busy", () => {
    for (const badge of CONNECTION_BADGES) {
      expect(presentBadge({ badge }, NOW).busy).toBe(badge === "verifying");
    }
  });

  it("a row that has been looked at says when, stale or not", () => {
    const twoHours = new Date(NOW - 2 * 3600_000).toISOString();
    expect(presentBadge({ badge: "connected", last_verified_at: twoHours }, NOW).note).toBe(
      "Verified 2h ago",
    );
    // Past the horizon the tone drops, the sentence does not change: a caption
    // about what has NOT happened since is not something a reader can act on.
    const twoDays = new Date(NOW - 2 * 24 * 3600_000).toISOString();
    const stale = presentBadge({ badge: "stale", last_verified_at: twoDays }, NOW);
    expect(stale.note).toBe("Verified 2d ago");
    expect(stale.tone).toBe("neutral");
  });

  it("says so plainly when there is no stamp at all", () => {
    for (const badge of ["connected", "stale"] as const) {
      expect(presentBadge({ badge }, NOW).note).toBe("Not verified yet");
      expect(presentBadge({ badge, last_verified_at: null }, NOW).note).toBe("Not verified yet");
    }
  });

  it("never says what has not happened since the last look", () => {
    const twoDays = new Date(NOW - 2 * 24 * 3600_000).toISOString();
    for (const badge of CONNECTION_BADGES) {
      const note = presentBadge({ badge, last_verified_at: twoDays }, NOW).note;
      expect(note).not.toMatch(/Nothing has checked it (since|recently)/);
      expect(note).not.toMatch(/^As of /);
    }
  });

  it("an infrastructure error says what happened and what to do, and stops", () => {
    const view = presentBadge({ badge: "error", outcome: "infrastructure" }, NOW);
    expect(view.label).toBe("Couldn't check");
    expect(view.tone).toBe("warning");
    // The middle sentence ("This is not a problem with the connection.") only
    // reassured: the reader is told to retry either way.
    expect(view.note).toBe("Couldn't run the check. Try again.");
    expect(view.note).not.toMatch(/not a problem with/);
    const plain = presentBadge({ badge: "error", outcome: "error" }, NOW);
    expect(plain.label).toBe("Error");
    expect(plain.tone).toBe("danger");
  });

  it("a timeout reads differently from a refused host", () => {
    expect(presentBadge({ badge: "unreachable", outcome: "timeout" }, NOW).note).toBe(
      "The host took too long to answer.",
    );
    expect(presentBadge({ badge: "unreachable", outcome: "unreachable" }, NOW).note).toBe(
      "The host did not answer.",
    );
  });

  it("the server's own sentence wins over the default note", () => {
    const view = presentBadge(
      { badge: "unreachable", outcome: "unreachable", badge_reason: "  DNS could not resolve wh.example  " },
      NOW,
    );
    expect(view.note).toBe("DNS could not resolve wh.example");
  });
});

describe("needs_reauth says what fixes it", () => {
  it.each([
    ["browser", "Sign in again"],
    ["reenter", "Update credential"],
    ["external_cli", "Sign in with the CLI"],
    ["admin", "Ask your admin"],
    [null, "Sign in again"],
    [undefined, "Sign in again"],
  ] as const)("%s → %s", (reauth, label) => {
    expect(reauthLabel(reauth)).toBe(label);
    const view = presentBadge({ badge: "needs_reauth", reauth, badge_reason: "The provider refused to renew your session." }, NOW);
    expect(view.label).toBe(label);
    expect(view.tone).toBe("warning");
    expect(view.note).toBe("The provider refused to renew your session.");
  });
});

describe("verifiedAgo", () => {
  it.each([
    [30, "just now"],
    [90, "2m ago"],
    [3600 * 3, "3h ago"],
    [3600 * 24, "a day ago"],
    [3600 * 24 * 3, "3d ago"],
    [3600 * 24 * 14, "2w ago"],
  ])("%d seconds ago reads %s", (seconds, expected) => {
    const iso = new Date(NOW - seconds * 1000).toISOString();
    expect(verifiedAgo(iso, NOW)).toBe(expected);
  });

  it("is empty for a missing or unparseable stamp", () => {
    expect(verifiedAgo(null, NOW)).toBe("");
    expect(verifiedAgo(undefined, NOW)).toBe("");
    expect(verifiedAgo("not a date", NOW)).toBe("");
  });
});
