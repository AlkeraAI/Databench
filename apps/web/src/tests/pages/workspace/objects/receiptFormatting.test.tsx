// The receipt and the card that produced it read the same facts the same way.
//
// A promoted result is shown twice: as a tool card in the transcript it came
// out of, and as the receipt on the object page it was saved into. Those two
// are meant to be comparable — that is what a receipt is FOR — and they were
// not: the page printed `2026-09-07T12:00:00+00:00`, `412` and
// `{"customer":"acme"}` where the card printed a locale timestamp, `412 ms`
// and a label/value list. Two clocks and two formats on the one surface whose
// job is to be checkable.
//
// So the cases below are equivalences, not fixed strings: whatever the reader's
// locale renders, BOTH surfaces must render it identically. A test that pinned
// a formatting would pass while the two drifted apart again.

import { cleanup, render, screen, within } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { formatCount, formatDuration, formatTimestamp } from "@alkera/ui";

import { createQueryClient } from "@/api/queryClient";
import { ObjectPage } from "@/pages/workspace/objects/ObjectPage";

import { renderBody, stepOf, toolPart } from "../../../chat-ui/_steps";
import { SERVER_RECEIPT } from "./receiptFixture";

const EXECUTED_AT = String(SERVER_RECEIPT.executed_at);
const DURATION_MS = Number(SERVER_RECEIPT.duration_ms);

/** A result big enough that grouping is visible — the demo's own share-of-voice
 *  reads are seven figures, and `1234567` on a trust surface is unreadable. */
const BIG_ROW_COUNT = 1_234_567;

const OBJECT = {
  id: "o1",
  logical_id: "l1",
  namespace: "org",
  type: "result" as const,
  title: "Daily orders",
  version: 1,
  status: "ready" as const,
  spec: { receipt: { ...SERVER_RECEIPT, row_count: BIG_ROW_COUNT } },
  visibility_scope: "org",
  created_at: "2026-09-06T12:00:00Z",
  updated_at: "2026-09-06T12:00:00Z",
  content_updated_at: "2026-09-06T12:00:00Z",
};

const ROWS = { columns: ["day", "orders"], rows: [["2026-09-05", 12]], total: BIG_ROW_COUNT };

function script(object: unknown = OBJECT) {
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

/** The object page's receipt panel, over the server's own receipt. */
async function receiptPanel(object: unknown = OBJECT): Promise<HTMLElement> {
  script(object);
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

/** The transcript's own card for the call that produced that receipt. */
function toolCard(): HTMLElement {
  return renderBody(
    stepOf(
      toolPart("sql_query", {
        input: { sql: String(SERVER_RECEIPT.sql), connection: String(SERVER_RECEIPT.connection_name) },
        output: {
          columns: ["day", "orders"],
          preview_rows: [["2026-09-05", 12]],
          row_count: BIG_ROW_COUNT,
          provenance: {
            connection_name: SERVER_RECEIPT.connection_name,
            role: SERVER_RECEIPT.role,
            engine: SERVER_RECEIPT.engine,
            executed_at: EXECUTED_AT,
            duration_ms: DURATION_MS,
            row_count: BIG_ROW_COUNT,
          },
        },
      }),
    ),
  );
}

/** One receipt row, by the label the panel gives it. */
function row(panel: HTMLElement, label: string): HTMLElement {
  const term = within(panel).getByText(label);
  const found = term.closest("div");
  if (!found) throw new Error(`no receipt row around "${label}"`);
  return found;
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("the receipt reads its facts the way the transcript does", () => {
  it("dates the run in the reader's own clock, zone named — not as the wire's ISO string", async () => {
    const panel = await receiptPanel();
    const reading = formatTimestamp(EXECUTED_AT);

    expect(row(panel, "Executed").textContent).toContain(reading);
    expect(panel.textContent).not.toContain(EXECUTED_AT);
    // …and it is the SAME reading the card beside it shows.
    expect(toolCard().textContent).toContain(reading);
  });

  it("states the duration in the unit a person thinks in, on both surfaces", async () => {
    const panel = await receiptPanel();
    const reading = formatDuration(DURATION_MS);

    expect(row(panel, "Duration").textContent).toContain(reading);
    expect(toolCard().textContent).toContain(reading);
  });

  it("groups a seven-figure row count instead of printing the digits raw", async () => {
    const panel = await receiptPanel();

    expect(row(panel, "Rows").textContent).toContain(formatCount(BIG_ROW_COUNT));
    expect(row(panel, "Rows").textContent).not.toContain(String(BIG_ROW_COUNT));
    expect(toolCard().textContent).toContain(formatCount(BIG_ROW_COUNT));
  });

  it("groups the count under the table too, so the page never disagrees with itself", async () => {
    await receiptPanel();
    const caption = await screen.findByText(/of .* rows$/);
    expect(caption.textContent).toContain(formatCount(BIG_ROW_COUNT));
  });

  it("lists the bound parameters by name instead of asking the reader to parse JSON", async () => {
    const panel = await receiptPanel();
    const params = row(panel, "Parameters");

    expect(params.textContent).toContain("customer");
    expect(params.textContent).toContain("acme");
    expect(params.textContent).not.toContain("{");
    expect(params.textContent).not.toContain('"');
  });

  it("still says a fact the machine never sent is missing, rather than formatting a zero", async () => {
    const panel = await receiptPanel({
      ...OBJECT,
      spec: { receipt: { ...SERVER_RECEIPT, duration_ms: null, executed_at: "", params: {} } },
    });

    expect(row(panel, "Duration").textContent).toBe("Duration—");
    expect(row(panel, "Executed").textContent).toBe("Executed—");
    expect(row(panel, "Parameters").textContent).toBe("Parameters—");
  });
});
