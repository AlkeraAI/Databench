// The safe-slug rule for a connection name, and the pure form logic under every
// connection form. A handle becomes a directory component on every member's
// machine, so `isSafeHandle` has to refuse the same names the server refuses at
// the save boundary — and accept the same ones, or the form blocks a name that
// would have saved fine. The case list is the twin of
// `packages/api-core/tests/test_naming.py`; a case added to one belongs in the other.

import { describe, expect, it } from "vitest";

import {
  SAFE_HANDLE_MESSAGE,
  docBlockedOn,
  docComplete,
  isSafeHandle,
  isValid,
  orderedFields,
  orderedRows,
  slotFields,
  slotPrefills,
  withDefaults,
} from "./helpers";
import type { ConnectionFormView, FormFieldView, ValueSlotView } from "./types";

const MAX_LENGTH = 128;

describe("isSafeHandle", () => {
  it.each([
    ["uppercase letters", "Analytics-Prod"],
    ["one letter and nothing else", "a"],
    ["a digit may lead", "9lives"],
    ["every permitted punctuation", "x.y_z-0"],
    ["dots inside are not traversal", "a..b"],
    ["the longest permitted name", "a".repeat(MAX_LENGTH)],
  ])("accepts %s", (_label, handle) => {
    expect(isSafeHandle(handle)).toBe(true);
  });

  it.each([
    ["nothing typed at all", ""],
    ["a leading dot hides the directory", ".hidden"],
    ["a dotdot chain", "../../etc"],
    ["a posix separator", "a/b"],
    ["a windows separator", "a\\b"],
    ["a non-ascii letter", "café"],
    ["a shell metacharacter", "wh$"],
    ["one character past the cap", "a".repeat(MAX_LENGTH + 1)],
  ])("refuses %s", (_label, handle) => {
    expect(isSafeHandle(handle)).toBe(false);
  });

  // A JS transliteration of the server's Python rule gets these wrong when it
  // ports `\Z` as `$` under the `m` flag, or trusts a `.trim()` upstream to have
  // removed the whitespace. Each name still has to become a directory, which
  // Windows refuses outright.
  it.each([
    ["a trailing newline", "wh\n"],
    ["a trailing space", "wh "],
  ])("refuses %s, which only an end-of-string anchor catches", (_label, handle) => {
    expect(isSafeHandle(handle)).toBe(false);
  });

  it("gives the same verdict on every call", () => {
    // A `g` flag on the shared pattern would make every other call fail from a
    // carried `lastIndex`, and the accept cases above all pass on a first call.
    expect(isSafeHandle("wh_main")).toBe(true);
    expect(isSafeHandle("wh_main")).toBe(true);
    expect(isSafeHandle("wh_main")).toBe(true);
  });
});

describe("SAFE_HANDLE_MESSAGE", () => {
  it("tells the admin what a legal name looks like, not only that theirs is wrong", () => {
    // The admin is staring at a form they filled in correctly except for one
    // character. A refusal that omits the rule leaves them guessing.
    for (const permitted of ["letters", "digits", "dots", "underscores", "dashes"]) {
      expect(SAFE_HANDLE_MESSAGE).toContain(permitted);
    }
    expect(SAFE_HANDLE_MESSAGE).toMatch(/^[A-Z]/);
  });
});

// --- the values a submit carries ----------------------------------------------
//
// The renderer draws `f.default` in an untouched box, so what a user reads and
// what the form submits are two different maps unless someone reconciles them.

function field(name: string, over: Partial<FormFieldView> = {}): FormFieldView {
  return {
    name,
    label: name,
    type: "text",
    required: false,
    secret: false,
    default: "",
    enum_values: [],
    help: "",
    placeholder: "",
    group: "",
    ...over,
  };
}

function form(over: Partial<ConnectionFormView> = {}): ConnectionFormView {
  return { note: null, auth_methods: [], shared_fields: [], ...over };
}

