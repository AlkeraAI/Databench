import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { StatusPill, statusIsResting, statusNeedsSaying, type StatusFactData } from "./StatusPill";

afterEach(cleanup);

const stalled: StatusFactData = {
  subject: "chat",
  state: "stalled",
  label: "Stalled",
  tone: "warning",
  reason_code: "turn_silent",
  sentence: "The agent has stopped reporting progress.",
  since: "2026-10-06T19:00:00Z",
  action: null,
};

const stopped: StatusFactData = {
  subject: "machine",
  state: "stopped",
  label: "Stopped",
  tone: "muted",
  sentence: "lab-b is stopped. It stopped responding.",
  action: { kind: "replace_machine", label: "Start on new hardware" },
};

describe("StatusPill", () => {
  it("draws the server's label, with its sentence as the tooltip", () => {
    render(<StatusPill status={stalled} />);
    const pill = screen.getByText("Stalled").closest(".alk-pill") as HTMLElement;
    expect(pill).toHaveAttribute("title", "The agent has stopped reporting progress.");
    expect(pill).toHaveAttribute("data-tone", "warning");
    expect(pill).toHaveAttribute("data-state", "stalled");
    expect(pill).toHaveAttribute("data-subject", "chat");
  });

  it("draws nothing for a thing with no status", () => {
    const { container, rerender } = render(<StatusPill status={null} />);
    expect(container).toBeEmptyDOMElement();
    rerender(<StatusPill status={undefined} variant="line" />);
    expect(container).toBeEmptyDOMElement();
  });

  it.each([
    ["neutral", "neutral"],
    ["info", "info"],
    ["success", "success"],
    ["warning", "warning"],
    ["danger", "danger"],
    ["muted", "catNeutral"],
    ["a-tone-from-a-newer-server", "neutral"],
  ])("draws tone %s as the %s pill", (tone, pillTone) => {
    render(<StatusPill status={{ ...stalled, tone }} />);
    expect(screen.getByText("Stalled").closest(".alk-pill")).toHaveAttribute("data-tone", pillTone);
  });

  it("draws a state it has never heard of exactly as it draws a known one", () => {
    render(
      <StatusPill
        status={{ ...stalled, state: "defragmenting", label: "Tidying up", sentence: "Tidying up the disk." }}
      />,
    );
    expect(screen.getByText("Tidying up").closest(".alk-pill")).toHaveAttribute("title", "Tidying up the disk.");
  });
});

describe("StatusPill dot", () => {
  it("names the mark with the label and explains it with the sentence", () => {
    render(<StatusPill status={stalled} variant="dot" />);
    const mark = screen.getByRole("img", { name: "Stalled" });
    expect(mark).toHaveAttribute("title", "The agent has stopped reporting progress.");
    expect(mark).toHaveTextContent("");
  });

  it.each([
    ["muted", false],
    ["success", false],
    ["info", true],
    ["neutral", true],
    ["warning", true],
    ["danger", true],
  ])("in a quiet list, tone %s is drawn: %s", (tone, drawn) => {
    render(<StatusPill status={{ ...stalled, tone }} variant="dot" quiet />);
    expect(screen.queryByRole("img", { name: "Stalled" }) !== null).toBe(drawn);
  });

  it("draws a resting status where the list is not quiet", () => {
    render(<StatusPill status={{ ...stalled, tone: "muted" }} variant="dot" />);
    expect(screen.getByRole("img", { name: "Stalled" })).toBeInTheDocument();
  });
});

describe("StatusPill line", () => {
  it("says the label and the sentence as a live status", () => {
    render(<StatusPill status={stalled} variant="line" />);
    const line = screen.getByRole("status");
    expect(line).toHaveTextContent("Stalled");
    expect(line).toHaveTextContent("The agent has stopped reporting progress.");
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("offers the action the host can perform, in the server's words", () => {
    const onAction = vi.fn();
    render(<StatusPill status={stopped} variant="line" actions={["replace_machine"]} onAction={onAction} />);
    fireEvent.click(screen.getByRole("button", { name: "Start on new hardware" }));
    expect(onAction.mock.calls).toEqual([["replace_machine"]]);
  });

  it("offers no button for an action kind the host cannot perform", () => {
    render(<StatusPill status={stopped} variant="line" actions={["add_credits"]} onAction={vi.fn()} />);
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("offers no button with nothing to handle it", () => {
    render(<StatusPill status={stopped} variant="line" actions={["replace_machine"]} />);
    expect(screen.queryByRole("button")).toBeNull();
  });
});

describe("what a surface may ask of a status", () => {
  it.each([
    ["muted", true],
    ["success", true],
    ["info", false],
    ["neutral", false],
    ["warning", false],
    ["danger", false],
  ])("tone %s is resting: %s", (tone, resting) => {
    expect(statusIsResting({ ...stalled, tone })).toBe(resting);
  });

  it("nothing at all is resting, and needs nothing said", () => {
    expect(statusIsResting(null)).toBe(true);
    expect(statusNeedsSaying(undefined)).toBe(false);
  });

  it.each([
    ["info", "", false],
    ["success", "", false],
    ["muted", "", false],
    ["neutral", "", false],
    ["info", "machine_starting", true],
    ["success", "machine_draining", true],
    ["warning", "", true],
    ["danger", "", true],
  ])("tone %s with reason %j needs saying: %s", (tone, reason_code, needs) => {
    expect(statusNeedsSaying({ ...stalled, tone, reason_code })).toBe(needs);
  });
});
