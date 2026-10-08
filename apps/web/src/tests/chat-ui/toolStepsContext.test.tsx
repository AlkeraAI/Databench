// The context/skills/discovery adapters: the KB item's three-state trust
// grade, the shared-to-private downgrade, the opencode skill envelope, the
// capability roster's uniform-signature hoist, and the result page whose
// gutter counts in the blob. Closed by a net over every newly designed card:
// a null or unparseable output renders an empty state, never a throw.
//
// The trust chip and warn strings asserted below are user-facing copy; the
// pins are the point (pins-source: the card's rendered wording IS the contract).

import { describe, expect, it } from "vitest";

import { renderBody, stepOf, toolPart } from "./_steps";

// -- context: trust is three states, not the wire's two --------------------

function getPart(result: Record<string, unknown>): Parameters<typeof stepOf>[0] {
  return toolPart("context_get", {
    input: { item_id: "itm_1" },
    output: { item_id: "itm_1", title: "Orders grain", ...result },
  });
}

const ASSERTED_WORD = ["agent", "asserted"].join(" "); // pins-source: the caution chip's rendered copy, asserted as pixels-facing text

describe("context trust grade", () => {
  it("does not render blank or unknown trusted sources as verified", () => {
    const cases = [
      ["trusted", "human", "verified"],
      ["trusted", "", ASSERTED_WORD],
      ["trusted", "human_verified", ASSERTED_WORD],
      ["trusted", "agent_asserted", ASSERTED_WORD],
      // An untrusted item keeps the neutral default whatever its source says.
      ["untrusted", "agent_asserted", "unverified"],
    ] as const;
    for (const [trust, trust_source, word] of cases) {
      const body = renderBody(
        stepOf(getPart({ trust, trust_source, body: "The orders table is daily grain." })),
      );
      expect(body.querySelector("[data-trust]")?.textContent, `${trust}/${trust_source}`).toBe(
        word,
      );
    }
  });

  it("tones the figure by the grade", () => {
    const verified = stepOf(getPart({ trust: "trusted", trust_source: "human" }));
    expect(verified.data).toEqual({ kind: "count", text: "verified", tone: "pass" });
    const asserted = stepOf(getPart({ trust: "trusted", trust_source: "" }));
    expect(asserted.data).toEqual({ kind: "count", text: ASSERTED_WORD });
  });

  it("rejects a trust-only shortcut for context_search hits", () => {
    const step = stepOf(
      toolPart("context_search", {
        input: { query: "orders" },
        output: {
          team_knowledge: [
            {
              item_id: "human",
              title: "Human-confirmed grain",
              trust: " TrUsTeD ",
              trust_source: " HuMaN ",
            },
            {
              item_id: "blank",
              title: "Unsourced trusted grain",
              trust: "trusted",
              trust_source: " ",
            },
            {
              item_id: "agent",
              title: "Agent-confirmed grain",
              trust: "trusted",
              trust_source: "agent_asserted",
            },
          ],
          catalog: [],
        },
      }),
    );

    expect(step.data).toEqual({ kind: "count", text: "3 matches" });
    expect(
      [...renderBody(step).querySelectorAll("[data-trust]")].map((mark) => mark.textContent),
    ).toEqual(["verified", ASSERTED_WORD, ASSERTED_WORD]);
  });

  it("marks a stale item and names what drifted", () => {
    const step = stepOf(
      getPart({ trust: "trusted", trust_source: "", stale_sources: ["models/orders.sql"] }),
    );
    expect(step.data).toEqual({ kind: "count", text: `${ASSERTED_WORD}, stale`, tone: "fail" });
    expect(renderBody(step).textContent).toContain(
      "Changed after this was written: models/orders.sql",
    );
  });

  it("states no trust until the store answers", () => {
    const step = stepOf(toolPart("context_get", { state: "running", input: { item_id: "itm_1" } }));
    expect(step.data).toBeUndefined();
    expect(renderBody(step).querySelector("[data-trust]")).toBeNull();
  });
});

describe("context_note downgrade", () => {
  const asked = { text: "Revenue is net of refunds.", visibility: "shared" };

  it("leads with a share that was downgraded", () => {
    const step = stepOf(
      toolPart("context_note", {
        input: asked,
        output: { item_id: "itm_2", visibility: "private" },
      }),
    );
    expect(step.data).toEqual({ kind: "count", text: "saved private", tone: "fail" });
    const body = renderBody(step);
    expect(body.textContent).toContain(
      "Asked to share this, saved it private. There was no team to share to.",
    );
    expect(body.querySelector("[data-vis]")?.textContent).toBe("Private");

    // An honored share says nothing about a downgrade that did not happen.
    const kept = stepOf(
      toolPart("context_note", {
        input: asked,
        output: { item_id: "itm_2", visibility: "shared" },
      }),
    );
    expect(kept.data).toEqual({ kind: "count", text: "shared" });
    expect(renderBody(kept).textContent).not.toContain("no team to share to");
  });
});

