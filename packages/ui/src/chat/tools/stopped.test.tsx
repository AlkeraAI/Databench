// A tool call the turn's Stop cut off is not a failed call and not an empty
// one: it ended without a result, said once. Not a bare X on its own line, not
// the harness's raw "Tool execution aborted", not the empty-result notice, and
// not "1 failed" in the run's header.
//
// These read the rendered run through the real registry and the real cascade
// (the sheets are injected, so a status line that loses its layout fails
// here), and they pin the two neighbours that must stay as they are: a real
// failure still reads as one, and a finished call with nothing to show still
// says so.

import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";

import { render, within } from "@testing-library/react";
import { afterAll, beforeAll, describe, expect, it } from "vitest";

import type { ToolConversationPart } from "@alkera/chat-model";

import { Activity } from "../activity/Activity";
import { resolveStep } from "./steps";
import type { CardStep } from "./step";

const STOPPED = "This tool was stopped before it finished.";
const EMPTY = "This call returned nothing.";
const HARNESS_ABORT = "Tool execution aborted";

let minted = 0;

function part(over: Partial<Omit<ToolConversationPart, "kind">>): ToolConversationPart {
  minted += 1;
  return { id: `stop-part-${minted}`, kind: "tool", callId: `stop-call-${minted}`, name: "call_tool", state: "completed", ...over };
}

function stepOf(over: Partial<Omit<ToolConversationPart, "kind">>): CardStep {
  const step = resolveStep(part(over));
  if (!step) throw new Error("resolveStep returned no step");
  return step;
}

/** Read a stylesheet by its path relative to this test (vitest empties `?raw`). */
function readCss(...segments: string[]): string {
  const testPath = expect.getState().testPath;
  if (!testPath) throw new Error("vitest testPath unavailable, cannot locate the stylesheet");
  return readFileSync(join(dirname(testPath), ...segments), "utf8");
}

/** The run as a reader sees it once it settled: mounted done, every well the
 *  run holds opened, so the status line and the body are both on screen. */
function run(steps: CardStep[], summary = `${steps.length} tool calls`): HTMLElement {
  const { container } = render(
    <div className="chat-root">
      <Activity summary={summary} steps={steps.map((step) => ({ ...step, expanded: true }))} folded={false} />
    </div>,
  );
  return container;
}

function digest(container: HTMLElement): string {
  return container.querySelector(".chat-activity-digest")?.textContent ?? "";
}

/** Every element whose own rendered text is empty but that draws a glyph: a
 *  mark standing on a line with no words beside it. */
function loneGlyphLines(container: HTMLElement): Element[] {
  return Array.from(container.querySelectorAll(".chat-activity-log p, .chat-activity-log div")).filter(
    (el) => el.querySelector("svg") !== null && (el.textContent ?? "").trim() === "" && el.children.length === 1,
  );
}

let sheet: HTMLStyleElement;
beforeAll(() => {
  sheet = document.createElement("style");
  sheet.textContent = readCss("tokens.css") + readCss("shared.css") + readCss("..", "activity", "activity.css");
  document.head.append(sheet);
});
afterAll(() => sheet.remove());

