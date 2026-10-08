import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeAll, describe, expect, it } from "vitest";

import { Tooltip } from "./Tooltip";

// The primitive's contract: opens on hover only after the delay (leave cancels a pending open),
// opens instantly on focus, closes on leave/blur/Escape, describes its trigger via
// aria-describedby, and portals out of the tree. Anchoring: the default pins the trigger element;
// anchor="pointer" pins the cursor's rest point at open and holds it until leave — focus always
// falls back to the element (no cursor to pin). Positions run through the shared useFloating
// positioner, so edge flip/shift collision is that hook's pinned contract, not re-proven here.
// In jsdom every element rect is zero, so an element-anchored tip lands at the shift middleware's
// 8px viewport padding while a cursor-anchored one computes left = the cursor x — the mode oracle.

afterEach(cleanup);

// jsdom's PointerEvent constructor drops MouseEventInit fields (clientX/Y arrive undefined), so
// the tests back it with MouseEvent, whose init jsdom honors.
beforeAll(() => {
  (window as { PointerEvent: unknown }).PointerEvent = class extends MouseEvent {};
});

const renderTip = (props: { anchor?: "trigger" | "pointer"; openDelay?: number; exitMs?: number } = {}) =>
  render(
    <Tooltip label="full reading" openDelay={0} {...props}>
      {(t) => (
        <span {...t} tabIndex={0}>
          clipped
        </span>
      )}
    </Tooltip>,
  );

const tip = () => screen.getByRole("tooltip");
const trigger = () => screen.getByText("clipped");
const pointerAt = (type: "pointerEnter" | "pointerMove", x: number, y: number) =>
  fireEvent[type](trigger(), { clientX: x, clientY: y });

describe("Tooltip base contract", () => {
  it("opens on hover only after the delay, and a leave cancels a pending open", async () => {
    renderTip({ openDelay: 120 });
    pointerAt("pointerEnter", 100, 100);
    expect(screen.queryByRole("tooltip")).toBeNull();
    fireEvent.pointerLeave(trigger());
    await new Promise((r) => setTimeout(r, 180));
    expect(screen.queryByRole("tooltip")).toBeNull();

    pointerAt("pointerEnter", 100, 100);
    expect(await screen.findByRole("tooltip")).toHaveTextContent("full reading");
  });

  it("describes the trigger, portals to the body, and closes on blur and Escape", async () => {
    renderTip();
    expect(trigger()).not.toHaveAttribute("aria-describedby");

    fireEvent.focus(trigger());
    const shown = await screen.findByRole("tooltip");
    expect(trigger()).toHaveAttribute("aria-describedby", shown.id);
    expect(shown.parentElement).toBe(document.body);

    fireEvent.blur(trigger());
    await waitFor(() => expect(screen.queryByRole("tooltip")).toBeNull());
    expect(trigger()).not.toHaveAttribute("aria-describedby");

    fireEvent.focus(trigger());
    await screen.findByRole("tooltip");
    fireEvent.keyDown(document, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("tooltip")).toBeNull());
  });
});

describe("Tooltip anchor", () => {
  it("pins where the cursor rested out the delay and holds there under anchor=pointer", async () => {
    renderTip({ anchor: "pointer", openDelay: 120 });
    // The cursor enters, then settles somewhere else before the delay elapses:
    // the tip opens at the resting spot, not the entry spot.
    pointerAt("pointerEnter", 300, 400);
    pointerAt("pointerMove", 350, 420);
    await screen.findByRole("tooltip");
    await waitFor(() => expect(tip().style.left).toBe("350px"));
    expect(tip().style.top).toBe("408px");

    // Once open it never follows the cursor (the native-title behavior).
    pointerAt("pointerMove", 500, 300);
    await new Promise((r) => setTimeout(r, 30));
    expect(tip().style.left).toBe("350px");
  });

  it("re-anchors an open pointer-pinned tip to the element when focus lands", async () => {
    renderTip({ anchor: "pointer" });
    pointerAt("pointerEnter", 350, 420);
    await screen.findByRole("tooltip");
    await waitFor(() => expect(tip().style.left).toBe("350px"));

    fireEvent.focus(trigger());
    await waitFor(() => expect(tip().style.left).toBe("8px"));
  });

  it("keeps a focus-opened tip element-anchored when the pointer arrives", async () => {
    renderTip({ anchor: "pointer" });
    fireEvent.focus(trigger());
    await screen.findByRole("tooltip");
    pointerAt("pointerEnter", 350, 420);
    await new Promise((r) => setTimeout(r, 30));
    expect(tip().style.left).toBe("8px");
  });

  it("reopens at the new rest point on a quick re-hover during the exit fade", async () => {
    renderTip({ anchor: "pointer", exitMs: 200 });
    pointerAt("pointerEnter", 300, 400);
    await screen.findByRole("tooltip");
    await waitFor(() => expect(tip().style.left).toBe("300px"));

    fireEvent.pointerLeave(trigger());
    pointerAt("pointerEnter", 600, 500);
    await waitFor(() => expect(tip().style.left).toBe("600px"));
  });

  it("keeps element anchoring by default even with cursor coordinates supplied", async () => {
    renderTip();
    pointerAt("pointerEnter", 300, 400);
    await screen.findByRole("tooltip");
    await new Promise((r) => setTimeout(r, 30));
    expect(tip().style.left).toBe("8px");
  });
});