describe("withDefaults", () => {
  const port = field("port", { default: "5432" });
  const user = field("user", { default: "alkera" });
  const host = field("host", { required: true });
  const token = field("token", { secret: true, default: "" });
  const tier = field("environment", { required: true, default: "dev" });

  const resolve = (values: Record<string, string>, methodFields: FormFieldView[] = [token]) =>
    withDefaults(
      form({ shared_fields: [host, port, user], trailing_fields: [tier] }),
      methodFields,
      values,
    );

  it.each([
    ["an answer that looks like blank but is not", "   "],
    ["a zero", "0"],
  ])("never overwrites %s", (_label, typed) => {
    // Whitespace is what a user typed. Treating it as blank would silently swap
    // their answer for the connector's, which is the opposite of the point.
    expect(resolve({ port: typed }).port).toBe(typed);
  });

  it("covers the method's own fields, not only the shared ones", () => {
    const secret = field("token", { secret: true, default: "seeded" });
    expect(resolve({}, [secret]).token).toBe("seeded");
  });

  it("covers the trailing fields", () => {
    // The tier is a trailing field, so a form that omitted it here would submit
    // a blank tier while showing an answer.
    expect(resolve({}).environment).toBe("dev");
  });

  it("fills every blank in one pass and carries the rest through", () => {
    const typed = { host: "db.internal", extra: "kept" };
    expect(resolve(typed)).toEqual({
      host: "db.internal",
      extra: "kept",
      port: "5432",
      user: "alkera",
      environment: "dev",
    });
  });

  it("leaves the caller's map untouched", () => {
    // The draft is React state; mutating it in place would seed a value the
    // renderer never re-read.
    const typed = { host: "db.internal" };
    resolve(typed);
    expect(typed).toEqual({ host: "db.internal" });
  });
});

// --- section order and the "Optional" rule ------------------------------------
//
// Three live connector forms, transcribed from their descriptors in
// the connector package's plugin descriptors, each carrying the
// `ask_groups` that `catalog.ask_groups` compiles for its TEAM form (deployment
// tier included). Between them they cover every shape the ordering has to
// survive: a form with no shared fields, a required field answerable by an
// optional alternative, and a method whose only field is optional.

/** The trailing deployment tier every team form appends, under its own heading. */
const ENVIRONMENT = field("environment", { required: true, group: "Deployment" });

interface ConnectorCase {
  form: ConnectionFormView;
  methodFields: FormFieldView[];
  /** Every input top to bottom, as an admin reads them. */
  order: string[];
  /** The field the "Optional" rule opens above. */
  dividerAbove: string;
}

const CONNECTORS: Record<string, ConnectorCase> = {
  // AWS declares an optional SSO region BETWEEN two required SSO fields. A
  // connector is free to do that, and the form has to survive it: the rule may
  // not open above `sso_account_id` / `sso_role_name`, which Save still demands.
  aws: {
    form: form({
      trailing_fields: [ENVIRONMENT],
      ask_groups: {
        sso: [["sso_start_url"], ["sso_account_id"], ["sso_role_name"], ["environment"]],
      },
    }),
    methodFields: [
      field("sso_start_url", { required: true }),
      field("sso_region", { default: "us-east-1" }),
      field("sso_account_id", { required: true }),
      field("sso_role_name", { required: true }),
      field("region", { default: "us-east-1" }),
      field("profile"),
    ],
    order: [
      "sso_start_url",
      "sso_account_id",
      "sso_role_name",
      "sso_region",
      "region",
      "profile",
      "environment",
    ],
    dividerAbove: "sso_region",
  },

  // The identity-then-credential shape: the method's required fields sit
  // directly after the required shared ones, so a user types account → user →
  // token rather than account → warehouse → database → token. `account_url` is
  // optional by its own flag yet answers `account`'s group, so it stays above
  // the rule — a divider announcing "nothing below is needed" directly above the
  // field `account`'s help tells you to use instead read as a contradiction.
  snowflake: {
    form: form({
      shared_fields: [
        field("account", { required: true }),
        field("account_url"),
        field("role"),
        field("warehouse"),
        field("database"),
        field("schema"),
      ],
      trailing_fields: [ENVIRONMENT],
      ask_groups: {
        pat: [["user"], ["token"], ["account", "account_url"], ["environment"]],
      },
    }),
    methodFields: [field("user", { required: true }), field("token", { required: true, secret: true })],
    order: [
      "account",
      "account_url",
      "user",
      "token",
      "role",
      "warehouse",
      "database",
      "schema",
      "environment",
    ],
    dividerAbove: "role",
  },

  // Postgres authenticates passwordless too, so its one method field is
  // OPTIONAL. It still lands beside the `user` it belongs to, ahead of `port`:
  // the method's own fields lead the optional tail. That adjacency is what the
  // connector declared — the password rides the auth method, the port rides the
  // shared fields — not an accident of the sort.
  postgres: {
    form: form({
      shared_fields: [
        field("host", { required: true }),
        field("port", { type: "number", default: "5432" }),
        field("dbname", { required: true }),
        field("user", { required: true }),
      ],
      trailing_fields: [ENVIRONMENT],
      ask_groups: {
        password: [["host"], ["dbname"], ["user"], ["environment"]],
      },
    }),
    methodFields: [field("password", { type: "password", secret: true })],
    order: ["host", "dbname", "user", "password", "port", "environment"],
    dividerAbove: "password",
  },
};

