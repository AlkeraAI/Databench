import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { Button } from "../../controls/Button";
import { Modal } from "./Modal";

// The Modal's action-row slot: an explicit `footer` node, or the convenience confirm row that
// replaced the deleted portal ConfirmModal.

afterEach(cleanup);

function footEl(): HTMLElement | null {
  return document.querySelector(".alk-modal__foot");
}

describe("Modal footer", () => {
  it("pins the footer node after the body", () => {
    render(
      <Modal
        open
        onClose={() => {}}
        title="Settings"
        footer={
          <>
            <Button variant="secondary" fill="ghost">
              Cancel
            </Button>
            <Button>Save changes</Button>
          </>
        }
      >
        <p>Body copy.</p>
      </Modal>,
    );
    const foot = footEl();
    expect(foot).not.toBeNull();
    // The action buttons live in the footer region, not the scrollable body.
    expect(screen.getByRole("button", { name: "Save changes" }).closest(".alk-modal__foot")).toBe(foot);
    expect(screen.getByRole("button", { name: "Cancel" }).closest(".alk-modal__foot")).toBe(foot);
    const body = screen.getByRole("dialog").querySelector(".alk-modal__body");
    expect(body).not.toBeNull();
    expect(body!.compareDocumentPosition(foot!) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it("rules the footer only when footerDivided is set", () => {
    // Off by default because the body's own padding sets the gap; the seam brings the hairline back
    // for a long, scrolling body.
    const { rerender } = render(
      <Modal open onClose={() => {}} title="Settings" footer={<Button>Save</Button>}>
        <p>Body copy.</p>
      </Modal>,
    );
    expect(footEl()).not.toHaveClass("alk-modal__foot--divided");
    rerender(
      <Modal open onClose={() => {}} title="Settings" footerDivided footer={<Button>Save</Button>}>
        <p>Body copy.</p>
      </Modal>,
    );
    expect(footEl()).toHaveClass("alk-modal__foot--divided");
  });

  it("closes from the icon-only close button", async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    render(
      <Modal open onClose={onClose} title="Settings" footer={<Button>Save</Button>}>
        <p>Body copy.</p>
      </Modal>,
    );
    const close = screen.getByRole("button", { name: "Close" });
    // The close is the shared Button primitive with its corner-position override, not a hand-rolled
    // <button>.
    expect(close).toHaveClass("alk-btn", "alk-modal__x");
    expect(close).toHaveAttribute("data-icon", "");
    await user.click(close);
    expect(onClose).toHaveBeenCalledOnce();
  });
});

describe("Modal confirm footer", () => {
  // confirmLabel is what opts a modal into the standard row; a stray onConfirm alone must not.
  it.each([
    { label: "nothing is given", props: {} },
    { label: "only onConfirm is given", props: { onConfirm: () => {} } },
  ])("renders no footer when $label", ({ props }) => {
    render(
      <Modal open onClose={() => {}} title="Bare" {...props}>
        <p>x</p>
      </Modal>,
    );
    expect(footEl()).toBeNull();
    expect(screen.queryByRole("button", { name: "Cancel" })).toBeNull();
  });

  it("renders both buttons in the footer from confirmLabel", () => {
    render(
      <Modal open onClose={() => {}} title="Delete team" confirmLabel="Delete" onConfirm={() => {}}>
        <p>Sure?</p>
      </Modal>,
    );
    expect(screen.getByRole("button", { name: "Delete" }).closest(".alk-modal__foot")).toBe(footEl());
    expect(screen.getByRole("button", { name: "Cancel" }).closest(".alk-modal__foot")).toBe(footEl());
  });

  // Confirming is not dismissing: the two handlers must never be crossed.
  it.each([
    { press: "Delete", fires: "confirm" },
    { press: "Cancel", fires: "close" },
  ])("pressing $press fires only $fires", async ({ press, fires }) => {
    const user = userEvent.setup();
    const onConfirm = vi.fn();
    const onClose = vi.fn();
    render(
      <Modal open onClose={onClose} title="Delete team" confirmLabel="Delete" onConfirm={onConfirm}>
        <p>Sure?</p>
      </Modal>,
    );
    await user.click(screen.getByRole("button", { name: press }));
    expect(fires === "confirm" ? onConfirm : onClose).toHaveBeenCalledOnce();
    expect(fires === "confirm" ? onClose : onConfirm).not.toHaveBeenCalled();
  });

  it.each([
    { label: "defaults", props: {}, variant: "primary", fill: "filled" },
    {
      label: "honours confirmVariant + confirmFill",
      props: { confirmVariant: "destructive" as const, confirmFill: "outline" as const },
      variant: "destructive",
      fill: "outline",
    },
  ])("the confirm button $label", ({ props, variant, fill }) => {
    render(
      <Modal open onClose={() => {}} title="t" confirmLabel="Save" onConfirm={() => {}} {...props}>
        <p>x</p>
      </Modal>,
    );
    const btn = screen.getByRole("button", { name: "Save" });
    expect(btn).toHaveAttribute("data-variant", variant);
    expect(btn).toHaveAttribute("data-fill", fill);
  });

  // null is the only value that omits cancel; a string retitles it.
  it.each([
    { label: "null omits the cancel button", cancelLabel: null, buttons: 1, named: null },
    { label: "a string retitles it", cancelLabel: "Not now", buttons: 2, named: "Not now" },
  ])("cancelLabel: $label", ({ cancelLabel, buttons, named }) => {
    render(
      <Modal open onClose={() => {}} title="t" confirmLabel="OK" cancelLabel={cancelLabel} onConfirm={() => {}}>
        <p>x</p>
      </Modal>,
    );
    expect(screen.getByRole("button", { name: "OK" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Cancel" })).toBeNull();
    expect(footEl()!.querySelectorAll("button")).toHaveLength(buttons);
    if (named) expect(screen.getByRole("button", { name: named })).toBeInTheDocument();
  });

  it("does not fire onConfirm when the confirm is disabled", async () => {
    const user = userEvent.setup();
    const onConfirm = vi.fn();
    render(
      <Modal open onClose={() => {}} title="t" confirmLabel="Save" confirmDisabled onConfirm={onConfirm}>
        <p>x</p>
      </Modal>,
    );
    const btn = screen.getByRole("button", { name: "Save" });
    expect(btn).toBeDisabled();
    await user.click(btn);
    expect(onConfirm).not.toHaveBeenCalled();
  });

  it("confirmBusy is busy, confirmDisabled is only disabled", () => {
    // Busy advertises an in-flight action to AT, so confirmBusy must route through `loading`. The two
    // props are not interchangeable.
    const { rerender } = render(
      <Modal open onClose={() => {}} title="t" confirmLabel="Save" confirmBusy onConfirm={() => {}}>
        <p>x</p>
      </Modal>,
    );
    const busy = screen.getByRole("button", { name: "Save" });
    expect(busy).toBeDisabled();
    expect(busy).toHaveAttribute("aria-busy", "true");
    rerender(
      <Modal open onClose={() => {}} title="t" confirmLabel="Save" confirmDisabled onConfirm={() => {}}>
        <p>x</p>
      </Modal>,
    );
    const disabled = screen.getByRole("button", { name: "Save" });
    expect(disabled).toBeDisabled();
    expect(disabled).not.toHaveAttribute("aria-busy");
  });

  it("a custom footer suppresses the convenience buttons", () => {
    render(
      <Modal open onClose={() => {}} title="t" confirmLabel="ShouldNotRender" footer={<Button>Custom</Button>}>
        <p>x</p>
      </Modal>,
    );
    expect(screen.getByRole("button", { name: "Custom" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "ShouldNotRender" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Cancel" })).toBeNull();
    expect(footEl()!.querySelectorAll("button")).toHaveLength(1);
  });
});
