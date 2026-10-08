import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type {
  PermissionAsk,
  PermissionConversationPart,
  PermissionPresentation,
} from "@alkera/chat-model";
import { findGatedCall, presentPermission } from "@alkera/chat-model";
import vectors from "@alkera/chat-model/permission-vectors";
import { PermissionCard } from "@alkera/ui";

import { permissionCardProps } from "@/pages/workspace/chat/options";

// The web half of the parity contract. The Slack card is held to the same
// vectors server-side (the Slack permission card's tests): for
// every registered kind of ask, every field the shared presentation marks as
// primary must be on the card a reader sees. A registry entry that gives one
// surface something to show and the other nothing fails one of the two.

interface Vector {
  name: string;
  ask: PermissionAsk;
  presentation: PermissionPresentation;
}

const VECTORS = vectors as unknown as Vector[];

/** The folded part a transcript would hold for the vector's ask. */
function partOf(ask: PermissionAsk): PermissionConversationPart {
  return {
    id: "ask-1",
    kind: "permission",
    requestId: "ask-1",
    permissionKind: ask.permissionKind,
    canonicalKind: ask.canonicalKind as PermissionConversationPart["canonicalKind"],
    patterns: ask.patterns,
    options: ask.options,
    status: "pending",
    prompting: true,
    ...(ask.subjectPending ? { subjectPending: true } : {}),
    ...(ask.subject
      ? {
          subject: {
            ...ask.subject,
            effect: ask.subject.effect as NonNullable<
              PermissionConversationPart["subject"]
            >["effect"],
            scope: ask.subject.scope as "operation" | "command" | undefined,
            confidence: ask.subject.confidence as NonNullable<
              PermissionConversationPart["subject"]
            >["confidence"],
            reasons: [],
          },
        }
      : {}),
    ...(ask.preview?.notebook ? { notebook: ask.preview.notebook } : {}),
    ...(ask.preview && !ask.preview.notebook
      ? {
          preview: {
            ...ask.preview,
            kind: ask.preview.kind as NonNullable<PermissionConversationPart["preview"]>["kind"],
          },
        }
      : {}),
  };
}

function renderVector(vector: Vector): HTMLElement {
  const props = permissionCardProps(
    partOf(vector.ask),
    undefined,
    () => {},
    () => {},
    undefined,
    vector.ask.call,
  );
  return render(
    <div className="chat-root">
      <PermissionCard {...props} />
    </div>,
  ).container;
}

describe("the web card renders the shared presentation", () => {
  it.each(VECTORS.map((vector) => [vector.name, vector] as const))(
    "%s shows every primary field",
    (_name, vector) => {
      // The web reads the ask off the folded part, not the vector's JSON: the
      // presentation it computes from there must be the server's.
      const shown = presentPermission(vector.ask);
      expect(shown).toEqual(vector.presentation);
      const container = renderVector(vector);
      const text = container.textContent ?? "";
      for (const field of shown.primary) {
        switch (field) {
          case "title":
            expect(screen.getByRole("heading", { level: 2 }).textContent).toBe(shown.title);
            break;
          case "subject": {
            // A painted inset lays each line out as its own element, so the
            // line breaks are layout rather than text.
            const flat = (value: string) => value.replace(/\s+/gu, "");
            expect(flat(text)).toContain(flat(shown.subject?.text ?? ""));
            break;
          }
          case "note":
            expect(
              container.querySelector(".chat-permission-item__body")?.textContent,
            ).toContain(shown.note?.body.split("\n")[0].replaceAll("**", ""));
            if (shown.note?.title) {
              expect(container.querySelector(".chat-permission-item__title")?.textContent).toBe(
                shown.note.title,
              );
            }
            break;
          case "change": {
            const diff = container.querySelector(".chat-permission-preview");
            const changed = (shown.change?.content ?? "")
              .split("\n")
              .find((line) => line.startsWith("+") && !line.startsWith("+++"));
            expect(diff?.textContent).toContain(changed?.slice(1));
            break;
          }
          case "details":
            expect(
              container.querySelector(".chat-permission-details__tool")?.textContent,
            ).toBe(shown.details?.tool);
            expect(
              container.querySelector(".chat-permission-details__input")?.textContent,
            ).toBe(shown.details?.input);
            break;
          case "notebook": {
            const nb = shown.notebook;
            const rows = container.querySelectorAll(".chat-notebook-ask__name");
            expect(Array.from(rows, (row) => row.textContent)).toEqual(nb?.cells.map((c) => c.name));
            for (const said of [nb?.reason, nb?.relatedLine, ...(nb?.packages ?? [])]) {
              if (said) expect(text).toContain(said);
            }
            break;
          }
          case "facts":
            for (const fact of shown.facts) expect(text).toContain(fact);
            break;
          case "decisions":
            for (const decision of shown.decisions) {
              if (decision.role === "always") {
                expect(container.querySelector(".chat-permission-vh")?.textContent).toBe(
                  decision.label,
                );
              } else if (decision.role === "allow" || decision.role === "deny") {
                expect(
                  within(container).getByRole("button", {
                    name: new RegExp(`^${decision.label}`),
                  }),
                ).toBeTruthy();
              }
            }
            break;
        }
      }
    },
  );

  it("never shows a secret the presentation redacted", () => {
    for (const name of ["shell_secrets", "generic_unknown_tool"]) {
      const vector = VECTORS.find((entry) => entry.name === name) as Vector;
      const text = renderVector(vector).textContent ?? "";
      for (const secret of ["ghp_abcdefghijklmnopqrstuvwx0123", "hunter2", "sk-live-abcdefghijklmnop"]) {
        expect(text).not.toContain(secret);
      }
      expect(text).toContain("[redacted]");
    }
  });
});

describe("the gated call", () => {
  const ask: PermissionConversationPart = {
    id: "p",
    kind: "permission",
    requestId: "p",
    permissionKind: "acme_deploy",
    canonicalKind: "other",
    patterns: [],
    options: [
      { optionId: "allow_once", name: "Allow once" },
      { optionId: "reject_once", name: "Reject once" },
    ],
    status: "pending",
    callIds: ["prt_1", "call_1"],
  };
  const tool = (callId: string, input: Record<string, unknown>) => ({
    id: `t-${callId}`,
    kind: "tool" as const,
    callId,
    name: "mcp__acme__deploy",
    state: "pending" as const,
    input,
  });

  it("is found by either id the ask gave for it", () => {
    const turns = [
      { id: "t1", role: "assistant" as const, parts: [tool("other", { x: 1 }), tool("call_1", { service: "billing" })] },
    ];
    expect(findGatedCall(turns as never, ask)).toEqual({
      name: "mcp__acme__deploy",
      input: { service: "billing" },
    });
  });

  it("is not guessed when the ask named no call", () => {
    const turns = [{ id: "t1", role: "assistant" as const, parts: [tool("call_1", { a: 1 })] }];
    expect(findGatedCall(turns as never, { ...ask, callIds: undefined })).toBeUndefined();
  });

  it("is shown on the card of an ask that named nothing else", () => {
    const props = permissionCardProps(ask, undefined, () => {}, () => {}, undefined, {
      name: "mcp__acme__deploy",
      input: { service: "billing" },
    });
    const { container } = render(
      <div className="chat-root">
        <PermissionCard {...props} />
      </div>,
    );
    expect(container.querySelector(".chat-permission-details__tool")?.textContent).toBe("deploy");
    expect(container.querySelector(".chat-permission-details__input")?.textContent).toContain(
      '"service": "billing"',
    );
  });
});
