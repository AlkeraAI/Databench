import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { Table } from "./Table";

// The stacked-card cases that Table.test.tsx does not carry: a full-width payload cell and the
// stackWide opt-in, plus the vertical-alignment hook.

afterEach(cleanup);

function bodyCells(): HTMLElement[] {
  return Array.from(document.querySelectorAll<HTMLElement>(".alk-table tbody td"));
}

function block(): HTMLElement {
  const el = document.querySelector<HTMLElement>(".alk-table-block");
  if (!el) throw new Error("no table block rendered");
  return el;
}

describe("Table responsive labels", () => {
  it("leaves a colSpan detail cell unlabelled", () => {
    // A Fragment row (a main row plus an expand-detail row) must be descended into. The detail row's
    // spanning cell is a full-width payload, not a column value, so no column name is stamped on it.
    render(
      <Table responsive columns={["When", "Actor"]}>
        <>
          <tr>
            <td>today</td>
            <td>ada</td>
          </tr>
          <tr>
            <td colSpan={2}>the full request payload</td>
          </tr>
        </>
      </Table>,
    );
    const cells = bodyCells();
    expect(cells[0]).toHaveAttribute("data-label", "When");
    expect(cells[1]).toHaveAttribute("data-label", "Actor");
    expect(cells[2]).toHaveAttribute("data-label", "");
  });

  it("stackWide columns are wide cells with no label", () => {
    // A status-chip column reads as a full-width row in a card, with no "Grade  value" pair.
    render(
      <Table responsive columns={["Change", "Grade", ""]} stackWide={[1]}>
        <tr>
          <td>orders.total narrowed</td>
          <td>Breaking</td>
          <td>waive</td>
        </tr>
      </Table>,
    );
    const [change, grade, actions] = bodyCells();
    expect(change).toHaveAttribute("data-label", "Change");
    expect(grade).toHaveAttribute("data-cell", "wide");
    expect(grade).toHaveAttribute("data-label", "");
    expect(actions).toHaveAttribute("data-cell", "actions");
  });
});

describe("Table vertical alignment", () => {
  it("carries data-valign only for a non-default alignment", () => {
    const { rerender } = render(
      <Table columns={["Name"]} verticalAlign="top">
        <tr>
          <td>Ada</td>
        </tr>
      </Table>,
    );
    expect(block()).toHaveAttribute("data-valign", "top");

    rerender(
      <Table columns={["Name"]}>
        <tr>
          <td>Ada</td>
        </tr>
      </Table>,
    );
    expect(block()).not.toHaveAttribute("data-valign");
  });
});