// -- skill: two wire encodings, one card -----------------------------------

const SKILL_ENVELOPE = [
  '<skill_content name="deploy">', // pins-source: opencode's skill wire envelope, a frozen wire shape the adapter must keep parsing
  "# Skill: deploy",
  "",
  "Release the app safely.",
  "",
  "Step one of the runbook.",
  "",
  "Base directory for this skill: /skills/deploy",
  "",
  "Files:",
  "<file>/skills/deploy/checklist.md</file>",
  "<file>/skills/deploy/scripts/run.sh</file>",
  "</skill_content>",
].join("\n");

describe("skill step", () => {
  it("parses the opencode skill_content envelope", () => {
    const step = stepOf(toolPart("skill", { output: SKILL_ENVELOPE }));
    expect(step.verb).toBe("Loaded");
    expect(step.object).toBe("deploy");
    // The card is a long document, so it waits behind its word.
    expect(step.expanded).toBe(false);
    const body = renderBody(step);
    expect(body.textContent).toContain("Release the app safely.");
    expect(body.textContent).toContain("Step one of the runbook.");
    // The base-directory note is envelope furniture, not the skill's text.
    expect(body.textContent).not.toContain("Base directory for this skill");
    const files = [...body.querySelectorAll(".chat-skill-file__path")].map(
      (file) => file.textContent,
    );
    expect(files).toEqual(["/skills/deploy/checklist.md", "/skills/deploy/scripts/run.sh"]);
    expect(body.textContent).toContain("The file list is sampled.");
  });

  it("reads the alkera JSON body without a sampled-files note", () => {
    const step = stepOf(
      toolPart("use_skill", {
        input: { name: "assay" },
        output: { name: "assay", body: "One line." },
      }),
    );
    expect(step.object).toBe("assay");
    expect(step.data).toEqual({ kind: "count", text: "1 line" });
    expect(renderBody(step).textContent).not.toContain("The file list is sampled.");
  });

  it("renders a nameless call as the roster of what can be loaded", () => {
    const skills = {
      skills: [
        { name: "assay", description: "Profile a dataset." },
        { name: "deploy", description: "" },
      ],
    };
    const step = stepOf(toolPart("use_skill", { output: skills }));
    expect(step.verb).toBe("Listed");
    expect(step.data).toEqual({ kind: "count", text: "2 skills" });
    expect(step.expanded).toBe(true);
    const names = [...renderBody(step).querySelectorAll(".chat-skill-skill__name")].map(
      (name) => name.textContent,
    );
    expect(names).toEqual(["assay", "deploy"]);
  });
});

// -- search_tools / list_agent_types: the uniform-signature hoist ----------

const schema = (required: string[], optional: string[] = []): Record<string, unknown> => ({
  properties: Object.fromEntries(
    [...required, ...optional].map((name) => [name, { type: "string" }]),
  ),
  required,
});

describe("capability roster", () => {
  it("hoists the signature only when more than one row shares it", () => {
    const rosterOf = (tools: Record<string, unknown>): HTMLElement =>
      renderBody(stepOf(toolPart("search_tools", { input: { query: "q" }, output: tools })));

    const uniform = rosterOf({
      tools: [
        {
          name: "spawn_analyst",
          description: "Runs analysis.",
          input_schema: schema(["prompt"], ["model"]),
        },
        {
          name: "spawn_writer",
          description: "Writes prose.",
          input_schema: schema(["prompt"], ["model"]),
        },
      ],
    });
    expect(uniform.textContent).toContain("Each takes prompt*, model");
    // Hoisted means the rows repeat nothing.
    expect(uniform.querySelector("[data-cap]")?.textContent).not.toContain("prompt");

    const mixed = rosterOf({
      tools: [
        { name: "sql.query", description: "", input_schema: schema(["sql"]) },
        { name: "web.search", description: "", input_schema: schema(["query"]) },
      ],
    });
    expect(mixed.textContent).not.toContain("Each takes");
    expect(mixed.querySelector("[data-cap]")?.textContent).toBe("sql.querysql*web.searchquery*");

    const lone = rosterOf({
      tools: [{ name: "sql.query", description: "", input_schema: schema(["sql"]) }],
    });
    expect(lone.textContent).not.toContain("Each takes");
    expect(lone.querySelector("[data-cap]")?.textContent).toBe("sql.querysql*");
  });

  it("reads list_agent_types off the agents key with no query band", () => {
    const agents = {
      agents: [
        { name: "analyst", description: "Analyzes.", input_schema: schema(["prompt"]) },
        { name: "writer", description: "Writes.", input_schema: schema(["prompt"]) },
      ],
    };
    const step = stepOf(toolPart("list_agent_types", { output: agents }));
    expect(step.verb).toBe("Listed");
    expect(step.object).toBe("agent types");
    expect(step.data).toEqual({ kind: "count", text: "2 agents" });
    const body = renderBody(step);
    expect(body.querySelector(".chat-tool-band")).toBeNull();
    expect(body.textContent).toContain("Each takes prompt*");
  });
});

