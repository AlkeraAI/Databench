import { describe, expect, it } from "vitest";

import { ApiError } from "@/api/errors";
import {
  QUOTA_STATUS,
  filesErrorCopy,
  isKnownFilesError,
} from "@/lib/files/errors";

// The mapping is asserted through the copy a person reads, never through the table:
// each case builds the envelope the backend actually sends and checks that the
// sentence names the fact that makes the refusal actionable.

function envelope(status: number, code: string, message = "") {
  return new ApiError(status, { error: { code, message } });
}

describe("filesErrorCopy", () => {
  it("names the legal hold and the action it blocked", () => {
    const copy = filesErrorCopy(envelope(409, "files.held"), { action: "moved" });
    expect(copy.code).toBe("files.held");
    expect(copy.title).toBe("This is on legal hold.");
    expect(copy.detail).toContain("cannot be moved");
    expect(copy.detail).toContain("org admin");
    expect(copy.retryable).toBe(false);
  });

  it("falls back to a true sentence when the page did not say what was attempted", () => {
    const copy = filesErrorCopy(envelope(409, "files.held"));
    expect(copy.detail).toContain("cannot be changed");
    expect(copy.detail).not.toContain("undefined");
  });

  it("points an inherited grant at the ancestor that granted it", () => {
    const copy = filesErrorCopy(envelope(409, "files.inherited_grant"), {
      grantedBy: "Engineering",
    });
    expect(copy.title).toContain("inherited");
    expect(copy.detail).toContain("Engineering");
  });

  it("still explains an inherited grant when the ancestor is unknown", () => {
    const copy = filesErrorCopy(envelope(409, "files.inherited_grant"));
    expect(copy.detail).toContain("further up");
    expect(copy.detail).not.toContain("undefined");
  });

  it("names the holder of a lease, and the machine when the facet had one", () => {
    const copy = filesErrorCopy(envelope(409, "files.leased"), {
      holder: "Dana Okoye",
      machine: "MacBook Pro",
    });
    expect(copy.title).toBe("Dana Okoye has this folder for local use.");
    expect(copy.detail).toContain("MacBook Pro");
    expect(copy.detail).toContain("ask Dana Okoye");
    expect(copy.retryable).toBe(true);
  });

  it.each([
    [{ yours: "chat", holder: "Kickoff" }, "Your chat Kickoff is using this folder.", "goes to sleep"],
    [{ yours: "chat" }, "Your chat is using this folder.", "goes to sleep"],
    [{ yours: "box", holder: "demo-box" }, "Your box demo-box is using this folder.", "box releases it"],
    [{ yours: "box" }, "Your box is using this folder.", "box releases it"],
    [{ yours: "you", machine: "MacBook Pro" }, "You have this folder for local use.", "release it on MacBook Pro"],
    [{ yours: "you" }, "You have this folder for local use.", "once you release it."],
  ] as const)("says the folder is the reader's own when the holder acts for them: %o", (context, title, detail) => {
    const copy = filesErrorCopy(envelope(409, "files.leased"), context);
    expect(copy.title).toBe(title);
    expect(copy.detail).toContain(detail);
    // Never "someone", and never an offer to ask themselves for it back.
    expect(`${copy.title} ${copy.detail}`).not.toMatch(/someone|ask /i);
    expect(copy.retryable).toBe(true);
  });

  it("capitalises a holder that opens the sentence, and leaves one mid-sentence alone", () => {
    // The holder reaches this copy as the drive's own fallback word, or as the
    // chat holding the folder — both lowercase, and both lead the title.
    for (const [holder, title] of [
      ["someone", "Someone has this folder for local use."],
      ["the chat Q3 review", "The chat Q3 review has this folder for local use."],
    ] as const) {
      const copy = filesErrorCopy(envelope(409, "files.leased"), { holder });
      expect(copy.title).toBe(title);
      // The same words sit mid-sentence in the detail, where they stay as they are.
      expect(copy.detail).toContain(`ask ${holder} for it back`);
    }
  });

  it("says a lease exists without inventing a holder the caller may not see", () => {
    const copy = filesErrorCopy(envelope(409, "files.leased"));
    expect(copy.title).toBe("Someone has this folder for local use.");
    expect(copy.title).not.toContain("undefined");
    expect(copy.detail).not.toContain("undefined");
  });

  it("reads the quota refusal off the 507 status, which carries no code of its own", () => {
    const copy = filesErrorCopy(
      new ApiError(QUOTA_STATUS, { error: { code: "error", message: "2.0 TB of 2.0 TB used." } }),
    );
    expect(copy.title).toBe("Your organization is out of storage.");
    expect(copy.detail).toContain("2.0 TB of 2.0 TB used.");
    expect(copy.detail).toContain("trash");
  });

  it("passes an unmapped refusal's own message straight through", () => {
    const copy = filesErrorCopy(envelope(409, "files.name_conflict", "A file called README exists."));
    expect(copy.title).toBe("A file called README exists.");
    expect(copy.detail).toBeUndefined();
  });

  it("treats a 5xx as retryable and a 4xx as not", () => {
    expect(filesErrorCopy(envelope(503, "unavailable", "Try later.")).retryable).toBe(true);
    expect(filesErrorCopy(envelope(403, "forbidden", "No.")).retryable).toBe(false);
  });

  it("still says something when the failure never reached the server", () => {
    const copy = filesErrorCopy(new TypeError("Failed to fetch"));
    expect(copy.code).toBe("error");
    expect(copy.title).toBe("That did not go through.");
    expect(copy.retryable).toBe(true);
  });
});

