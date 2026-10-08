import { IconArrowLeft } from "@tabler/icons-react";
import type { ReactNode } from "react";
import { Button, cx } from "../../primitives";
import "./StackedPage.css";

export interface StackedPageProps {
  /** Names the page beside the back button. A page that carries `nav` is named
   *  by its trail's last step instead, so it needs no title. */
  title?: ReactNode;
  subtitle?: ReactNode;
  actions?: ReactNode;
  /** A host-rendered trail back through the stack, in place of the title: it
   *  names every level above this page, not just that one exists. It rides
   *  beside the back button, which stays the one-press way up a level. */
  nav?: ReactNode;
  children: ReactNode;
  /** Extra class on the page root, so a host can restyle the chrome/back for its surface. */
  className?: string;
  bodyClassName?: string;
  backLabel?: string;
  /** Omit to render WITHOUT a back button — e.g. when the page is its own editor
   *  tab (a WebviewPanel), not a subpage pushed onto another surface. */
  onBack?: () => void;
}

/** Reusable editor-tab page for detail views opened from another IDE surface:
 *  a top-bar-height chrome row (back button, title, subtitle, actions) over a
 *  scrolling body. */
export function StackedPage({
  title,
  subtitle,
  actions,
  nav,
  children,
  className,
  bodyClassName,
  backLabel = "Back",
  onBack,
}: StackedPageProps) {
  return (
    <section
      className={cx("alk-stacked-page", className)}
      data-nav={nav ? "" : undefined}
      data-back={onBack ? "" : undefined}
    >
      <header className="alk-stacked-page__chrome">
        {onBack ? (
          <Button
            iconOnly
            variant="secondary"
            fill="ghost"
            size="sm"
            aria-label={backLabel}
            onClick={onBack}
          >
            <IconArrowLeft size="var(--alkIconMd)" />
          </Button>
        ) : null}
        {nav ? (
          <div className="alk-stacked-page__nav">{nav}</div>
        ) : (
          <div className="alk-stacked-page__identity">
            <div className="alk-stacked-page__title alk-truncate">{title}</div>
            {subtitle ? <div className="alk-stacked-page__subtitle alk-truncate">{subtitle}</div> : null}
          </div>
        )}
        {actions ? <div className="alk-stacked-page__actions">{actions}</div> : null}
      </header>
      <main className={cx("alk-stacked-page__body", bodyClassName)}>{children}</main>
    </section>
  );
}
