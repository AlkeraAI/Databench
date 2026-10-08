// A spec sheet's rows can carry an `output_outdated` flag on every row (the
// notebook tools did, before they became one-line cards; any tool may). As a presence
// mark it read "Output outdated" beside a hollow X on every fresh cell, which a
// reader took for a dismiss button. The flag is a warning: left out when it is
// not set, and said in words when it is. The booleans that do read as a run of
// comparable answers keep their marks.

import { render, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { ToolConversationPart } from "@alkera/chat-model";

import { Activity } from "../activity/Activity";
import { resolveStep } from "./steps";

let minted = 0;

function body(name: string, output: unknown): HTMLElement {
  minted += 1;
  const part: ToolConversationPart = {
    id: `flag-part-${minted}`,
    kind: "tool",
    callId: `flag-call-${minted}`,
    name,
    state: "completed",
    input: { path: "analysis.alknb.py" },
    output: JSON.stringify(output),
  };
  const step = resolveStep(part);
  if (!step) throw new Error("resolveStep returned no step");
  const { container } = render(
    <div className="chat-root">
      <Activity summary="1 tool call" steps={[{ ...step, expanded: true }]} folded={false} />
    </div>,
  );
  return container;
}

const cell = (over: Record<string, unknown>) => ({ id: "c1", name: "load", status: "fresh", output_outdated: false, ...over });

describe("rows with an output_outdated flag on the spec sheet", () => {
  it("leaves the flag out of a cell whose output is current", () => {
    const sheet = body("bigquery.get_job", { run_id: "r1", status: "ok", cells: [cell({}), cell({ id: "c2", name: "plot" })] });
    expect(sheet).not.toHaveTextContent(/outdated/i);
    expect(within(sheet).queryByRole("img", { name: "No" })).toBeNull();
    // The rest of the cell is still there.
    expect(sheet).toHaveTextContent("plot");
  });

  it("says in words that a cell's output is outdated, under a plain label", () => {
    const sheet = body("bigquery.get_job", { run_id: "r1", status: "ok", cells: [cell({}), cell({ id: "c2", name: "plot", output_outdated: true })] });
    const said = within(sheet).getAllByText("Outdated: the code or environment changed since it ran");
    expect(said).toHaveLength(1);
    expect(said[0]!.closest(".chat-spec-f")).toHaveTextContent(/^Output/);
    expect(within(sheet).queryByRole("img", { name: "Yes" })).toBeNull();
  });

  it("keeps the yes and no marks for other booleans", () => {
    const sheet = body("bigquery.get_job", { run_id: "r1", status: "ok", cells: [cell({ has_table: false }), cell({ id: "c2", has_table: true })] });
    expect(within(sheet).getAllByRole("img", { name: "No" }).length).toBeGreaterThan(0);
    expect(within(sheet).getAllByRole("img", { name: "Yes" }).length).toBeGreaterThan(0);
  });
});
