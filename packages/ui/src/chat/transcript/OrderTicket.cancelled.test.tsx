// A message the box took and then dropped — a Stop emptied the lane it was
// waiting in. The words stay on screen, because they were the transcript's the
// moment the server took them; what changes is that the ticket now says what
// became of them, and offers the one thing the reader wants: sending them again.

import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { OrderTicket } from "./OrderTicket";

const AT = "12:04 AM";
const SAID = "Deploy the staging stack";

describe("a message that was never sent", () => {
  it("says so under the reader's own words, which stay as they were", () => {
    render(<OrderTicket text={SAID} at={AT} cancelled={{ note: "Not sent (stopped)" }} />);
    expect(screen.getByText(SAID)).toBeInTheDocument();
    expect(screen.getByText("Not sent (stopped)")).toBeInTheDocument();
  });

  it("carries no time stamp: the turn it would name never started", () => {
    const { container } = render(
      <OrderTicket text={SAID} at={AT} cancelled={{ note: "Not sent (stopped)" }} />,
    );
    expect(container.querySelector(".chat-ticket__at")).toBeNull();
    expect(screen.queryByText(AT)).toBeNull();
  });

  it("keeps its stamp and says nothing when the message WAS sent", () => {
    // The asymmetry that proves the line is driven by the cancellation rather
    // than printed on every ticket.
    const { container } = render(<OrderTicket text={SAID} at={AT} />);
    expect(container.querySelector(".chat-ticket__at")).toHaveTextContent(AT);
    expect(screen.queryByRole("button", { name: /resend/i })).toBeNull();
    expect(screen.queryByText(/not sent/i)).toBeNull();
  });

  it("offers a Resend that puts the reader's original words back through", async () => {
    const onResend = vi.fn();
    render(<OrderTicket text={SAID} at={AT} cancelled={{ note: "Not sent (stopped)", onResend }} />);
    await userEvent.click(screen.getByRole("button", { name: "Resend" }));
    expect(onResend).toHaveBeenCalledTimes(1);
  });

  it("states the fact and offers no control where the shell cannot send", () => {
    // A button that answers a click with nothing is worse than no button.
    render(<OrderTicket text={SAID} at={AT} cancelled={{ note: "Not sent" }} />);
    expect(screen.getByText("Not sent")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /resend/i })).toBeNull();
  });

  it("is not also reading as still going out", () => {
    // `pending` (taken, not yet picked up) and `cancelled` (taken, never run)
    // are opposite ends of the same message; a ticket must not claim both.
    const { container } = render(
      <OrderTicket text={SAID} at={AT} cancelled={{ note: "Not sent (stopped)" }} />,
    );
    const ticket = container.querySelector(".chat-ticket");
    expect(ticket).toHaveAttribute("data-cancelled");
    expect(ticket).not.toHaveAttribute("data-pending");
  });
});
