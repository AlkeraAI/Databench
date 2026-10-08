// A card's head speaks for where the call is. "Ran …" over a command still
// waiting for approval, "Wrote 1 note" before anything was written, or a
// refused read under "0 lines" with "Show preview" would each claim a result
// that never existed. While a call is
// pending or running its verb is in the present and it carries no result
// chrome; a refused call reads "Refused <object>" with only its reason.

import { render, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { ToolConversationPart, ToolState } from "@alkera/chat-model";

import { Activity } from "../activity";
import type { CardStep } from "./step";
import { failed, inProgress, resolveStep } from "./steps";

interface Case {
  name: string;
  input: Record<string, unknown>;
  output: ToolConversationPart["output"];
  /** The settled verb, so the in-flight one is proved to differ from it. */
  settled: string;
  inFlight: RegExp;
  /** The head over a call that failed without being refused. */
  failedVerb: string;
}

const CASES: Case[] = [
  {
    name: "bash",
    input: { command: "for i in $(seq 1 8); do date; done | wc -l" },
    output: JSON.stringify({ output: "8\n", exit_code: 0, truncated: false }),
    settled: "Ran",
    failedVerb: "Could not run",
    inFlight: /^Running$/,
  },
  {
    name: "read",
    input: { filePath: "/work/notes.md" },
    output: "<content>\n1: hello\n</content>",
    settled: "Read",
    failedVerb: "Could not read",
    inFlight: /^Reading$/,
  },
  {
    name: "write",
    input: { filePath: "/work/notes.md", content: "hello\n" },
    output: "Wrote file successfully.",
    settled: "Wrote",
    failedVerb: "Could not write",
    inFlight: /^Writing/,
  },
  {
    name: "edit",
    input: { filePath: "/work/notes.md", oldString: "a", newString: "b" },
    output: "Edit applied successfully.",
    settled: "Edited",
    failedVerb: "Could not edit",
    inFlight: /^Editing/,
  },
  {
    name: "alkera_sql.query",
    input: { connection: "pg", sql: "SELECT 1" },
    output: JSON.stringify({ columns: ["x"], rows: [[1]], row_count: 1 }),
    settled: "Queried",
    failedVerb: "Could not query",
    inFlight: /^Querying$/,
  },
  {
    name: "alkera_context_note",
    input: { title: "Walkthrough note", text: "hello" },
    output: JSON.stringify({ item_id: "fact:1", title: "Walkthrough note" }),
    settled: "Noted",
    failedVerb: "Could not note",
    inFlight: /^Noting$/,
  },
];

let minted = 0;

function stepOf(c: Case, state: ToolState, over: Partial<ToolConversationPart> = {}): CardStep {
  minted += 1;
  const part: ToolConversationPart = {
    id: `cs-part-${minted}`,
    kind: "tool",
    callId: `cs-call-${minted}`,
    name: c.name,
    state,
    input: c.input,
    output: state === "completed" ? c.output : null,
    ...over,
  };
  const step = resolveStep(part);
  if (!step) throw new Error(`${c.name} resolved to no step`);
  return step;
}

function rendered(step: CardStep): HTMLElement {
  const { container } = render(<Activity summary="1 tool call" steps={[step]} folded={false} />);
  return container;
}

const REASON =
  'The workspace policy refused this read: "/etc/shadow" is outside this chat\'s workspace; read inside /work instead.';

describe.each(CASES)("the $name card", (c) => {
  it("reads past tense once the call has completed", () => {
    expect(stepOf(c, "completed").verb).toBe(c.settled);
  });

  it.each(["pending", "running"] as const)("reads in the present while %s, with no result chrome", (state) => {
    const step = stepOf(c, state);
    expect(step.verb).toMatch(c.inFlight);
    expect(step.verb).not.toBe(c.settled);
    expect(step.data).toBeUndefined();
    expect(step.footer).toBeUndefined();
    expect(step.loneSummary.split(" ")[0]).not.toBe(stepOf(c, "completed").loneSummary.split(" ")[0]);
    const view = rendered(step);
    expect(view.textContent).not.toMatch(/\bShow\b/);
  });

  it("reads Refused with only the reason when the call was refused", () => {
    const step = stepOf(c, "error", { errorText: REASON });
    expect(step.verb).toBe("Refused");
    expect(step.refused).toBe(true);
    expect(step.data).toBeUndefined();
    expect(step.body).toBeUndefined();
    expect(step.footer).toBeUndefined();
    expect(step.loneSummary.startsWith("Refused ")).toBe(true);
    const view = rendered(step);
    const text = view.textContent ?? "";
    expect(text).toContain(REASON);
    expect(text).not.toMatch(/\bShow\b/);
    expect(text).not.toMatch(/\b0 lines\b/);
  });

  it("reads Could not, with no result figure, on a failure that is not a refusal", () => {
    const step = stepOf(c, "error", { errorText: "connection 'pg' timed out" });
    expect(step.verb).toBe(c.failedVerb);
    expect(step.refused).toBeFalsy();
    expect(step.data).toBeUndefined();
    expect(step.loneSummary.startsWith("Could not ")).toBe(true);
    expect(step.failure).toBe("connection 'pg' timed out");
  });
});

describe("a person's refusal and the in-tool gate's are refusals too", () => {
  it.each([
    "Permission was not granted for this call.",
    "permission denied: connection 'pg' is read-only by its nature",
    JSON.stringify({ error: "permission denied: not approved", tool: "bash", classification: "error" }),
    "The user rejected permission to use this specific tool call.",
  ])("%s", (errorText) => {
    expect(stepOf(CASES[0], "error", { errorText }).verb).toBe("Refused");
  });
});

describe("a failed edit", () => {
  const edit = CASES.find((c) => c.name === "edit");
  if (!edit) throw new Error("no edit case");
  it("says it could not edit the file, shows no diff figure and says nothing changed", () => {
    const step = stepOf(edit, "error", {
      errorText: "Could not find oldString in the file. It must match exactly.",
      resources: [{ path: "/work/notes.md", diff: { insertions: 0, deletions: 0 } }] as ToolConversationPart["resources"],
    });
    const text = rendered({ ...step, expanded: true }).textContent ?? "";
    expect(text).toContain("Could not edit");
    expect(text).not.toMatch(/\bEdited\b/);
    expect(text).not.toContain("+0");
    expect(text).toContain("Nothing was changed.");
    expect(text).not.toContain("landed");
  });
});

describe("failed", () => {
  it.each([
    ["Ran 1 terminal command", "Could not run 1 terminal command"],
    ["Edited", "Could not edit"],
    ["Wrote 1 note", "Could not write 1 note"],
    ["Writing…", "Could not finish"],
  ])("%s → %s", (past, failure) => {
    expect(failed(past)).toBe(failure);
  });
});

describe("inProgress", () => {
  it.each([
    ["Ran 1 terminal command", "Running 1 terminal command"],
    ["Wrote 1 note", "Writing 1 note"],
    ["Listed dashboards on", "Listing dashboards on"],
    ["Writing…", "Writing…"],
  ])("%s → %s", (past, present) => {
    expect(inProgress(past)).toBe(present);
  });
});

describe("the rendered head", () => {
  it("never says Ran over a bash command awaiting approval", () => {
    const view = rendered(stepOf(CASES[0], "pending"));
    const head = within(view).getByText("Running");
    expect(head).toBeTruthy();
    expect(view.textContent).not.toMatch(/\bRan\b/);
  });
});