const CONNECTOR_CASES = Object.entries(CONNECTORS);

/** The contract's own reading of the required block — a field the form requires,
 *  or one the server named in some ask group. Recomputed here rather than
 *  imported, so the source cannot supply both the answer and the expectation. */
function requiredBlock(f: ConnectionFormView): Set<string> {
  const names = new Set(Object.values(f.ask_groups ?? {}).flatMap((groups) => groups.flat()));
  for (const spec of [...f.shared_fields, ...(f.trailing_fields ?? [])]) {
    if (spec.required) names.add(spec.name);
  }
  return names;
}

/** Every row from the "Optional" rule down, each paired with whether a group
 *  heading has re-opened a section by then. A heading starts a new section, so
 *  the rule above it no longer speaks for what follows. */
function belowOptionalRule(
  f: ConnectionFormView,
  methodFields: FormFieldView[],
): Array<{ name: string; headed: boolean }> {
  const rows = orderedRows(f, methodFields);
  const start = rows.findIndex((r) => r.optionalDivider);
  if (start < 0) return [];
  let headed = false;
  return rows.slice(start).map((r) => {
    headed = headed || r.heading !== null;
    return { name: r.field.name, headed };
  });
}

describe("orderedFields", () => {
  it.each(CONNECTOR_CASES)("reads %s required-first, method-owned next, trailing last", (_name, c) => {
    const actual = orderedFields(c.form, c.methodFields).map((f) => f.name);
    expect(actual).toEqual(c.order);

    // The same claim structurally, so a future reorder that keeps every name has
    // to answer for itself: the credential follows the identity fields with
    // nothing wedged between them.
    const required = requiredBlock(c.form);
    const requiredShared = c.form.shared_fields.filter((f) => required.has(f.name)).map((f) => f.name);
    const requiredMethod = c.methodFields.filter((f) => required.has(f.name)).map((f) => f.name);
    expect(actual.slice(0, requiredShared.length + requiredMethod.length)).toEqual([
      ...requiredShared,
      ...requiredMethod,
    ]);
  });
});

describe("orderedRows — the Optional rule", () => {
  it.each(CONNECTOR_CASES)("never opens above a field the %s form still requires", (_name, c) => {
    // The whole contract in one line: below the rule, a required-block field is
    // only allowed once a heading has started a section the rule doesn't cover.
    const required = requiredBlock(c.form);
    const stranded = belowOptionalRule(c.form, c.methodFields)
      .filter((r) => !r.headed && required.has(r.name))
      .map((r) => r.name);
    expect(stranded).toEqual([]);
  });

  it.each(CONNECTOR_CASES)("opens %s's rule above its first session default", (_name, c) => {
    const rows = orderedRows(c.form, c.methodFields);
    expect(rows.filter((r) => r.optionalDivider).map((r) => r.field.name)).toEqual([c.dividerAbove]);
  });

  it("exempts the trailing tier because a heading reopens the section, not because it trails", () => {
    // The one sanctioned exception, exercised rather than assumed: `environment`
    // is required AND sits below the rule, and what makes that legible is the
    // "Deployment" heading between them. Without this the invariant above could
    // pass on a form that never reaches its trailing field.
    const { form: awsForm, methodFields } = CONNECTORS.aws;
    expect(belowOptionalRule(awsForm, methodFields)).toContainEqual({
      name: "environment",
      headed: true,
    });
    const tier = orderedRows(awsForm, methodFields).at(-1);
    expect(tier).toMatchObject({ heading: "Deployment", optionalDivider: false });
  });

  it.each([
    ["every field is in the required block", [field("host", { required: true })], {}],
    ["nothing is required at all", [field("role"), field("warehouse")], {}],
    [
      "the only optional-looking field answers a group",
      [field("account", { required: true }), field("account_url")],
      { "": [["account", "account_url"]] },
    ],
  ])("draws no rule when %s", (_label, shared, groups) => {
    // A rule with nothing under it is a heading over an empty section, and one
    // over the whole form says every field is a session default. Both mislead.
    const f = form({ shared_fields: shared as FormFieldView[], ask_groups: groups as Record<string, string[][]> });
    expect(orderedRows(f, []).some((r) => r.optionalDivider)).toBe(false);
  });
});

