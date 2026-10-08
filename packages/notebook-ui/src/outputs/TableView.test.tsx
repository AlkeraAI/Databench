import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import type { FrameQuery, TablePageWire, TablePayload } from "../model/types";
import { buildFilterSql } from "./tableQuery";
import { registerDefaultOutputRenderers } from "./defaults";
import { OutputArea } from "./OutputView";
import { OutputRegistry } from "./registry";
import { TableView, UNREADABLE_PAGE, readTablePayload } from "./TableView";

const fields = [
  { name: "id", type: "int64" },
  { name: "region", type: "string" },
];

function rowsFrom(start: number, count: number): Record<string, unknown>[] {
  return Array.from({ length: count }, (_, i) => ({ id: start + i, region: (start + i) % 2 ? "north" : "south" }));
}

/** A table page as the kernel makes it: a list per row in column order. */
function wirePage(rows: Record<string, unknown>[], total: number, offset: number) {
  return { schema: fields, rows: rows.map((r) => fields.map((f) => r[f.name])), total_rows: total, offset };
}

/** A frame held in memory, answering queries the way the engine would. */
function fakeFrame(total: number) {
  const all = rowsFrom(0, total);
  const queries: FrameQuery[] = [];
  const inspectFrame = (query: FrameQuery): Promise<TablePageWire> => {
    queries.push(query);
    let rows = all;
    if (query.filter_sql?.includes("'%north%'")) rows = rows.filter((r) => r.region === "north");
    if (query.sort?.[0]) {
      const { column, descending } = query.sort[0];
      rows = [...rows].sort((a, b) => ((a[column] as number) - (b[column] as number)) * (descending ? -1 : 1));
    }
    return Promise.resolve(wirePage(rows.slice(query.offset, query.offset + query.limit), rows.length, query.offset));
  };
  return { inspectFrame, queries };
}

/** The pager's "Page n of N", with the typed number read from its input. */
function pageLabel(): string {
  const input = screen.getByRole("textbox", { name: "Page number" }) as HTMLInputElement;
  return (input.closest("label")?.textContent ?? "").replace(/\s+/g, " ").trim().replace("Page of", `Page ${input.value} of`);
}

function bodyIds(): number[] {
  return [...document.querySelectorAll("tbody tr[data-row]")].map((tr) => Number(tr.querySelector("td")?.textContent));
}

