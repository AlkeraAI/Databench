// The composer's mode menu draws each entry's description on ONE line, cut with
// an ellipsis when it runs past the row. A sentence a reader only ever sees two
// thirds of is worse than the shorter sentence that says the same thing, so the
// copy is held inside the width the menu actually has.

import { describe, expect, it } from "vitest";

import { PERMISSION_MODE_OPTIONS } from "@/pages/workspace/chat/adapters";

/** The longest description the menu was observed to render whole at its own
 *  width — "Works on its own and pauses only for risky or destructive steps."
 *  Anything past it is cut mid-word in the popup. */
const ONE_LINE = 64;

describe("permission-mode descriptions", () => {
  it.each(PERMISSION_MODE_OPTIONS.map((option) => [option.value, option.description] as const))(
    "%s fits the menu's one line",
    (_value, description) => {
      expect(description.length).toBeLessThanOrEqual(ONE_LINE);
    },
  );

  it.each(PERMISSION_MODE_OPTIONS.map((option) => [option.value, option.description] as const))(
    "%s states the fact in a single sentence",
    (_value, description) => {
      expect(description).toMatch(/^[A-Z][^.]*\.$/);
      expect(description).not.toContain("…");
    },
  );

  // The entry that was cut. Pinned by its words, not only by its length, so
  // trimming it back to a fragment to win the budget is a failure too.
  it("says what Plan does without the clause the menu was cutting off", () => {
    const plan = PERMISSION_MODE_OPTIONS.find((option) => option.value === "plan");
    expect(plan?.description).toBe("Explores, writes only in the chat's files, and proposes a plan.");
  });

  // Every mode admits a write inside the chat's own folder — the agent's working
  // directory, and where plan.md is drafted. A line promising to ask before every
  // edit is one a Default chat breaks the first time the agent writes a scratch
  // file, so the exception is named rather than assumed. What each mode then does
  // with a change outside it is held against the machine's own stance table in
  // `apps/cli/tests/harness/test_stance_contract.py`.
  // Default and Plan write the chat's files unasked, and in a workspace those
  // are the workspace's shared files, so they say "the chat's files" rather
  // than "this chat". Read-only writes nothing in a shared folder.
  it.each([
    ["default", /the chat's files/],
    ["plan", /the chat's files/],
    ["read_only", /this chat/],
  ] as const)("%s names the folder it changes without asking", (value, names) => {
    const option = PERMISSION_MODE_OPTIONS.find((entry) => entry.value === value);
    expect(option?.description).toMatch(names);
  });
});
