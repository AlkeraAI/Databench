import { useRef, type ReactNode } from "react";
import { createPortal } from "react-dom";

import { useFloating, usePresence } from "../hooks";
import type { VizAnchorRect } from "./useVizHover";

/**
 * The floating readout a viz shows for the sample under the pointer, shared by every viz so they
 * anchor, flip, and animate the same way. It pins to the hovered POINT (not the cursor) via the
 * `anchorRect` from `useVizHover`, and portals to the themed root so a card's `overflow` can't clip
 * it. Reuses the control tooltip's paint (`.alk-tooltip`); only the point marker is viz-specific.
 */
export function VizTooltip({
  anchorRect,
  children,
  portalTarget,
}: {
  /** The hovered sample's anchor, or null when the pointer is away (drives open/close). */
  anchorRect: VizAnchorRect | null;
  /** The caller's rendered content for the sample. Absent → nothing to show, stay closed. */
  children: ReactNode;
  /** Themed root to portal into; defaults to document.body. */
  portalTarget?: HTMLElement;
}) {
  const open = anchorRect != null && children != null && children !== false;
  const { mounted, state } = usePresence(open, 120);
  const float = useFloating({ open, side: "top", align: "center", gap: 8, clampHeight: false });

  // Latch the last shown anchor + content so the EXIT frame still has them: on pointer-leave the hook
  // nulls anchorRect and content together, but presence keeps the node mounted to animate out — an
  // empty, anchorless exit would fade a blank box and skip the marker's shrink.
  const latch = useRef<{ rect: VizAnchorRect; content: ReactNode }>(null);
  if (open && anchorRect) latch.current = { rect: anchorRect, content: children };
  const shown = open ? { rect: anchorRect!, content: children } : latch.current;

  if (!mounted || !shown) return null;
  const { rect, content } = shown;

  const tip = (
    <>
      {/* Zero-size fixed anchor at the sample's x. Re-keyed on its rounded position so floating-ui
          re-measures when the pointer moves to a new sample (autoUpdate only watches scroll/resize). */}
      <div
        key={`${Math.round(rect.left)}:${Math.round(rect.top)}`}
        ref={float.setReference}
        aria-hidden="true"
        style={{ position: "fixed", left: rect.left, top: rect.top, width: rect.width, height: rect.height, pointerEvents: "none" }}
      />
      {rect.pointY != null ? (
        // A crisp round DOM marker on the value — an SVG circle would stretch to an ellipse under the
        // sparkline's non-uniform viewBox. Painted in the captured series colour (the portal escapes
        // the --alk-viz-color scope).
        <span
          className="alk-viz-dot"
          data-state={state}
          aria-hidden="true"
          style={{
            position: "fixed",
            left: rect.left,
            top: rect.pointY,
            ...(rect.color ? { ["--alk-viz-color" as string]: rect.color } : {}),
          }}
        />
      ) : null}
      <div ref={float.setFloating} role="tooltip" className="alk-tooltip" style={float.floatingStyles} data-side={float.side} data-state={state}>
        {content}
      </div>
    </>
  );

  return createPortal(tip, portalTarget ?? document.body);
}
