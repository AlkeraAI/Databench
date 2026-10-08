// The controller's pure machinery: the working-indicator rule, the local
// interrupt overlay, thinking-stripping, the create-failure turn, and the
// stacked-navigation URL helpers.

import type { ConversationTurn } from "@alkera/chat-model";
import { describe, expect, it } from "vitest";
import {
  applyLocalInterruptState,
  backTargetFromSearch,
  chatIdFromPath,
  conversationAwaitsResponse,
  createErrorTurn,
  findSubagentSource,
  stackedTarget,
  stripThinkingParts,
} from "./controller"; // same-author-ok: mechanical import-path update for the controller/ split

function assistantTurn(status: ConversationTurn["status"]): ConversationTurn {
  return { id: "a1", author: "assistant", status, parts: [{ id: "a1-t", kind: "text", text: "hi" }] };
}

function userTurn(id = "u1"): ConversationTurn {
  return { id, author: "user", status: "done", parts: [{ id: `${id}-t`, kind: "text", text: "hi" }] };
}

// A turn the daemon has FINISHED — message.completed / turn.finished set completedAt.
function finishedAssistantTurn(status: ConversationTurn["status"]): ConversationTurn {
  return {
    id: "a1",
    author: "assistant",
    status,
    completedAt: "2026-06-10T00:00:01Z",
    parts: [{ id: "a1-t", kind: "text", text: "hi" }],
  };
}

const compactionTurn: ConversationTurn = {
  id: "comp",
  author: "assistant",
  status: "done",
  parts: [{ id: "comp-p", kind: "compaction", title: "Compaction", text: "summary" }],
};

describe("conversationAwaitsResponse (drives the working indicator)", () => {
  it.each([
    ["a trailing user message", [userTurn()], true],
    ["a new user message after a finished exchange", [finishedAssistantTurn("done"), userTurn("u2")], true],
    ["a running assistant turn", [assistantTurn("running")], true],
    ["an assistant turn with no status", [assistantTurn(undefined)], true],
    // The fold can mark a fresh assistant turn "done" before turn.started lands;
    // with no turn_summary it is still the agent's turn, and status alone would
    // flicker the indicator off mid-response.
    ["an unfinished turn already marked done", [userTurn(), assistantTurn("done")], true],
    ["a finished assistant turn", [userTurn(), finishedAssistantTurn("done")], false],
    ["an errored turn", [userTurn(), assistantTurn("error")], false],
    ["a cancelled turn", [userTurn(), assistantTurn("cancelled")], false],
    ["an empty conversation", [], false],
    ["a trailing system turn", [{ id: "s", author: "system", status: "done", parts: [{ id: "s-t", kind: "text", text: "note" }] } as ConversationTurn], false],
    // A compaction summary lands with NO completedAt. Without the compaction guard
    // it reads as "the agent still owes a response" and the spinner never stops,
    // since the idle that follows a compaction is a fold no-op.
    ["a trailing compaction summary", [userTurn(), finishedAssistantTurn("done"), compactionTurn], false],
  ])("reads %s as %s", (_why, turns, expected) => {
    expect(conversationAwaitsResponse(turns)).toBe(expected);
  });
});

describe("applyLocalInterruptState", () => {
  const pending: ConversationTurn[] = [
    {
      id: "a1",
      author: "assistant",
      status: "running",
      parts: [
        {
          id: "perm",
          kind: "permission",
          requestId: "perm-1",
          permissionKind: "shell",
          canonicalKind: "shell",
          patterns: ["pnpm test"],
          options: [{ optionId: "allow_once", name: "Yes" }],
          status: "pending",
        },
        {
          id: "q",
          kind: "question",
          requestId: "q-1",
          questionKind: "question",
          questions: [{ question: "Which file?", options: [], multiple: false, custom: true }],
          status: "pending",
        },
      ],
    },
  ];

  it("marks locally settled interrupts so the UI can clear their panels", () => {
    const answered = applyLocalInterruptState(pending, { expiredPermissions: {}, resolvedPermissions: { "perm-1": "allow_once" }, answeredQuestions: { "q-1": [["src/app.ts"]] }, rejectedQuestions: {} }); // same-author-ok: mechanical call-shape update for the options-object signature
    expect(answered[0].parts[0]).toMatchObject({
      kind: "permission",
      status: "resolved",
      selectedOptionId: "allow_once",
    });
    expect(answered[0].parts[1]).toMatchObject({
      kind: "question",
      status: "answered",
      answers: [["src/app.ts"]],
    });

    const rejected = applyLocalInterruptState(pending, { resolvedPermissions: {}, expiredPermissions: {}, answeredQuestions: {}, rejectedQuestions: { "q-1": "changed my mind" } }); // same-author-ok: mechanical call-shape update for the options-object signature
    expect(rejected[0].parts[1]).toMatchObject({ status: "rejected", reason: "changed my mind" });
  });
});

