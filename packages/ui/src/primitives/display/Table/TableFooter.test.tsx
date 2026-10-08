import type { ReactNode } from "react";

import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  Table,
  rowCountLabel,
  rowRangeLabel,
  type TableServerPagination,
} from "./Table";
import { TableFooter } from "./TablePager";
import { TABLE_PAGER_LABELS } from "./TablePager";

// An un-paged table names its total row count; a paged one (client- or server-side) names WHICH rows
// are in view. Every label comes from the shared formatters, so no format string is copied here.

afterEach(cleanup);

const HEADER_ROWS = 1; // <thead> contributes one <tr> to the row count.

/** A client-side / un-paged table with `n` rows. */
function tableOf(
  n: number,
  props?: { footerStart?: ReactNode; pageSize?: number },
) {
  return (
    <Table
      columns={["A"]}
      footerStart={props?.footerStart}
      pageSize={props?.pageSize}
    >
      {Array.from({ length: n }, (_, i) => (
        <tr key={i}>
          <td>row {i}</td>
        </tr>
      ))}
    </Table>
  );
}

/** A server-paginated table: `rowsOnPage` is only the CURRENT page's rows (the caller fetched them). */
function serverTable(rowsOnPage: number, pagination: TableServerPagination) {
  return (
    <Table columns={["A"]} pagination={pagination}>
      {Array.from({ length: rowsOnPage }, (_, i) => (
        <tr key={i}>
          <td>row {i}</td>
        </tr>
      ))}
    </Table>
  );
}

const foot = () => document.querySelector(".alk-table__foot");
const footStart = () => document.querySelector(".alk-table__footstart");
const prevBtn = () =>
  screen.getByRole("button", { name: TABLE_PAGER_LABELS.previous });
const nextBtn = () =>
  screen.getByRole("button", { name: TABLE_PAGER_LABELS.next });
const queryPrevBtn = () =>
  screen.queryByRole("button", { name: TABLE_PAGER_LABELS.previous });
const queryNextBtn = () =>
  screen.queryByRole("button", { name: TABLE_PAGER_LABELS.next });
const pageInput = () => screen.getByLabelText(TABLE_PAGER_LABELS.pageInput);

describe("TableFooter primitive", () => {
  it("keeps a start reading without inventing single-page navigation", () => {
    render(
      <TableFooter
        start="1–5 of 5"
        pager={{ page: 1, pageCount: 1, onPage: vi.fn() }}
      />,
    );
    expect(foot()).toHaveTextContent("1–5 of 5");
    expect(queryPrevBtn()).toBeNull();
  });

  it("places complete multi-page navigation after the start reading", () => {
    render(
      <TableFooter
        start="6–10 of 12"
        pager={{ page: 2, pageCount: 3, onPage: vi.fn() }}
      />,
    );
    expect(foot()?.firstElementChild).toHaveTextContent("6–10 of 12");
    expect(nextBtn()).toBeInTheDocument();
  });

  it("renders nothing without a reading or a usable navigator", () => {
    const { container } = render(
      <TableFooter pager={{ page: 1, pageCount: 1, onPage: vi.fn() }} />,
    );
    expect(container).toBeEmptyDOMElement();
  });
});

describe("Table footer — un-paged", () => {
  it.each([0, 1, 3])("states the total row count for %i rows", (rows) => {
    render(tableOf(rows));
    expect(screen.getByText(rowCountLabel(rows))).toBeInTheDocument();
  });

  it("a footerStart node overrides the default label", () => {
    const ROWS = 3;
    const OVERRIDE = "selection summary goes here"; // a test-only literal
    render(tableOf(ROWS, { footerStart: OVERRIDE }));
    expect(screen.getByText(OVERRIDE)).toBeInTheDocument();
    expect(screen.queryByText(rowCountLabel(ROWS))).toBeNull();
  });

  // With nothing to say and nothing to page, the whole footer goes; a pager keeps it alive.
  it.each([
    { label: "un-paged", pageSize: undefined, keepsFoot: false },
    { label: "paged", pageSize: 10, keepsFoot: true },
  ])(
    "footerStart={null} drops the label on a $label table",
    ({ pageSize, keepsFoot }) => {
      render(tableOf(30, { footerStart: null, pageSize }));
      expect(footStart()).toBeNull();
      if (keepsFoot) {
        expect(foot()).not.toBeNull();
        expect(nextBtn()).toBeInTheDocument();
      } else {
        expect(foot()).toBeNull();
      }
    },
  );
});

describe("Table footer — client-side pagination", () => {
  it("advances the range as you page, ending on the remainder", async () => {
    const user = userEvent.setup();
    const TOTAL = 25;
    const PER_PAGE = 10; // the last page holds TOTAL - 2*PER_PAGE = 5 rows
    render(tableOf(TOTAL, { pageSize: PER_PAGE }));
    expect(
      screen.getByText(rowRangeLabel(1, PER_PAGE, TOTAL)),
    ).toBeInTheDocument();

    await user.click(nextBtn());
    expect(
      screen.getByText(rowRangeLabel(PER_PAGE + 1, 2 * PER_PAGE, TOTAL)),
    ).toBeInTheDocument();

    await user.click(nextBtn());
    expect(
      screen.getByText(rowRangeLabel(2 * PER_PAGE + 1, TOTAL, TOTAL)),
    ).toBeInTheDocument();
  });
});

