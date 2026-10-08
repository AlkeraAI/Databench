// "Always" on a permission card is offered only where the server says this
// reader may give it.
//
// An "Always" answer is recorded as a rule of the chat's own owner and applies
// in the owner's other chats on the box, so the server refuses it from anyone
// else (`can_answer_always` on the chat row says so in advance). A card that
// offered it anyway would hand a collaborator a key that fails every time.

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { PermissionConversationPart } from "@alkera/chat-model";
import { PermissionCard } from "@alkera/ui";

import { permissionCardProps } from "@/pages/workspace/chat/options";

function ask(): PermissionConversationPart {
  return {
    id: "p1",
    kind: "permission",
    requestId: "req-1",
    permissionKind: "edit",
    canonicalKind: "edit",
    prompting: true,
    patterns: ["notes.md"],
    options: [
      { optionId: "allow_once", name: "Allow once" },
      { optionId: "allow_always", name: "Always allow" },
      { optionId: "reject_once", name: "Reject once" },
    ],
    status: "pending",
  } as unknown as PermissionConversationPart;
}

function card(canAnswerAlways: boolean) {
  return permissionCardProps(ask(), undefined, () => {}, () => {}, undefined, undefined, canAnswerAlways);
}

describe("the card's Always", () => {
  it("is offered to a reader the server says may give it", () => {
    expect(card(true).always?.options.map((o) => o.optionId)).toEqual(["allow_always"]);
  });

  it("is not offered to anyone else, while allow and decline still are", () => {
    const props = card(false);
    expect(props.always).toBeUndefined();
    expect(props.allow?.optionId).toBe("allow_once");
    expect(props.deny.optionId).toBe("reject_once");
    render(<PermissionCard {...props} />);
    expect(screen.queryByText("Always allow")).toBeNull();
  });
});
