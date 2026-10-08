import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  clockTime,
  machineStateLabel,
  machineStateTone,
  startingLine,
  stockLabel,
  stockTone,
  waitLine,
  MACHINE_RATE_FORMAT,
  PROVIDER_LABELS,
} from "./format";
import { MachineCardView, machineSpecLine, type MachineCardData } from "./MachineCardView";

// The product registers its cloud providers' names; these fixtures run on them.
PROVIDER_LABELS.register({ key: "ec2", label: "EC2" });
PROVIDER_LABELS.register({ key: "runpod", label: "RunPod" });
// A product that prices machines registers how a rate reads; this stands in for one.
MACHINE_RATE_FORMAT.register({ key: "test", format: (nanos) => `${nanos} n/min` });

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

const NOW = Date.parse("2026-10-05T12:00:00Z");

function card(overrides: Partial<MachineCardData> = {}): MachineCardData {
  return {
    kind: "org_machine",
    org_machine_id: "om-1",
    name: "A100 Lab",
    spec: {
      offering_name: "A100 80 GB",
      provider: "runpod",
      region: "US-TX",
      gpu: { name: "A100", count: 1, memory_gb: 80 },
      vcpu: 16,
      memory_gb: 125,
      disk_gb: 200,
      rate_per_minute_nanos: 31_500_000,
      storage_rate_per_minute_nanos: 100_000,
    },
    state: "running",
    step: null,
    step_started_at: null,
    step_expected_seconds: null,
    stop_reason: "",
    ...overrides,
  };
}

describe("spec line", () => {
  it("names the GPU, the hardware, the place, the rate and the state", () => {
    render(<MachineCardView card={card()} now={NOW} />);
    expect(screen.getByText("A100 Lab")).toBeInTheDocument();
    expect(screen.getByText("1x A100 80 GB · 16 vCPU · 125 GB memory · 200 GB disk · RunPod US-TX · 31500000 n/min")).toBeInTheDocument();
    expect(screen.getByText("Running")).toBeInTheDocument();
  });

  it("leaves out a GPU the machine does not have and a rate the reader may not see", () => {
    const cpu = card({
      spec: { ...card().spec!, gpu: null, rate_per_minute_nanos: null, provider: "ec2", region: "us-east-1" },
    });
    render(<MachineCardView card={cpu} now={NOW} />);
    expect(screen.getByText("16 vCPU · 125 GB memory · 200 GB disk · EC2 us-east-1")).toBeInTheDocument();
    expect(screen.queryByText(/n\/min/)).toBeNull();
    expect(screen.queryByText(/x A100/)).toBeNull();
  });

  it("does not repeat a GPU memory the name already carries", () => {
    const named = card({ spec: { ...card().spec!, gpu: { name: "A100 80GB PCIe", count: 2, memory_gb: 80 } } });
    expect(machineSpecLine(named, NOW)).toMatch(/^2x A100 80GB PCIe · 16 vCPU/);
  });

  it("renders the shared machines by name only", () => {
    render(<MachineCardView card={{ kind: "shared", name: "Shared machines", spec: null, state: "shared" }} now={NOW} />);
    expect(screen.getByText("Shared machines")).toBeInTheDocument();
    expect(document.querySelector(".alk-pill")).toBeNull();
    expect(document.querySelector(".alk-machine__spec")).toBeNull();
  });
});

