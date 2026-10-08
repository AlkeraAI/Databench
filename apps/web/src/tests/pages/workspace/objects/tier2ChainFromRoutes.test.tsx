// The Tier-2 chain's browser-facing ends, fed the bytes the ROUTES served.
//
// `apps/cli/tests/cloud/test_cloud_round3c_seams.py` drives the whole chain
// with production code on both sides — the browser's saved query re-run
// through the real route, relay, mirror and `sql.query`; that card promoted
// through the real route, mirror, blob and payload upload — and records what
// the browser would read at each end: the transcript entry carrying the re-run's
// result, the promoted object with its receipt and chart, its rows page, and the
// stored query object. Every other test of these surfaces hand-builds those
// documents. This one renders them over what a real run left behind, so a key
// the server renames, a value it stops sending, or a wrapper it adds goes red
// here rather than on the object page in front of a customer.

import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { unwrapCallTool, type ToolConversationPart } from "@alkera/chat-model";

import type { DocHandle } from "@/api/realtime/docSync";
import { createQueryClient } from "@/api/queryClient";
import { CloudDataSource } from "@/pages/workspace/chat/data/CloudDataSource";
import { columnsOf, eventIdOf, previewOf, querySpecOf } from "@/pages/workspace/chat/saveResult";
import { ObjectRoute } from "@/pages/workspace/objects/ObjectRoute";
import { guardSpec } from "@alkera/ui";

import { chartCaption, chartSpecOf, rowsForChart } from "@/pages/workspace/objects/chartSpec";
import { RECEIPT_FIELDS, receiptValue } from "@/pages/workspace/objects/receipt";

import { renderBody, stepOf } from "../../../chat-ui/_steps";

const REPO_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "../../../../../../..");

interface Recorded {
  chat_id: string;
  run_id: string;
  call_id: string;
  connection_id: string;
  query_object: Record<string, unknown> & { id: string; spec: Record<string, unknown> };
  transcript_entry: Record<string, unknown> & { seq: number; event_id: string };
  result_object: Record<string, unknown> & { id: string; status: string; spec: Record<string, unknown> };
  rows_page: { columns: string[]; keys: string[]; rows: unknown[][]; total: number };
  csv_head: string;
}

const RECORDED = JSON.parse(
  readFileSync(
    resolve(REPO_ROOT, "packages/api-core/tests/fixtures/objects/seam/tier2_chain_from_routes.json"),
    "utf-8",
  ),
) as Recorded;

function idleDoc(): DocHandle<never> {
  return {
    onMessage: () => () => undefined,
    onPhase: () => () => undefined,
    getPhase: () => ({ phase: "live", epoch: 1, seq: 0, peerId: "p:1", canWrite: false, pending: 0, error: null }),
    sendOp: () => Promise.reject(new Error("a reader never writes to a chat document")),
    dispose: () => undefined,
  } as unknown as DocHandle<never>;
}

/** The re-run's card, exactly as a reader who reloads the chat gets it: the
 *  served transcript entry, folded by the real source, unwrapped as the step
 *  resolver unwraps it before the card reads it. */
async function servedCard(): Promise<ToolConversationPart> {
  const page = {
    items: [RECORDED.transcript_entry],
    next_after_seq: RECORDED.transcript_entry.seq,
    resync_from: null,
  };
  const listMessages = vi
    .fn()
    .mockResolvedValueOnce(page)
    .mockResolvedValue({ items: [], next_after_seq: page.next_after_seq, resync_from: null });
  const source = new CloudDataSource({
    rest: { listMessages } as never,
    openDoc: () => idleDoc(),
    acquire: () => () => undefined,
    clientId: () => "client-1",
  });
  const turns = await source.getChatTurns(RECORDED.chat_id);
  const [part] = turns
    .flatMap((turn) => turn.parts)
    .filter((candidate): candidate is ToolConversationPart => candidate.kind === "tool");
  if (!part) throw new Error("the served entry folded to no tool card");
  return unwrapCallTool(part);
}

function scriptObjects(): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      const json = (body: unknown, status = 200) =>
        new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
      if (url.includes("/rows")) return json(RECORDED.rows_page);
      if (url.includes(`/api/v1/objects/${RECORDED.result_object.id}`)) return json(RECORDED.result_object);
      if (url.includes(`/api/v1/objects/${RECORDED.query_object.id}`)) return json(RECORDED.query_object);
      if (url.includes("/api/v1/chats/")) {
        return json({
          id: RECORDED.chat_id,
          title: "orders by day",
          machine_id: null,
          machine_status: "none",
          created_at: "2026-09-07T00:00:00Z",
          updated_at: "2026-09-07T00:00:00Z",
          last_seq: 2,
        });
      }
      return json({ detail: "not scripted" }, 404);
    }),
  );
}