// The completeness rule arrives COMPILED from the server as ask groups; the
// gate only checks the draft against them. No field here declares which other
// field answers it — that knowledge lives in one place, server-side.

const SNOWSIGHT_URL = "https://myorg.snowflakecomputing.com";

describe("isValid", () => {
  const host = field("host", { required: true });
  const draft = (values: Record<string, string>) => ({ handle: "wh", method: "", values });

  it("counts a required field whose served group another member answered", () => {
    // Snowflake takes an Account URL in place of the account identifier and the
    // connector builds from either, so a gate that demanded the identifier
    // disabled the button on a connection that works. The server compiled that
    // allowance into the group; the gate just reads it.
    const f = form({
      shared_fields: [field("account", { required: true }), field("account_url")],
      ask_groups: { "": [["account", "account_url"]] },
    });
    expect(isValid(draft({ account_url: SNOWSIGHT_URL }), f)).toBe(true);
    expect(isValid(draft({}), f)).toBe(false);
  });

  it("blocks on a served group nothing in the draft answers", () => {
    const f = form({
      shared_fields: [host, field("account_url")],
      ask_groups: { "": [["host"]] },
    });
    expect(isValid(draft({ account_url: SNOWSIGHT_URL }), f)).toBe(false);
  });

  it("falls back to required-fields-alone when an older server sends no groups", () => {
    // Conservative on purpose: without the compiled rule the gate cannot know
    // which field answers which, so the alternative allowance disappears from
    // the button while the server itself would still accept.
    const f = form({ shared_fields: [field("account", { required: true }), field("account_url")] });
    expect(isValid(draft({ account_url: SNOWSIGHT_URL }), f)).toBe(false);
    expect(isValid(draft({ account: "myorg-myaccount" }), f)).toBe(true);
  });

  it("holds the gate on a value past the field's declared bound", () => {
    // The bound is declared once server-side and rides the field; the longest
    // permitted value passes and one more character does not — even when the
    // group is otherwise answered.
    const f = form({
      shared_fields: [field("account", { required: true, max_length: 255 }), field("account_url")],
      ask_groups: { "": [["account", "account_url"]] },
    });
    expect(isValid(draft({ account: "a".repeat(255) }), f)).toBe(true);
    expect(isValid(draft({ account: "a".repeat(256) }), f)).toBe(false);
    expect(isValid(draft({ account: "a".repeat(256), account_url: SNOWSIGHT_URL }), f)).toBe(false);
  });
});

// --- the member gate over the one values document -------------------------------

function slot(name: string, over: Partial<ValueSlotView> = {}): ValueSlotView {
  return {
    name,
    label: name,
    value: "",
    owner: "member",
    deferred: false,
    secret: false,
    type: "text",
    required: false,
    default: "",
    enum_values: [],
    enum_labels: {},
    help: "",
    placeholder: "",
    group: "",
    max_length: 0,
    ...over,
  };
}

