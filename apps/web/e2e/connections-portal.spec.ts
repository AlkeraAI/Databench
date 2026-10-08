// Adding a preconfigured connection, end to end in a real browser against a
// stubbed API: the admin fills the form in, the server takes a verification
// record, the record moves queued → running → settled, and only then does the
// real save go out carrying the record's id.
//
// This is the layer the jsdom suite cannot reach. There, every poll is a
// hand-turned tick and every phase change is a fetch the test resolves itself;
// here the dialog's own 2 s interval does the polling, react-query owns the
// timing, and the assertions are on what a person sees on screen as the record
// moves under them. The two failure modes this tier is for — a dialog that saves
// before the check settles, and a dialog that sits forever on a check nobody
// took — are both about elapsed real time, which is exactly what jsdom fakes.
//
// Run: pnpm --filter @alkera/web e2e -- connections-portal

import { expect, test, type Page, type Route } from "@playwright/test";

const ORG_ID = "00000000-0000-4000-8000-0000000000ff";
const VERIFICATION_ID = "44444444-4444-4444-4444-444444444444";

/** The account the guard lets through: named and verified, so neither the
 *  complete-profile gate nor the verification gate diverts before /teams. */
const ME = {
  id: "00000000-0000-4000-8000-000000000001",
  email: "dana@alkera.test",
  first_name: "Dana",
  last_name: "Ford",
  display_name: "Dana Ford",
  org_team_id: ORG_ID,
  org_name: "Tideline",
  platform_role: null,
  email_verified_at: "2026-01-01T00:00:00Z",
  email_verification_required: false,
  email_verification_deadline: null,
  created_at: "2026-01-01T00:00:00Z",
};

const ORG_TEAM = {
  id: ORG_ID,
  name: "Tideline",
  parent_team_id: null,
  is_root: true,
  created_at: "2026-01-01T00:00:00Z",
  member_count: 1,
};

/** Dana admins the org root, which is what puts the plate's writes in reach. */
const ROSTER = [
  {
    user_id: ME.id,
    email: ME.email,
    display_name: ME.display_name,
    first_name: ME.first_name,
    last_name: ME.last_name,
    role: "admin",
    team_id: ORG_ID,
    team_name: ORG_TEAM.name,
    is_direct: true,
    created_at: "2026-01-01T00:00:00Z",
  },
];

/** One connector, with the smallest form that still has a required field, a
 *  credential and the tier every team form carries. Fixture input, not a copy of
 *  the real catalog: the spec asserts the flow, not this connector's schema. */
const FORMS = {
  connectors: [
    {
      name: "postgres",
      title: "PostgreSQL",
      team_capable_methods: ["password"],
      ask_groups: {},
      form: {
        note: "",
        auth_methods: [
          {
            name: "password",
            label: "Password",
            oauth: null,
            fields: [
              {
                name: "password",
                label: "Password",
                type: "password",
                required: false,
                secret: true,
                default: "",
                enum_values: [],
                enum_labels: {},
                help: "",
                placeholder: "",
                group: "",
              },
            ],
          },
        ],
        shared_fields: [
          {
            name: "host",
            label: "Host",
            type: "text",
            required: true,
            secret: false,
            default: "",
            enum_values: [],
            enum_labels: {},
            help: "",
            placeholder: "",
            group: "",
          },
        ],
        trailing_fields: [
          {
            name: "environment",
            label: "Environment",
            type: "select",
            required: true,
            secret: false,
            default: "dev",
            enum_values: ["dev", "prod"],
            enum_labels: { dev: "Development", prod: "Production" },
            help: "",
            placeholder: "",
            group: "",
          },
        ],
      },
    },
  ],
};

type State = "queued" | "running" | "settled" | "abandoned";

/** One verification record, as `VerificationRead` spells it. */
function record(state: State, over: Record<string, unknown> = {}) {
  return {
    id: VERIFICATION_ID,
    connection_id: null,
    state,
    outcome: null,
    detail: "",
    endpoint_results: {},
    requested_at: new Date().toISOString(),
    dispatched_at: null,
    dispatch_attempts: 1,
    last_dispatch_error: "",
    started_at: null,
    finished_at: null,
    ...over,
  };
}

/** The row the server answers with once the save lands: already verified, since
 *  the record it was saved on IS its first check. */
function savedRow(handle: string) {
  return {
    id: "33333333-3333-3333-3333-333333333333",
    team_id: ORG_ID,
    team_name: ORG_TEAM.name,
    plugin: "postgres",
    handle,
    shared_values: { host: "db.internal", environment: "dev" },
    auth_mode: "shared",
    auth_method: "password",
    member_fields: [],
    values_doc: [],
    ask_groups: [],
    shared_consent: null,
    auto_add: false,
    enabled: true,
    has_shared_secret: true,
    has_primary_secret: true,
    credential_version: 1,
    oauth_client_id: null,
    has_oauth_client_secret: false,
    oauth_config: null,
    badge: "connected",
    badge_reason: "",
    outcome: "ok",
    last_detail: "",
    last_verified_at: new Date().toISOString(),
    verification_state: null,
    credential_state: "present",
    reauth: null,
    members: null,
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
  };
}

const json = (route: Route, body: unknown, status = 200) =>
  route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });

interface Recorder {
  /** Every body POSTed to start a check. */
  starts: Record<string, unknown>[];
  /** Every body PUT to save. */
  puts: Record<string, unknown>[];
}

