import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { CopyButton } from "./CopyButton";

// Pins the OBSERVABLE contract: the label is written to the clipboard on click, the visible label
// HOLDS across the copy (it never swaps to the confirmation), and a polite confirmation is announced
// for a moment then reverts. The icon flip and pixels need a real browser.

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

function stubClipboard(): ReturnType<typeof vi.fn> {
  const writeText = vi.fn().mockResolvedValue(undefined);
  Object.defineProperty(window.navigator, "clipboard", { value: { writeText }, configurable: true });
  return writeText;
}

describe("CopyButton", () => {
  it("holds the visible label and announces the copy, then reverts", () => {
    vi.useFakeTimers();
    const writeText = stubClipboard();
    render(<CopyButton value="curl -fsSL https://x | sh">Copy workflow</CopyButton>);
    const btn = screen.getByRole("button");
    expect(btn).toHaveTextContent("Copy workflow");
    expect(screen.queryByText("Copied")).toBeNull(); // no confirmation at rest

    fireEvent.click(btn);
    expect(writeText).toHaveBeenCalledWith("curl -fsSL https://x | sh");
    expect(btn).toHaveTextContent("Copy workflow"); // label held, NOT swapped to "Copied"
    expect(screen.getByText("Copied")).toBeInTheDocument(); // announced in the live region

    act(() => {
      vi.advanceTimersByTime(1500);
    });
    expect(screen.queryByText("Copied")).toBeNull(); // reverts
    expect(btn).toHaveTextContent("Copy workflow");
  });

  it("a custom copiedLabel is the announcement", () => {
    stubClipboard();
    render(
      <CopyButton value="v" copiedLabel="Copied to clipboard">
        Copy
      </CopyButton>,
    );
    fireEvent.click(screen.getByRole("button"));
    expect(screen.getByText("Copied to clipboard")).toBeInTheDocument();
    expect(screen.getByRole("button")).toHaveTextContent("Copy");
  });

  it("does not throw when the environment has no clipboard", () => {
    Object.defineProperty(window.navigator, "clipboard", { value: undefined, configurable: true });
    render(<CopyButton value="v">Copy</CopyButton>);
    fireEvent.click(screen.getByRole("button"));
    // Best-effort copy: the confirmation still shows so the interaction never dead-ends.
    expect(screen.getByText("Copied")).toBeInTheDocument();
  });
});
