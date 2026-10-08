import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import type { FindMatch } from "../../model/find";
import type { CellOutput, DocCell, NotebookOp } from "../../model/types";
import { FindReplacePanel } from "./FindReplacePanel";

function cell(id: string, source: string, kind = "python"): DocCell {
  return { id, kind, name: "_", source, config: {}, meta: {} };
}

const cells = [cell("a", "total = 1\ntotal += 2"), cell("m", "The total", "markdown"), cell("b", "print(total)")];
const runtime = {
  b: { outputs: [{ output_id: "o", type: "stream", name: "stdout", text: "total 3" }] as CellOutput[] },
};

/** A host that applies replace ops to its cells, as the editor does. */
function Host({ readOnly = false, onReveal }: { readOnly?: boolean; onReveal?: (m: FindMatch) => void }) {
  const [doc, setDoc] = useState(cells);
  const apply = (ops: NotebookOp[]): void => {
    setDoc((current) =>
      current.map((c) => {
        const op = ops.find((o) => o.op === "replace" && o.cell_id === c.id);
        return op && op.op === "replace" ? { ...c, source: op.source } : c;
      }),
    );
  };
  return (
    <>
      <FindReplacePanel cells={doc} runtime={runtime} readOnly={readOnly} onApply={apply} onReveal={onReveal} />
      <pre data-testid="doc">{doc.map((c) => c.source).join("|")}</pre>
    </>
  );
}

function status(): string {
  return screen.getByRole("status").textContent ?? "";
}

