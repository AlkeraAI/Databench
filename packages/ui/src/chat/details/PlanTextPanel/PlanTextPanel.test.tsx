import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { PlanTextPanel } from "./PlanTextPanel";

describe("PlanTextPanel", () => {
  it("renders the plan Markdown as blocks", () => {
    render(<PlanTextPanel plan={"# Migration plan\n\n- step one\n- step two"} />);
    expect(screen.getByRole("heading", { level: 1, name: "Migration plan" })).toBeInTheDocument();
    const items = screen.getAllByRole("listitem");
    expect(items.map((item) => item.textContent)).toEqual(["step one", "step two"]);
  });

  it("an empty plan renders an empty panel", () => {
    const { container } = render(<PlanTextPanel plan="" />);
    expect(container.querySelector(".alk-planview")).toHaveTextContent("");
  });
});
