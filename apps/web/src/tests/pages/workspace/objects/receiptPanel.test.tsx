// The receipt panel: the nine facts a reader checks an answer against.
//
// This is the trust surface, so the cases are about the WRITER's spelling
// rather than the page's: a panel that asks for `connection` and `principal`
// when the receipt carries `connection_name` and `principal_chain` renders
// "Connection —" and "Run by —" with every page-spelled test green.

import { cleanup, render, screen, within } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { createQueryClient } from "@/api/queryClient";
import { ObjectPage } from "@/pages/workspace/objects/ObjectPage";
import { principalSummary, RECEIPT_FIELDS } from "@/pages/workspace/objects/receipt";

import { RECEIPT_FROM_ROUTES, SERVER_RECEIPT } from "./receiptFixture";

const OBJECT = {
  id: "o1",
  logical_id: "l1",
  namespace: "org",
  type: "result" as const,
  title: "Daily orders",
  version: 2,
  status: "ready" as const,
  spec: { receipt: SERVER_RECEIPT },
  visibility_scope: "org",
  created_at: "2026-09-06T12:00:00Z",
  updated_at: "2026-09-06T12:00:00Z",
  content_updated_at: "2026-09-06T12:00:00Z",
};

const ROWS = { columns: ["day", "orders"], rows: [["2026-09-05", 12]], total: 1 };

function script(object: unknown) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const body = String(input).includes("/rows") ? ROWS : object;
      return new Response(JSON.stringify(body), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    }),
  );
}

async function receiptPanel(receipt: unknown = SERVER_RECEIPT) {
  script({ ...OBJECT, spec: { receipt } });
  render(
    <QueryClientProvider client={createQueryClient({ retry: false })}>
      <MemoryRouter initialEntries={["/objects/o1"]}>
        <Routes>
          <Route path="/objects/:objectId" element={<ObjectPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return screen.findByLabelText("Receipt");
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("the nine facts a receipt carries", () => {
  it("asks the receipt for the field names the server writes", () => {
    // The panel's keys ARE the writer's keys — checked against the receipt the
    // server generated, not against a list retyped here.
    expect(RECEIPT_FIELDS.map((field) => field.key).filter((key) => !(key in SERVER_RECEIPT))) //
      .toEqual([]);
  });

  it.each(RECEIPT_FIELDS.map((field) => [field.label] as const))(
    "labels %s and gives it a value, without a click",
    async (label) => {
      const panel = await receiptPanel();
      const row = within(panel).getByText(label).closest("div");
      expect(row?.textContent).toContain(label);
      expect(row?.textContent).not.toMatch(new RegExp(`^${label}—$`));
    },
  );

  it("shows the parameters the query was bound with", async () => {
    const panel = await receiptPanel();
    expect(panel.textContent).toContain("acme");
  });

  it("shows a dash for what the machine could not learn", async () => {
    const panel = await receiptPanel({ ...SERVER_RECEIPT, role: null, duration_ms: null });
    const role = within(panel).getByText("Role").closest("div");
    // The unit rides the value now (`412 ms`), so the label is just "Duration".
    const duration = within(panel).getByText("Duration").closest("div");
    expect(role?.textContent).toBe("Role—");
    expect(duration?.textContent).toBe("Duration—");
  });

  it("shows a dash for a receipt written before the schema admitted null", async () => {
    const panel = await receiptPanel({ ...SERVER_RECEIPT, role: "", executed_at: "" });
    expect(within(panel).getByText("Role").closest("div")?.textContent).toBe("Role—");
  });
});

describe("who ran it", () => {
  it("names the person and the agent that acted in their session", () => {
    expect(principalSummary(SERVER_RECEIPT.principal_chain)).toBe(
      "ops@example.com via sess-01J7Q3M8",
    );
  });

  it("reads the chain the cloud records at promote time, under its own key", () => {
    // `POST /chats/{id}/promote` writes `{promoted_by: <actor document>}`.
    expect(
      principalSummary({
        promoted_by: {
          schema_version: "1.0.0",
          acting: { kind: "user", id: "u1", label: "dana@example.com", org_id: "o" },
          delegating_user: null,
          chain: [],
        },
      }),
    ).toBe("dana@example.com");
  });

  it("falls back to the id when a link carries no label", () => {
    expect(
      principalSummary({ acting: { kind: "user", id: "u-7", label: "", org_id: "o" }, chain: [] }),
    ).toBe("u-7");
  });

  it("says nothing it cannot read is an actor chain", () => {
    expect(principalSummary("dana@example.com")).toBeNull();
    expect(principalSummary({ who: "dana" })).toBeNull();
  });

  it("renders an unreadable chain verbatim rather than dropping it", async () => {
    const panel = await receiptPanel({ ...SERVER_RECEIPT, principal_chain: { who: "dana" } });
    expect(within(panel).getByText("Run by").closest("div")?.textContent).toContain("dana");
  });
});

describe("the receipt a real promotion leaves", () => {
  // The block above renders `generate.py`'s dump. That dump's principal chain
  // is `{acting, chain}` — a shape no route writes. The routes write
  // `promoted_by` + `uploaded_by`, each a nested actor-chain record, and these
  // cases render the panel over the recorded bytes so the difference is caught here.
  it("is the shape the routes write, not the one the generator dumps", () => {
    expect(Object.keys(RECEIPT_FROM_ROUTES.principal_chain as object).sort()).toEqual([
      "promoted_by",
      "uploaded_by",
    ]);
  });

  it("asks it for the same field names the panel reads", () => {
    expect(
      RECEIPT_FIELDS.map((field) => field.key).filter((key) => !(key in RECEIPT_FROM_ROUTES)),
    ).toEqual([]);
  });

  it("names who ran it — never a dash, never a raw object", async () => {
    const panel = await receiptPanel(RECEIPT_FROM_ROUTES);
    const runBy = within(panel).getByText("Run by").closest("div");
    expect(runBy?.textContent).not.toBe("Run by—");
    expect(runBy?.textContent).not.toContain("{");
    // The member who promoted it is the person the panel names.
    const promoter = (RECEIPT_FROM_ROUTES.principal_chain as { promoted_by: { acting: { label: string } } }).promoted_by.acting.label;
    expect(promoter).toContain("@");
    expect(runBy?.textContent).toContain(promoter);
  });

  it("shows the statement and the values it was bound with", async () => {
    const panel = await receiptPanel(RECEIPT_FROM_ROUTES);
    expect(panel.textContent).toContain("acme");
    expect(panel.textContent).toContain("Tideline Postgres");
  });
});
