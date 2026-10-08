// What `/objects` and `/objects/:id` answer now, through the REAL route table.
//
// Two retirements meet at this address. The saved-object INDEX went first: a
// saved query and a report are nodes in the Files tree, so a second listing of
// the same things was a second index — `/objects` lands on Files instead. Then
// the two TYPES went: the reusable unit is a chat template now, and nothing
// renders a `.alkeraquery` or a `.alkerareport` any more.
//
// What must survive both is the per-object address itself. A promoted RESULT is
// still a result — its receipt, its rows and its CSV are the whole point of
// saving one — and the address a Files node carries in its `object.web_url`
// facet is exactly `/objects/:objectId`. So the route still decides by TYPE: a
// result gets its page, and a row of a retired type says its kind went rather
// than rendering a page that no longer exists, or a 404 that blames the reader.

import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AppContent } from "@/App";
import { queryClient } from "@/api/queryClient";
import { objectRoute } from "@/lib/files/objectRoute";
import { NAV, hasChildren } from "@/app/nav";

import { SERVER_RECEIPT } from "../pages/workspace/objects/receiptFixture";

const VIEWER = {
  id: "viewer",
  email: "vera@x.io",
  first_name: "Vera",
  last_name: "Ng",
  display_name: "Vera Ng",
  email_verified_at: "2026-01-01T00:00:00Z",
  email_verification_required: false,
  email_verification_deadline: null,
  has_password: true,
};

const ORG = {
  id: "org-1",
  name: "Acme",
  parent_team_id: null,
  is_root: true,
  created_at: "2026-01-01T00:00:00Z",
  member_count: 1,
};

/** The stamps every object row carries, spelled once. */
const STAMPS = {
  version: 1,
  visibility_scope: "org",
  created_at: "2026-09-06T12:00:00Z",
  updated_at: "2026-09-06T12:00:00Z",
  content_updated_at: "2026-09-06T12:00:00Z",
};

/** The one type this address still renders: a promoted result, receipt and all. */
const RESULT = {
  id: "res_1",
  logical_id: "l0",
  namespace: "org",
  type: "result" as const,
  title: "Daily orders",
  status: "ready" as const,
  spec: { receipt: SERVER_RECEIPT },
  ...STAMPS,
};

const ROWS = {
  columns: ["day", "orders"],
  keys: ["day", "orders"],
  rows: [["2026-09-05", 12]],
  total: 1,
};

/** A saved report, as a row written before the retirement still reads off the
 *  wire: the type is retired, the row is not gone. */
const REPORT = {
  id: "rpt_1",
  logical_id: "l1",
  namespace: "org",
  type: "report" as const,
  title: "Weekly revenue review",
  status: "ready" as const,
  spec: {
    schema_version: "1.0.0",
    title: "Weekly revenue review",
    narrative: "Where revenue moved this week and which accounts moved it.",
    questions: [{ name: "week", type: "daterange", label: "Week", prompt: "Which week?" }],
    connections: [{ plugin: "snowflake", handle: "analytics-prod" }],
    steps: [
      { kind: "query", text: "select day, revenue from orders", connection: "analytics-prod" },
    ],
    rendering: { format: "pdf", template: "Headline, chart, then the movers table." },
    source_chat_id: "cht_7",
  },
  ...STAMPS,
};

/** And a saved query, the other retired type. */
const QUERY = {
  id: "qry_1",
  logical_id: "l2",
  namespace: "org",
  type: "query" as const,
  title: "Orders by customer",
  status: "ready" as const,
  spec: {
    sql_template: "select * from orders where customer = {customer}",
    params: [{ name: "customer", type: "string", label: "Customer", required: true }],
    engine: "postgres",
    connection_id: "conn_1",
  },
  ...STAMPS,
};

const OBJECTS = [RESULT, REPORT, QUERY];

const json = (body: unknown, status = 200): Response =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

/** Every path the app asked for, so a page can be asked what it did NOT fetch. */
let asked: string[] = [];

function route(req: Request): Response {
  const p = new URL(req.url).pathname;
  asked.push(p);
  if (p === "/api/v1/auth/me") return json(VIEWER);
  if (p === "/api/v1/config") return json({ self_hosted: false });
  if (p === "/api/v1/dashboard") {
    return json({
      user: VIEWER,
      org: ORG,
      teams: [ORG],
      pending_invitations: [],
      is_org_admin: false,
    });
  }
  if (p === "/api/v1/me/credits") {
    return json({ tier_key: "pro", tier_name: "Pro", pct_used: 0, reset_at: null, prepaid_credits: 0 });
  }
  if (p === "/api/v1/invitations/me") return json([]);
  if (p === "/api/v1/me/preferences") return json({ preferences: { schema_version: "2.0.0" } });
  if (p === "/api/v1/chat/models") return json({ items: [] });
  if (p === `/api/v1/objects/${RESULT.id}/rows`) return json(ROWS);
  const object = OBJECTS.find((candidate) => p === `/api/v1/objects/${candidate.id}`);
  if (object) return json(object);
  return json({ detail: `unmatched ${p}` }, 404);
}