describe("isKnownFilesError", () => {
  it.each([
    ["files.held", 409, true],
    ["files.inherited_grant", 409, true],
    ["files.leased", 409, true],
    ["error", QUOTA_STATUS, true],
    ["files.name_conflict", 409, false],
  ] as const)("%s at %i → %s", (code, status, expected) => {
    expect(isKnownFilesError(envelope(status, code))).toBe(expected);
  });

  it("is false for anything that is not an API refusal", () => {
    expect(isKnownFilesError(new Error("boom"))).toBe(false);
  });
});

describe("a person's own storage limit (files.user_quota_bytes)", () => {
  const refusal = (detail: Record<string, unknown> | null) =>
    new ApiError(507, {
      code: "files.user_quota_bytes",
      message: "This file needs 2 MB; your L3 storage limit is 1 MB and 700 KB is left.",
      ...(detail ? { detail } : {}),
    });

  it("names what the file needs, the limit, the team that set it and what is left — never 'out of storage'", () => {
    const copy = filesErrorCopy(
      refusal({
        kind: "bytes",
        scope: "org",
        limit_bytes: 1_000_000,
        used_bytes: 300_000,
        remaining_bytes: 700_000,
        needed_bytes: 2_000_000,
        team_id: "l3",
        team_name: "L3",
      }),
    );
    expect(copy.title).toBe("2 MB does not fit your L3 storage limit.");
    expect(copy.detail).toBe("The limit is 1 MB and 700 KB is left. Free up space in the trash, or ask an admin of L3 to raise it.");
    expect(copy.title + copy.detail).not.toMatch(/out of storage/i);
    // A deterministic refusal: retrying the same bytes against the same limit cannot succeed.
    expect(copy.retryable).toBe(false);
  });

  it("says the limit is reached, with nothing left, when the write adds no bytes at the ceiling", () => {
    const copy = filesErrorCopy(
      refusal({ kind: "bytes", scope: "org", limit_bytes: 1_000_000, used_bytes: 1_000_000, remaining_bytes: 0, needed_bytes: 0, team_id: null, team_name: null }),
    );
    expect(copy.title).toBe("Your storage limit is reached.");
    expect(copy.detail).toBe("The limit is 1 MB and nothing is left. Free up space in the trash, or ask your org or team admin to raise it.");
  });

  it("stays true, if vaguer, for a server that sent no figures", () => {
    const copy = filesErrorCopy(refusal(null));
    expect(copy.title).toBe("Your storage limit is reached.");
    expect(copy.detail).toContain("ask your org or team admin");
    expect(copy.retryable).toBe(false);
  });
});