describe("docComplete", () => {
  const accountGroup = [["account", "account_url"]];

  it("counts what the member typed against their slot's group", () => {
    const doc = [slot("account", { required: true })];
    expect(docComplete(doc, [["account"]], {})).toBe(false);
    expect(docComplete(doc, [["account"]], { account: "myorg-myaccount" })).toBe(true);
    expect(docComplete(doc, [["account"]], { account: "   " })).toBe(false);
  });

  it("lets an admin slot the member has no box for close their group", () => {
    // The admin pasted the Account URL and withheld the identifier: the member
    // sees an Account box, types nothing into it, and Connect must still open.
    // The document carries both halves, so no hand merge is needed to see it.
    const doc = [
      slot("account", { required: true }),
      slot("account_url", { owner: "admin", value: SNOWSIGHT_URL }),
    ];
    expect(docComplete(doc, accountGroup, {})).toBe(true);
  });

  it("counts a deferred slot as answered — the value lives somewhere safer", () => {
    const doc = [slot("token", { required: true, secret: true, deferred: true })];
    expect(docComplete(doc, [["token"]], {})).toBe(true);
    expect(docComplete(doc, [["token"]], { token: "" })).toBe(true);
  });

  it("holds the gate on a typed value past the slot's bound", () => {
    const doc = [
      slot("account", { required: true, max_length: 255 }),
      slot("account_url", { owner: "admin", value: SNOWSIGHT_URL }),
    ];
    expect(docComplete(doc, accountGroup, { account: "a".repeat(255) })).toBe(true);
    // The group is closed by the admin's URL either way; only the bound trips.
    expect(docComplete(doc, accountGroup, { account: "a".repeat(256) })).toBe(false);
  });

  it("gates every served group, the admin's included", () => {
    // A stored row can carry a blank required admin slot; the gate reads the
    // groups it is given rather than deciding whose slots count.
    const doc = [slot("host", { owner: "admin", required: true }), slot("user", { required: true })];
    expect(docComplete(doc, [["host"], ["user"]], { user: "ada" })).toBe(false);
  });

  it("falls back to required member slots alone when an older daemon sends no groups", () => {
    const doc = [
      slot("user", { required: true }),
      slot("role"),
      slot("host", { owner: "admin", required: true }),
    ];
    expect(docComplete(doc, [], { user: "ada" })).toBe(true);
    expect(docComplete(doc, [], {})).toBe(false);
  });
});

describe("docBlockedOn", () => {
  // The completeness gate already refuses such a document; this names the field
  // so the member is not left staring at a dead button. Reachable only on rows
  // saved before the server validated completeness.

  it("names the first group no box on the form can answer, by its label", () => {
    // The warning reads one field name, and the label is what the admin's own
    // form showed for it — never the wire name.
    const doc = [
      slot("host", { owner: "admin", required: true, label: "Host" }),
      slot("port", { owner: "admin", required: true, label: "Port" }),
      slot("user", { required: true }),
    ];
    expect(docBlockedOn(doc, [["host"], ["port"], ["user"]], {})).toBe("Host");
  });

  it("stays silent while a member box can still close the group", () => {
    // An unanswered group is the normal state of a fresh form. The warning is
    // for a group the member cannot answer, not one they have not answered yet.
    const doc = [
      slot("account", { required: true }),
      slot("account_url", { owner: "admin", label: "Account URL" }),
    ];
    expect(docBlockedOn(doc, [["account", "account_url"]], {})).toBeNull();
  });

  it("stays silent once the admin's group holds an answer", () => {
    const group = [["host"]];
    const blank = slot("host", { owner: "admin", required: true });
    const filled = slot("host", { owner: "admin", required: true, value: "db.internal" });
    expect(docBlockedOn([filled], group, {})).toBeNull();
    // Structural like the gate itself: a typed entry closes the group wherever
    // the draft got it from.
    expect(docBlockedOn([blank], group, { host: "db.internal" })).toBeNull();
  });
});

describe("slotFields / slotPrefills", () => {
  const doc = [
    slot("account_url", { owner: "admin", label: "Account URL", value: SNOWSIGHT_URL }),
    slot("warehouse", { owner: "admin", label: "Warehouse", value: "" }),
    slot("token", { owner: "admin", label: "Token", value: "", secret: true, deferred: true }),
    slot("user", { required: true, label: "User", max_length: 32 }),
  ];

  it("renders exactly the member's slots, with their render surface intact", () => {
    const fields = slotFields(doc);
    expect(fields.map((f) => f.name)).toEqual(["user"]);
    expect(fields[0]).toMatchObject({ label: "User", required: true, max_length: 32 });
  });

  it("shows the admin's answered, non-secret slots and nothing else", () => {
    // A blank slot would read as a field with a value, and a secret's slot has
    // no value to show by construction.
    expect(slotPrefills(doc)).toEqual([["Account URL", SNOWSIGHT_URL]]);
  });
});