/** The router's current path, read from inside the router the app renders in. */
function Where() {
  return <span data-testid="where">{useLocation().pathname}</span>;
}

const renderAt = (path: string) =>
  render(
    <MemoryRouter initialEntries={[path]}>
      <AppContent />
      <Where />
    </MemoryRouter>,
  );

beforeEach(() => {
  asked = [];
  queryClient.clear();
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: Request | string, init?: RequestInit) =>
      route(input instanceof Request ? input : new Request(input, init)),
    ),
  );
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

/** Every leaf the nav declares, parents' children included. */
const leaves = () =>
  NAV.flatMap((g) => g.items).flatMap((item) => (hasChildren(item) ? item.children : [item]));

describe("the nav after the Results listing was removed", () => {
  it("offers no leaf labelled Results", () => {
    expect(leaves().map((l) => l.label)).not.toContain("Results");
  });

  it("routes no leaf at /objects — the drive is the index now", () => {
    expect(leaves().map((l) => l.to)).not.toContain("/objects");
  });

  it("still offers Files, which is where those objects live", () => {
    expect(leaves().map((l) => l.to)).toContain("/files");
  });
});

describe("an old link to the saved-object index", () => {
  it("lands on Files rather than the catch-all 404", async () => {
    renderAt("/objects");

    await waitFor(() => expect(screen.getByTestId("where").textContent).toBe("/files"), {
      timeout: 5000,
    });
    expect(screen.queryByText(/page not found/i)).toBeNull();
  });
});

describe("the per-object page the drive opens", () => {
  it("is the address a result node's object facet carries", () => {
    // The facet is the whole contract between the two surfaces: whatever the
    // server hangs on the node is where Files sends the reader.
    const node = { object: { type: "result", id: RESULT.id, web_url: `/objects/${RESULT.id}` } };
    expect(objectRoute(node as never)).toBe(`/objects/${RESULT.id}`);
  });

  it("renders the result's receipt and its rows at that address", async () => {
    renderAt(`/objects/${RESULT.id}`);

    await screen.findByText("Daily orders", {}, { timeout: 5000 });
    const receipt = await screen.findByLabelText("Receipt", {}, { timeout: 5000 });
    expect(receipt.textContent).toContain(String(SERVER_RECEIPT.connection_name));
    const table = await screen.findByRole("table", {}, { timeout: 5000 });
    expect(table.querySelectorAll("tbody tr")).toHaveLength(1);
    expect(screen.queryByText(/page not found/i)).toBeNull();
  });
});

describe("a saved query or a saved report at that same address", () => {
  it.each([
    ["a report", REPORT],
    ["a query", QUERY],
  ])("%s says its kind was retired rather than rendering a page", async (_name, object) => {
    renderAt(`/objects/${object.id}`);

    await screen.findByText(/this kind of object was retired/i, {}, { timeout: 5000 });
    expect(screen.queryByText(/page not found/i)).toBeNull();
  });

  it("shows the reader none of the retired page's own body", async () => {
    renderAt(`/objects/${REPORT.id}`);

    await screen.findByText(/this kind of object was retired/i, {}, { timeout: 5000 });
    // The narrative, the connections and the steps WERE the report page. A
    // retirement that still renders them has retired a heading, not a page.
    expect(screen.queryByText(/where revenue moved this week/i)).toBeNull();
    expect(screen.queryByText(/analytics-prod/)).toBeNull();
    expect(screen.queryByText(/select day, revenue from orders/)).toBeNull();
  });

  it("asks the server for the row itself and for nothing behind it", async () => {
    renderAt(`/objects/${QUERY.id}`);

    await screen.findByText(/this kind of object was retired/i, {}, { timeout: 5000 });
    // Rows and the CSV are result-only routes that answer 404 for a query, so a
    // page that fires them anyway leaves 404s in the log of a screen that looks
    // fine. One read decides the type; nothing under it is asked for.
    expect(asked).toContain(`/api/v1/objects/${QUERY.id}`);
    expect(asked.filter((path) => path.startsWith(`/api/v1/objects/${QUERY.id}/`))).toEqual([]);
  });
});