describe("TableView with a registered frame", () => {
  const payload: TablePayload = { name: "df", schema: { fields }, data: rowsFrom(0, 50), total_rows: 1234 };

  it("shows the payload's rows and the frame's total without asking", () => {
    const { inspectFrame, queries } = fakeFrame(1234);
    render(<TableView payload={payload} inspectFrame={inspectFrame} />);
    expect(screen.getByText("1,234 rows")).toBeInTheDocument();
    expect(bodyIds().slice(0, 3)).toEqual([0, 1, 2]);
    expect(pageLabel()).toBe("Page 1 of 25");
    expect(queries).toEqual([]);
  });

  it("asks for the first page when the payload carries the schema alone", async () => {
    const { inspectFrame, queries } = fakeFrame(300);
    render(<TableView payload={{ name: "df", schema: { fields }, data: [], total_rows: 300 }} inspectFrame={inspectFrame} />);
    await waitFor(() => expect(bodyIds()[0]).toBe(0));
    expect(queries).toEqual([{ name: "df", offset: 0, limit: 100 }]);
    expect(pageLabel()).toBe("Page 1 of 3");
  });

  it("pages through inspectFrame with the payload's page size", async () => {
    const user = userEvent.setup();
    const { inspectFrame, queries } = fakeFrame(1234);
    render(<TableView payload={payload} inspectFrame={inspectFrame} />);
    await user.click(screen.getByRole("button", { name: "Next" }));
    await waitFor(() => expect(bodyIds()[0]).toBe(50));
    expect(queries).toEqual([{ name: "df", offset: 50, limit: 50 }]);
    expect(pageLabel()).toBe("Page 2 of 25");
    expect(document.querySelector("tbody tr[data-row]")).toHaveAttribute("data-row", "50");
    await user.click(screen.getByRole("button", { name: "Previous" }));
    // Back on the first page with no sort or filter: the payload again.
    await waitFor(() => expect(bodyIds()[0]).toBe(0));
    expect(queries).toHaveLength(1);
  });

  it("jumps to a typed page number, clamped to the last page", async () => {
    const user = userEvent.setup();
    const { inspectFrame, queries } = fakeFrame(1234);
    render(<TableView payload={payload} inspectFrame={inspectFrame} />);
    const input = screen.getByRole("textbox", { name: "Page number" });
    await user.clear(input);
    await user.type(input, "7{Enter}");
    await waitFor(() => expect(bodyIds()[0]).toBe(300));
    expect(queries).toEqual([{ name: "df", offset: 300, limit: 50 }]);
    expect(pageLabel()).toBe("Page 7 of 25");
    await user.clear(input);
    await user.type(input, "999{Enter}");
    await waitFor(() => expect(bodyIds()[0]).toBe(1200));
    expect(pageLabel()).toBe("Page 25 of 25");
  });

  it("puts the page back when the typed text is not a number", async () => {
    const user = userEvent.setup();
    const { inspectFrame, queries } = fakeFrame(1234);
    render(<TableView payload={payload} inspectFrame={inspectFrame} />);
    const input = screen.getByRole("textbox", { name: "Page number" });
    await user.clear(input);
    await user.type(input, "abc{Enter}");
    expect(pageLabel()).toBe("Page 1 of 25");
    expect(queries).toEqual([]);
  });

  it("sorts by a header click: ascending, descending, then off", async () => {
    const user = userEvent.setup();
    const { inspectFrame, queries } = fakeFrame(1234);
    render(<TableView payload={payload} inspectFrame={inspectFrame} />);
    const header = screen.getByRole("columnheader", { name: /id/ });
    await user.click(within(header).getByRole("button"));
    await waitFor(() => expect(queries).toHaveLength(1));
    expect(queries[0]).toEqual({ name: "df", offset: 0, limit: 50, sort: [{ column: "id", descending: false }] });
    expect(header).toHaveAttribute("aria-sort", "ascending");

    await user.click(within(header).getByRole("button"));
    await waitFor(() => expect(bodyIds()[0]).toBe(1233));
    expect(queries[1].sort).toEqual([{ column: "id", descending: true }]);
    expect(header).toHaveAttribute("aria-sort", "descending");

    await user.click(within(header).getByRole("button"));
    expect(header).toHaveAttribute("aria-sort", "none");
    await waitFor(() => expect(bodyIds()[0]).toBe(0));
  });

  it("filters through filter_sql and returns to the first page", async () => {
    const user = userEvent.setup();
    const { inspectFrame, queries } = fakeFrame(1234);
    render(<TableView payload={payload} inspectFrame={inspectFrame} />);
    await user.click(screen.getByRole("button", { name: "Next" }));
    await waitFor(() => expect(queries).toHaveLength(1));
    await user.type(screen.getByRole("searchbox", { name: "Filter rows" }), "north");
    await waitFor(() => expect(screen.getByText("617 rows")).toBeInTheDocument());
    const last = queries[queries.length - 1];
    expect(last).toEqual({ name: "df", offset: 0, limit: 50, filter_sql: buildFilterSql("north", ["id", "region"]) });
    expect(bodyIds().slice(0, 2)).toEqual([1, 3]);
  });

  it("narrows the filter to one column", async () => {
    const user = userEvent.setup();
    const { inspectFrame, queries } = fakeFrame(1234);
    render(<TableView payload={payload} inspectFrame={inspectFrame} />);
    await user.selectOptions(screen.getByRole("combobox", { name: "Filter column" }), "region");
    await user.type(screen.getByRole("searchbox", { name: "Filter rows" }), "north");
    await waitFor(() => expect(queries.at(-1)?.filter_sql).toBe(buildFilterSql("north", ["id", "region"], "region")));
  });

  it("ignores a slower, older answer that arrives after a newer one", async () => {
    const user = userEvent.setup();
    const pending: ((page: TablePageWire) => void)[] = [];
    const inspectFrame = () => new Promise<TablePageWire>((resolve) => pending.push(resolve));
    render(<TableView payload={payload} inspectFrame={inspectFrame} />);
    await user.click(screen.getByRole("button", { name: "Next" }));
    // Disabled while loading; sort instead, which issues a second query.
    await user.click(within(screen.getByRole("columnheader", { name: /id/ })).getByRole("button"));
    expect(pending).toHaveLength(2);
    await act(async () => pending[1](wirePage([{ id: 999, region: "x" }], 1, 0)));
    await act(async () => pending[0](wirePage([{ id: 111, region: "x" }], 1234, 50)));
    expect(bodyIds()).toEqual([999]);
  });

  it("says when the frame could not be read, keeping the table", async () => {
    const user = userEvent.setup();
    const inspectFrame = () => Promise.reject(new Error("frame df is gone"));
    render(<TableView payload={payload} inspectFrame={inspectFrame} />);
    await user.click(screen.getByRole("button", { name: "Next" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Frame df is gone");
    expect(screen.getByRole("table")).toBeInTheDocument();
  });

  it("starts the server's refusal with a capital, keeping the rest as written", async () => {
    const user = userEvent.setup();
    const inspectFrame = () => Promise.reject(new Error("filtering a table needs duckdb in this environment"));
    render(<TableView payload={payload} inspectFrame={inspectFrame} />);
    await user.click(screen.getByRole("button", { name: "Next" }));
    expect((await screen.findByRole("alert")).textContent).toBe("Filtering a table needs duckdb in this environment");
  });

  it("says when a page arrives in a shape it can't read, keeping the table", async () => {
    const user = userEvent.setup();
    // What a server from before pages had one shape answered.
    const inspectFrame = () => Promise.resolve({ rows: { columns: fields, rows: [[51, "north"]] }, total_rows: 1234 });
    render(<TableView payload={payload} inspectFrame={inspectFrame} />);
    await user.click(screen.getByRole("button", { name: "Next" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(UNREADABLE_PAGE);
    expect(screen.getByRole("table")).toBeInTheDocument();
  });

  it("draws a later page's dates and decimals as it draws the first page's", async () => {
    const user = userEvent.setup();
    const typed = [
      { name: "day", type: "Date" },
      { name: "amount", type: "Decimal(precision=4, scale=2)" },
      { name: "ratio", type: "Float64" },
      { name: "raw", type: "Binary" },
    ];
    // The kernel's page, as an output carries its first one and as the table
    // route answers a later one: the same keys, the same encoded cells.
    const pageAt = (offset: number) => ({
      schema: typed,
      rows: [[`2026-01-0${offset + 1}`, "12.50", "NaN", "0x00ff"]],
      total_rows: 2,
      offset,
    });
    const first = readTablePayload({ ...pageAt(0), source: { name: "df" } });
    expect(first).toEqual({
      name: "df",
      schema: { fields: typed },
      data: [{ day: "2026-01-01", amount: "12.50", ratio: "NaN", raw: "0x00ff" }],
      total_rows: 2,
    });
    const queries: FrameQuery[] = [];
    const inspectFrame = (query: FrameQuery) => {
      queries.push(query);
      return Promise.resolve(pageAt(query.offset));
    };
    render(<TableView payload={first!} inspectFrame={inspectFrame} />);
    const cells = () => [...document.querySelectorAll("tbody tr[data-row] td")].map((td) => td.textContent);
    expect(cells()).toEqual(["2026-01-01", "12.50", "NaN", "0x00ff"]);
    await user.click(screen.getByRole("button", { name: "Next" }));
    await waitFor(() => expect(cells()).toEqual(["2026-01-02", "12.50", "NaN", "0x00ff"]));
    expect(queries).toEqual([{ name: "df", offset: 1, limit: 1 }]);
  });

  it("virtualizes: draws a window of rows, not all of them", () => {
    const big: TablePayload = { name: "df", schema: { fields }, data: rowsFrom(0, 500), total_rows: 100_000 };
    render(<TableView payload={big} inspectFrame={fakeFrame(10).inspectFrame} />);
    const drawn = bodyIds().length;
    expect(drawn).toBeGreaterThan(0);
    expect(drawn).toBeLessThan(60);
  });
});

describe("TableView without a frame to ask", () => {
  const payload: TablePayload = {
    schema: { fields },
    data: [
      { id: 3, region: "north" },
      { id: 1, region: "south" },
      { id: 2, region: "North-east" },
    ],
    total_rows: 3,
  };

  it("sorts and filters the rows it has", async () => {
    const user = userEvent.setup();
    render(<TableView payload={payload} />);
    expect(bodyIds()).toEqual([3, 1, 2]);
    await user.click(within(screen.getByRole("columnheader", { name: /id/ })).getByRole("button"));
    expect(bodyIds()).toEqual([1, 2, 3]);
    await user.type(screen.getByRole("searchbox", { name: "Filter rows" }), "north");
    await waitFor(() => expect(bodyIds()).toEqual([2, 3]));
    expect(screen.getByText("2 of 3 rows")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Next" })).toBeNull();
  });

  it("works locally when a name is given but the host has no frame reader", () => {
    render(<TableView payload={{ ...payload, name: "df", total_rows: 900 }} />);
    expect(screen.getByText("3 of 900 rows shown")).toBeInTheDocument();
  });

  it("draws null cells distinctly from the text null", () => {
    render(<TableView payload={{ schema: { fields }, data: [{ id: null, region: "null" }], total_rows: 1 }} />);
    const cells = [...document.querySelectorAll("tbody td")];
    expect(cells[0]).toHaveClass("nb-table-null");
    expect(cells[1]).not.toHaveClass("nb-table-null");
  });

  it("copies what it shows as CSV", async () => {
    const user = userEvent.setup();
    const written: string[] = [];
    render(<TableView payload={payload} writeClipboard={async (text) => void written.push(text)} />);
    await user.click(within(screen.getByRole("columnheader", { name: /id/ })).getByRole("button"));
    await user.click(screen.getByRole("button", { name: "Copy as CSV" }));
    expect(written).toEqual(["id,region\r\n1,south\r\n2,North-east\r\n3,north\r\n"]);
    expect(screen.getByRole("button", { name: "Copied" })).toBeInTheDocument();
  });
});

describe("the kernel's table payload", () => {
  // As `_alkera_kernel.display` sends a polars or pandas frame.
  const kernelPayload = {
    rows: [
      ["east", 12],
      ["west", null],
    ],
    schema: [
      { name: "region", type: "str" },
      { name: "units", type: "i64" },
    ],
    total_rows: 3,
  };

  it("reads rows in column order into rows by column, the source name, and the total", () => {
    expect(readTablePayload({ ...kernelPayload, source: { name: "df" } })).toEqual({
      name: "df",
      schema: { fields: kernelPayload.schema },
      data: [
        { region: "east", units: 12 },
        { region: "west", units: null },
      ],
      total_rows: 3,
    });
  });

  it("has no source to page through when the frame was not bound to a name", () => {
    expect(readTablePayload(kernelPayload)?.name).toBeUndefined();
  });

  it("still reads a Table Schema payload", () => {
    expect(readTablePayload({ name: "df", schema: { fields }, data: rowsFrom(0, 2), total_rows: 9 })).toEqual({
      name: "df",
      schema: { fields },
      data: rowsFrom(0, 2),
      total_rows: 9,
    });
  });

  it.each([
    ["nothing", null],
    ["rows without a schema", { rows: [[1]] }],
    ["a schema without rows", { schema: [{ name: "a", type: "i64" }] }],
    ["a column with no name", { rows: [], schema: [{ type: "i64" }] }],
  ])("reads %s as no table", (_what, value) => {
    expect(readTablePayload(value)).toBeNull();
  });

  it("draws in a cell and asks for pages by the cell it belongs to", async () => {
    const user = userEvent.setup();
    const registry = new OutputRegistry();
    registerDefaultOutputRenderers(registry);
    const queries: FrameQuery[] = [];
    const inspectFrame = (query: FrameQuery): Promise<TablePageWire> => {
      queries.push(query);
      return Promise.resolve({ ...kernelPayload, rows: [["north", 30]], total_rows: 3, offset: 2 });
    };
    render(
      <OutputArea
        outputs={[
          {
            output_id: "o1",
            type: "display",
            data: {
              "application/vnd.alkera.table+json": { ...kernelPayload, source: { name: "df" } },
              "text/plain": "shape: (3, 2)",
            },
          },
        ]}
        context={{ theme: "light", readonly: false, cellId: "qqcg5gcgta", inspectFrame }}
        registry={registry}
      />,
    );
    expect(screen.queryByText("This table could not be read.")).toBeNull();
    expect(screen.getByText("east")).toBeInTheDocument();
    expect(pageLabel()).toBe("Page 1 of 2");
    await user.click(screen.getByRole("button", { name: "Next" }));
    await waitFor(() => expect(screen.getByText("north")).toBeInTheDocument());
    expect(queries).toEqual([{ name: "df", cell_id: "qqcg5gcgta", offset: 2, limit: 2 }]);
  });
});

describe("a column header", () => {
  it("shows a short type under the name, and the full type on hover", () => {
    const payload: TablePayload = {
      name: "df",
      schema: { fields: [{ name: "ok_pct", type: "Decimal(precision=4, scale=2)" }] },
      data: [{ ok_pct: 96.23 }],
      total_rows: 1,
    };
    render(<TableView payload={payload} />);
    const header = screen.getByRole("columnheader", { name: /ok_pct/ });
    const type = within(header).getByText("Decimal(4, 2)");
    expect(type.getAttribute("title")).toBe("Decimal(precision=4, scale=2)");
  });
});
