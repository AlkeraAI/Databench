import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { PreviewFacts, PreviewProps } from "../types";
import { CsvPreview, csvRenderer, parseDelimited, parseDelimitedRows } from "./CsvPreview";
import { manualGate, placeScroller, WindowedHost } from "./windowedHost.testkit";

afterEach(cleanup);

function facts(over: Partial<PreviewFacts> = {}): PreviewFacts {
  return { mime: "text/csv", name: "rows.csv", size: 200, ...over };
}

function props(text: string, over: Partial<PreviewProps> = {}): PreviewProps {
  return {
    facts: facts(),
    content: { kind: "text", text },
    version: "etag-1",
    status: "ready",
    onDownload: () => {},
    ...over,
  };
}

/** The visible grid, row-major, headers excluded. */
function bodyRows(container: HTMLElement): string[][] {
  return [...container.querySelectorAll("tbody tr")].map((row) =>
    [...row.querySelectorAll("td:not(.alk-datatable__rownum)")].map((cell) => cell.textContent ?? ""),
  );
}

/** The column names, without the row-number gutter's blank heading. */
function headers(container: HTMLElement): string[] {
  return [...container.querySelectorAll("thead th:not(.alk-datatable__rownum)")].map(
    (cell) => cell.textContent ?? "",
  );
}

describe("the csv renderer's claim", () => {
  it.each([
    ["a spreadsheet", "text/csv", "rows.csv", true],
    ["a spreadsheet under the other mime spelling", "application/csv", "rows.csv", true],
    // `text/plain` is a family, not a type: the name picks the better reader in it.
    ["a spreadsheet the sniffer read as text", "text/plain", "rows.csv", true],
    ["a spreadsheet the sniffer could not place", "application/octet-stream", "rows.csv", true],
    ["a note", "text/plain", "notes.txt", false],
    ["a json document", "application/json", "rows.json", false],
  ])("%s", (_name, mime, name, claimed) => {
    expect(csvRenderer.match(facts({ mime, name }))).toBe(claimed);
  });

  it("asks the host for the decoded body", () => {
    expect(csvRenderer.needs(facts())).toBe("text");
  });
});

describe("reading delimited text", () => {
  it.each([
    ["a bare row", "a,b,c", [["a", "b", "c"]]],
    ["windows line endings", "a,b\r\nc,d", [["a", "b"], ["c", "d"]]],
    ["a quoted field holding the delimiter", 'a,"b,c"', [["a", "b,c"]]],
    ["a quoted field holding a newline", 'a,"b\nc"\nd,e', [["a", "b\nc"], ["d", "e"]]],
    ["a doubled quote inside a quoted field", 'a,"say ""hi"""', [["a", 'say "hi"']]],
    ["an empty field", "a,,c", [["a", "", "c"]]],
    ["a trailing newline", "a,b\n", [["a", "b"]]],
    ["a byte-order mark", "﻿a,b", [["a", "b"]]],
  ])("reads %s", (_name, text, rows) => {
    expect(parseDelimited(text)).toEqual(rows);
  });

  it("keeps a quoted field's leading and trailing spaces", () => {
    expect(parseDelimited('" a ",b')).toEqual([[" a ", "b"]]);
  });

  it.each([
    ["a text ending on a row terminator", "a,b\nc,d\n", true],
    ["a text ending mid-row", "a,b\nc,d", false],
    ["a text ending on a newline inside a quoted field", 'a,b\nc,"d\n', false],
    ["an empty text", "", true],
  ])("says whether %s ended between rows", (_name, text, closed) => {
    expect(parseDelimitedRows(text).closed).toBe(closed);
  });
});