describe("state", () => {
  it.each([
    ["running", "Running", "success"],
    ["unhealthy", "Can't run chats", "danger"],
    ["starting", "Starting", "neutral"],
    ["stopping", "Stopping", "neutral"],
    ["stopped", "Stopped", "catNeutral"],
    ["waiting_for_hardware", "Waiting for hardware", "warning"],
    ["failed", "Couldn't start", "danger"],
    ["unreachable", "Not responding", "danger"],
  ])("%s reads %s in the %s tone", (state, label, tone) => {
    render(<MachineCardView card={card({ state })} now={NOW} />);
    const pill = document.querySelector(".alk-pill") as HTMLElement;
    expect(pill).toHaveTextContent(label);
    expect(pill.dataset.tone).toBe(tone);
    expect(machineStateLabel(state)).toBe(label);
    expect(machineStateTone(state)).toBe(tone);
  });

  it.each([
    ["running", true, "Almost out of disk", "warning"],
    ["running", false, "Running", "success"],
    // A fault outranks a full disk: the machine cannot run chats either way.
    ["unhealthy", true, "Can't run chats", "danger"],
    ["unreachable", true, "Not responding", "danger"],
  ])("a %s machine whose disk the server calls full (%s) reads %s", (state, full, label, tone) => {
    const nearlyFull = card({ state, disk_full: full });
    render(<MachineCardView card={nearlyFull} now={NOW} />);
    const pill = document.querySelector(".alk-pill") as HTMLElement;
    expect(pill).toHaveTextContent(label);
    expect(pill.dataset.tone).toBe(tone);
    expect(machineSpecLine(nearlyFull, NOW).endsWith(label)).toBe(true);
  });

  it("reads an unknown state as itself", () => {
    expect(machineStateLabel("hibernating")).toBe("hibernating");
    expect(machineStateTone("hibernating")).toBe("neutral");
  });

  it("shows the starting step with the minutes left and a progress bar", () => {
    const starting = card({
      state: "starting",
      step: "installing",
      // 60 s into a 240 s step: three minutes left, a quarter done.
      step_started_at: new Date(NOW - 60_000).toISOString(),
      step_expected_seconds: 240,
    });
    render(<MachineCardView card={starting} now={NOW} />);
    expect(screen.getByText("Installing the runtime · about 3 min")).toBeInTheDocument();
    expect(screen.getByRole("progressbar")).toHaveAttribute("aria-valuenow", "25");
  });

  it("never reads zero minutes or a full bar for a step running long", () => {
    const late = new Date(NOW - 600_000).toISOString();
    expect(startingLine("booting", late, 90, NOW)).toBe("Booting · about 1 min");
    render(
      <MachineCardView
        card={card({ state: "starting", step: "booting", step_started_at: late, step_expected_seconds: 90 })}
        now={NOW}
      />,
    );
    expect(screen.getByRole("progressbar")).toHaveAttribute("aria-valuenow", "95");
  });

  it("shows the step alone without timings, and no bar", () => {
    render(<MachineCardView card={card({ state: "starting", step: "reserving" })} now={NOW} />);
    expect(screen.getByText("Reserving hardware")).toBeInTheDocument();
    expect(screen.queryByRole("progressbar")).toBeNull();
  });

  it("shows no step for a machine that is not starting", () => {
    render(<MachineCardView card={card({ state: "stopped", step: "installing", step_expected_seconds: 240 })} now={NOW} />);
    expect(screen.queryByText(/Installing the runtime/)).toBeNull();
  });
});

describe("compact", () => {
  it("draws the name and a state dot, with the spec line in a tooltip", () => {
    vi.useFakeTimers();
    render(<MachineCardView card={card()} variant="compact" now={NOW} />);
    const chip = screen.getByRole("group", { name: "A100 Lab, Running" });
    expect(chip.querySelector(".alk-machine__dot")?.getAttribute("data-tone")).toBe("success");
    expect(screen.queryByText(/16 vCPU/)).toBeNull();
    fireEvent.focus(chip);
    act(() => {
      vi.advanceTimersByTime(400);
    });
    expect(screen.getByRole("tooltip")).toHaveTextContent(
      "1x A100 80 GB · 16 vCPU · 125 GB memory · 200 GB disk · RunPod US-TX · 31500000 n/min · Running",
    );
  });

  it("is a button when it opens something, and says what is happening to it", () => {
    const onClick = vi.fn();
    render(<MachineCardView card={card()} variant="compact" now={NOW} onClick={onClick} status="Saving chats" />);
    const button = screen.getByRole("button", { name: "A100 Lab, Saving chats" });
    expect(button).toHaveTextContent("Saving chats");
    fireEvent.click(button);
    expect(onClick).toHaveBeenCalledTimes(1);
  });

  it("draws the shared machines with no dot and no tooltip", () => {
    render(<MachineCardView card={{ kind: "shared", name: "Standard", state: "shared" }} variant="compact" />);
    expect(document.querySelector(".alk-machine__dot")).toBeNull();
    expect(screen.getByText("Standard")).toBeInTheDocument();
  });

  it.each([
    ["an org machine", card()],
    ["the shared machines", { kind: "shared", name: "Standard", state: "shared" } as const],
  ])("marks %s with a machine glyph", (_what, c) => {
    render(<MachineCardView card={c} variant="compact" now={NOW} />);
    expect(document.querySelector(".alk-machine--compact .alk-machine__icon")).not.toBeNull();
  });
});

