import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";

import { Tabs, type TabItem } from "./Tabs";

const ITEMS: TabItem[] = [
  { key: "running", label: "Running", count: 1 },
  { key: "scheduled", label: "Scheduled", count: 3 },
  { key: "recent", label: "Recent" },
];

function Harness({ initial = "running", onChange }: { initial?: string; onChange?: (k: string) => void }) {
  const [value, setValue] = useState(initial);
  return (
    <Tabs
      items={ITEMS}
      value={value}
      onChange={(k) => {
        setValue(k);
        onChange?.(k);
      }}
      label="Job lifecycle"
    />
  );
}

describe("Tabs", () => {
  it("selects one tab and keeps a single tab stop on it", () => {
    render(<Harness />);
    expect(screen.getByRole("tablist", { name: "Job lifecycle" })).toBeInTheDocument();
    const running = screen.getByRole("tab", { name: /^Running/ });
    expect(running).toHaveAttribute("aria-selected", "true");
    expect(running).toHaveAttribute("tabindex", "0");
    // The count rides the accessible name.
    const scheduled = screen.getByRole("tab", { name: "Scheduled 3" });
    expect(scheduled).toHaveAttribute("aria-selected", "false");
    expect(scheduled).toHaveAttribute("tabindex", "-1");
    expect(screen.getByRole("tab", { name: /^Recent/ })).toHaveAttribute("tabindex", "-1");
  });

  it("selects on click", async () => {
    const onChange = vi.fn();
    render(<Harness onChange={onChange} />);
    await userEvent.click(screen.getByRole("tab", { name: /^Scheduled/ }));
    expect(onChange).toHaveBeenCalledWith("scheduled");
    expect(screen.getByRole("tab", { name: /^Scheduled/ })).toHaveAttribute("aria-selected", "true");
  });

  it("moves selection with ArrowRight/ArrowLeft, wrapping, and Home/End", async () => {
    render(<Harness />);
    const running = screen.getByRole("tab", { name: /^Running/ });
    running.focus();
    await userEvent.keyboard("{ArrowRight}");
    expect(screen.getByRole("tab", { name: /^Scheduled/ })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("tab", { name: /^Scheduled/ })).toHaveFocus();
    await userEvent.keyboard("{ArrowLeft}{ArrowLeft}");
    // wrapped past the first tab to the last
    expect(screen.getByRole("tab", { name: /^Recent/ })).toHaveAttribute("aria-selected", "true");
    await userEvent.keyboard("{Home}");
    expect(screen.getByRole("tab", { name: /^Running/ })).toHaveAttribute("aria-selected", "true");
    await userEvent.keyboard("{End}");
    expect(screen.getByRole("tab", { name: /^Recent/ })).toHaveAttribute("aria-selected", "true");
  });

  it("parks no selection (and no marker) on an unmatched value", () => {
    render(<Tabs items={ITEMS} value="nope" onChange={() => {}} label="Job lifecycle" />);
    for (const tab of screen.getAllByRole("tab")) {
      expect(tab).toHaveAttribute("aria-selected", "false");
    }
    expect(document.querySelector(".alk-tabs__marker")).toBeNull();
  });
});