describe("FindReplacePanel", () => {
  it("counts matches and steps through them, wrapping both ways", async () => {
    const user = userEvent.setup();
    const revealed: string[] = [];
    render(<Host onReveal={(m) => revealed.push(`${m.cell_id}:${m.from}`)} />);
    expect(status()).toBe("");
    await user.type(screen.getByRole("searchbox", { name: "Find" }), "total");
    expect(status()).toBe("1 of 4");
    await user.click(screen.getByRole("button", { name: "Next" }));
    expect(status()).toBe("2 of 4");
    await user.keyboard("{Enter}");
    expect(status()).toBe("3 of 4");
    await user.click(screen.getByRole("button", { name: "Previous" }));
    await user.click(screen.getByRole("button", { name: "Previous" }));
    await user.click(screen.getByRole("button", { name: "Previous" }));
    expect(status()).toBe("4 of 4");
    screen.getByRole("searchbox", { name: "Find" }).focus();
    await user.keyboard("{Shift>}{Enter}{/Shift}");
    expect(status()).toBe("3 of 4");
    expect(revealed).toEqual(["a:10", "m:4", "a:10", "a:0", "b:6", "m:4"]);
  });

  it("applies the case, whole word and regex toggles", async () => {
    const user = userEvent.setup();
    render(<Host />);
    const find = screen.getByRole("searchbox", { name: "Find" });
    await user.type(find, "Total");
    expect(status()).toBe("1 of 4");
    await user.click(screen.getByRole("button", { name: "Match case" }));
    expect(screen.getByRole("button", { name: "Match case" })).toHaveAttribute("aria-pressed", "true");
    expect(status()).toBe("No results");
    await user.click(screen.getByRole("button", { name: "Match case" }));
    await user.clear(find);
    await user.type(find, "tot");
    await user.click(screen.getByRole("button", { name: "Whole word" }));
    expect(status()).toBe("No results");
    await user.click(screen.getByRole("button", { name: "Whole word" }));
    await user.clear(find);
    await user.type(find, "t.t");
    expect(status()).toBe("No results");
    await user.click(screen.getByRole("button", { name: "Regular expression" }));
    expect(status()).toBe("1 of 4");
  });

  it("shows an invalid regex as an error, not a crash", async () => {
    const user = userEvent.setup();
    render(<Host />);
    await user.click(screen.getByRole("button", { name: "Regular expression" }));
    await user.type(screen.getByRole("searchbox", { name: "Find" }), "(");
    expect(status()).toBe("Invalid regular expression");
    expect(screen.getByRole("searchbox", { name: "Find" })).toHaveAttribute("aria-invalid", "true");
    expect(screen.getByRole("button", { name: "Replace all" })).toBeDisabled();
  });

  it("narrows and widens the scope", async () => {
    const user = userEvent.setup();
    render(<Host />);
    await user.type(screen.getByRole("searchbox", { name: "Find" }), "total");
    await user.click(screen.getByRole("checkbox", { name: "Markdown" }));
    expect(status()).toBe("1 of 3");
    await user.click(screen.getByRole("checkbox", { name: "Outputs" }));
    expect(status()).toBe("1 of 4");
    await user.click(screen.getByRole("checkbox", { name: "Code" }));
    expect(status()).toBe("1 of 1");
  });

  it("replaces the current match, then the next one slides in", async () => {
    const user = userEvent.setup();
    render(<Host />);
    await user.type(screen.getByRole("searchbox", { name: "Find" }), "total");
    await user.type(screen.getByRole("textbox", { name: "Replace" }), "sum");
    await user.click(screen.getByRole("button", { name: "Next" }));
    await user.click(screen.getByRole("button", { name: "Replace" }));
    expect(screen.getByTestId("doc")).toHaveTextContent("total = 1 sum += 2|The total|print(total)");
    expect(status()).toBe("2 of 3");
  });

  it("replaces every source match and leaves outputs alone", async () => {
    const user = userEvent.setup();
    render(<Host />);
    await user.click(screen.getByRole("checkbox", { name: "Outputs" }));
    await user.type(screen.getByRole("searchbox", { name: "Find" }), "total");
    expect(status()).toBe("1 of 5");
    await user.type(screen.getByRole("textbox", { name: "Replace" }), "sum");
    await user.click(screen.getByRole("button", { name: "Replace all" }));
    expect(screen.getByTestId("doc")).toHaveTextContent("sum = 1 sum += 2|The sum|print(sum)");
    expect(status()).toBe("1 of 1");
  });

  it("will not replace an output match", async () => {
    const user = userEvent.setup();
    render(<Host />);
    await user.click(screen.getByRole("checkbox", { name: "Code" }));
    await user.click(screen.getByRole("checkbox", { name: "Markdown" }));
    await user.click(screen.getByRole("checkbox", { name: "Outputs" }));
    await user.type(screen.getByRole("searchbox", { name: "Find" }), "total");
    expect(status()).toBe("1 of 1");
    expect(screen.getByRole("button", { name: "Replace" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Replace all" })).toBeDisabled();
    expect(screen.getByText("Outputs can't be replaced.")).toBeInTheDocument();
  });

  it("finds but never replaces when read-only", async () => {
    const user = userEvent.setup();
    render(<Host readOnly />);
    await user.type(screen.getByRole("searchbox", { name: "Find" }), "total");
    expect(status()).toBe("1 of 4");
    expect(screen.getByRole("textbox", { name: "Replace" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Replace" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Replace all" })).toBeDisabled();
  });

  it("starts from an initial query and closes on Escape", async () => {
    const user = userEvent.setup();
    const closed: boolean[] = [];
    render(<FindReplacePanel cells={cells} runtime={runtime} initialQuery="print" onApply={() => {}} onClose={() => closed.push(true)} />);
    expect(status()).toBe("1 of 1");
    screen.getByRole("button", { name: "Next" }).focus();
    await user.keyboard("{Escape}");
    expect(closed).toEqual([true]);
  });

  it("does nothing on Next with no matches", async () => {
    const user = userEvent.setup();
    const revealed: FindMatch[] = [];
    render(<FindReplacePanel cells={cells} runtime={runtime} initialQuery="absent" onApply={() => {}} onReveal={(m) => revealed.push(m)} />);
    screen.getByRole("searchbox", { name: "Find" }).focus();
    await user.keyboard("{Enter}");
    expect(status()).toBe("No results");
    expect(revealed).toEqual([]);
  });
});
