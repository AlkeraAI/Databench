// What a permission card offers, and what it says instead, in each of the five
// stances a cloud chat runs in.
//
// A chat in Auto showed every ask as reject-only under "This workspace is
// read-only, so it won't run this." Auto is the stance built for an unattended
// run: it clears the recoverable middle itself and PAUSES for the risky step,
// and the box acts on whatever the reader answers to that pause. So the card
// withheld the one control the stance exists to reach, and explained it with a
// sentence about a stance the chat was not in.
//
// Both halves are read here off the real source and the real card mapping, for
// every stance and for each shape of ask a turn can stop on:
//
// * a WRITE-CLASS ask (the command, the edit) — the one the stance decides;
// * a READ-EFFECT ask — answerable in every stance, including the analyst ones;
// * a PLAN-APPROVAL ask — a question, not a permission, so no stance gates it.
//
// The verdict is the server's, on the chat row; the server's table is held
// against the machine's own rows by `apps/cli/tests/harness/test_stance_contract.py`.
// This file is what a reader would see.

import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";
import { PERMISSION_MODE_VALUES } from "@alkera/chat-model";

import type { ConversationTurn, PermissionConversationPart } from "@alkera/chat-model";
import { findActiveQuestion, findPendingPermissions } from "@alkera/chat-model";
import { PermissionCard } from "@alkera/ui";

import { queryClient } from "@/api/queryClient";
import { CloudDataSource } from "@/pages/workspace/chat/data/CloudDataSource";
import { permissionCardProps, planChoicesOf } from "@/pages/workspace/chat/options";

const CHAT = "c1";

/** A source whose route echoes whatever stance it is handed, with the
 *  server's verdict for it (the floor's for a word the server does not know),
 *  which is how a word this client does not know reaches the record. */
function newSource(): { source: CloudDataSource; told: string[] } {
  const source = new CloudDataSource({
    rest: {
      setPermissionMode: async (_chatId: string, wanted: string) => ({
        permission_mode: wanted,
        approval_refusal:
          wanted in WRITE_CLASS_BY_STANCE ? WRITE_CLASS_BY_STANCE[wanted] : WRITE_CLASS_BY_STANCE.read_only,
      }),
    } as never,
  });
  const told: string[] = [];
  source.subscribePermissionMode(CHAT, (mode) => told.push(mode));
  return { source, told };
}

/** A chat this source has read the stance of. The stance rides the route's
 *  answer, which is what the source believes — not an optimistic local guess. */
async function sourceIn(mode: string): Promise<CloudDataSource> {
  const { source } = newSource();
  await source.setPermissionMode(CHAT, mode);
  return source;
}

function ask(overrides: Partial<PermissionConversationPart> = {}): PermissionConversationPart {
  return {
    id: "p1",
    kind: "permission",
    requestId: "req-1",
    permissionKind: "bash",
    canonicalKind: "shell",
    prompting: true,
    patterns: ["uv run python -c 'print(7)'"],
    options: [
      { optionId: "allow_once", name: "Allow once" },
      { optionId: "allow_always", name: "Always allow" },
      { optionId: "reject_once", name: "Reject once" },
    ],
    status: "pending",
    ...overrides,
  } as unknown as PermissionConversationPart;
}

/** The ask a turn stops on to run a command or write a file — the shape the
 *  stance actually decides. */
const writeClassAsk = (): PermissionConversationPart => ask();

/** An ask the classifier read as a pure read. Every stance admits a read, so
 *  the approving keys stand whatever the chat is in. */
const readEffectAsk = (): PermissionConversationPart =>
  ask({
    permissionKind: "webfetch",
    canonicalKind: "network",
    subject: { capability: "network", operation: "fetch", effect: "read" },
  } as unknown as Partial<PermissionConversationPart>);

/** What the card offers for one ask in one stance, read through the same
 *  mapping the dock renders with. */
function cardFor(source: CloudDataSource, part: PermissionConversationPart) {
  return permissionCardProps(
    part,
    undefined,
    () => {},
    () => {},
    source.mayAllow(CHAT, part),
  );
}

/** What the server says each stance owes a write-class ask, written out as
 *  the stub server's own table.
 *
 *  `null` is "the approval reaches the agent, so there is nothing to refuse";
 *  a sentence is what the card says in place of the approving keys.
 *
 *  Read against the machine: `default` asks the reader, `auto` pauses for the
 *  risky step and acts on the answer, `bypass` raised the ask for a reason of
 *  its own — all three live. `plan` and `read_only` refuse the write whatever
 *  anyone clicks (`mirror._refuses_the_write`). The same split is held against
 *  the harness's stance rows in `test_stance_contract.py`. */
const WRITE_CLASS_BY_STANCE: Record<string, string | null> = {
  default: null,
  plan: "Plan mode explores and proposes a plan, so it won't run this.",
  auto: null,
  read_only: "This workspace is read-only, so it won't run this.",
  bypass: null,
};

/** Whether the box would discard an approval given in this stance. */
const discards = (mode: string): boolean => WRITE_CLASS_BY_STANCE[mode] !== null;

