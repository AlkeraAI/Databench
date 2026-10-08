import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { GraphView } from "../../model/types";
import { GraphPanel } from "./GraphPanel";
import { cellLabel, formatBytes, graphErrorLabel } from "./labels";

const cells = [
  { id: "load", name: "load" },
  { id: "clean", name: "clean" },
  { id: "side", name: "_" },
  { id: "train", name: "train" },
  { id: "plot", name: "plot" },
];
const graph: GraphView = {
  edges: [
    ["load", "clean"],
    ["clean", "train"],
    ["clean", "plot"],
  ],
  errors: { train: ["multiple_definitions"], side: ["cycle", "odd_problem"] },
};
const names = {
  load: { defs: ["raw"], refs: [] },
  clean: { defs: ["df"], refs: ["raw"] },
  train: { defs: ["model"], refs: ["df"] },
};

function nodeNames(): string[] {
  return within(screen.getByRole("list")).getAllByRole("button").map((b) => b.textContent ?? "");
}

describe("GraphPanel", () => {
  it("draws every cell in layers, parents above children", () => {
    render(<GraphPanel cells={cells} graph={graph} onJump={() => {}} />);
    const top = (name: string): number => parseFloat(screen.getByRole("button", { name: new RegExp(`^!?${name}$`) }).style.top);
    expect(top("load")).toBe(0);
    expect(top("Cell 3")).toBe(0);
    expect(top("clean")).toBeGreaterThan(top("load"));
    expect(top("train")).toBeGreaterThan(top("clean"));
    expect(top("plot")).toBe(top("train"));
    // Keyboard order is row by row.
    expect(nodeNames()).toEqual(["load", "!Cell 3", "clean", "!train", "plot"]);
  });

  it("draws one path per edge and names what flows along it", () => {
    const { container } = render(<GraphPanel cells={cells} graph={graph} names={names} onJump={() => {}} />);
    const paths = [...container.querySelectorAll("path")];
    expect(paths.map((p) => p.getAttribute("data-edge"))).toEqual(["load->clean", "clean->train", "clean->plot"]);
    expect(paths.map((p) => p.querySelector("title")?.textContent ?? null)).toEqual(["raw", "df", null]);
  });

  it("marks cells with graph errors and says why", () => {
    render(<GraphPanel cells={cells} graph={graph} onJump={() => {}} />);
    const train = screen.getByRole("button", { name: "train" });
    expect(train).toHaveClass("nb-graph__node--error");
    expect(train).toHaveAccessibleDescription("Defines a name another cell also defines");
    expect(screen.getByRole("button", { name: "Cell 3" })).toHaveAccessibleDescription("Part of a cycle. Odd problem");
    expect(screen.getByRole("button", { name: "clean" })).not.toHaveClass("nb-graph__node--error");
    expect(screen.getByRole("button", { name: "clean" })).not.toHaveAccessibleDescription();
  });

  it("jumps to a cell on click and on Enter", async () => {
    const user = userEvent.setup();
    const jumps: string[] = [];
    render(<GraphPanel cells={cells} graph={graph} onJump={(id) => jumps.push(id)} />);
    await user.click(screen.getByRole("button", { name: "plot" }));
    screen.getByRole("button", { name: "clean" }).focus();
    await user.keyboard("{Enter}");
    expect(jumps).toEqual(["plot", "clean"]);
  });

  it("moves focus with the arrow keys, Home and End", async () => {
    const user = userEvent.setup();
    render(<GraphPanel cells={cells} graph={graph} onJump={() => {}} />);
    screen.getByRole("button", { name: "load" }).focus();
    await user.keyboard("{ArrowDown}");
    expect(screen.getByRole("button", { name: "Cell 3" })).toHaveFocus();
    await user.keyboard("{ArrowRight}");
    expect(screen.getByRole("button", { name: "clean" })).toHaveFocus();
    await user.keyboard("{ArrowUp}");
    expect(screen.getByRole("button", { name: "Cell 3" })).toHaveFocus();
    await user.keyboard("{End}");
    expect(screen.getByRole("button", { name: "plot" })).toHaveFocus();
    await user.keyboard("{ArrowDown}");
    expect(screen.getByRole("button", { name: "plot" })).toHaveFocus();
    await user.keyboard("{Home}");
    expect(screen.getByRole("button", { name: "load" })).toHaveFocus();
  });

  it("filters to what the selected cell reads from or what reads from it", async () => {
    const user = userEvent.setup();
    render(<GraphPanel cells={cells} graph={graph} selectedId="clean" onJump={() => {}} />);
    expect(screen.getByRole("button", { name: "clean" })).toHaveAttribute("aria-current", "true");
    await user.click(screen.getByRole("button", { name: "Upstream" }));
    expect(screen.getByRole("button", { name: "Upstream" })).toHaveAttribute("aria-pressed", "true");
    expect(nodeNames()).toEqual(["load", "clean"]);
    await user.click(screen.getByRole("button", { name: "Downstream" }));
    expect(nodeNames()).toEqual(["clean", "!train", "plot"]);
    await user.click(screen.getByRole("button", { name: "All" }));
    expect(nodeNames()).toHaveLength(5);
  });

  it("offers only All when no cell is selected", () => {
    render(<GraphPanel cells={cells} graph={graph} selectedId="gone" onJump={() => {}} />);
    expect(screen.getByRole("button", { name: "Upstream" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Downstream" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "All" })).toHaveAttribute("aria-pressed", "true");
  });

  it("lights the edges that touch the selected cell", () => {
    const { container } = render(<GraphPanel cells={cells} graph={graph} selectedId="train" onJump={() => {}} />);
    const lit = [...container.querySelectorAll("path.nb-graph__edge--active")].map((p) => p.getAttribute("data-edge"));
    expect(lit).toEqual(["clean->train"]);
  });

  it("lays out a cycle without hanging", () => {
    const cyclic: GraphView = { edges: [["a", "b"], ["b", "a"]], errors: {} };
    render(<GraphPanel cells={[{ id: "a", name: "a" }, { id: "b", name: "b" }]} graph={cyclic} onJump={() => {}} />);
    expect(nodeNames()).toEqual(["a", "b"]);
  });

  it("says so when there are no cells", () => {
    render(<GraphPanel cells={[]} graph={{ edges: [], errors: {} }} onJump={() => {}} />);
    expect(screen.getByText("No cells")).toBeInTheDocument();
  });
});

describe("labels", () => {
  it.each([
    [{ name: "load" }, 0, "load"],
    [{ name: "_" }, 2, "Cell 3"],
    [{ name: "" }, 0, "Cell 1"],
  ])("names %j at %d", (cell, index, expected) => {
    expect(cellLabel(cell, index)).toBe(expected);
  });

  it.each([
    ["multiple_definitions", "Defines a name another cell also defines"],
    ["cycle", "Part of a cycle"],
    ["delete_nonlocal", "Delete nonlocal"],
    ["multiple_definitions:df", "Defines a name another cell also defines: df"],
    ["multiple_definitions: df", "Defines a name another cell also defines: df"],
  ])("words the graph error %s", (code, expected) => {
    expect(graphErrorLabel(code)).toBe(expected);
  });

  it.each([
    [0, "0 B"],
    [512, "512 B"],
    [1536, "1.5 KB"],
    [12 * 1024 * 1024, "12 MB"],
    [1.25 * 1024 ** 3, "1.3 GB"],
    [5 * 1024 ** 5, "5120 TB"],
    [-1, ""],
  ])("formats %d bytes", (bytes, expected) => {
    expect(formatBytes(bytes)).toBe(expected);
  });
});
