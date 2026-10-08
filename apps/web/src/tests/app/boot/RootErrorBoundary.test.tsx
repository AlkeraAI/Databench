import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// The app-root crash surface: it catches a render error, reports it once, and offers recovery. The
// contract pinned here is what the user sees: the two actions sit side-by-side (not stacked), the
// copy stays free of em dashes / unicode ellipses, and sending a report confirms.

const submitCrashReport = vi.fn();
const reportClientError = vi.fn();
vi.mock("@/app/boot/reportClientError", () => ({
  submitCrashReport: (input: unknown) => submitCrashReport(input),
  reportClientError: (...args: unknown[]) => reportClientError(...args),
}));

const { RootErrorBoundary } = await import("@/app/boot/RootErrorBoundary");

function Thrower(): never {
  throw new Error("kaboom");
}

afterEach(cleanup);
beforeEach(() => {
  vi.spyOn(console, "error").mockImplementation(() => {}); // React logs caught render errors
  submitCrashReport.mockReset().mockResolvedValue("crash_123");
  reportClientError.mockReset();
});

describe("RootErrorBoundary", () => {
  const renderCrashed = () =>
    render(
      <RootErrorBoundary>
        <Thrower />
      </RootErrorBoundary>,
    );

  it("shows the crash surface, reports the error once, and puts both actions on one row", () => {
    renderCrashed();
    expect(screen.getByRole("heading", { name: /something went wrong/i })).toBeInTheDocument();
    expect(reportClientError).toHaveBeenCalledTimes(1);

    const send = screen.getByRole("button", { name: /send report/i });
    const reload = screen.getByRole("button", { name: /^reload$/i });
    // Side-by-side, not stacked: both actions share one Inline row.
    const row = send.closest(".alk-inline");
    expect(row).not.toBeNull();
    expect(row).toContainElement(reload);
  });

  it("keeps the copy free of em dashes and unicode ellipses", () => {
    const { container } = renderCrashed();
    expect(container.textContent ?? "").not.toMatch(/[—…]/);
  });

  it("submits a crash report with the typed comment and confirms", async () => {
    renderCrashed();
    fireEvent.change(screen.getByPlaceholderText(/what were you doing/i), { target: { value: "pressed back" } });
    fireEvent.click(screen.getByRole("button", { name: /send report/i }));
    await screen.findByRole("status");
    expect(submitCrashReport).toHaveBeenCalledWith(expect.objectContaining({ message: "kaboom", comment: "pressed back" }));
    expect(screen.getByRole("status")).toHaveTextContent(/your report was sent/i);
  });
});
