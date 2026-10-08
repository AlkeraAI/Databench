import type { ReactNode } from "react";

import { cleanup, fireEvent, render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { Table, stackThresholdPx } from "./Table";

// The stacked (narrow) layout hangs off two per-cell attributes: `data-label` carries the column's
// flattened header text so a card can render "Label  value", and `data-cell` carries the card-mode
// role. Both are pure DOM. The MEASURED stack toggle needs real layout, so it is verified on the
// running app at several widths, never here.

afterEach(cleanup);

function bodyCells(): HTMLElement[] {
  return Array.from(document.querySelectorAll<HTMLElement>(".alk-table tbody td"));
}

/** The card-mode role each stacked cell is tagged with — null for a field cell, which carries none. */
function cellRoles(): (string | null)[] {
  return bodyCells().map((c) => c.getAttribute("data-cell"));
}

describe("Table responsive labels", () => {
  it("tags each data cell with its column's label", () => {
    render(
      <Table responsive columns={["Name", <span key="p">Priority</span>, ""]}>
        <tr>
          <td>Ada</td>
          <td>3</td>
          <td>actions</td>
        </tr>
      </Table>,
    );
    const [name, priority, actions] = bodyCells();
    expect(name).toHaveAttribute("data-label", "Name");
    expect(priority).toHaveAttribute("data-label", "Priority");
    // An empty header means an actions cell; the stacked CSS drops the marker.
    expect(actions).toHaveAttribute("data-label", "");
  });

  // Every header shape must read as one plain string, never "[object Object]" or a first fragment.
  it.each([
    { label: "array with a nested element", header: ["Full ", <b key="n">Name</b>], text: "Full Name" },
    { label: "nested element", header: <span>Owning {<em>Team</em>}</span>, text: "Owning Team" },
    { label: "number", header: 2024, text: "2024" },
    { label: "th element", header: <th>Credits</th>, text: "Credits" },
  ] as { label: string; header: ReactNode; text: string }[])(
    "flattens a $label header to its text",
    ({ header, text }) => {
      render(
        <Table responsive columns={[header]}>
          <tr>
            <td>value</td>
          </tr>
        </Table>,
      );
      expect(bodyCells()[0]).toHaveAttribute("data-label", text);
    },
  );

  it("labels a cell past the last column empty", () => {
    // N labelled columns plus a trailing un-headered actions cell: the extra cell indexes past
    // `columns`, so its label is empty rather than a crash or a leaked "undefined".
    render(
      <Table responsive columns={["Name"]}>
        <tr>
          <td>Ada</td>
          <td>edit / delete</td>
        </tr>
      </Table>,
    );
    const [name, actions] = bodyCells();
    expect(name).toHaveAttribute("data-label", "Name");
    expect(actions).toHaveAttribute("data-label", "");
  });

  it("keeps labels aligned past a falsy cell", () => {
    // A conditionally-omitted cell is compacted out BEFORE labelling, so the surviving cells stay
    // aligned to their columns by rendered position.
    render(
      <Table responsive columns={["Name", "Priority"]}>
        <tr>
          <td>Ada</td>
          {false}
          <td>3</td>
        </tr>
      </Table>,
    );
    const cells = bodyCells();
    expect(cells).toHaveLength(2);
    expect(cells[0]).toHaveAttribute("data-label", "Name");
    expect(cells[1]).toHaveAttribute("data-label", "Priority");
  });

  it("does not tag cells when the table is not responsive", () => {
    render(
      <Table columns={["Name"]}>
        <tr>
          <td>Ada</td>
        </tr>
      </Table>,
    );
    expect(bodyCells()[0]).not.toHaveAttribute("data-label");
  });
});

describe("Table card-mode roles", () => {
  // The stacked card places each cell by role: `primary` (the title), `actions` (the cluster that
  // shares the title line, never a lone bottom row), `marker` (a label-less decorative cell the card
  // drops), `wide` (a colSpan payload), and a field (no attribute).
  it.each([
    {
      label: "default identity and trailing actions",
      props: {},
      columns: ["Name", "Email", "Role", ""],
      cells: ["Ada", "ada@x.io", "Admin", "edit"],
      roles: ["primary", null, null, "actions"],
    },
    {
      label: "stackPrimary moves the identity off column 0",
      props: { stackPrimary: 2 },
      columns: ["When", "Who", "Error", ""],
      cells: ["now", "Ada", "Boom", "delete"],
      roles: [null, null, "primary", "actions"],
    },
    {
      label: "a label-less non-actions column is a marker",
      props: { stackPrimary: 2 },
      columns: ["", "Component", "Error", "When", ""],
      cells: ["dot", "cli", "Boom", "now", "actions"],
      roles: ["marker", null, "primary", null, "actions"],
    },
    {
      label: "stackActions overrides the trailing column",
      props: { stackActions: 1 },
      columns: ["Name", "", "Detail"],
      cells: ["Ada", "act", "x"],
      roles: ["primary", "actions", null],
    },
  ])("$label", ({ props, columns, cells, roles }) => {
    render(
      <Table responsive columns={columns} {...props}>
        <tr>
          {cells.map((c) => (
            <td key={c}>{c}</td>
          ))}
        </tr>
      </Table>,
    );
    expect(cellRoles()).toEqual(roles);
  });

  it("tags a colSpan payload wide and descends into a Fragment", () => {
    render(
      <Table responsive columns={["When", "Actor", ""]}>
        <>
          <tr>
            <td>now</td>
            <td>Ada</td>
            <td>expand</td>
          </tr>
          <tr>
            <td colSpan={3}>the expanded detail payload</td>
          </tr>
        </>
      </Table>,
    );
    expect(cellRoles()).toEqual(["primary", null, "actions", "wide"]);
  });

  it("adds no data-cell when the table is not responsive", () => {
    render(
      <Table columns={["Name", ""]}>
        <tr>
          <td>Ada</td>
          <td>edit</td>
        </tr>
      </Table>,
    );
    expect(cellRoles()).toEqual([null, null]);
  });
});

describe("Table onRowClick", () => {
  // A clickable row activates by pointer AND keyboard, never hijacks an inner control, and reports
  // the row's ABSOLUTE child index so a page can map it back to its own data array.
  function firstRow(): HTMLElement {
    const el = document.querySelector<HTMLElement>(".alk-table tbody tr");
    if (!el) throw new Error("no row");
    return el;
  }

  it("makes rows focusable only when a handler is given", () => {
    const { rerender } = render(
      <Table columns={["Name"]} onRowClick={() => {}}>
        <tr key="a">
          <td>Ada</td>
        </tr>
      </Table>,
    );
    expect(firstRow()).toHaveAttribute("data-rowclick", "");
    expect(firstRow()).toHaveAttribute("tabindex", "0");

    rerender(
      <Table columns={["Name"]}>
        <tr key="a">
          <td>Ada</td>
        </tr>
      </Table>,
    );
    expect(firstRow()).not.toHaveAttribute("data-rowclick");
    expect(firstRow()).not.toHaveAttribute("tabindex");
  });

  it("fires with the clicked row's index", () => {
    const onRowClick = vi.fn();
    render(
      <Table columns={["Name"]} onRowClick={onRowClick}>
        <tr key="a">
          <td>Ada</td>
        </tr>
        <tr key="b">
          <td>Bea</td>
        </tr>
      </Table>,
    );
    const rows = document.querySelectorAll<HTMLElement>(".alk-table tbody tr");
    fireEvent.click(rows[1].querySelector("td")!);
    expect(onRowClick).toHaveBeenCalledWith(1);
  });

  it("activates on Enter and Space when the row holds focus", () => {
    const onRowClick = vi.fn();
    render(
      <Table columns={["Name"]} onRowClick={onRowClick}>
        <tr key="a">
          <td>Ada</td>
        </tr>
      </Table>,
    );
    const row = firstRow();
    fireEvent.keyDown(row, { key: "Enter" });
    fireEvent.keyDown(row, { key: " " });
    expect(onRowClick).toHaveBeenCalledTimes(2);
    expect(onRowClick).toHaveBeenNthCalledWith(1, 0);
  });

  it("ignores a click or Enter aimed at an inner control", () => {
    const onRowClick = vi.fn();
    render(
      <Table columns={["Name", ""]} onRowClick={onRowClick}>
        <tr key="a">
          <td>Ada</td>
          <td>
            <button type="button">delete</button>
          </td>
        </tr>
      </Table>,
    );
    const inner = document.querySelector("button")!;
    fireEvent.click(inner);
    fireEvent.keyDown(inner, { key: "Enter" });
    expect(onRowClick).not.toHaveBeenCalled();
  });

  it("preserves a per-row aria-label the page set", () => {
    render(
      <Table columns={["Name"]} onRowClick={() => {}}>
        <tr key="a" aria-label="View Ada">
          <td>Ada</td>
        </tr>
      </Table>,
    );
    expect(firstRow()).toHaveAttribute("aria-label", "View Ada");
  });

  it("reports the absolute child index for a paged row", () => {
    const onRowClick = vi.fn();
    render(
      <Table columns={["Name"]} pageSize={2} onRowClick={onRowClick}>
        {["Ada", "Bea", "Cy", "Del"].map((n) => (
          <tr key={n}>
            <td>{n}</td>
          </tr>
        ))}
      </Table>,
    );
    fireEvent.click(document.querySelector<HTMLButtonElement>(`[aria-label="${"Next page"}"]`)!);
    const rows = document.querySelectorAll<HTMLElement>(".alk-table tbody tr");
    fireEvent.click(rows[0].querySelector("td")!);
    // First row of page 2 is child index 2, not "0".
    expect(onRowClick).toHaveBeenCalledWith(2);
  });
});

describe("stackThresholdPx", () => {
  // The container width below which a responsive table stacks. It sums each column's MINIMUM: a fixed
  // column's exact width, or a MIN_COL_PX floor for a flexible one. So a table folds BEFORE any column
  // is squeezed below that minimum, which is what makes column overlap impossible.
  it.each([
    { label: "no colWidths falls back to columns x 104", cols: 4, widths: undefined, expected: 416 },
    // 1rem + 32px + one flexible floor = 152, under the 3 x 104 flat floor.
    { label: "the flat floor wins under it", cols: 3, widths: ["1rem", "32px", undefined], expected: 312 },
  ] as { label: string; cols: number; widths: (string | number | undefined)[] | undefined; expected: number }[])(
    "$label",
    ({ cols, widths, expected }) => {
      expect(stackThresholdPx(cols, widths)).toBe(expected);
    },
  );

  it("reserves one MIN_COL_PX for a flexible column, not zero", () => {
    // The crash register: fixed columns sum to ~700px and the flexible error column reserves 104 for
    // its own header, so the table folds at ~804px, before that column is squeezed too narrow.
    const crash = ["1.75rem", "9rem", undefined, "12rem", "13.5rem", "7.5rem"];
    expect(stackThresholdPx(6, crash)).toBe((1.75 + 9 + 12 + 13.5 + 7.5) * 16 + 104);
  });

  it("never returns below the fixed-column sum", () => {
    // With `table-layout: fixed`, a container narrower than the fixed sum can't fit the columns. The
    // fold point must be >= that sum for EVERY column set, so a mid-width window where the columns
    // don't fit yet the table hasn't stacked cannot exist.
    const sets: Array<[number, Array<string | number | undefined>]> = [
      [3, ["10rem", "10rem", "10rem"]],
      [4, [200, undefined, 300, undefined]],
      [6, ["1.75rem", "9rem", undefined, "12rem", "13.5rem", "7.5rem"]],
      [2, ["40rem", undefined]],
    ];
    for (const [cols, widths] of sets) {
      const fixedSum = widths.reduce<number>(
        (s, w) => s + (typeof w === "number" ? w : w?.endsWith("rem") ? parseFloat(w) * 16 : 0),
        0,
      );
      expect(stackThresholdPx(cols, widths)).toBeGreaterThanOrEqual(fixedSum);
    }
  });

  // stackAt is a FLOOR: it can fold a table sooner than its columns would, but a too-small override
  // can't drop the fold below the derived minimum and reopen the scroll/crush gap.
  it.each([
    { label: "with colWidths", widths: ["6rem", undefined], above: 600, below: 100 },
    { label: "without colWidths", widths: undefined, above: 500, below: 200 },
  ] as { label: string; widths: (string | number | undefined)[] | undefined; above: number; below: number }[])(
    "stackAt raises the fold but never lowers it ($label)",
    ({ widths, above, below }) => {
      const derived = stackThresholdPx(2, widths);
      expect(stackThresholdPx(2, widths, above)).toBe(above);
      expect(stackThresholdPx(2, widths, below)).toBe(derived);
    },
  );
});

describe("Table th-element columns", () => {
  it("renders a <th> column as the header cell itself", () => {
    // The caller's element IS the cell, so cell-level attributes the ARIA spec requires on a real
    // <th> (aria-sort, alignment) land where they belong instead of on a wrapper.
    render(
      <Table
        columns={[
          <th key="credits" aria-sort="descending">
            Credits
          </th>,
          "Name",
        ]}
      >
        <tr>
          <td>12</td>
          <td>Ada</td>
        </tr>
      </Table>,
    );
    const headers = Array.from(document.querySelectorAll(".alk-table thead th"));
    expect(headers).toHaveLength(2);
    expect(headers[0]).toHaveAttribute("aria-sort", "descending");
    expect(headers[0]).toHaveTextContent("Credits");
    expect(headers[0].querySelector("th")).toBeNull();
    expect(headers[1]).not.toHaveAttribute("aria-sort");
  });
});
