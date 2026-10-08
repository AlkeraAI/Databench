import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { DateInput } from "./DateInput";

// Tests for the @alkera/ui DateInput — a custom single-day picker (trigger + portaled calendar
// dialog), never the browser's native date control. These pin the observable contract: the
// trigger's formatted reading and placeholder state, the calendar's month/selection, the value
// round-trip as `YYYY-MM-DD`, clearing, and the keyboard grid.

afterEach(cleanup);

describe("DateInput — trigger reading", () => {
  it("formats a picked day and falls back to the placeholder", () => {
    const { rerender } = render(<DateInput aria-label="From date" value="2026-06-01" />);
    const btn = () => screen.getByRole("button", { name: "From date" });
    expect(btn()).toHaveTextContent("Jun 1, 2026");
    expect(btn()).not.toHaveAttribute("data-placeholder");

    rerender(<DateInput aria-label="From date" value="" placeholder="Any date" />);
    expect(btn()).toHaveTextContent("Any date");
    expect(btn()).toHaveAttribute("data-placeholder");
  });

  it("carries data-size only for a non-default size", () => {
    render(<DateInput aria-label="From date" size="sm" />);
    expect(screen.getByRole("button", { name: "From date" })).toHaveAttribute("data-size", "sm");
    cleanup();
    render(<DateInput aria-label="From date" />);
    expect(screen.getByRole("button", { name: "From date" })).not.toHaveAttribute("data-size");
  });
});

describe("DateInput — calendar", () => {
  it("opens on the value's month and reports the clicked day as ISO", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<DateInput aria-label="From date" value="2026-06-15" onChange={onChange} />);
    await user.click(screen.getByRole("button", { name: "From date" }));
    expect(screen.getByRole("dialog")).toHaveTextContent("June 2026");
    expect(screen.getByRole("button", { name: "June 15, 2026" })).toHaveAttribute("aria-pressed", "true");
    await user.click(screen.getByRole("button", { name: "June 1, 2026" }));
    expect(onChange).toHaveBeenCalledWith("2026-06-01");
  });

  it("month navigation turns the page without committing anything", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<DateInput aria-label="From date" value="2026-06-15" onChange={onChange} />);
    await user.click(screen.getByRole("button", { name: "From date" }));
    await user.click(screen.getByRole("button", { name: "Previous month" }));
    expect(screen.getByRole("dialog")).toHaveTextContent("May 2026");
    await user.click(screen.getByRole("button", { name: "Next month" }));
    await user.click(screen.getByRole("button", { name: "Next month" }));
    expect(screen.getByRole("dialog")).toHaveTextContent("July 2026");
    expect(onChange).not.toHaveBeenCalled();
  });

  it("uncontrolled: picking updates its own reading", async () => {
    const user = userEvent.setup();
    render(<DateInput aria-label="From date" defaultValue="2026-06-15" />);
    await user.click(screen.getByRole("button", { name: "From date" }));
    await user.click(screen.getByRole("button", { name: "June 2, 2026" }));
    expect(screen.getByRole("button", { name: "From date" })).toHaveTextContent("Jun 2, 2026");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });
});

describe("DateInput — clearing", () => {
  it("the clear control resets to unset without opening the calendar", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<DateInput aria-label="From date" value="2026-06-15" onChange={onChange} />);
    await user.click(screen.getByRole("button", { name: "Clear date" }));
    expect(onChange).toHaveBeenCalledWith("");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("no clear control while unset, when disabled, or with clearable off", () => {
    render(<DateInput aria-label="A" value="" />);
    render(<DateInput aria-label="B" value="2026-06-15" disabled />);
    render(<DateInput aria-label="C" value="2026-06-15" clearable={false} />);
    expect(screen.queryByRole("button", { name: "Clear date" })).not.toBeInTheDocument();
  });
});

describe("DateInput — keyboard grid", () => {
  it("arrows walk days and cross the month edge; Enter picks the focused day", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<DateInput aria-label="From date" value="2026-06-01" onChange={onChange} />);
    await user.click(screen.getByRole("button", { name: "From date" }));
    // Focus follows the dialog in, seated on the selected day.
    expect(screen.getByRole("button", { name: "June 1, 2026" })).toHaveFocus();
    await user.keyboard("{ArrowRight}");
    expect(screen.getByRole("button", { name: "June 2, 2026" })).toHaveFocus();
    await user.keyboard("{ArrowUp}");
    expect(screen.getByRole("dialog")).toHaveTextContent("May 2026");
    expect(screen.getByRole("button", { name: "May 26, 2026" })).toHaveFocus();
    await user.keyboard("{Enter}");
    expect(onChange).toHaveBeenCalledWith("2026-05-26");
  });

  it("Tab leaves the calendar and returns focus to the field", async () => {
    const user = userEvent.setup();
    render(<DateInput aria-label="From date" value="2026-06-15" />);
    await user.click(screen.getByRole("button", { name: "From date" }));
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    await user.tab();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "From date" })).toHaveFocus();
  });

  it("a malformed value renders as unset instead of crashing", () => {
    render(<DateInput aria-label="From date" value="2026-13-45" />);
    expect(screen.getByRole("button", { name: "From date" })).toHaveAttribute("data-placeholder");
  });
});