describe("hardware only", () => {
  it("leaves the rate and the state to the table's other columns", () => {
    render(<MachineCardView card={card()} facts="hardware" now={NOW} />);
    expect(screen.getByText("1x A100 80 GB · 16 vCPU · 125 GB memory · 200 GB disk · RunPod US-TX")).toBeInTheDocument();
    expect(document.querySelector(".alk-pill")).toBeNull();
  });
});

describe("rate without a state", () => {
  it("names the rate but no state, for a machine not bought yet", () => {
    render(<MachineCardView card={card({ state: "stopped" })} facts="rate" now={NOW} />);
    expect(screen.getByText("1x A100 80 GB · 16 vCPU · 125 GB memory · 200 GB disk · RunPod US-TX · 31500000 n/min")).toBeInTheDocument();
    expect(screen.queryByText("Stopped")).toBeNull();
    expect(document.querySelector(".alk-pill")).toBeNull();
    expect(document.querySelector("[data-state]")).toBeNull();
  });
});

describe("a card that carries the server's status", () => {
  const overdue = {
    subject: "machine",
    state: "starting",
    label: "Starting",
    tone: "info",
    reason_code: "installing_overdue",
    sentence: "Installing Alkera is taking longer than usual.",
  };

  it("draws the server's words, not an estimate this side worked out", () => {
    // Fourteen minutes into a step expected to take four: a local estimate would
    // read "about 1 min" for as long as it lasted.
    render(
      <MachineCardView
        card={card({
          state: "starting",
          step: "installing",
          step_started_at: new Date(NOW - 14 * 60_000).toISOString(),
          step_expected_seconds: 240,
          status: overdue,
        })}
        now={NOW}
      />,
    );
    expect(screen.getByText("Installing Alkera is taking longer than usual.")).toBeInTheDocument();
    expect(screen.queryByText(/about \d+ min/)).toBeNull();
  });

  it("draws a state this build has no word for in the server's word", () => {
    render(
      <MachineCardView
        card={card({
          state: "hibernating",
          status: { subject: "machine", state: "hibernating", label: "Hibernating", tone: "muted", sentence: "A Lab is hibernating." },
        })}
        now={NOW}
      />,
    );
    expect(screen.getByText("Hibernating")).toBeInTheDocument();
  });
});

describe("waiting for hardware", () => {
  const wait = {
    reason: "RunPod has no 8 vCPU hosts right now.",
    next_try_at: "2026-10-06T19:47:00Z",
    gives_up_at: "2026-10-06T20:12:00Z",
  };

  it("says the server's reason and when it is tried again", () => {
    expect(waitLine(wait)).toBe(`RunPod has no 8 vCPU hosts right now. Trying again at ${clockTime(wait.next_try_at)}.`);
    render(<MachineCardView card={card({ state: "waiting_for_hardware", wait })} now={NOW} />);
    expect(screen.getByText(waitLine(wait))).toBeInTheDocument();
  });

  it("says until when a start is retried every pass", () => {
    expect(waitLine({ ...wait, next_try_at: null })).toBe(
      `RunPod has no 8 vCPU hosts right now. Retrying until ${clockTime(wait.gives_up_at)}.`,
    );
  });

  it("says nothing of a wait on a machine that runs", () => {
    render(<MachineCardView card={card({ state: "running", wait })} now={NOW} />);
    expect(screen.queryByText(waitLine(wait))).toBeNull();
  });
});

describe("stock words", () => {
  it.each([
    ["in_stock", "In stock", "success"],
    ["limited", "Limited", "warning"],
    ["out_of_stock", "Out of stock", "danger"],
    ["unknown", "Stock unknown", "neutral"],
  ])("%s reads %s in the %s tone", (state, label, tone) => {
    expect(stockLabel(state)).toBe(label);
    expect(stockTone(state)).toBe(tone);
  });
});
