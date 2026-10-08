import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { DataTable } from "./DataTable";

afterEach(cleanup);

const COLUMNS = ["id", "name"];
// 25 rows so pageSize 10 → 3 pages with a short (5-row) last page.
const ROWS = Array.from({ length: 25 }, (_, i) => [String(i + 1), `row-${i + 1}`]);

const bodyRows = (container: HTMLElement) => [...container.querySelectorAll(".alk-datatable__table tbody tr")];
const firstRowNum = (container: HTMLElement) =>
  container.querySelector(".alk-datatable__table tbody .alk-datatable__rownum")?.textContent;
const lastRowNum = (container: HTMLElement) =>
  [...container.querySelectorAll(".alk-datatable__table tbody .alk-datatable__rownum")].at(-1)?.textContent;

describe("DataTable", () => {
  it("renders every row and no pager when pageSize is unset", () => {
    const { container } = render(<DataTable columns={COLUMNS} rows={ROWS} />);
    expect(bodyRows(container)).toHaveLength(25);
    expect(container.querySelector(".alk-datatable-pager")).toBeNull();
  });

  it("slices to one page and walks pages with the pager", () => {
    const { container } = render(<DataTable columns={COLUMNS} rows={ROWS} pageSize={10} />);
    // Page 1: rows 1–10 only.
    expect(bodyRows(container)).toHaveLength(10);
    expect(screen.getByText("row-1")).toBeInTheDocument();
    expect(screen.queryByText("row-11")).toBeNull();
    expect(screen.getByText("25 rows")).toBeInTheDocument();
    expect(screen.getByLabelText("Page number").closest(".alk-datatable-pager__pagelabel")?.textContent).toContain(
      "of 3",
    );

    // → page 2: rows 11–20.
    fireEvent.click(screen.getByRole("button", { name: "Next page" }));
    expect(screen.getByText("row-11")).toBeInTheDocument();
    expect(screen.queryByText("row-1")).toBeNull();

    // → page 3 (last): the 5 remaining rows; Next disabled.
    fireEvent.click(screen.getByRole("button", { name: "Next page" }));
    expect(bodyRows(container)).toHaveLength(5);
    expect(screen.getByText("row-25")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Next page" })).toBeDisabled();
  });

  it("numbers rows by absolute index across pages", () => {
    // 25 rows, pageSize 10. Page 1 gutter 1..10; page 3 (the 5-row tail) must read
    // 21..25 — both the first-row offset (start = (p-1)*pageSize) and the last-row
    // index (+1, not +0) are off-by-one traps. `start + rowIndex` alone (no +1)
    // would render 0..; `rowIndex + 1` (ignoring start) would reset to 1 each page.
    const { container } = render(<DataTable columns={COLUMNS} rows={ROWS} pageSize={10} rowNumbers />);
    expect(firstRowNum(container)).toBe("1");
    expect(lastRowNum(container)).toBe("10");

    fireEvent.click(screen.getByRole("button", { name: "Next page" }));
    expect(firstRowNum(container)).toBe("11"); // page 2 starts at 11, not 1

    fireEvent.click(screen.getByRole("button", { name: "Next page" })); // page 3 (tail)
    expect(firstRowNum(container)).toBe("21");
    expect(lastRowNum(container)).toBe("25"); // last absolute number, not 5
  });

  it("jumps to a typed page, clamping out of range", () => {
    render(<DataTable columns={COLUMNS} rows={ROWS} pageSize={10} />);
    const input = screen.getByLabelText("Page number");
    fireEvent.change(input, { target: { value: "3" } });
    fireEvent.blur(input);
    expect(screen.getByText("row-21")).toBeInTheDocument();

    fireEvent.change(input, { target: { value: "99" } });
    fireEvent.blur(input);
    expect(screen.getByText("row-25")).toBeInTheDocument(); // clamped to page 3, not blank
  });

  it("a single page shows its count and no nav", () => {
    const { rerender } = render(<DataTable columns={COLUMNS} rows={ROWS.slice(0, 4)} pageSize={10} />);
    expect(screen.getByText("4 rows")).toBeInTheDocument();
    expect(screen.queryByLabelText("Page number")).toBeNull();
    expect(screen.queryByRole("button", { name: "Next page" })).toBeNull();

    rerender(<DataTable columns={COLUMNS} rows={ROWS.slice(0, 1)} pageSize={10} />);
    expect(screen.getByText("1 row")).toBeInTheDocument(); // "1 row", never "1 rows"
  });

  it("re-clamps when the row set shrinks", () => {
    // On the last page, then the data shrinks to one page — the render-time clamp
    // shows real rows, never a stranded empty page past the new end.
    const { rerender } = render(<DataTable columns={COLUMNS} rows={ROWS} pageSize={10} />);
    fireEvent.click(screen.getByRole("button", { name: "Next page" }));
    fireEvent.click(screen.getByRole("button", { name: "Next page" })); // page 3
    expect(screen.getByText("row-25")).toBeInTheDocument();

    rerender(<DataTable columns={COLUMNS} rows={ROWS.slice(0, 8)} pageSize={10} />);
    expect(screen.getByText("row-1")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Next page" })).toBeNull(); // 1 page now
  });

  it("no rows renders only the empty label", () => {
    const { container } = render(
      <DataTable columns={COLUMNS} rows={[]} pageSize={10} emptyLabel="Nothing here." />,
    );
    expect(screen.getByText("Nothing here.")).toBeInTheDocument();
    // The empty branch returns before the footer, so the pager chrome (count slot
    // included) must be wholly absent — never a stray "0 rows" or an empty footer.
    expect(container.querySelector(".alk-datatable-pager")).toBeNull();
    expect(container.querySelector(".alk-datatable__foot")).toBeNull();
  });
});
