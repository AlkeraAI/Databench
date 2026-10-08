// A saved object's page: its receipt, its export, and its chart.
//
// The receipt cases are the point of the page. A number without its source,
// role and timestamp is a number nobody can act on, so every one of those
// fields is asserted as VISIBLE — not present in the DOM behind a disclosure.

import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { formatTimestamp } from "@alkera/ui";

import { createQueryClient } from "@/api/queryClient";
import { ObjectPage } from "@/pages/workspace/objects/ObjectPage";

import { SERVER_RECEIPT } from "./receiptFixture";

// The receipt is the SERVER's, dumped from `alkera_core.schemas.objects.Receipt`
// by its own generator. A hand-written one is how the page came to read
// `connection` and `principal` — names the receipt has never had — with a test
// that agreed with the page instead of with the writer.
const RECEIPT = SERVER_RECEIPT;

const OBJECT = {
  id: "o1",
  logical_id: "l1",
  namespace: "org",
  type: "result" as const,
  title: "Daily orders",
  version: 1,
  status: "ready" as const,
  spec: {
    receipt: RECEIPT,
    chart_spec: { mark: "line", encoding: { x: { field: "day" }, y: { field: "orders" } } },
  },
  visibility_scope: "org",
  created_at: "2026-09-06T12:00:00Z",
  updated_at: "2026-09-06T12:00:00Z",
  content_updated_at: "2026-09-06T12:00:00Z",
};

// The rows page carries the display labels AND the stable keys behind them, in
// the same order — the server sends both because a chart's encoding names a key.
const ROWS = {
  columns: ["day", "orders"],
  keys: ["day", "orders"],
  rows: [
    ["2026-09-05", 12],
    ["2026-09-06", 17],
  ],
  total: 2,
};

// The same result after someone renamed its columns for the reader: the labels
// on the table have moved, the keys the chart names have not.
const RENAMED_ROWS = { ...ROWS, columns: ["Day", "Orders (net)"] };

