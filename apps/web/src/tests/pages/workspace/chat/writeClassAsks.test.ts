// Which asks an approval would authorize a write for — the browser's half of a
// reading the box makes too.
//
// The box gates a relayed `allow` on `mirror._authorizes_a_write`; the browser
// decides whether to OFFER that allow on the same question. Where the two
// disagree the reader loses either way: an Allow the box then drops is a button
// that does nothing, and an Allow never offered is a turn they cannot release.
//
// So the cases are not written here. They are the fixture the mirror's own test
// drives (`apps/cli/tests/cloud/fixtures/write_class_asks.json`), read by both
// sides, and a case added there is a case both must answer.
//
// And the ask is not written here either. The fixture's `effect` is the word
// the BOX puts on the wire, which the browser never sees directly: every ask is
// folded first, and the fold has its own opinion of that word. A hand-built
// part skips that opinion, which is how a wire verdict the fold erased could
// read as "no verdict at all" here while the box was calling it a write. So
// each case is raised as the `permission.request` the machine publishes, folded
// by the real fold, and answered through the source.
//
// The stance used is `read_only` — the one that refuses the write — because it
// is the only one in which the two answers are distinguishable at all: where
// the box honours approvals every ask is answerable whatever its shape.

import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

import type { PermissionConversationPart } from "@alkera/chat-model";
import { findPendingPermissions } from "@alkera/chat-model";

import {
  createConversationFoldState,
  foldHarnessEvent,
} from "@/pages/workspace/chat/data/harnessEventFold";
import { CloudDataSource, WRITE_CLASS_KINDS } from "@/pages/workspace/chat/data/CloudDataSource";

const REPO_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "../../../../../../..");
const FIXTURE = JSON.parse(
  readFileSync(resolve(REPO_ROOT, "apps/cli/tests/cloud/fixtures/write_class_asks.json"), "utf-8"),
) as {
  write_class_kinds: string[];
  cases: {
    id: string;
    canonical_kind: string;
    effect?: string;
    authorizes_a_write: boolean;
  }[];
};

const CHAT = "c1";

/** The ask as the machine publishes it, through the fold that every transcript
 *  in the browser is built by — the part the dock would hold. */
function foldedAsk(canonicalKind: string, effect?: string): PermissionConversationPart {
  const state = createConversationFoldState();
  foldHarnessEvent(state, { event_type: "turn.started", turn_id: "turn-1" });
  foldHarnessEvent(state, {
    event_type: "permission.request",
    request_id: "perm-1",
    permission_kind: canonicalKind,
    canonical_kind: canonicalKind,
    // The broker's own broadcast: the arrival that makes an ask a person's to
    // answer, and the only kind the dock ever holds.
    prompting: true,
    patterns: ["the subject"],
    options: [
      { option_id: "allow_once", name: "Allow once" },
      { option_id: "reject_once", name: "Reject once" },
    ],
    ...(effect === undefined
      ? {}
      : {
          subject: {
            capability: "shell",
            operation: "run",
            effect,
            targets: [],
          },
        }),
  });
  const [ask] = findPendingPermissions(state.turns);
  if (!ask) throw new Error(`the fold raised no permission for ${canonicalKind}/${effect ?? "-"}`);
  return ask;
}

async function analystChat(): Promise<CloudDataSource> {
  const source = new CloudDataSource({
    rest: {
      setPermissionMode: async (_chatId: string, wanted: string) => ({
        permission_mode: wanted,
        approval_refusal: "This workspace is read-only, so it won't run this.",
      }),
    } as never,
  });
  await source.setPermissionMode(CHAT, "read_only");
  return source;
}

describe("the tier the fold keeps", () => {
  // The fold keeps the words it has a slot for and folds any other to
  // `unknown`. `exec` is the strictest tier; folded to `unknown` the card and
  // the activity log would lose the one word that says a program runs.
  it.each(["read", "write", "destroy", "egress", "exec", "memory"])("keeps %s", (effect) => {
    expect(foldedAsk("shell", effect).subject?.effect).toBe(effect);
  });

  it("folds a word it has no slot for to unknown", () => {
    expect(foldedAsk("shell", "frobnicate").subject?.effect).toBe("unknown");
  });
});

describe("which asks an approval would authorize a write for", () => {
  it("reads the same kinds the box does", () => {
    expect([...WRITE_CLASS_KINDS].sort()).toEqual([...FIXTURE.write_class_kinds].sort());
  });

  it("has cases worth calling a table", () => {
    // Both answers, and every path to each: a verdict the fold keeps, a verdict
    // it does not recognise, and a kind that had to answer alone. A fixture
    // that drifted to one answer would pass every assertion below while
    // proving nothing.
    expect(FIXTURE.cases.filter((c) => c.authorizes_a_write).length).toBeGreaterThan(3);
    expect(FIXTURE.cases.filter((c) => !c.authorizes_a_write).length).toBeGreaterThan(3);
    expect(FIXTURE.cases.some((c) => c.effect === undefined)).toBe(true);
    expect(FIXTURE.cases.some((c) => c.effect === "read")).toBe(true);
    // The words the fold has no slot for. They are the whole reason the ask is
    // folded here rather than built by hand.
    expect(FIXTURE.cases.some((c) => c.effect === "memory")).toBe(true);
    expect(
      FIXTURE.cases.some(
        (c) =>
          c.effect !== undefined &&
          !["read", "write", "destroy", "egress", "memory", ""].includes(c.effect),
      ),
    ).toBe(true);
  });

  it.each(FIXTURE.cases.map((c) => [c.id, c] as const))("%s", async (_id, testCase) => {
    const source = await analystChat();
    const verdict = source.mayAllow(CHAT, foldedAsk(testCase.canonical_kind, testCase.effect));

    // In an analyst's chat the box drops an allow for exactly the asks that
    // authorize a write, so the card offers one for exactly the rest.
    expect(verdict.allowed).toBe(!testCase.authorizes_a_write);
  });
});
