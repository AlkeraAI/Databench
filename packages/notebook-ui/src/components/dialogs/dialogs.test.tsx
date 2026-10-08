import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import type { PlannedStep, RunConfirmation } from "../../model/types";
import { ConfirmRunDialog } from "./ConfirmRunDialog";
import { EnvironmentSwitchDialog } from "./EnvironmentSwitchDialog";
import { RestartDialog } from "./RestartDialog";
import { formatDuration } from "./formatDuration";

describe("formatDuration", () => {
  it.each([
    [850, "850 ms"],
    [0, "0 ms"],
    [-5, "0 ms"],
    [Number.NaN, "0 ms"],
    [999.4, "999 ms"],
    [999.6, "1 s"],
    [1000, "1 s"],
    [1200, "1.2 s"],
    [59_940, "59.9 s"],
    [59_960, "1 min"],
    [125_000, "2 min 5 s"],
    [120_000, "2 min"],
    [3_599_600, "1 h"],
    [3 * 3_600_000 + 12 * 60_000, "3 h 12 min"],
    [3 * 3_600_000 + 12 * 60_000 + 40_000, "3 h 13 min"],
    [26 * 3_600_000, "26 h"],
  ])("formats %d ms as %j", (ms, expected) => {
    expect(formatDuration(ms)).toBe(expected);
  });
});

function step(name: string, reason: PlannedStep["reason"], last_duration_ms: number | null = null): PlannedStep {
  return { cell_id: `id-${name}`, name, reason, last_duration_ms };
}

function confirmation(plan: PlannedStep[], estimate_s = 0): RunConfirmation {
  return { run_id: "r1", plan, estimate_s };
}

describe("ConfirmRunDialog", () => {
  it("names the one expensive step and how long it last took", () => {
    const plan = [step("report", "target"), step("train", "upstream", 3 * 3_600_000 + 12 * 60_000)];
    render(<ConfirmRunDialog confirmation={confirmation(plan, 11_700)} onRun={() => {}} onCancel={() => {}} />);
    const dialog = screen.getByRole("dialog", { name: "Run these cells?" });
    expect(dialog).toHaveTextContent("This also re-runs train (last took 3 h 12 min).");
    expect(dialog).toHaveTextContent("Estimated time: 3 h 15 min");
    expect(screen.getByText("train").tagName).toBe("CODE");
  });

  it("lists several implicit steps and leaves the targets out", () => {
    const plan = [step("report", "target"), step("load", "upstream", 850), step("_", "descendant"), step("plot", "descendant", 125_000)];
    render(<ConfirmRunDialog confirmation={confirmation(plan)} onRun={() => {}} onCancel={() => {}} />);
    const items = screen.getAllByRole("listitem").map((li) => li.textContent);
    expect(items).toEqual(["load (last took 850 ms)", "An unnamed cell", "plot (last took 2 min 5 s)"]);
    expect(screen.queryByText(/Estimated time/)).toBeNull();
    expect(screen.getByRole("dialog")).not.toHaveTextContent("report");
  });

  it("says so when only the targets are costly", () => {
    render(<ConfirmRunDialog confirmation={confirmation([step("q", "target")], 30)} onRun={() => {}} onCancel={() => {}} />);
    expect(screen.getByRole("dialog")).toHaveTextContent("This run may be costly.");
  });

  it("names an unnamed single step in the sentence", () => {
    render(<ConfirmRunDialog confirmation={confirmation([step("_", "upstream")])} onRun={() => {}} onCancel={() => {}} />);
    expect(screen.getByRole("dialog")).toHaveTextContent("This also re-runs an unnamed cell.");
  });

  it("runs on Run, cancels on Cancel and on Escape", async () => {
    const user = userEvent.setup();
    const calls: string[] = [];
    render(<ConfirmRunDialog confirmation={confirmation([step("t", "upstream")])} onRun={() => calls.push("run")} onCancel={() => calls.push("cancel")} />);
    expect(screen.getByRole("button", { name: "Cancel" })).toHaveFocus();
    await user.click(screen.getByRole("button", { name: "Run" }));
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    await user.keyboard("{Escape}");
    expect(calls).toEqual(["run", "cancel", "cancel"]);
  });
});

describe("the dialog frame", () => {
  function Host() {
    const [open, setOpen] = useState(false);
    return (
      <>
        <button type="button" onClick={() => setOpen(true)}>
          Open
        </button>
        {open ? <RestartDialog onConfirm={() => setOpen(false)} onCancel={() => setOpen(false)} /> : null}
      </>
    );
  }

  it("keeps Tab and Shift+Tab inside the dialog", async () => {
    const user = userEvent.setup();
    render(<RestartDialog onConfirm={() => {}} onCancel={() => {}} />);
    const cancel = screen.getByRole("button", { name: "Cancel" });
    const restart = screen.getByRole("button", { name: "Restart" });
    expect(cancel).toHaveFocus();
    await user.tab();
    expect(restart).toHaveFocus();
    await user.tab();
    expect(cancel).toHaveFocus();
    await user.tab({ shift: true });
    expect(restart).toHaveFocus();
  });

  it("is a modal dialog described by its body", () => {
    render(<RestartDialog onConfirm={() => {}} onCancel={() => {}} />);
    const dialog = screen.getByRole("dialog", { name: "Restart the kernel?" });
    expect(dialog).toHaveAttribute("aria-modal", "true");
    expect(dialog).toHaveAccessibleDescription("Restarting clears every variable in memory.");
  });

  it("gives focus back to what opened it", async () => {
    const user = userEvent.setup();
    render(<Host />);
    const opener = screen.getByRole("button", { name: "Open" });
    await user.click(opener);
    expect(screen.getByRole("button", { name: "Cancel" })).toHaveFocus();
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(opener).toHaveFocus();
  });
});

describe("RestartDialog", () => {
  it.each([
    [false, "Restart the kernel?", "Restart"],
    [true, "Restart and run all?", "Restart and run all"],
  ])("with runAll %s asks %j", async (runAll, title, action) => {
    const user = userEvent.setup();
    const calls: string[] = [];
    render(<RestartDialog runAll={runAll} onConfirm={() => calls.push("confirm")} onCancel={() => calls.push("cancel")} />);
    expect(screen.getByRole("dialog", { name: title })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: action }));
    expect(calls).toEqual(["confirm"]);
  });
});

describe("EnvironmentSwitchDialog", () => {
  it("says the kernel restarts and switches on confirm", async () => {
    const user = userEvent.setup();
    const calls: string[] = [];
    render(<EnvironmentSwitchDialog environment="analytics" onConfirm={() => calls.push("switch")} onCancel={() => calls.push("cancel")} />);
    const dialog = screen.getByRole("dialog", { name: "Switch to analytics?" });
    expect(dialog).toHaveTextContent("Switching the environment restarts the kernel.");
    await user.keyboard("{Escape}");
    await user.click(screen.getByRole("button", { name: "Switch" }));
    expect(calls).toEqual(["cancel", "switch"]);
  });
});