describe("rendering a spreadsheet", () => {
  it("uses the first row as the header when it reads like one", () => {
    const { container } = render(<CsvPreview {...props("name,qty\r\nwidget,3\r\ncog,11")} />);

    expect(headers(container)).toEqual(["name", "qty"]);
    expect(bodyRows(container)).toEqual([
      ["widget", "3"],
      ["cog", "11"],
    ]);
  });

  it("numbers the columns when the first row is data, and keeps that row", () => {
    const { container } = render(<CsvPreview {...props("1,2\n3,4")} />);

    expect(headers(container)).toEqual(["Column 1", "Column 2"]);
    expect(bodyRows(container)).toEqual([
      ["1", "2"],
      ["3", "4"],
    ]);
  });

  it("keeps every row of a file that arrived whole, with no line under it", () => {
    const rows = Array.from({ length: 6000 }, (_, index) => `r${index},${index}`).join("\n");

    render(<CsvPreview {...props(`name,qty\n${rows}`)} />);

    // The grid pages the rows; nothing past a count is dropped.
    expect(screen.getByText("6,000 rows")).toBeInTheDocument();
    expect(screen.queryByTestId("preview-more")).toBeNull();
  });

  it("sorts by a column and hands the sort back to the host to keep", () => {
    const onViewState = vi.fn();
    const { container } = render(
      <CsvPreview {...props("name,qty\nwidget,3\ncog,11\nbolt,2", { onViewState })} />,
    );

    fireEvent.click(screen.getByRole("button", { name: /^qty/ }));

    expect(bodyRows(container).map((row) => row[1])).toEqual(["2", "3", "11"]);
    expect(onViewState).toHaveBeenCalledWith({ column: "qty", direction: "asc" });
  });

  it("turns the sort around on a second press", () => {
    const { container } = render(<CsvPreview {...props("name,qty\nwidget,3\ncog,11\nbolt,2")} />);
    const sort = screen.getByRole("button", { name: /^qty/ });

    fireEvent.click(sort);
    fireEvent.click(sort);

    expect(bodyRows(container).map((row) => row[1])).toEqual(["11", "3", "2"]);
  });

  it("returns to the file's own order on a third press", () => {
    const { container } = render(<CsvPreview {...props("name,qty\nwidget,3\ncog,11\nbolt,2")} />);
    const sort = screen.getByRole("button", { name: /^qty/ });

    fireEvent.click(sort);
    fireEvent.click(sort);
    fireEvent.click(sort);

    expect(bodyRows(container).map((row) => row[0])).toEqual(["widget", "cog", "bolt"]);
  });

  it("opens already sorted the way the host remembered", () => {
    const { container } = render(
      <CsvPreview
        {...props("name,qty\nwidget,3\ncog,11\nbolt,2", {
          viewState: { column: "qty", direction: "desc" },
        })}
      />,
    );

    expect(bodyRows(container).map((row) => row[1])).toEqual(["11", "3", "2"]);
  });

  it("ignores a remembered sort naming a column this file does not have", () => {
    const { container } = render(
      <CsvPreview
        {...props("name,qty\nwidget,3\ncog,11", { viewState: { column: "price", direction: "desc" } })}
      />,
    );

    expect(bodyRows(container).map((row) => row[0])).toEqual(["widget", "cog"]);
  });

  it("orders text by its letters, not by its numbers", () => {
    const { container } = render(<CsvPreview {...props("name,qty\nwidget,3\nbolt,2\ncog,11")} />);

    fireEvent.click(screen.getByRole("button", { name: /^name/ }));

    expect(bodyRows(container).map((row) => row[0])).toEqual(["bolt", "cog", "widget"]);
  });

  it("draws nothing when the host has no bytes yet", () => {
    const { container } = render(
      <CsvPreview {...props("", { content: { kind: "none" }, status: "loading" })} />,
    );

    expect(container.textContent).toBe("");
  });
});

describe("a spreadsheet that has not all arrived", () => {
  const TOTAL = 3 * 1024 * 1024;
  // The first window ends on a newline inside a quoted field — the edge falls
  // on a line end that is not a row end.
  const windows = [
    'name,note\nwidget,plain\ncog,"line one\n',
    'line two"\nbolt,plain\n',
    "nut,last\n",
  ];

  function host(gate = manualGate()) {
    const view = render(
      <WindowedHost
        windows={windows}
        total={TOTAL}
        gate={gate}
        draw={(content) => <CsvPreview {...props("", { content })} />}
      />,
    );
    return { ...view, gate };
  }

  it("never shows a row the window's edge cut through", () => {
    const { container } = host();

    expect(headers(container)).toEqual(["name", "note"]);
    expect(bodyRows(container)).toEqual([["widget", "plain"]]);
    expect(screen.getByRole("status")).toHaveTextContent("Showing 1 MB of 3.1 MB");
  });

  it("completes the cut row and keeps the header when the next window lands", async () => {
    const { container, gate } = host();

    fireEvent.click(screen.getByRole("button", { name: "Show more" }));
    expect(screen.getByRole("status")).toHaveTextContent("Loading more…");
    await act(async () => gate.open());

    expect(headers(container)).toEqual(["name", "note"]);
    expect(bodyRows(container)).toEqual([
      ["widget", "plain"],
      ["cog", "line one\nline two"],
      ["bolt", "plain"],
    ]);
  });

  it("asks once from a scroll near the end of the grid", async () => {
    const { container, gate } = host();
    const grid = container.querySelector(".alk-datatable__wrap")!;
    placeScroller(grid, { scrollHeight: 2000, clientHeight: 400, top: 1000 });

    fireEvent.scroll(grid);
    fireEvent.scroll(grid);
    await act(async () => gate.open());

    expect(bodyRows(container).map((row) => row[0])).toEqual(["widget", "cog", "bolt"]);
  });

  it("draws no line and keeps the last row once the file is whole", async () => {
    const { container, gate } = host();
    for (let window = 1; window < windows.length; window += 1) {
      fireEvent.click(screen.getByRole("button", { name: "Show more" }));
      await act(async () => gate.open());
    }

    expect(bodyRows(container).map((row) => row[0])).toEqual(["widget", "cog", "bolt", "nut"]);
    expect(screen.queryByTestId("preview-more")).toBeNull();
  });
});
