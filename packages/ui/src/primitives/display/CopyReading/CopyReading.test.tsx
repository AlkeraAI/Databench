import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { CopyReading } from "./CopyReading";

// Tests pin the OBSERVABLE contract: the mono value, the copy button's accessible name, the
// clipboard write, the transient confirmation (data-copied + the polite live text) and its revert,
// and the data-attribute axes (precious / size). Pixels need a real browser.

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

function root(): HTMLElement {
  return document.querySelector(".alk-copyreading") as HTMLElement;
}

/** jsdom has no navigator.clipboard — install a stub and return its writeText spy. */
function stubClipboard(): ReturnType<typeof vi.fn> {
  const writeText = vi.fn().mockResolvedValue(undefined);
  Object.defineProperty(window.navigator, "clipboard", {
    value: { writeText },
    configurable: true,
  });
  return writeText;
}

describe("CopyReading value", () => {
  it("renders the value as a mono <code> reading", () => {
    render(<CopyReading value="org_9f2c81" />);
    const val = screen.getByText("org_9f2c81");
    expect(val.tagName).toBe("CODE");
    expect(val).toHaveClass("alk-code", "alk-truncate");
  });

  it("copyLabel overrides the copy button's accessible name", () => {
    render(<CopyReading value="v" copyLabel="Copy org ID" />);
    expect(screen.getByRole("button", { name: "Copy org ID" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Copy" })).toBeNull();
  });
});

describe("CopyReading copy behavior", () => {
  it("writes the value to the clipboard on click", () => {
    const writeText = stubClipboard();
    render(<CopyReading value="postgres://warehouse/main" />);
    fireEvent.click(screen.getByRole("button", { name: "Copy" }));
    expect(writeText).toHaveBeenCalledTimes(1);
    expect(writeText).toHaveBeenCalledWith("postgres://warehouse/main");
  });

  it("shows the confirmation, announces it politely, and reverts after the delay", () => {
    vi.useFakeTimers();
    stubClipboard();
    render(<CopyReading value="v" />);

    // Before copying: no confirmation anywhere.
    expect(root()).not.toHaveAttribute("data-copied");
    const live = document.querySelector(".alk-copyreading__live") as HTMLElement;
    expect(live).toHaveAttribute("aria-live", "polite");
    expect(live).toHaveTextContent("");

    fireEvent.click(screen.getByRole("button", { name: "Copy" }));
    expect(root()).toHaveAttribute("data-copied");
    expect(live).toHaveTextContent("Copied");

    // The confirmation is transient — it reverts on its own.
    act(() => {
      vi.advanceTimersByTime(1500);
    });
    expect(root()).not.toHaveAttribute("data-copied");
    expect(live).toHaveTextContent("");
  });

  it("a second copy while confirmed restarts the revert timer", () => {
    vi.useFakeTimers();
    stubClipboard();
    render(<CopyReading value="v" />);
    const btn = screen.getByRole("button", { name: "Copy" });

    fireEvent.click(btn);
    act(() => {
      vi.advanceTimersByTime(1000);
    });
    fireEvent.click(btn);
    // 1000ms after the SECOND click (2000ms after the first) it must still be confirmed.
    act(() => {
      vi.advanceTimersByTime(1000);
    });
    expect(root()).toHaveAttribute("data-copied");
    act(() => {
      vi.advanceTimersByTime(500);
    });
    expect(root()).not.toHaveAttribute("data-copied");
  });

  it("does not throw when the environment has no clipboard", () => {
    Object.defineProperty(window.navigator, "clipboard", { value: undefined, configurable: true });
    render(<CopyReading value="v" />);
    fireEvent.click(screen.getByRole("button", { name: "Copy" }));
    // Best-effort copy: the confirmation still shows so the interaction never dead-ends.
    expect(root()).toHaveAttribute("data-copied");
  });
});

describe("CopyReading axes", () => {
  it("carries the precious and size axes only when non-default", () => {
    const { rerender } = render(<CopyReading value="v" />);
    expect(root()).not.toHaveAttribute("data-precious");
    expect(root()).not.toHaveAttribute("data-size");
    rerender(<CopyReading value="v" precious size="sm" />);
    expect(root()).toHaveAttribute("data-precious");
    expect(root()).toHaveAttribute("data-size", "sm");
  });

  it("merges a caller className onto the root", () => {
    render(<CopyReading value="v" className="pg-reading" />);
    expect(root()).toHaveClass("alk-copyreading", "pg-reading");
  });
});