function script(object: unknown = OBJECT, rows: unknown = ROWS) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    const body = url.includes("/rows") ? rows : object;
    return new Response(JSON.stringify(body), {
      status: 200,
      headers: { "content-type": "application/json" },
    });
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function renderObject() {
  render(
    <QueryClientProvider client={createQueryClient({ retry: false })}>
      <MemoryRouter initialEntries={["/objects/o1"]}>
        <Routes>
          <Route path="/objects/:objectId" element={<ObjectPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("a saved result's receipt", () => {
  it.each([
    ["the statement", RECEIPT.sql],
    ["the connection", RECEIPT.connection_name],
    ["the engine", RECEIPT.engine],
    ["the role it ran under", RECEIPT.role],
    ["who ran it", "ops@example.com via sess-01J7Q3M8"],
    // The instant as the reader's own clock renders it — the same reading the
    // transcript's card gives it, which is the whole point of a receipt.
    ["when it ran", formatTimestamp(String(RECEIPT.executed_at))],
    ["how many rows", "3"],
    ["how long it took", "412 ms"],
  ])("shows %s without a click", async (_case, value) => {
    script();
    renderObject();
    const receipt = await screen.findByLabelText("Receipt");
    expect(receipt.textContent).toContain(String(value));
  });

  it("shows the parameters it was run with", async () => {
    script();
    renderObject();
    const receipt = await screen.findByLabelText("Receipt");
    expect(receipt.textContent).toContain("acme");
  });

  it("says a field is missing rather than showing an empty space", async () => {
    script({ ...OBJECT, spec: { receipt: {} } });
    renderObject();
    const receipt = await screen.findByLabelText("Receipt");
    expect(receipt.textContent).toContain("—");
  });
});

describe("a saved result's export", () => {
  it("offers CSV as a link to the export route, with no credential in it", async () => {
    script();
    renderObject();
    const link = await screen.findByRole("link", { name: "Export CSV" });
    const href = link.getAttribute("href") ?? "";
    expect(href).toContain("/api/v1/objects/o1/export.csv");
    expect(href).not.toMatch(/token|ticket|key=/i);
  });
});

describe("a saved result's chart", () => {
  /** The line Vega drew: one M/L command per point. */
  const pointsIn = (figure: HTMLElement): number =>
    (figure.querySelector(".mark-line path")?.getAttribute("d")?.match(/[ML]/g) ?? []).length;

  it("draws the series the persisted spec names", async () => {
    script();
    renderObject();
    const figure = await screen.findByRole("figure", { name: "orders over day" });
    expect(screen.getByText("orders over day")).toBeVisible();
    await waitFor(() => expect(pointsIn(figure)).toBe(ROWS.rows.length));
  });

  it("draws the spec exactly as the server persists it — schema, typed channels and a series split", async () => {
    // What `validate_chart_spec(...).persisted()` writes for a chart with a
    // colour channel: one line per engine. If the writer and the reader drift
    // apart, this goes red rather than a saved result quietly losing its chart.
    script(
      {
        ...OBJECT,
        spec: {
          receipt: RECEIPT,
          chart_spec: {
            $schema: "https://vega.github.io/schema/vega-lite/v5.json",
            mark: "line",
            encoding: {
              x: { field: "day", type: "temporal" },
              y: { field: "orders", type: "quantitative" },
              color: { field: "engine", type: "nominal" },
            },
          },
        },
      },
      {
        columns: ["day", "orders", "engine"],
        keys: ["day", "orders", "engine"],
        rows: [
          ["2026-09-05", 12, "duckdb"],
          ["2026-09-06", 17, "duckdb"],
          ["2026-09-05", 4, "postgres"],
          ["2026-09-06", 9, "postgres"],
        ],
        total: 4,
      },
    );
    renderObject();
    const figure = await screen.findByRole("figure", { name: "orders over day" });
    await waitFor(() => expect(figure.querySelectorAll(".mark-line path")).toHaveLength(2));
  });

  it("still draws after the columns were renamed — the encoding names keys", async () => {
    // The chart is bound to `day` / `orders`; the table now shows "Day" and
    // "Orders (net)". A renderer that looked the label up would find neither
    // and draw an empty figure on a result whose data never changed.
    script(OBJECT, RENAMED_ROWS);
    renderObject();
    const figure = await screen.findByRole("figure", { name: "orders over day" });
    await waitFor(() => expect(pointsIn(figure)).toBe(RENAMED_ROWS.rows.length));
    expect(await screen.findByRole("columnheader", { name: "Orders (net)" })).toBeVisible();
  });

  it("draws nothing when the object carries no chart spec", async () => {
    script({ ...OBJECT, spec: { receipt: RECEIPT } });
    renderObject();
    await screen.findByLabelText("Receipt");
    expect(screen.queryByRole("figure")).toBeNull();
  });

  it("blocks a spec that could name a location, and never fetches it", async () => {
    const fetchMock = script({
      ...OBJECT,
      spec: {
        receipt: RECEIPT,
        chart_spec: {
          mark: "line",
          data: { url: "https://example.test/steal" },
          encoding: { x: { field: "day" }, y: { field: "orders" } },
        },
      },
    });
    renderObject();
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "This chart is blocked because it tries to load or run something.",
    );
    expect(document.querySelector(".alk-chart svg")).toBeNull();
    expect(fetchMock.mock.calls.map(([input]) => String(input))).not.toContainEqual(
      expect.stringContaining("example.test"),
    );
  });
});

// A 90-day series is 90 points. The rows route pages, and the table draws one
// page — right for a table — but a chart drawn from the first page is a
// different, shorter chart with nothing on screen to say so: the line simply
// ends five weeks early and reads as the truth.
describe("a chart over a result longer than one page", () => {
  const LONG_TOTAL = 90;
  const SERVER_PAGE = 50;
  const LONG_ROWS = Array.from({ length: LONG_TOTAL }, (_, i) => [
    `2026-06-${String((i % 28) + 1).padStart(2, "0")}`,
    i + 1,
  ]);

  /** A rows route that pages: it never serves more than `SERVER_PAGE` at once,
   *  whatever it is asked for — which is a server's right, and what forces the
   *  reader to page rather than to ask once and hope. */
  function scriptPaged() {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = new URL(String(input), "http://test.local");
      if (!url.pathname.includes("/rows")) {
        return new Response(JSON.stringify(OBJECT), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      }
      const offset = Number(url.searchParams.get("offset") ?? 0);
      const limit = Math.min(Number(url.searchParams.get("limit") ?? 50), SERVER_PAGE);
      const body = {
        columns: ["day", "orders"],
        keys: ["day", "orders"],
        rows: LONG_ROWS.slice(offset, offset + limit),
        total: LONG_TOTAL,
      };
      return new Response(JSON.stringify(body), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    });
    vi.stubGlobal("fetch", fetchMock);
    return fetchMock;
  }

  it("draws every row, not the first page of them", async () => {
    scriptPaged();
    renderObject();
    const figure = await screen.findByRole("figure", { name: "orders over day" });
    await waitFor(() => {
      const path = figure.querySelector(".mark-line path");
      expect(path?.getAttribute("d")?.match(/[ML]/g)?.length ?? 0).toBe(LONG_TOTAL);
    });
  });

  it("leaves the table paged — a table says how much of it you are seeing", async () => {
    scriptPaged();
    renderObject();
    const table = await screen.findByRole("table");
    await waitFor(() => {
      expect(table.querySelectorAll("tbody tr")).toHaveLength(SERVER_PAGE);
    });
    expect(screen.getByText(`${SERVER_PAGE} of ${LONG_TOTAL} rows`)).toBeInTheDocument();
  });

  it("asks for the rest rather than assuming one page was all of it", async () => {
    const fetchMock = scriptPaged();
    renderObject();
    await screen.findByRole("figure", { name: "orders over day" });
    await waitFor(() => {
      const offsets = fetchMock.mock.calls
        .map(([input]) => new URL(String(input), "http://test.local"))
        .filter((url) => url.pathname.includes("/rows"))
        .map((url) => Number(url.searchParams.get("offset") ?? 0));
      expect(offsets).toContain(SERVER_PAGE);
    });
  });
});

describe("a saved result's heading", () => {
  it("names the kind the way the list does, not the way the wire spells it", async () => {
    script(OBJECT, { columns: [], keys: [], rows: [], total: 0 });
    renderObject();
    expect(await screen.findByText("Result")).toBeInTheDocument();
    expect(screen.queryByText("result")).toBeNull();
  });

  it("says a result that is neither ready nor failed is not ready, without the enum", async () => {
    script({ ...OBJECT, status: "promoting" }, { columns: [], keys: [], rows: [], total: 0 });
    renderObject();
    const line = await screen.findByRole("status");
    expect(line).toHaveTextContent("Not ready");
    expect(line.textContent).not.toMatch(/promoting|Status:/);
  });
});

describe("a result still uploading", () => {
  it("says so rather than showing an empty table", async () => {
    script(
      { ...OBJECT, status: "pending_upload" },
      { columns: [], keys: [], rows: [], total: 0 },
    );
    renderObject();
    expect(await screen.findByRole("status")).toHaveTextContent(/uploading/i);
  });

  it("does not ask for rows that cannot exist yet", async () => {
    // The rows route answers 409 `payload_pending` until the machine delivers,
    // which was four console errors per visit for a result that never arrived.
    const fetchMock = script(
      { ...OBJECT, status: "pending_upload" },
      { columns: [], keys: [], rows: [], total: 0 },
    );
    renderObject();
    await screen.findByRole("status");
    const asked = fetchMock.mock.calls.map(([input]) => String(input));
    expect(asked.some((url) => url.includes("/rows"))).toBe(false);
  });

  it("offers no CSV key for a file that does not exist", async () => {
    script(
      { ...OBJECT, status: "pending_upload" },
      { columns: [], keys: [], rows: [], total: 0 },
    );
    renderObject();
    await screen.findByRole("status");
    expect(screen.queryByRole("link", { name: /export csv/i })).toBeNull();
  });
});

// A promote the machine cannot honour must not leave the object saying
// "Saving…" for ever while the reason goes into a chat the reader has left.
describe("a result the machine could not save", () => {
  it("says it failed, and says why, where the reader is looking", async () => {
    script(
      {
        ...OBJECT,
        status: "failed",
        spec: {
          ...OBJECT.spec,
          failure_reason: "no tool result with event id prt_07ad57d1c0016yL2P6MozuMrVf",
        },
      },
      { columns: [], keys: [], rows: [], total: 0 },
    );
    renderObject();

    const status = await screen.findByRole("status");
    expect(status).toHaveTextContent(/could not be saved/i);
    expect(status).toHaveTextContent("no tool result with event id prt_07ad57d1c0016yL2P6MozuMrVf");
    expect(status).not.toHaveTextContent(/uploading/i);
  });

  it("still says it failed when the machine gave no reason", async () => {
    script({ ...OBJECT, status: "failed" }, { columns: [], keys: [], rows: [], total: 0 });
    renderObject();
    expect(await screen.findByRole("status")).toHaveTextContent(/could not be saved/i);
  });
});
