import { useEffect, useRef, useState } from "react";

/* Keeps a node mounted through its exit transition so an overlay animates both
   ways. While `open` is true the node mounts at data-state="open"; on close it
   flips to "closed", the CSS plays the exit, and only then does it unmount.
   Falls back to an immediate unmount when reduced motion is requested. */
export function usePresence(open: boolean, exitMs = 140): { mounted: boolean; state: "open" | "closed" } {
  const [mounted, setMounted] = useState(open);
  // ALWAYS born closed — even when `open` is already true on mount. The first commit
  // paints the closed start-state and the effect's two-frame flip plays the enter
  // transition; initializing to "open" would teleport a panel that mounts open.
  const [state, setState] = useState<"open" | "closed">("closed");
  const timer = useRef<number | null>(null);

  useEffect(() => {
    if (timer.current) window.clearTimeout(timer.current);
    if (open) {
      setMounted(true);
      // Two frames: the first lets the closed start-state paint, the second flips
      // to open so the entry transition runs from it instead of teleporting.
      let inner = 0;
      const outer = requestAnimationFrame(() => {
        inner = requestAnimationFrame(() => setState("open"));
      });
      return () => {
        cancelAnimationFrame(outer);
        cancelAnimationFrame(inner);
      };
    }
    setState("closed");
    const reduce = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
    timer.current = window.setTimeout(() => setMounted(false), reduce ? 0 : exitMs);
    return () => {
      if (timer.current) window.clearTimeout(timer.current);
    };
  }, [open, exitMs]);

  return { mounted, state };
}