// -- fetch_result: the gutter counts in the blob ---------------------------

const PAGE_RESULT = {
  kind: "rows",
  columns: ["id", "name"],
  rows: [
    [1, "a"],
    [2, "b"],
  ],
  offset: 4800,
  total: 9000,
  returned: 2,
  has_more: true,
  next_offset: 4802,
};

describe("fetch_result step", () => {
  it("numbers each row at its place in the blob, not in the page", () => {
    const step = stepOf(
      toolPart("fetch_result", { input: { handle: "d".repeat(64) }, output: PAGE_RESULT }),
    );
    const gutters = [...renderBody(step).querySelectorAll(".chat-fetch-d.chat-fetch-n")].map(
      (cell) => cell.textContent,
    );
    expect(gutters).toEqual(["4,801", "4,802"]);

    const first = { ...PAGE_RESULT, offset: 0, has_more: false, next_offset: null };
    const body = renderBody(
      stepOf(toolPart("fetch_result", { input: { handle: "d".repeat(64) }, output: first })),
    );
    expect(
      [...body.querySelectorAll(".chat-fetch-d.chat-fetch-n")].map((cell) => cell.textContent),
    ).toEqual(["1", "2"]);
  });

  it("states the window and the share of the blob", () => {
    const step = stepOf(
      toolPart("fetch_result", { input: { handle: "d".repeat(64) }, output: PAGE_RESULT }),
    );
    expect(step.data).toEqual({ kind: "count", text: "2 of 9,000 rows" });
    expect(step.footer).toBe("rows 4,801 to 4,802 of 9,000, more from row 4,803");
    expect(step.expanded).toBe(false);

    // The last page owes no "more from" clause.
    const last = { ...PAGE_RESULT, has_more: false, next_offset: null };
    const end = stepOf(
      toolPart("fetch_result", { input: { handle: "d".repeat(64) }, output: last }),
    );
    expect(end.footer).toBe("rows 4,801 to 4,802 of 9,000");
  });

  it("decodes the numbers JSON cannot carry", () => {
    const wrapped = {
      ...PAGE_RESULT,
      rows: [
        [{ $nonfinite: "nan" }, "a"],
        [{ $nonfinite: "inf" }, { $nonfinite: "-inf" }],
      ],
    };
    const body = renderBody(
      stepOf(toolPart("fetch_result", { input: { handle: "d".repeat(64) }, output: wrapped })),
    );
    const cells = [...body.querySelectorAll(".chat-fetch-d:not(.chat-fetch-n)")].map(
      (cell) => cell.textContent,
    );
    expect(cells).toContain("NaN");
    expect(cells).toContain("Infinity");
    expect(cells).toContain("-Infinity");
  });

  it("renders a text page as text, with characters as the unit", () => {
    const text = {
      kind: "text",
      text: "spilled prose",
      offset: 0,
      total: 13,
      returned: 13,
      has_more: false,
    };
    const step = stepOf(
      toolPart("fetch_result", { input: { handle: "d".repeat(64) }, output: text }),
    );
    expect(step.data).toEqual({ kind: "count", text: "13 of 13 characters" });
    const body = renderBody(step);
    expect(body.querySelector(".chat-fetch-grid")).toBeNull();
    expect(body.querySelector(".chat-fetch-text")?.textContent).toBe("spilled prose");
  });
});

// -- The empty-state net: no payload shape may take down a step ------------

const DESIGNED_CARDS = [
  "todowrite",
  "apply_patch",
  "lsp",
  "repo_clone",
  "repo_overview",
  "sql.schema",
  "sql.connections",
  "blob.create",
  "blob.delete",
  "blob.info",
  "blob.materialize",
  "blob.profile",
  "blob.query",
  "blob.derive",
  "fetch_result",
  "context_get",
  "context_note",
  "context_edit",
  "context_discover",
  "use_skill",
  "skill",
  "search_tools",
  "list_agent_types",
  "list_plugins",
  "looker.list_dashboards",
  "looker.dashboard_tiles",
];

describe("designed cards", () => {
  it("survives a null or unparseable output with a speakable head", () => {
    for (const name of DESIGNED_CARDS) {
      for (const output of [null, "{ not json"]) {
        const step = stepOf(toolPart(name, { output }));
        expect(step.verb.length, `${name} on ${output}`).toBeGreaterThan(0);
        expect(step.loneSummary.length, `${name} on ${output}`).toBeGreaterThan(0);
        expect(() => renderBody(step), `${name} on ${output}`).not.toThrow();
      }
    }
  });
});