describe("Table footer — server-side pagination", () => {
  const noop = () => {};

  it("renders every provided row without slicing the children", () => {
    const ROWS_ON_PAGE = 5;
    render(
      serverTable(ROWS_ON_PAGE, {
        page: 3,
        pageSize: 10,
        total: 97,
        onPageChange: noop,
      }),
    );
    expect(screen.getAllByRole("row")).toHaveLength(ROWS_ON_PAGE + HEADER_ROWS);
  });

  it("ends the last short page at total, not at a full page", () => {
    // The caller fetches a partial last page. The range must end at `total` (...-97 of 97), never at
    // start + pageSize (...-100).
    const PER_PAGE = 10;
    const TOTAL = 97;
    const LAST = Math.ceil(TOTAL / PER_PAGE);
    const ROWS_ON_LAST = TOTAL - (LAST - 1) * PER_PAGE; // 7, fewer than PER_PAGE
    render(
      serverTable(ROWS_ON_LAST, {
        page: LAST,
        pageSize: PER_PAGE,
        total: TOTAL,
        onPageChange: noop,
      }),
    );
    const start = (LAST - 1) * PER_PAGE + 1;
    expect(
      screen.getByText(rowRangeLabel(start, TOTAL, TOTAL)),
    ).toBeInTheDocument();
    expect(
      screen.queryByText(rowRangeLabel(start, LAST * PER_PAGE, TOTAL)),
    ).toBeNull();
    expect(
      screen.queryByText(rowRangeLabel(start, start + ROWS_ON_LAST - 1, TOTAL)),
    ).not.toBeNull();
  });

  // A range over no rows is meaningless, and a lone page needs no pager at all.
  it.each([
    { label: "client", node: () => tableOf(0, { pageSize: 10 }) },
    {
      label: "server",
      node: () =>
        serverTable(0, { page: 1, pageSize: 10, total: 0, onPageChange: noop }),
    },
  ])(
    "an empty $label table shows the zero-row count and no pager",
    ({ node }) => {
      render(node());
      expect(screen.getByText(rowCountLabel(0))).toBeInTheDocument();
      expect(rowRangeLabel(0, 0, 0)).toBe(rowCountLabel(0));
      expect(queryPrevBtn()).toBeNull();
      expect(queryNextBtn()).toBeNull();
    },
  );

  it("prev/next report the neighbouring page instead of moving on their own", async () => {
    const user = userEvent.setup();
    const PAGE = 3;
    const onPageChange = vi.fn();
    render(
      serverTable(10, { page: PAGE, pageSize: 10, total: 97, onPageChange }),
    );
    await user.click(nextBtn());
    expect(onPageChange).toHaveBeenCalledWith(PAGE + 1);
    await user.click(prevBtn());
    expect(onPageChange).toHaveBeenCalledWith(PAGE - 1);
  });

  it("disables prev on the first page and next on the last", () => {
    const PER_PAGE = 10;
    const TOTAL = 25;
    const LAST = Math.ceil(TOTAL / PER_PAGE);
    const { unmount } = render(
      serverTable(PER_PAGE, {
        page: 1,
        pageSize: PER_PAGE,
        total: TOTAL,
        onPageChange: noop,
      }),
    );
    expect(prevBtn()).toBeDisabled();
    expect(nextBtn()).not.toBeDisabled();
    unmount();
    const rowsOnLast = TOTAL - (LAST - 1) * PER_PAGE;
    render(
      serverTable(rowsOnLast, {
        page: LAST,
        pageSize: PER_PAGE,
        total: TOTAL,
        onPageChange: noop,
      }),
    );
    expect(nextBtn()).toBeDisabled();
    expect(prevBtn()).not.toBeDisabled();
  });

  it.each([
    {
      label: "past the end",
      typed: (last: number) => last + 4,
      expected: (last: number) => last,
    },
    { label: "below the floor", typed: () => 0, expected: () => 1 },
  ])("a typed page $label commits clamped", async ({ typed, expected }) => {
    const user = userEvent.setup();
    const PER_PAGE = 10;
    const TOTAL = 42;
    const LAST = Math.ceil(TOTAL / PER_PAGE);
    const onPageChange = vi.fn();
    render(
      serverTable(PER_PAGE, {
        page: 2,
        pageSize: PER_PAGE,
        total: TOTAL,
        onPageChange,
      }),
    );
    await user.clear(pageInput());
    await user.type(pageInput(), String(typed(LAST)));
    await user.keyboard("{Enter}");
    expect(onPageChange).toHaveBeenCalledWith(expected(LAST));
  });
});
