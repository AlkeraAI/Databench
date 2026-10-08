import {
  useCallback,
  useEffect,
  useId,
  useRef,
  type CSSProperties,
  type ReactNode,
} from "react";
import { createPortal } from "react-dom";

import { cx } from "../../cx";
import { usePresence, useFocusTrap, pushEsc } from "../../../hooks";
import { Button } from "../../controls/Button";
import { CloseIcon } from "../../icons";

export type SidePanelAnchor = "viewport" | "belowTopbar";
export type SidePanelMode = "modal" | "inline";
export type SidePanelSide = "right" | "left";

export interface SidePanelProps {
  open: boolean;
  onClose: () => void;
  /** WHERE it sits. `viewport` covers the whole screen (portaled to document.body); `belowTopbar`
   *  renders in place inside the caller's `position: relative` body container (NOT portaled), so
   *  the topbar/chrome above stay live. */
  anchor?: SidePanelAnchor;
  /** HOW modal it is. `modal` brings the scrim + focus trap + `aria-modal`; `inline` has no scrim
   *  and a `role="region"` so the page behind stays interactive. */
  mode?: SidePanelMode;
  side?: SidePanelSide;
  /** Panel width; defaults to 480. Pass a number (px) or any length. */
  width?: number | string;
  /** Escape closes the panel. Default true. */
  escape?: boolean;
  /** Small-caps eyebrow above the title. */
  eyebrow?: ReactNode;
  title?: ReactNode;
  /** Actions that ride the header, left of the close button. */
  headActions?: ReactNode;
  footer?: ReactNode;
  /** Accessible name — the id of the title element. Pass a string `title` and this is derived. */
  labelledById?: string;
  /** Extra class on the panel surface, for page-specific overrides. */
  className?: string;
  style?: CSSProperties;
  children: ReactNode;
}

/**
 * SidePanel — the unified edge drawer.
 *
 * Two orthogonal axes carry every variant:
 *  - `anchor` — WHERE it sits. `viewport` covers the whole screen (portaled to `document.body`);
 *    `belowTopbar` is positioned within its own `position: relative` page-body container so the
 *    topbar and chrome stay live.
 *  - `mode` — HOW modal it is. `modal` brings the scrim + focus trap + `aria-modal` (the page
 *    behind is inert to a keyboard user); `inline` has no scrim and a `role="region"` so the page
 *    stays interactive.
 *
 * It always mounts-and-slides both ways (`usePresence`), goes `visibility:hidden` when closed (so
 * the off-screen panel is unreachable by tab + AT), and owns its close button. Esc closes it in
 * either mode unless `escape={false}`.
 */
export function SidePanel({
  open,
  onClose,
  anchor = "viewport",
  mode = "modal",
  side = "right",
  width,
  escape = true,
  eyebrow,
  title,
  headActions,
  footer,
  labelledById,
  className,
  style,
  children,
}: SidePanelProps) {
  const { mounted, state } = usePresence(open, 240);
  const panelRef = useRef<HTMLElement>(null);
  const titleId = useId();

  // Focus trap only in modal mode — an inline panel deliberately leaves the page reachable.
  useFocusTrap(panelRef, { active: open && mounted && mode === "modal", onClose, escapeClosable: escape });

  // Esc for the inline (non-trapped) mode, routed through the shared stack so only the top overlay
  // closes when several are open.
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;
  useEffect(() => {
    if (!open || !mounted || mode !== "inline" || !escape) return;
    return pushEsc(() => onCloseRef.current());
  }, [open, mounted, mode, escape]);

  const onScrimDown = useCallback(
    (e: React.MouseEvent) => {
      // preventDefault stops the mousedown from blurring focus to <body> before the trap restores it.
      if (e.target === e.currentTarget) {
        e.preventDefault();
        onClose();
      }
    },
    [onClose],
  );

  const headingId = labelledById ?? titleId;

  if (!mounted) return null;

  const panelStyle: CSSProperties = {
    ...(width != null ? { ["--alk-sidepanel-w" as string]: typeof width === "number" ? `${width}px` : width } : {}),
    ...style,
  };

  const tree = (
    <>
      {mode === "modal" ? (
        <div className="alk-sidepanel-scrim" data-anchor={anchor} data-state={state} onMouseDown={onScrimDown} />
      ) : null}
      <aside
        ref={panelRef}
        className={cx("alk-sidepanel", className)}
        data-anchor={anchor}
        data-mode={mode}
        data-side={side}
        data-state={state}
        style={panelStyle}
        role={mode === "modal" ? "dialog" : "region"}
        {...(mode === "modal" ? { "aria-modal": true } : {})}
        {...(title ? { "aria-labelledby": headingId } : labelledById ? { "aria-labelledby": labelledById } : {})}
      >
        <header className="alk-sidepanel__head">
          <div className="alk-sidepanel__id">
            {eyebrow ? <div className="alk-sidepanel__eyebrow">{eyebrow}</div> : null}
            {title ? (
              <h2 className="alk-sidepanel__title" id={headingId}>
                {title}
              </h2>
            ) : null}
          </div>
          <div className="alk-sidepanel__headactions">
            {headActions}
            <Button iconOnly variant="secondary" fill="ghost" size="md" aria-label="Close" onClick={onClose}>
              <CloseIcon size={18} />
            </Button>
          </div>
        </header>
        <div className="alk-sidepanel__body">{children}</div>
        {footer ? <footer className="alk-sidepanel__foot">{footer}</footer> : null}
      </aside>
    </>
  );

  if (anchor === "belowTopbar") return tree;

  return createPortal(tree, document.body);
}
