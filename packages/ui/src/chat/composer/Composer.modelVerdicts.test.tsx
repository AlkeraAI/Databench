// Models the open chat may not move to stay in the menu, after the ones it may
// move to, listed under the one line that says why. A row with a way out is a
// control of its own (a new chat on that model), reachable like any other row,
// and never moves the chat; a row with no way out cannot be activated at all.

import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { Composer } from "./Composer";

const GROUP = "This chat has reasoning from M1 that these models can't read. Use them in a new chat.";

afterEach(cleanup);

function mount(hint?: string) {
  const onModelChange = vi.fn();
  const onEscape = vi.fn();
  render(
    <Composer
      modes={[{ value: "default", label: "Default" }]}
      mode="default"
      models={[
        { value: "m1", label: "M1" },
        { value: "m3", label: "M3" },
        {
          value: "m2",
          label: "M2",
          unavailable: GROUP,
          escape: { label: "Start a new chat with M2", onSelect: onEscape },
        },
        { value: "m4", label: "M4", unavailable: GROUP, escape: { label: "Start a new chat with M4", onSelect: vi.fn() } },
        { value: "m5", label: "M5", unavailable: "Not available in this workspace." },
      ]}
      model="m1"
      onModelChange={onModelChange}
      modelHint={hint}
      efforts={[]}
      effort=""
      onSend={vi.fn()}
    />,
  );
  fireEvent.click(screen.getByRole("button", { name: "Model: M1" }));
  return { onModelChange, onEscape, menu: screen.getByRole("listbox", { name: "Model" }) };
}

describe("models the chat may not move to", () => {
  it("are listed under their reason once, not each repeating it", () => {
    const { menu } = mount();

    expect(within(menu).getAllByText(GROUP)).toHaveLength(1);
    expect(within(menu).getAllByText("Not available in this workspace.")).toHaveLength(1);
  });

  it("offer the way out as a row of their own that never moves the chat", () => {
    const { onModelChange, onEscape, menu } = mount();
    const way = within(menu).getByRole("option", { name: "Start a new chat with M2" });

    expect(way.tagName).toBe("BUTTON");
    expect(way).not.toHaveAttribute("aria-disabled");
    fireEvent.click(way);

    expect(onEscape).toHaveBeenCalledOnce();
    expect(onModelChange).not.toHaveBeenCalled();
  });

  it("reach the way out from the keyboard", () => {
    const { menu } = mount();
    const rows = within(menu).getAllByRole("option");
    const way = within(menu).getByRole("option", { name: "Start a new chat with M2" });

    for (let i = 0; i < rows.indexOf(way); i += 1) fireEvent.keyDown(menu, { key: "ArrowDown" });

    expect(document.activeElement).toBe(way);
  });

  it("cannot activate a row that has no way out", () => {
    const { onModelChange, menu } = mount();
    const row = within(menu).getByText("M5").closest('[role="option"]') as HTMLElement;

    expect(row).toHaveAttribute("aria-disabled", "true");
    fireEvent.click(row);
    expect(onModelChange).not.toHaveBeenCalled();
  });

  it("leave an allowed model pickable beside them", () => {
    const { onModelChange, menu } = mount();

    fireEvent.click(within(menu).getByRole("option", { name: /M3/ }));

    expect(onModelChange).toHaveBeenCalledWith("m3");
  });

  it("show the host's note under the menu", () => {
    mount("Applies after the chat restarts.");

    expect(screen.getByText("Applies after the chat restarts.")).toBeTruthy();
  });
});
