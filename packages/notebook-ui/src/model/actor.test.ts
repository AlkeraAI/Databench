import { runFooter } from "../editor/status";
import { OPEN_ACTOR_NAMES, actorLabel, nameActors } from "./actor";
import type { ActorRef, RunAttribution } from "./types";

const person = (display_name: string): ActorRef => ({ kind: "person", id: "user:5f939658", display_name });
const agent = (display_name: string, actingFor?: string): ActorRef => ({
  kind: "agent",
  id: "agent:a1",
  display_name,
  acting_for: actingFor === undefined ? null : { id: "user:1", display_name: actingFor },
});

describe("actorLabel", () => {
  it.each([
    ["a person, by name", person("Ada Lovelace"), "Ada Lovelace"],
    ["a person the server could not name", person("  "), "A former member"],
    ["an agent for a person", agent("Analyst", "Ada"), "Agent for Ada"],
    ["an agent for a person the server could not name, by its name", agent("Analyst", ""), "Analyst"],
    ["an agent for nobody, by its name", agent("Nightly refresh"), "Nightly refresh"],
    ["an unnamed agent for nobody", agent(""), "Agent"],
    ["the platform", { kind: "system", id: "system", display_name: "scheduler" } as ActorRef, "Databench"],
    ["nobody known", null, "Databench"],
  ])("labels %s", (_what, actor, label) => {
    expect(actorLabel(actor)).toBe(label);
  });

  it("never shows an id, or says someone or an agent", () => {
    for (const actor of [person(""), agent(""), agent("", "")]) {
      const label = actorLabel(actor);
      expect(label).not.toMatch(/user:|agent:|someone|an agent/i);
    }
  });

  it("names the platform and its agent the way a product registers them", () => {
    nameActors({ system: "Acme", agent: "Acme agent" });
    try {
      expect(actorLabel(agent("Analyst", "Ada"))).toBe("Acme agent for Ada");
      expect(actorLabel(agent(""))).toBe("Acme agent");
      expect(actorLabel(null)).toBe("Acme");
    } finally {
      nameActors(OPEN_ACTOR_NAMES);
    }
    expect(actorLabel(agent(""))).toBe("Agent");
  });
});

describe("runFooter", () => {
  const now = Date.parse("2026-10-05T10:05:00Z");
  const run = (by: ActorRef, trigger: RunAttribution["trigger"] = "run"): RunAttribution => ({
    run_id: "r1",
    by,
    trigger,
    started_at: null,
    finished_at: "2026-10-05T10:03:00Z",
  });

  it.each([
    [person("Ada"), "by Ada"],
    [agent("Analyst", "Ada"), "by Agent for Ada"],
    [person(""), "by A former member"],
    [{ kind: "system", id: "", display_name: "" } as ActorRef, "by Databench"],
  ])("keeps when apart from who", (by, who) => {
    expect(runFooter(run(by), 1200, now)).toEqual({ when: "2 min ago · 1.2 s", by: who });
  });

  it("marks an autorun among who and says nothing for a cell never run", () => {
    expect(runFooter(run(person("Ada"), "autorun"), null, now)).toEqual({ when: "2 min ago", by: "by Ada, autorun" });
    expect(runFooter(null, null, now)).toBeNull();
  });
});
