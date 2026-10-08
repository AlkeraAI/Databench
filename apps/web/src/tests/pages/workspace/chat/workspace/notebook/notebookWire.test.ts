// The wire readers: who did something, a table page and its sort, and the
// agents a view places in cells.

import { describe, expect, it } from "vitest";

import { actorLabel } from "@alkera/notebook-ui";

import type { NotebookPresence } from "@/api/notebooks";
import { actorOf, agentsOfView, attributionOf, queuedBy, sortParam } from "@/pages/workspace/chat/workspace/notebook/notebookWire";

describe("actors", () => {
  it.each([
    ["a person", { kind: "person", id: "user:1", display_name: "Ada Lovelace" }, "Ada Lovelace"],
    ["an agent for a person", { kind: "agent", id: "agent:a", display_name: "Analyst", acting_for: { id: "user:1", display_name: "Ada" } }, "Agent for Ada"],
    ["an unnamed agent for a person the server could not name", { kind: "agent", id: "agent:a", display_name: "", acting_for: { id: "user:9", display_name: "" } }, "Agent"],
    ["the platform", { kind: "system", id: "system", display_name: "" }, "Databench"],
    ["a member whose name is gone", { kind: "person", id: "user:9", display_name: "" }, "A former member"],
  ])("labels %s", (_what, wire, label) => {
    expect(actorLabel(actorOf(wire))).toBe(label);
  });

  it.each([
    ["user:5f939658-762b-4c80-8854-5117adbbc20c", "person"],
    ["agent:run-7", "agent"],
  ])("never shows a bare id (%s) as a name", (id, kind) => {
    const actor = actorOf(id);
    expect(actor).toEqual({ kind, id, display_name: "", acting_for: null });
    expect(actorLabel(actor)).not.toContain(id);
  });

  it.each([
    ["a name, as the engine's queue sends it", "Alkera agent for Ada", "Alkera agent for Ada"],
    ["an id, as an older one did", "user:5f939658-762b-4c80-8854-5117adbbc20c", "A former member"],
  ])("reads a queued run's `by` holding %s", (_what, by, label) => {
    expect(actorLabel(queuedBy(by))).toBe(label);
  });

  it("reads a run's attribution, or nothing without a run id", () => {
    expect(attributionOf({ run_id: "r1", by: { kind: "person", id: "u", display_name: "Bo" }, trigger: "run_all", started_at: "s", finished_at: "f" })).toEqual({
      run_id: "r1",
      by: { kind: "person", id: "u", display_name: "Bo", acting_for: null },
      trigger: "run_all",
      started_at: "s",
      finished_at: "f",
    });
    expect(attributionOf({ by: "user:1" })).toBeNull();
    expect(attributionOf(null)).toBeNull();
  });
});

describe("agents in a view", () => {
  const row = (extra: Partial<NotebookPresence> & Record<string, unknown>): NotebookPresence => ({ who: "agent:a1", cell_id: "c1", kind: "agent", ...extra }) as NotebookPresence;

  it("take the name and the person each works for when the server sends them", () => {
    expect(agentsOfView([row({ display_name: "Analyst", acting_for: { id: "u1", display_name: "Ada" } })], 0)).toEqual([
      { actor: "agent:a1", who: "agent:a1", display_name: "Analyst", acting_for: { id: "u1", display_name: "Ada" }, cell_id: "c1", until: null },
    ]);
  });

  it.each([["agent:a1"], ["user:1"], ["5f939658-762b-4c80-8854-5117adbbc20c"]])("never take an id (%s) for a name", (who) => {
    expect(agentsOfView([row({ who })], 0)[0]!.display_name).toBe("");
  });

  it("take a plain name an older server sent as `who`, and leave people out", () => {
    expect(agentsOfView([row({ who: "Analyst" }), row({ who: "Ada", kind: "person" })], 0)).toEqual([
      { actor: "Analyst", who: "Analyst", display_name: "Analyst", acting_for: null, cell_id: "c1", until: null },
    ]);
  });

  it("are one actor by the id the server names, wherever they are", () => {
    const found = agentsOfView([row({ who: "Analyst", actor_id: "agent:chat-1" }), row({ who: "Analyst", actor_id: "agent:chat-1", cell_id: "c2" })], 0);
    expect(found.map((a) => [a.actor, a.cell_id])).toEqual([
      ["agent:chat-1", "c1"],
      ["agent:chat-1", "c2"],
    ]);
  });

  it.each([
    [12.5, 1000, 13_500],
    [0, 1000, 1000],
    [-3, 1000, 1000],
  ])("lapse %s s after they were heard at %s ms (at %s ms)", (expiresIn, heard, until) => {
    expect(agentsOfView([row({ expires_in: expiresIn })], heard)[0]!.until).toBe(until);
  });
});

describe("table pages", () => {
  it.each([
    [[{ column: "units", descending: false }], "units:asc"],
    [[{ column: "units", descending: true }, { column: "a:b", descending: false }], "units:desc,a:b:asc"],
    [[], null],
    [undefined, null],
  ])("writes the sort %j as %j", (sort, param) => {
    expect(sortParam(sort)).toBe(param);
  });

  it("refuses a column whose name the sort syntax cannot hold", () => {
    expect(() => sortParam([{ column: "a,b", descending: false }])).toThrow("can't be sorted");
  });
});
