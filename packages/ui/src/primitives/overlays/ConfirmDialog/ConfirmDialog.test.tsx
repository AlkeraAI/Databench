import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useRef } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ConfirmDialog, type ConfirmDialogProps } from "./ConfirmDialog";

// Tests for ConfirmDialog — the one confirmation surface every setting that prompts goes through.
// They assert the contract a caller depends on: nothing happens until the person confirms; cancel,
// Escape and the scrim leave the caller untouched; Enter is a shortcut only where confirming is
// safe; the typed gate actually gates; and focus opens where the tone says it should.

afterEach(cleanup);

function setup(props: Partial<ConfirmDialogProps> = {}) {
  const onConfirm = vi.fn();
  const onClose = vi.fn();
  const utils = render(
    <ConfirmDialog
      open
      onClose={onClose}
      onConfirm={onConfirm}
      title="Disable Google sign-in?"
      confirmLabel="Disable"
      {...props}
    />,
  );
  return { onConfirm, onClose, ...utils };
}

describe("ConfirmDialog", () => {
  it("renders the fact as the dialog's accessible name, with the consequence beneath it", () => {
    setup({ consequence: "Members who sign in with Google lose access." });
    const dialog = screen.getByRole("dialog", { name: "Disable Google sign-in?" });
    expect(dialog).toHaveTextContent("Members who sign in with Google lose access.");
  });

  it("renders no dialog while closed", () => {
    setup({ open: false });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("does nothing until the primary action is pressed", async () => {
    const user = userEvent.setup();
    const { onConfirm, onClose } = setup();
    expect(onConfirm).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Disable" }));
    expect(onConfirm).toHaveBeenCalledTimes(1);
    expect(onClose).not.toHaveBeenCalled();
  });

  it("cancel closes without confirming", async () => {
    const user = userEvent.setup();
    const { onConfirm, onClose } = setup();
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(onConfirm).not.toHaveBeenCalled();
  });

  it.each([
    { tone: "default" as const },
    { tone: "warning" as const },
    { tone: "destructive" as const },
  ])("Escape closes a $tone dialog without confirming", async ({ tone }) => {
    const user = userEvent.setup();
    const { onConfirm, onClose } = setup({ tone });
    await user.keyboard("{Escape}");
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(onConfirm).not.toHaveBeenCalled();
  });

  it.each([
    { tone: "default" as const, variant: "primary", fill: "filled" },
    { tone: "warning" as const, variant: "destructive", fill: "outline" },
    { tone: "destructive" as const, variant: "destructive", fill: "filled" },
  ])("a $tone tone renders a $variant/$fill primary action", ({ tone, variant, fill }) => {
    setup({ tone });
    const confirm = screen.getByRole("button", { name: "Disable" });
    expect(confirm).toHaveAttribute("data-variant", variant);
    expect(confirm).toHaveAttribute("data-fill", fill);
  });

  it("focus opens on the action, and on cancel when the tone is destructive", async () => {
    const { unmount } = setup({ tone: "default" });
    await waitFor(() => expect(screen.getByRole("button", { name: "Disable" })).toHaveFocus());
    unmount();

    setup({ tone: "destructive" });
    await waitFor(() => expect(screen.getByRole("button", { name: "Cancel" })).toHaveFocus());
  });

  // Enter where the dialog opened focus. A non-destructive dialog opens on its action, so the key
  // confirms; a destructive one opens on Cancel, where Enter does nothing at all — a person who
  // answers one prompt and keeps typing must not lose the next question to the same keystroke.
  it.each([
    { tone: "default" as const, confirms: 1, closes: 0 },
    { tone: "warning" as const, confirms: 1, closes: 0 },
    { tone: "destructive" as const, confirms: 0, closes: 0 },
  ])("Enter on a $tone dialog confirms $confirms time(s)", async ({ tone, confirms, closes }) => {
    const user = userEvent.setup();
    const { onConfirm, onClose } = setup({ tone });
    await waitFor(() => expect(document.activeElement).toHaveClass("alk-btn"));
    await user.keyboard("{Enter}");
    expect(onConfirm).toHaveBeenCalledTimes(confirms);
    expect(onClose).toHaveBeenCalledTimes(closes);
  });

  it("a destructive dialog stays open after Enter on its focused Cancel", async () => {
    const user = userEvent.setup();
    setup({ tone: "destructive", consequence: "It cannot be brought back." });
    await waitFor(() => expect(document.activeElement).toHaveClass("alk-btn"));
    await user.keyboard("{Enter}{Enter}");
    expect(screen.getByRole("dialog", { name: "Disable Google sign-in?" })).toBeInTheDocument();
  });

  // Swallowing the key must not cost the button its job: the same Cancel still cancels
  // when it is pressed, which is the only way out a keyboard has besides Escape.
  it("Cancel on a destructive dialog still closes when it is pressed", async () => {
    const user = userEvent.setup();
    const { onClose, onConfirm } = setup({ tone: "destructive" });
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(onConfirm).not.toHaveBeenCalled();
  });

  it("Escape still closes a destructive dialog whose Cancel has focus", async () => {
    const user = userEvent.setup();
    const { onClose } = setup({ tone: "destructive" });
    await waitFor(() => expect(document.activeElement).toHaveClass("alk-btn"));
    await user.keyboard("{Escape}");
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  // Enter from inside the body — the typed field is the one focusable body control — goes through
  // the dialog's own handler rather than a button's native activation.
  it("Enter in the typed field confirms a warning dialog once the word matches", async () => {
    const user = userEvent.setup();
    const { onConfirm } = setup({ tone: "warning", requireTyped: "Google" });
    await user.type(screen.getByLabelText("Type Google to confirm"), "Google{Enter}");
    expect(onConfirm).toHaveBeenCalledTimes(1);
  });

  it("Enter in the typed field never confirms a destructive dialog", async () => {
    const user = userEvent.setup();
    const { onConfirm } = setup({ tone: "destructive", requireTyped: "Google" });
    await user.type(screen.getByLabelText("Type Google to confirm"), "Google{Enter}");
    expect(onConfirm).not.toHaveBeenCalled();
  });

  it("Enter in the typed field does nothing before the word matches", async () => {
    const user = userEvent.setup();
    const { onConfirm } = setup({ requireTyped: "Google" });
    await user.type(screen.getByLabelText("Type Google to confirm"), "nope{Enter}");
    expect(onConfirm).not.toHaveBeenCalled();
  });

  it("the typed gate holds the primary action until the exact word is typed", async () => {
    const user = userEvent.setup();
    const { onConfirm } = setup({ tone: "destructive", requireTyped: "Google" });
    const confirm = screen.getByRole("button", { name: "Disable" });
    const field = screen.getByLabelText("Type Google to confirm");
    expect(confirm).toBeDisabled();

    await user.type(field, "Googl");
    expect(confirm).toBeDisabled();
    await user.click(confirm);
    expect(onConfirm).not.toHaveBeenCalled();

    await user.type(field, "e");
    await waitFor(() => expect(confirm).toBeEnabled());
    await user.click(confirm);
    expect(onConfirm).toHaveBeenCalledTimes(1);
  });

  it("the typed gate is case-sensitive and forgives surrounding space", async () => {
    const user = userEvent.setup();
    setup({ requireTyped: "Google" });
    const confirm = screen.getByRole("button", { name: "Disable" });
    const field = screen.getByLabelText("Type Google to confirm");
    await user.type(field, "google");
    expect(confirm).toBeDisabled();
    await user.clear(field);
    await user.type(field, "  Google  ");
    await waitFor(() => expect(confirm).toBeEnabled());
  });

  it("focus opens in the typed field when there is a gate", async () => {
    setup({ tone: "destructive", requireTyped: "Google" });
    await waitFor(() => expect(screen.getByLabelText("Type Google to confirm")).toHaveFocus());
  });

  it("re-opening clears a half-typed gate", async () => {
    const user = userEvent.setup();
    const props = {
      onClose: () => {},
      onConfirm: vi.fn(),
      title: "Disable Google sign-in?",
      confirmLabel: "Disable",
      requireTyped: "Google",
    };
    const { rerender } = render(<ConfirmDialog open {...props} />);
    await user.type(screen.getByLabelText("Type Google to confirm"), "Google");
    rerender(<ConfirmDialog open={false} {...props} />);
    rerender(<ConfirmDialog open {...props} />);
    expect(screen.getByLabelText("Type Google to confirm")).toHaveValue("");
    expect(screen.getByRole("button", { name: "Disable" })).toBeDisabled();
  });

  it("a busy dialog spins and refuses a second press", async () => {
    const user = userEvent.setup();
    const { onConfirm } = setup({ busy: true });
    const confirm = screen.getByRole("button", { name: "Disable" });
    expect(confirm).toHaveAttribute("aria-busy", "true");
    await user.click(confirm);
    expect(onConfirm).not.toHaveBeenCalled();
  });

  it("takes a custom cancel label", () => {
    setup({ cancelLabel: "Keep it" });
    expect(screen.getByRole("button", { name: "Keep it" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Cancel" })).not.toBeInTheDocument();
  });

  // A gate the CALLER owns — a reason still to be written, a change that can't be asked for yet.
  // It has to hold the action as firmly as the typed gate does, on the pointer and on the key.
  it("the caller's gate holds the primary action, and lifts when it does", async () => {
    const user = userEvent.setup();
    const props = {
      onClose: () => {},
      onConfirm: vi.fn(),
      title: "Waive this rule?",
      confirmLabel: "Create waiver",
    };
    const { rerender } = render(<ConfirmDialog open confirmDisabled {...props} />);
    const confirm = screen.getByRole("button", { name: "Create waiver" });
    expect(confirm).toBeDisabled();
    await user.click(confirm);
    expect(props.onConfirm).not.toHaveBeenCalled();

    rerender(<ConfirmDialog open confirmDisabled={false} {...props} />);
    await waitFor(() => expect(confirm).toBeEnabled());
    await user.click(confirm);
    expect(props.onConfirm).toHaveBeenCalledTimes(1);
  });

  it("Enter in the body does not confirm past the caller's gate", async () => {
    const user = userEvent.setup();
    const { onConfirm } = setup({
      confirmDisabled: true,
      children: <input aria-label="Reason" />,
    });
    await user.type(screen.getByLabelText("Reason"), "because{Enter}");
    expect(onConfirm).not.toHaveBeenCalled();
  });

  // A disabled button cannot take focus, so opening on it would drop the keyboard on <body>.
  it("focus opens on cancel when the caller's gate holds the action shut", async () => {
    setup({ confirmDisabled: true });
    await waitFor(() => expect(screen.getByRole("button", { name: "Cancel" })).toHaveFocus());
  });

  it("a caller that names its own first focus gets it, gate or no gate", async () => {
    function Harness() {
      const ref = useRef<HTMLTextAreaElement>(null);
      return (
        <ConfirmDialog
          open
          onClose={() => {}}
          onConfirm={() => {}}
          title="Waive this rule?"
          confirmLabel="Create waiver"
          tone="destructive"
          initialFocusRef={ref}
        >
          <textarea ref={ref} aria-label="Reason" />
        </ConfirmDialog>
      );
    }
    render(<Harness />);
    await waitFor(() => expect(screen.getByLabelText("Reason")).toHaveFocus());
  });

  // The gates are independent: meeting one must not open the action while the other still holds.
  it("both gates must lift before the action opens", async () => {
    const user = userEvent.setup();
    setup({ requireTyped: "Google", confirmDisabled: true });
    const confirm = screen.getByRole("button", { name: "Disable" });
    await user.type(screen.getByLabelText("Type Google to confirm"), "Google");
    expect(confirm).toBeDisabled();
  });
});
