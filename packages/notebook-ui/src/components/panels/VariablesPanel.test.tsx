import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { VarSummary } from "../../model/types";
import { VariablesPanel, sortVariables } from "./VariablesPanel";

const variables: VarSummary[] = [
  {
    name: "model",
    type: "LinearRegression",
    repr: "LinearRegression()",
    size_bytes: 2048,
    cell_id: "c2",
  },
  {
    name: "df",
    type: "DataFrame",
    repr: "<DataFrame 100 × 3>",
    size_bytes: 12 * 1024 * 1024,
    shape: [100, 3],
    cell_id: "c1",
  },
  {
    name: "alpha",
    type: "float",
    repr: "0.5",
    size_bytes: null,
    cell_id: "c3",
  },
  { name: "loose", type: "int", repr: "1" },
];

function rowNames(): string[] {
  return screen
    .getAllByRole("row")
    .slice(1)
    .map((row) => within(row).getAllByRole("rowheader")[0].textContent ?? "");
}

describe("VariablesPanel", () => {
  it("lists variables by name with their type, size and value", () => {
    render(<VariablesPanel variables={variables} onJump={() => {}} />);
    expect(rowNames()).toEqual(["alpha", "df", "loose", "model"]);
    const df = screen.getAllByRole("row")[2];
    expect(
      within(df)
        .getAllByRole("cell")
        .map((c) => c.textContent),
    ).toEqual(["DataFrame", "100 × 3, 12 MB", "<DataFrame 100 × 3>"]);
  });

  it("names a dotted type by its last part and keeps the full name on hover", () => {
    render(
      <VariablesPanel
        variables={[
          {
            name: "engine_health",
            type: "polars.dataframe.frame.DataFrame",
            repr: "shape: (3, 7)",
          },
        ]}
        onJump={() => {}}
      />,
    );
    const [type] = within(screen.getAllByRole("row")[1]).getAllByRole("cell");
    expect(type.textContent).toBe("DataFrame");
    expect(type.getAttribute("title")).toBe("polars.dataframe.frame.DataFrame");
  });

  it.each([
    ["Name", ["model", "loose", "df", "alpha"], "descending"],
    ["Type", ["df", "alpha", "loose", "model"], "ascending"],
    ["Size", ["model", "df", "alpha", "loose"], "ascending"],
  ])("sorts by %s", async (column, expected, sort) => {
    const user = userEvent.setup();
    render(<VariablesPanel variables={variables} onJump={() => {}} />);
    await user.click(screen.getByRole("button", { name: column }));
    expect(rowNames()).toEqual(expected);
    expect(
      screen.getByRole("columnheader", { name: new RegExp(column) }),
    ).toHaveAttribute("aria-sort", sort);
  });

  it("reverses a column on a second click", async () => {
    const user = userEvent.setup();
    render(<VariablesPanel variables={variables} onJump={() => {}} />);
    await user.click(screen.getByRole("button", { name: "Size" }));
    await user.click(screen.getByRole("button", { name: /Size/ }));
    expect(rowNames()).toEqual(["df", "model", "alpha", "loose"]);
    expect(screen.getByRole("columnheader", { name: /Name/ })).toHaveAttribute(
      "aria-sort",
      "none",
    );
  });

  it("filters by name or type", async () => {
    const user = userEvent.setup();
    render(<VariablesPanel variables={variables} onJump={() => {}} />);
    await user.type(
      screen.getByRole("searchbox", { name: "Filter variables" }),
      "FRAME",
    );
    expect(rowNames()).toEqual(["df"]);
    await user.clear(screen.getByRole("searchbox"));
    await user.type(screen.getByRole("searchbox"), "zzz");
    expect(screen.getByText("No matches")).toBeInTheDocument();
  });

  it("jumps to the defining cell, and offers no jump without one", async () => {
    const user = userEvent.setup();
    const jumps: string[] = [];
    render(
      <VariablesPanel variables={variables} onJump={(id) => jumps.push(id)} />,
    );
    await user.click(screen.getByRole("button", { name: "df" }));
    expect(jumps).toEqual(["c1"]);
    expect(screen.queryByRole("button", { name: "loose" })).toBeNull();
  });

  it("says so when there are no variables", () => {
    render(<VariablesPanel variables={[]} onJump={() => {}} />);
    expect(screen.getByText("No variables")).toBeInTheDocument();
  });
});

describe("sortVariables", () => {
  const v = (name: string, size: number | null, type = "t"): VarSummary => ({
    name,
    type,
    repr: "",
    size_bytes: size,
  });

  it("keeps unknown sizes last in both directions and breaks ties by name", () => {
    const list = [v("b", null), v("z", 5), v("a", null), v("y", 5), v("x", 9)];
    expect(sortVariables(list, "size", false).map((x) => x.name)).toEqual([
      "y",
      "z",
      "x",
      "a",
      "b",
    ]);
    expect(sortVariables(list, "size", true).map((x) => x.name)).toEqual([
      "x",
      "y",
      "z",
      "a",
      "b",
    ]);
  });

  it("breaks type ties by name even when descending", () => {
    const list = [v("b", 1, "int"), v("a", 1, "int"), v("c", 1, "str")];
    expect(sortVariables(list, "type", true).map((x) => x.name)).toEqual([
      "c",
      "a",
      "b",
    ]);
  });
});