describe("what a permission card offers, by stance", () => {
  beforeEach(() => queryClient.clear());

  // A stance with no row below is one this file silently skips, so the two
  // lists are held equal rather than assumed.
  it("covers every stance the browser can put a chat in", () => {
    expect([...PERMISSION_MODE_VALUES].sort()).toEqual(
      Object.keys(WRITE_CLASS_BY_STANCE).sort(),
    );
    expect([...PERMISSION_MODE_VALUES].sort()).toEqual([
      "auto",
      "bypass",
      "default",
      "plan",
      "read_only",
    ]);
  });

  describe.each([...PERMISSION_MODE_VALUES])("%s", (mode) => {
    it("offers Allow and Always allow on a write-class ask exactly when the box would act on it", async () => {
      const source = await sourceIn(mode);
      const card = cardFor(source, writeClassAsk());

      expect(Boolean(card.allow)).toBe(!discards(mode));
      expect(Boolean(card.always)).toBe(!discards(mode));
      // Declining is what releases the turn, so it is offered in every stance.
      expect(card.deny.optionId).toBe("reject_once");
    });

    it("says the stance's own reason, or nothing at all where there is nothing to refuse", async () => {
      const source = await sourceIn(mode);
      const card = cardFor(source, writeClassAsk());

      expect(card.refusal).toBe(WRITE_CLASS_BY_STANCE[mode] ?? undefined);
    });

    it("leaves a read-effect ask answerable", async () => {
      const source = await sourceIn(mode);
      const card = cardFor(source, readEffectAsk());

      expect(card.allow).toBeDefined();
      expect(card.refusal).toBeUndefined();
    });

    // A plan approval is a QUESTION, answered on its own card with the mode to
    // continue in. It is what a reader in plan mode is waiting for, so a stance
    // must never reach it: the selectors sort it away from the permissions the
    // stance decides, and its accept options stand in every stance.
    it("keeps a plan approval answerable, and out of the stance's reach", async () => {
      const source = await sourceIn(mode);
      const planTurns = [
        {
          id: "a1",
          author: "assistant",
          status: "running",
          parts: [
            {
              id: "q1",
              kind: "question",
              requestId: "req-plan",
              questionKind: "plan_approval",
              planMarkdown: "# The plan",
              questions: [
                {
                  question: "Approve this plan?",
                  header: null,
                  options: [
                    { label: "Accept — run normally (ask before each change)" },
                    { label: "Accept — auto mode (run automatically, pause for risky steps)" },
                    { label: "Accept — bypass all permission prompts" },
                  ],
                  multiple: false,
                  custom: false,
                },
              ],
              status: "pending",
            },
          ],
        },
      ] as unknown as ConversationTurn[];

      // Nothing here is a permission, so `mayAllow` is never consulted for it.
      expect(findPendingPermissions(planTurns)).toEqual([]);
      const question = findActiveQuestion(planTurns);
      expect(question?.questionKind).toBe("plan_approval");
      expect(planChoicesOf(question!.questions[0]!).options.map((o) => o.mode)).toEqual([
        "default",
        "auto",
        "bypass",
      ]);
      // And the source that would have refused a write says nothing about it.
      expect(source.mayAllow(CHAT, readEffectAsk()).allowed).toBe(true);
    });
  });

  // A stance the client does not know is never the permissive end: the pill
  // hears the floor, and the card offers no Allow under a row whose stance the
  // client could not read.
  //
  // What reaches `noteMode` is a string off the chat row or the document's
  // meta, so a server ahead of this build, a publisher writing a stance this
  // build never heard of, or a malformed row all land here. Reading the table
  // by that string alone answered "no sentence, so nothing refuses it" — which
  // offered the Allow on every one of them. Two of the strings are worse than
  // unknown: an object literal inherits `Object.prototype`, so `toString` and
  // `constructor` return a FUNCTION where a sentence was expected and
  // `__proto__` returns an object, none of which a card can render.
  describe.each([
    ["a stance from a newer server", "supervised"],
    ["an empty stance", ""],
    ["a stance spelled as an inherited key", "toString"],
    ["another inherited key", "constructor"],
    ["the prototype key", "__proto__"],
    ["a near miss", "read-only"],
  ])("%s (%j)", (_name, mode) => {
    it("withholds the approval", async () => {
      const source = await sourceIn(mode);
      const card = cardFor(source, writeClassAsk());

      expect(card.allow).toBeUndefined();
      expect(card.always).toBeUndefined();
      // Not a function, not an object: what the card renders is a sentence.
      expect(typeof card.refusal).toBe("string");
    });

    it("is never recorded, or announced, as the stance this chat is in", async () => {
      const { source, told } = newSource();
      await source.setPermissionMode(CHAT, mode);

      // Everything downstream of the record — the composer's pill among them —
      // hears a stance this client can act on, or hears nothing at all. A word
      // it cannot act on must not be passed off as the chat's stance.
      expect(told).not.toContain(mode);
      for (const announced of told) {
        expect(PERMISSION_MODE_VALUES as readonly string[]).toContain(announced);
      }
    });
  });

  // The one that matters most: a chat the reader HAD put in a stance that
  // approves, moved by the server to a word this build does not know. Keeping
  // the old stance would go on offering an Allow for a chat that is no longer
  // in it.
  it("drops back to the floor when a known stance is replaced by an unknown one", async () => {
    const source = await sourceIn("bypass");
    expect(cardFor(source, writeClassAsk()).allow).toBeDefined();

    await source.setPermissionMode(CHAT, "supervised");

    const card = cardFor(source, writeClassAsk());
    expect(card.allow).toBeUndefined();
    expect(card.always).toBeUndefined();
  });

  // The sentence a reader actually reads, on the card, in the two stances that
  // withhold the approval — neither of which may claim to be the other.
  it.each([
    ["read_only", "This workspace is read-only, so it won't run this."],
    ["plan", "Plan mode explores and proposes a plan, so it won't run this."],
  ] as const)("a %s chat says why on the card itself", async (mode, sentence) => {
    const source = await sourceIn(mode);
    render(
      <div className="chat-root">
        <PermissionCard {...cardFor(source, writeClassAsk())} />
      </div>,
    );

    expect(screen.getByText(sentence)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /allow/i })).toBeNull();
  });
});
