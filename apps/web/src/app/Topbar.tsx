import { createContext, useContext, useEffect, type ReactNode } from "react";
import { createPortal } from "react-dom";

import { Icon } from "./icons";

/**
 * The masthead slot. `AppLayout` owns the persistent topbar (route title + search/account);
 * a concern page contributes a SUBTITLE and ACTIONS (its own search, a Refresh, a filter
 * control) into it without re-rendering the shell. The page renders `<TopbarSubtitle>` /
 * `<TopbarActions>` anywhere in its tree and the content is portaled into the topbar — so each
 * page owns its masthead the way every Hearth prototype does, on one shared chrome.
 *
 * The slot nodes are state in `AppLayout` (set via callback refs), so a page's portal mounts
 * once the nodes exist and re-renders with the page's own state (no deps array to keep in sync).
 */

export interface TopbarSlotNodes {
  subtitle: HTMLElement | null;
  actions: HTMLElement | null;
  /** True when a shell masthead hosts the slots. The nodes arrive a commit later (callback refs),
   *  so a page that needs its own chrome fallback keys off this flag, never off `actions === null`
   *  — that is also the not-yet-mounted state and would flash the fallback on first paint. */
  framed: boolean;
  /** Hide the shell's route title while a page holds it hidden. A full-canvas surface whose
   *  subtitle already names the subject spends the masthead on its own chrome instead of a word
   *  the nav rail is already showing. */
  setTitleHidden: (hidden: boolean) => void;
  /** Hide the whole shell masthead while a page holds it hidden. Portals into its slots no-op. */
  setTopbarHidden: (hidden: boolean) => void;
}

export const TopbarSlotsContext = createContext<TopbarSlotNodes>({
  subtitle: null,
  actions: null,
  framed: false,
  setTitleHidden: () => {},
  setTopbarHidden: () => {},
});

/** Hide the shell masthead title while the calling page is mounted. */
export function useHideTopbarTitle(): void {
  const { setTitleHidden } = useContext(TopbarSlotsContext);
  useEffect(() => {
    setTitleHidden(true);
    return () => setTitleHidden(false);
  }, [setTitleHidden]);
}

/** Hide the entire shell masthead while `hidden` holds. */
export function useHideTopbar(hidden: boolean): void {
  const { setTopbarHidden } = useContext(TopbarSlotsContext);
  useEffect(() => {
    setTopbarHidden(hidden);
    return () => setTopbarHidden(false);
  }, [hidden, setTopbarHidden]);
}

/** The shell masthead. AppLayout and the dev harness render this one anatomy, so a masthead
 *  flag lands once. The menu button renders only when the shell passes an opener. */
export function Masthead({
  title,
  titleHidden,
  hidden,
  onOpenMenu,
  setSubtitleEl,
  setActionsEl,
}: {
  title: string;
  titleHidden: boolean;
  hidden: boolean;
  onOpenMenu?: () => void;
  setSubtitleEl: (el: HTMLElement | null) => void;
  setActionsEl: (el: HTMLElement | null) => void;
}) {
  if (hidden) return null;
  return (
    <header className="alk-top">
      {onOpenMenu ? (
        <button type="button" className="alk-menubtn" aria-label="Open menu" onClick={onOpenMenu}>
          <Icon name="panel" size={20} />
        </button>
      ) : null}
      <div className="alk-top__title">
        {titleHidden ? null : <h1>{title}</h1>}
        <div className="alk-top__sub" ref={setSubtitleEl} />
      </div>
      <div className="alk-top__actions" ref={setActionsEl} />
    </header>
  );
}

/** A sub-line under the page title — e.g. "Shared catalog · Tideline Analytics". */
export function TopbarSubtitle({ children }: { children: ReactNode }) {
  const { subtitle } = useContext(TopbarSlotsContext);
  return subtitle ? createPortal(children, subtitle) : null;
}

/** Right-aligned masthead controls — a page-scoped search, a Refresh, a filter button. */
export function TopbarActions({ children }: { children: ReactNode }) {
  const { actions } = useContext(TopbarSlotsContext);
  return actions ? createPortal(children, actions) : null;
}