function renderRoute(objectId: string): void {
  render(
    <QueryClientProvider client={createQueryClient({ retry: false })}>
      <MemoryRouter initialEntries={[`/objects/${objectId}`]}>
        <Routes>
          <Route path="/objects/:objectId" element={<ObjectRoute />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("the recording is a real Tier-2 run", () => {
  it("re-ran the saved query on the machine and promoted that card", () => {
    expect(RECORDED.call_id).toBe(`run-${RECORDED.run_id}`);
    expect(RECORDED.result_object.status).toBe("ready");
    expect(RECORDED.result_object.spec.source_event_id).toBe(RECORDED.call_id);
    expect(RECORDED.rows_page.total).toBeGreaterThan(50);
    expect(RECORDED.csv_head.split("\n")[0]).toBe("day,orders");
  });
});

describe("the re-run's card over the served transcript entry", () => {
  it.each([
    ["the connection", "Tideline Postgres"],
    ["the engine", "· postgres"],
    ["the role", "as analytics_readonly"],
    ["the row count", "60 rows"],
    ["the statement", "SELECT day, count(*) FROM prompts"],
  ])("shows %s without a click", async (_case, text) => {
    const body = renderBody(stepOf(await servedCard()));
    expect(body.textContent).toContain(text);
  });

  it("shows a bounded preview of the rows, never all of them", async () => {
    const preview = previewOf(await servedCard());
    expect(preview.columns).toEqual(["day", "orders"]);
    expect(preview.rows.length).toBeGreaterThan(0);
    expect(preview.rows.length).toBeLessThan(RECORDED.rows_page.total);
  });

  it("names, for the promote, the id the machine resolved the rows by", async () => {
    // The promote in the recording was made with THIS id, and the machine
    // answered it with the rows — so the browser's reading of the card and the
    // machine's reading of the transcript name the same thing.
    expect(eventIdOf(await servedCard())).toBe(RECORDED.call_id);
    expect(columnsOf(await servedCard())).toEqual([
      { name: "day", label: null },
      { name: "orders", label: null },
    ]);
  });

  it("saves a query that names the connection and the engine the card ran on", async () => {
    const spec = querySpecOf(await servedCard(), RECORDED.chat_id);
    expect(spec.connection_id).toBe(RECORDED.connection_id);
    expect(spec.engine).toBe("postgres");
    expect(spec.source_chat_id).toBe(RECORDED.chat_id);
  });
});

describe("the promoted result's page over the served object and rows", () => {
  it("renders every receipt fact the routes stored, none as a dash", () => {
    const receipt = RECORDED.result_object.spec.receipt as Record<string, unknown>;
    const rendered = Object.fromEntries(
      RECEIPT_FIELDS.map((field) => [field.label, receiptValue(receipt, field.key)]),
    );
    for (const label of ["SQL", "Connection", "Role", "Engine", "Run by", "Executed", "Rows", "Duration"]) {
      expect(rendered[label], `receipt field ${label}`).not.toBe("—");
    }
    expect(rendered["Connection"]).toBe("Tideline Postgres");
    expect(rendered["Role"]).toBe("analytics_readonly");
    expect(rendered["Engine"]).toBe("postgres");
    expect(rendered["Run by"]).not.toContain("{");
    expect(rendered["Parameters"]).toContain("customer: acme");
  });

  it("shows the receipt, the table and the export on screen", async () => {
    scriptObjects();
    renderRoute(RECORDED.result_object.id);
    const receipt = await screen.findByLabelText("Receipt");
    expect(receipt.textContent).toContain("Tideline Postgres");
    expect(receipt.textContent).toContain("analytics_readonly");
    const table = await screen.findByRole("table");
    expect(table.querySelectorAll("tbody tr").length).toBeGreaterThan(0);
    const link = await screen.findByRole("link", { name: "Export CSV" });
    expect(link.getAttribute("href")).toContain(`/api/v1/objects/${RECORDED.result_object.id}/export.csv`);
  });

  it("draws the chart the promote persisted, bound by the keys the rows route serves", async () => {
    const chart = chartSpecOf(RECORDED.result_object.spec.chart_spec);
    expect(chart).not.toBeNull();
    expect(guardSpec(chart)).toEqual({ ok: true });
    expect(chartCaption(chart as Record<string, unknown>)).toBe("orders over day");
    // Bound by key: every row of the recorded page reaches the chart with the
    // fields the encoding names.
    const rows = rowsForChart(RECORDED.rows_page);
    expect(rows).toHaveLength(RECORDED.rows_page.rows.length);
    expect(rows.every((row) => "day" in row && "orders" in row)).toBe(true);
    scriptObjects();
    renderRoute(RECORDED.result_object.id);
    const figure = await screen.findByRole("figure", { name: "orders over day" });
    await waitFor(() =>
      expect(figure.querySelector(".mark-line path")?.getAttribute("d")?.match(/[ML]/g)?.length ?? 0).toBeGreaterThan(1),
    );
  });
});

describe("the saved query the same run left behind", () => {
  // The recording pre-dates the retirement, so the row it captured is the real
  // thing: a `query` object carrying a parameterized statement and the slots
  // the browser's builder declared. That is exactly the shape a deployment
  // still holds after the type went, which makes these the right bytes to
  // prove the retirement over — not a row written to agree with it.
  it("is still a query row carrying the statement and its parameters", () => {
    const spec = RECORDED.query_object.spec as { sql_template?: string; params?: unknown[] };
    expect(RECORDED.query_object.type).toBe("query");
    expect(spec.sql_template).toContain("{customer}");
    expect(spec.params?.length).toBeGreaterThan(0);
  });

  it("says its kind was retired instead of offering the form that ran it", async () => {
    scriptObjects();
    renderRoute(RECORDED.query_object.id);
    await screen.findByText(/this kind of object was retired/i);
    expect(screen.queryByRole("form", { name: "Parameters" })).toBeNull();
    // The statement was the page's other half, echoed above the form; a
    // retirement that still prints it has retired a button.
    expect(screen.queryByText(/select/i)).toBeNull();
  });
});
