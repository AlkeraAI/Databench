// One slot grammar, driven from the recording that proved there were two.
//
// A real agent does not write `{customer}`. Asked for a parameterised
// deliverable against Tinybird it writes Tinybird's own typed slots —
// `{{String(customer_id)}}`, `{{Date(start_date)}}` — because that is the
// syntax the engine binds. Reading only our own spelling, the query page would
// say "This query takes no parameters." over a statement with three of them.
//
// The server's `slots_of` is driven over the SAME fixture by
// `packages/api-core/tests/schemas/objects/test_query_slot_grammar.py`, so a
// spelling one side learns and the other does not is a red test here.

import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

import {
  MAX_DERIVED_PARAMS,
  PARAM_TYPES,
  TEMPLATE_SLOT_TYPES,
  parametersFromLiterals,
  slotsOf,
} from "@/lib/querySlots";

const REPO_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "../../../../..");
const FIXTURE = "packages/api-core/tests/fixtures/objects/seam/query_slots.json";
const SPECS_PY = "packages/api-core/alkera_core/schemas/objects/specs.py";
const read = (path: string): string => readFileSync(resolve(REPO_ROOT, path), "utf-8");

type Slot = { name: string; type: string };
type Case = { id: string; template: string; slots: Slot[] };
type Recorded = {
  engine: string;
  sql_template: string;
  slots: Slot[];
  values: Record<string, string>;
  changed: Record<string, string>;
};

const CASES = JSON.parse(read(FIXTURE)) as { grammar: Case[]; recorded: Recorded };

describe("the slot grammar", () => {
  it("has a fixture to be driven from", () => {
    expect(CASES.grammar.length).toBeGreaterThan(0);
  });

  it.each(CASES.grammar.map((c) => [c.id, c] as const))("%s", (_id, testCase) => {
    expect(slotsOf(testCase.template)).toEqual(testCase.slots);
  });

  it("maps every type function to the kind the compiler maps it to", () => {
    // Read off the table the server declares, so neither side can quietly
    // drop a type function or map one somewhere else.
    const source = read("packages/api-core/alkera_core/schemas/objects/query_params.py");
    const block = /TEMPLATE_SLOT_TYPES: dict\[str, ParamType\] = \{([^}]*)\}/s.exec(source);
    if (!block) throw new Error("no TEMPLATE_SLOT_TYPES table in query_params.py");
    const declared = Object.fromEntries(
      [...block[1].matchAll(/"([A-Za-z0-9]+)":\s*"([a-z]+)"/g)].map((m) => [m[1], m[2]]),
    );
    expect(TEMPLATE_SLOT_TYPES).toEqual(declared);
  });

  it("declares the parameter types the compiler declares", () => {
    const match = /ParamType\s*=\s*Literal\[([^\]]*)\]/s.exec(read(SPECS_PY));
    const members = [...(match?.[1] ?? "").matchAll(/"([^"]+)"/g)].map((m) => m[1]);
    expect([...PARAM_TYPES]).toEqual(members);
  });
});

describe("a saved Tinybird statement", () => {
  const recorded = CASES.recorded;

  it("declares its three parameters — the whole finding, as one assertion", () => {
    expect(slotsOf(recorded.sql_template)).toEqual(recorded.slots);
  });

  it("types the dates as dates, so the form asks for them with a date picker", () => {
    const byName = Object.fromEntries(slotsOf(recorded.sql_template).map((s) => [s.name, s.type]));
    expect(byName).toEqual({ customer_id: "string", start_date: "date", end_date: "date" });
  });

  it("offers no field for a directive that is not a typed slot", () => {
    // A field for `{{sql_and(…)}}` would be a box whose contents the server
    // refuses to compile at all — the browser must not invent one.
    const withDirectives = `${recorded.sql_template} /* {{sql_and(a=Int64(x))}} {% if y %}1{% end %} {{tb_secret('admin')}} */`;
    expect(slotsOf(withDirectives)).toEqual(recorded.slots);
  });
});

// A statement whose filters are LITERALS.
//
// Asked a plain question an agent writes `WHERE run_day >= toDate('…')`,
// because nothing asked it for a slot — so a query saved off that card has no
// parameter to change. Its own filters are offered as
// parameters instead, with the literals it ran with as their values, so a
// re-run that changes nothing runs the query that was saved.
describe("offering a statement's own literals as parameters", () => {
  const SQL =
    "SELECT run_day AS day, count() AS citations FROM citations " +
    "WHERE customer_id = 'cust_7' AND run_day >= toDate('2026-08-31') " +
    "AND run_day <= toDate('2026-09-06') GROUP BY run_day ORDER BY run_day";

  it("names one parameter per filter, and a window by its ends", () => {
    expect(parametersFromLiterals(SQL).params).toEqual([
      { name: "customer_id", type: "string" },
      { name: "run_day_from", type: "date" },
      { name: "run_day_to", type: "date" },
    ]);
  });

  it("keeps the values the statement ran with, so nothing changes by itself", () => {
    expect(parametersFromLiterals(SQL).defaults).toEqual({
      customer_id: "cust_7",
      run_day_from: "2026-08-31",
      run_day_to: "2026-09-06",
    });
  });

  it("rewrites only the literals — putting them back gives the statement back", () => {
    // The invariant that makes the rewrite safe to do without asking: it is
    // reversible, so the saved query still means what the agent wrote.
    const derived = parametersFromLiterals(SQL);
    let restored = derived.template;
    for (const { name, text } of derived.replaced) restored = restored.replace(`{${name}}`, text);
    expect(restored).toBe(SQL);
  });

  it("leaves a statement that already declares slots exactly as its author wrote it", () => {
    const authored = "SELECT 1 WHERE c = {{String(customer)}} AND d >= toDate('2026-01-01')";
    const derived = parametersFromLiterals(authored);
    expect(derived.params).toEqual([]);
    expect(derived.template).toBe(authored);
  });

  it.each([
    ["no filter at all", "SELECT count(*) FROM prompts"],
    ["a column compared to a column", "SELECT 1 FROM a JOIN b ON a.id = b.id"],
    ["a function argument that is not a comparison", "SELECT round(avg(x), 4) FROM t"],
    ["a quoting this reader cannot split", "SELECT 1 FROM t WHERE name = 'it''s'"],
    ["an unknown function around the value", "SELECT 1 FROM t WHERE name = lower('ACME')"],
  ])("offers nothing from %s", (_case, sql) => {
    const derived = parametersFromLiterals(sql);
    expect(derived.params).toEqual([]);
    expect(derived.template).toBe(sql);
  });

  it("stops at a form a person can still use", () => {
    const wide = `SELECT 1 FROM t WHERE ${Array.from({ length: 20 }, (_, i) => `c${i} = ${i}`).join(" AND ")}`;
    expect(parametersFromLiterals(wide).params).toHaveLength(MAX_DERIVED_PARAMS);
  });
});