/**
 * Stub the whole API behind the team detail page.
 *
 * `records` is the script the record's own GET walks, one entry per poll, the
 * last repeating — so a spec says "queued, then running, then settled" and the
 * dialog's real interval is what walks it.
 */
async function installConnections(
  page: Page,
  { records }: { records: ReturnType<typeof record>[] },
): Promise<Recorder> {
  const seen: Recorder = { starts: [], puts: [] };
  const rows: unknown[] = [];
  let polls = 0;

  // The event stream is the delivery path in production; here there is none, so
  // it is refused and the portal falls back to its polls — which is the path
  // under test.
  await page.route("**/api/v1/events*", (route) => route.abort());

  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const p = new URL(request.url()).pathname;
    const method = request.method();

    if (p.endsWith("/auth/me")) return json(route, ME);
    if (p.endsWith("/config")) {
      return json(route, { product_name: "Databench", oauth_providers: [], signup_enabled: true });
    }
    if (p.endsWith("/plugins/connection-forms")) return json(route, FORMS);

    if (method === "POST" && p.endsWith("/connections/verifications")) {
      seen.starts.push(JSON.parse(request.postData() ?? "{}"));
      return json(route, { id: VERIFICATION_ID }, 202);
    }
    if (method === "GET" && p.includes("/connections/verifications/")) {
      const served = records[Math.min(polls, records.length - 1)];
      polls += 1;
      return json(route, served);
    }
    if (method === "PUT" && p.endsWith("/connections")) {
      const body = JSON.parse(request.postData() ?? "{}") as { handle: string };
      seen.puts.push(body);
      rows.push(savedRow(body.handle));
      return json(route, rows[rows.length - 1]);
    }
    if (p.endsWith("/connections")) return json(route, rows);

    if (p.endsWith("/api/v1/teams")) return json(route, [ORG_TEAM]);
    if (p.endsWith("/members")) return json(route, ROSTER);
    if (p.endsWith("/invitations")) return json(route, []);

    return json(route, {});
  });

  return seen;
}

/** Vite's HMR client reloads the page on every dev-server ping, which would
 *  restart a dialog mid-check. Same guard the other harnesses use. */
async function stopViteReload(page: Page): Promise<void> {
  await page.addInitScript(() => {
    (window as unknown as { __vite_plugin_react_preamble_installed__: boolean })
      .__vite_plugin_react_preamble_installed__ = true;
  });
}

/** Open the add dialog with a complete postgres connection typed into it. */
async function fillTheForm(page: Page, handle: string): Promise<void> {
  await page.goto(`/teams/${ORG_ID}`, { waitUntil: "domcontentloaded" });
  await page.getByRole("button", { name: "Add a connection" }).click();
  await page.getByRole("button", { name: "PostgreSQL" }).click();
  await page.getByLabel(/Connection name/).fill(handle);
  await page.getByLabel(/^Host/).fill("db.internal");
}

test("the add dialog waits for the record, then saves carrying its id", async ({ page }) => {
  await stopViteReload(page);
  // Two polls of waiting before it settles, so the phases are ones the dialog's
  // own interval reaches rather than ones the first response hands it.
  const seen = await installConnections(page, {
    records: [
      record("queued"),
      record("running", { started_at: new Date().toISOString() }),
      record("settled", { outcome: "ok", finished_at: new Date().toISOString() }),
    ],
  });

  await fillTheForm(page, "wh_e2e");
  await page.getByRole("button", { name: "Save connection" }).click();

  // The check is asked for first, and nothing is saved while it is out.
  await expect(page.getByText("Starting the check…")).toBeVisible();
  expect(seen.puts).toHaveLength(0);
  await expect(page.getByText("Checking the connection…")).toBeVisible();
  expect(seen.puts).toHaveLength(0);

  // It settles, the save goes out and the dialog closes behind it.
  const row = page.getByRole("row", { name: /wh_e2e/ });
  await expect(row).toBeVisible();
  await expect(page.getByRole("dialog")).toBeHidden();

  // One check asked for, one save — naming the record it is the consequence of.
  expect(seen.starts).toHaveLength(1);
  expect(seen.puts).toHaveLength(1);
  expect(seen.puts[0]).toMatchObject({
    plugin: "postgres",
    handle: "wh_e2e",
    verification_id: VERIFICATION_ID,
  });

  // The row lands already verified. A second, pointless "Verifying…" here is the
  // flicker the whole record-carrying save exists to remove.
  await expect(row.getByText("Connected")).toBeVisible();
  await expect(page.getByText("Verifying…")).toBeHidden();
});

test("a record nobody picked up says so, and saves nothing", async ({ page }) => {
  await stopViteReload(page);
  const seen = await installConnections(page, {
    records: [record("queued"), record("abandoned")],
  });

  await fillTheForm(page, "wh_lost");
  await page.getByRole("button", { name: "Save connection" }).click();

  await expect(page.getByText("The worker didn't pick this up")).toBeVisible();
  // The blame lands on the worker that never took it, and not on the connection.
  await expect(page.getByText(/Nothing checked this connection/)).toBeVisible();
  expect(seen.puts).toHaveLength(0);

  // The dialog is editable again and offers the one thing that helps.
  await expect(page.getByLabel(/^Host/)).toBeEnabled();
  await page.getByRole("button", { name: "Try again" }).click();
  await expect.poll(() => seen.starts.length).toBe(2);
  expect(seen.puts).toHaveLength(0);
});
