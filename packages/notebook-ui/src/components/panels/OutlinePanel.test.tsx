import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { DocCell } from "../../model/types";
import { OutlinePanel, markdownHeadings, outlineEntries } from "./OutlinePanel";

function cell(id: string, source: string, kind = "markdown", name = "_"): DocCell {
  return { id, kind, name, source, config: {}, meta: {} };
}

describe("markdownHeadings", () => {
  it.each([
    ["an ATX heading", "# Title", [{ text: "Title", level: 1 }]],
    ["every ATX level", "## Two\n###### Six", [{ text: "Two", level: 2 }, { text: "Six", level: 6 }]],
    ["a closing sequence", "## Two ##", [{ text: "Two", level: 2 }]],
    ["up to three spaces of indent", "   # In", [{ text: "In", level: 1 }]],
    ["inline markup stripped", "# The **`df`** [frame](http://x) _now_", [{ text: "The df frame now", level: 1 }]],
    ["a setext level one", "Title\n=====", [{ text: "Title", level: 1 }]],
    ["a setext level two", "Sub\n---", [{ text: "Sub", level: 2 }]],
    ["CRLF line ends", "# A\r\nB\r\n---", [{ text: "A", level: 1 }, { text: "B", level: 2 }]],
  ])("reads %s", (_label, source, expected) => {
    expect(markdownHeadings(source)).toEqual(expected);
  });

  it.each([
    ["a hashtag", "#tag"],
    ["seven hashes", "####### Seven"],
    ["four spaces of indent", "    # Code"],
    ["an empty heading", "##"],
    ["a rule after a blank line", "\n---"],
    ["a heading in a backtick fence", "```\n# not\n```"],
    ["a heading in a tilde fence", "~~~python\n# not\nnot\n---\n~~~"],
    ["a heading in a longer fence closed only by a long enough one", "````\n```\n# not\n````"],
  ])("ignores %s", (_label, source) => {
    expect(markdownHeadings(source)).toEqual([]);
  });

  it("reads headings after a fence closes", () => {
    expect(markdownHeadings("```\n# not\n```\n# Yes")).toEqual([{ text: "Yes", level: 1 }]);
  });
});

const cells = [
  cell("intro", "# Data\nSome text."),
  cell("load", "raw = read()", "python", "load"),
  cell("anon", "x = 1", "python"),
  cell("md2", "## Model\n### Fit"),
  cell("fit", "m = fit()", "python", "fit"),
  cell("named_md", "Plain prose", "markdown", "notes"),
];

describe("outlineEntries", () => {
  it("interleaves headings and named cells in document order", () => {
    expect(outlineEntries(cells)).toEqual([
      { cell_id: "intro", kind: "heading", text: "Data", level: 1 },
      { cell_id: "load", kind: "cell", text: "load", level: 2 },
      { cell_id: "md2", kind: "heading", text: "Model", level: 2 },
      { cell_id: "md2", kind: "heading", text: "Fit", level: 3 },
      { cell_id: "fit", kind: "cell", text: "fit", level: 4 },
      { cell_id: "named_md", kind: "cell", text: "notes", level: 4 },
    ]);
  });

  it("does not read headings from a code cell", () => {
    expect(outlineEntries([cell("c", "# a comment", "python")])).toEqual([]);
  });

  it("caps a named cell's level at six", () => {
    expect(outlineEntries([cell("h", "###### Deep"), cell("n", "", "python", "n")])[1].level).toBe(6);
  });
});

describe("OutlinePanel", () => {
  it("jumps to the cell an entry belongs to", async () => {
    const user = userEvent.setup();
    const jumps: string[] = [];
    render(<OutlinePanel cells={cells} activeId="md2" onJump={(id) => jumps.push(id)} />);
    expect(screen.getByRole("navigation", { name: "Outline" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Fit" }));
    await user.click(screen.getByRole("button", { name: "load" }));
    expect(jumps).toEqual(["md2", "load"]);
    expect(screen.getByRole("button", { name: "Model" })).toHaveAttribute("aria-current", "location");
    expect(screen.getByRole("button", { name: "Data" })).not.toHaveAttribute("aria-current");
  });

  it("indents by level", () => {
    render(<OutlinePanel cells={cells} onJump={() => {}} />);
    expect(screen.getByRole("button", { name: "Fit" }).parentElement).toHaveStyle({ paddingLeft: "24px" });
  });

  it("says so when there is nothing to outline", () => {
    render(<OutlinePanel cells={[cell("c", "x = 1", "python")]} onJump={() => {}} />);
    expect(screen.getByText("No headings or named cells")).toBeInTheDocument();
  });
});
