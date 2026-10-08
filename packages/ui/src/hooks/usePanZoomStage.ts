import { useCallback, useEffect, useRef, useState } from "react";

/** Below this pointer travel a press is a click; beyond it, a pan. */
const PAN_THRESHOLD_PX = 4;

export interface PanZoomStageOptions {
  minZoom: number;
  maxZoom: number;
  /** Per 1px of wheel travel, the zoom exponent step. */
  zoomRate?: number;
  /** Re-attach the wheel listener when this changes (the scroll element can
   *  mount late, e.g. after a degraded-view check). */
  rebind?: unknown;
}

export interface PanZoomStage {
  scrollRef: React.RefObject<HTMLDivElement | null>;
  zoom: number;
  onPointerDown: (e: React.PointerEvent<HTMLDivElement>) => void;
  onPointerMove: (e: React.PointerEvent<HTMLDivElement>) => void;
  endPan: (e: React.PointerEvent<HTMLDivElement>) => void;
  /** True when the click now landing merely closed a drag; consuming it resets the latch. */
  consumeClick: () => boolean;
}

/** Drag-to-pan plus wheel-to-zoom for a scrollable stage. The wheel listener is
 *  native and non-passive (React's synthetic onWheel could not preventDefault, so
 *  the wheel would scroll the transcript instead), zoom anchors at the cursor by
 *  re-seating the scroll position after the re-render, and a press that travels
 *  under the threshold still clicks its node. */
export function usePanZoomStage({
  minZoom,
  maxZoom,
  zoomRate = 0.002,
  rebind,
}: PanZoomStageOptions): PanZoomStage {
  const scrollRef = useRef<HTMLDivElement>(null);
  const drag = useRef<{ x: number; y: number; sl: number; st: number } | null>(
    null,
  );
  const panned = useRef(false);

  const [zoom, setZoom] = useState(1);
  const zoomRef = useRef(zoom);
  zoomRef.current = zoom;
  const pendingScroll = useRef<{ left: number; top: number } | null>(null);

  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      const z = zoomRef.current;
      const next = Math.min(
        maxZoom,
        Math.max(minZoom, z * Math.pow(2, -e.deltaY * zoomRate)),
      );
      if (next === z) return;
      const r = el.getBoundingClientRect();
      const cx = e.clientX - r.left;
      const cy = e.clientY - r.top;
      pendingScroll.current = {
        left: ((el.scrollLeft + cx) / z) * next - cx,
        top: ((el.scrollTop + cy) / z) * next - cy,
      };
      setZoom(next);
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
  }, [rebind, minZoom, maxZoom, zoomRate]);

  useEffect(() => {
    const el = scrollRef.current;
    const pending = pendingScroll.current;
    if (!el || !pending) return;
    pendingScroll.current = null;
    el.scrollLeft = pending.left;
    el.scrollTop = pending.top;
  }, [zoom]);

  const onPointerDown = useCallback((e: React.PointerEvent<HTMLDivElement>) => {
    const el = scrollRef.current;
    if (!el || e.button !== 0) return;
    drag.current = {
      x: e.clientX,
      y: e.clientY,
      sl: el.scrollLeft,
      st: el.scrollTop,
    };
    panned.current = false;
  }, []);

  const onPointerMove = useCallback((e: React.PointerEvent<HTMLDivElement>) => {
    const el = scrollRef.current;
    const d = drag.current;
    if (!el || !d) return;
    const dx = e.clientX - d.x;
    const dy = e.clientY - d.y;
    if (!panned.current && Math.hypot(dx, dy) < PAN_THRESHOLD_PX) return;
    if (!panned.current) {
      panned.current = true;
      el.setPointerCapture?.(e.pointerId);
      el.dataset.panning = "true";
    }
    el.scrollLeft = d.sl - dx;
    el.scrollTop = d.st - dy;
  }, []);

  const endPan = useCallback((e: React.PointerEvent<HTMLDivElement>) => {
    const el = scrollRef.current;
    if (el && panned.current) {
      el.releasePointerCapture?.(e.pointerId);
      delete el.dataset.panning;
    }
    drag.current = null;
  }, []);

  const consumeClick = useCallback(() => {
    const consumed = panned.current;
    panned.current = false;
    return consumed;
  }, []);

  return {
    scrollRef,
    zoom,
    onPointerDown,
    onPointerMove,
    endPan,
    consumeClick,
  };
}