describe("a tool call the turn's Stop cut off", () => {
  // The common shape: a wrapped call stopped before its arguments
  // landed, stamped with the harness's own abort text.
  const stoppedNameless = (): CardStep => stepOf({ state: "stopped", input: {}, errorText: HARNESS_ABORT });

  it("says so once, as one sentence with its mark on the same line", () => {
    const container = run([stoppedNameless()]);
    const lines = within(container).getAllByText(STOPPED);
    expect(lines).toHaveLength(1);
    const line = lines[0].closest(".chat-tool-stop");
    expect(line, "the sentence sits in the step's status line").not.toBeNull();
    expect(line?.querySelector("svg"), "the mark rides the sentence's own line").not.toBeNull();
    // The line lays its mark beside the words rather than stacking them.
    expect(getComputedStyle(line as Element).display).toBe("flex");
    expect(loneGlyphLines(container)).toEqual([]);
  });

  it("does not repeat the harness's words or claim an empty result", () => {
    const text = run([stoppedNameless()]).textContent ?? "";
    expect(text).not.toContain(HARNESS_ABORT);
    expect(text).not.toContain(EMPTY);
  });

  it("offers no result to open when it never had one", () => {
    const step = stoppedNameless();
    expect(step.body).toBeUndefined();
    expect(step.data).toBeUndefined();
    expect(run([step]).textContent ?? "").not.toMatch(/(Show|Hide) result/);
  });

  it("keeps the arguments it had, without a result notice under them", () => {
    const step = stepOf({ state: "stopped", name: "mystery_tool", input: { table: "orders" }, errorText: HARNESS_ABORT });
    const text = run([step]).textContent ?? "";
    expect(text).toContain("orders");
    expect(text).not.toContain(EMPTY);
    expect(text.split(STOPPED)).toHaveLength(2);
  });

  it("is counted as stopped in the run's header, not as failed", () => {
    const container = run([stoppedNameless()], "Ran 1 tool call");
    expect(digest(container)).toContain("1 stopped");
    expect(digest(container)).not.toContain("failed");
  });

  it("carries no failure mark on its badge", () => {
    const container = run([stoppedNameless()]);
    const badge = container.querySelector(".chat-activity-badge");
    expect(badge?.getAttribute("data-status")).toBe("stopped");
    expect(container.querySelector(".chat-activity-badge__x")).toBeNull();
  });

  it("stops a shell command the same way, not as a failed command", () => {
    const step = stepOf({ state: "stopped", name: "bash", input: { command: "sleep 40" }, errorText: HARNESS_ABORT });
    expect(step.status).toBe("stopped");
    expect(step.failure).toBeUndefined();
    const text = run([step]).textContent ?? "";
    expect(text.split(STOPPED)).toHaveLength(2);
    expect(text).not.toContain(HARNESS_ABORT);
  });

  it("does not read as a failed spec lookup", () => {
    const step = stepOf({ state: "stopped", name: "bigquery.get_table", input: { table: "orders" }, errorText: HARNESS_ABORT });
    expect(step.status).toBe("stopped");
    expect(step.data).toBeUndefined();
    const text = run([step]).textContent ?? "";
    expect(text).not.toContain("Failed");
    expect(text).not.toContain("returned no result");
  });

  it("writes its sentence in sentence case", () => {
    expect(STOPPED).toMatch(/^[A-Z][^A-Z]*\.$/);
  });
});

describe("the neighbours the stopped state must leave alone", () => {
  it("a failed call still reads its error, on one laid-out line, counted as failed", () => {
    const step = stepOf({ state: "error", name: "mystery_tool", input: { table: "orders" }, errorText: "permission denied for relation orders" });
    const container = run([step], "Ran 1 tool call");
    const fail = within(container).getByText("permission denied for relation orders").closest(".chat-tool-fail");
    expect(fail).not.toBeNull();
    expect(fail?.querySelector("svg")).not.toBeNull();
    // The line that stranded its X: outside the well, no rule laid it out.
    expect(getComputedStyle(fail as Element).display).toBe("flex");
    expect(digest(container)).toContain("1 failed");
    expect(container.textContent ?? "").not.toContain(STOPPED);
    expect(loneGlyphLines(container)).toEqual([]);
  });

  it("a failed call does not also claim it returned nothing", () => {
    // The error is what the call returned; an empty-result notice under it
    // contradicts the line above.
    const step = stepOf({ state: "error", name: "mystery_tool", input: { table: "orders" }, output: null, errorText: "timeout" });
    expect(run([step]).textContent ?? "").not.toContain(EMPTY);
  });

  it("a finished call that returned nothing still says so", () => {
    const step = stepOf({ state: "completed", name: "mystery_tool", input: { table: "orders" }, output: null });
    const text = run([step]).textContent ?? "";
    expect(text).toContain(EMPTY);
    expect(text).not.toContain(STOPPED);
  });

  it("a run holding both counts each under its own word", () => {
    const failed = stepOf({ state: "error", name: "mystery_tool", errorText: "timeout" });
    const stopped = stepOf({ state: "stopped", name: "other_tool", errorText: HARNESS_ABORT });
    const header = digest(run([failed, stopped], "Ran 2 tool calls"));
    expect(header).toContain("1 failed");
    expect(header).toContain("1 stopped");
  });
});
