// A reader who may not change the permission mode sees the stance and why it
// is fixed for them: the chip is locked with the reason as its title, opens no
// menu, and Shift+Tab does not cycle it — even when the host still hands the
// composer a handler.

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { Composer } from "./Composer";

const MODES = [
  { value: "default", label: "Default", description: "Asks before changes." },
  { value: "plan", label: "Plan", description: "Proposes a plan." },
];
const REASON = "You can view this chat, not change its permission mode.";

afterEach(cleanup);

function mount(modeLockedReason?: string) {
  const onModeChange = vi.fn();
  render(
    <Composer
      modes={MODES}
      mode="default"
      onModeChange={onModeChange}
      modeLockedReason={modeLockedReason}
      models={[{ value: "m1", label: "M1" }]}
      model="m1"
      efforts={[]}
      effort=""
      onSend={vi.fn()}
    />,
  );
  return onModeChange;
}

describe("the mode chip for a reader who may not change it", () => {
  it("is disabled, carries the reason, and opens nothing", () => {
    const onModeChange = mount(REASON);
    const chip = screen.getByRole("button", { name: `Permission mode: Default. ${REASON}` });
    expect(chip).toBeDisabled();
    expect(chip).toHaveAttribute("title", REASON);
    fireEvent.click(chip);
    expect(screen.queryByRole("listbox", { name: "Permission mode" })).toBeNull();
    fireEvent.keyDown(screen.getByRole("textbox"), { key: "Tab", shiftKey: true });
    expect(onModeChange).not.toHaveBeenCalled();
  });

  it("is operable when no reason is given", () => {
    const onModeChange = mount();
    const chip = screen.getByRole("button", { name: "Permission mode: Default" });
    expect(chip).toBeEnabled();
    fireEvent.keyDown(screen.getByRole("textbox"), { key: "Tab", shiftKey: true });
    expect(onModeChange).toHaveBeenCalledWith("plan");
  });
});