describe("stripThinkingParts", () => {
  it("removes thinking parts and drops the turns left empty", () => {
    const turns = [
      { id: "u1", author: "user", status: "done", parts: [{ id: "u1-t", kind: "text", text: "hi" }] },
      // Mid-stream thinking-only assistant turn → dropped entirely.
      { id: "a1", author: "assistant", status: "done", parts: [{ id: "a1-th", kind: "thinking", text: "…" }] },
      {
        id: "a2",
        author: "assistant",
        status: "done",
        parts: [
          { id: "a2-th", kind: "thinking", text: "…" },
          { id: "a2-t", kind: "text", text: "done" },
        ],
      },
    ] as ConversationTurn[];
    const out = stripThinkingParts(turns);
    expect(out.map((turn) => turn.id)).toEqual(["u1", "a2"]);
    expect(out.every((turn) => turn.parts.every((part) => part.kind !== "thinking"))).toBe(true);
  });
});

describe("createErrorTurn", () => {
  it.each([
    [{ message: "Open a workspace folder for Alkera to work in." }, "Open a workspace folder"],
    [{ message: "auth required (missing)", code: -32001 }, "you were signed out"],
    [undefined, "The chat could not be started"],
  ])("renders %j as an error turn saying %s", (rejection, says) => {
    const turn = createErrorTurn(rejection);
    expect(turn.status).toBe("error");
    expect(turn.parts[0]).toMatchObject({
      kind: "system",
      tone: "error",
      text: expect.stringContaining(says),
    });
  });

  it("names nothing the reader does not own when there is no reason to give", () => {
    // A rejection with no message must not tell a browser reader to go and
    // check a process they cannot see.
    const part = createErrorTurn(undefined).parts[0];
    if (part.kind !== "system") throw new Error("a create failure no longer reads as a system line");
    expect(part.text).not.toMatch(/backend|gateway|daemon|terminal/i);
    expect(part.text).toBe("The chat could not be started. Try again in a moment.");
  });
});

describe("stacked-navigation URL helpers", () => {
  it("backTargetFromSearch accepts only same-app paths", () => {
    expect(backTargetFromSearch("?backTo=%2Fchat%2Fc1")).toBe("/chat/c1");
    expect(backTargetFromSearch("?backTo=https%3A%2F%2Fevil.example")).toBeNull();
    expect(backTargetFromSearch("")).toBeNull();
  });

  it("chatIdFromPath decodes the id from live and editor chat routes", () => {
    expect(chatIdFromPath("/chat/c1")).toBe("c1");
    expect(chatIdFromPath("/editor/chat/c1?backTo=%2Fsidecar")).toBe("c1");
    expect(chatIdFromPath("/chat/a%2Fb")).toBe("a/b");
    expect(chatIdFromPath("/sidecar")).toBeNull();
    expect(chatIdFromPath(null)).toBeNull();
    // The empty composer is a surface, not a chat. Reading it as an id made
    // every chat whose back pointer led there look like a chat drilled into
    // from a parent named "new" — trail in place of the title, and the
    // subagent's own reading of the surface.
    expect(chatIdFromPath("/chat/new")).toBeNull();
    expect(chatIdFromPath("/chat/new?backTo=%2Fchat")).toBeNull();
  });

  it("stackedTarget carries the source location as backTo", () => {
    expect(stackedTarget("/editor/blobs/c1", "/chat/c1")).toBe("/editor/blobs/c1?backTo=%2Fchat%2Fc1");
  });

  it("findSubagentSource matches the spawn part bound to the child session", () => {
    const turns: ConversationTurn[] = [
      {
        id: "a1",
        author: "assistant",
        status: "done",
        parts: [
          {
            id: "sub",
            kind: "subagent",
            childSessionId: "child-1",
            name: "explore",
            status: "running",
            avatarSeed: "seed-1",
          },
        ],
      },
    ];
    expect(findSubagentSource(turns, "child-1")).toMatchObject({ childSessionId: "child-1" });
    expect(findSubagentSource(turns, "child-2")).toBeNull();
    expect(findSubagentSource(turns, null)).toBeNull();
  });
});
