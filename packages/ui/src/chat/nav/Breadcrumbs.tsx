// The trail a stacked page carries instead of a back button.
//
// STANCE: a detail page opened off a chat is a level in a stack, not a
// dead-end with one exit. A single back arrow says only "there is something
// behind this" and makes a reader climb one rung at a time; the trail names
// every level and lets any of them be reached in one press. The last crumb is
// where you are, so it is text rather than a control, and the crumb before it
// is the plain way back.
//
// Presentational only: the host resolves the levels and hands over what each
// one is called and where it goes.

import { Fragment, type ReactElement } from "react";

import { Text } from "../sharedUi";

import s from "./nav.module.css";

/** A crumb must never submit a form it happens to sit inside. `Text` types its
 *  DOM props as HTMLAttributes, which cannot spell a button's `type`. */
const PLAIN_BUTTON = { type: "button" } as const;

export interface Crumb {
  label: string;
  /** Jumps straight to this level. The level you are on has none, so it reads
   *  as text; an ancestor the host cannot navigate to reads the same way. */
  onGo?: () => void;
}

export interface BreadcrumbsProps {
  /** Root first, the current level last. An empty trail renders nothing. */
  crumbs: Crumb[];
  /** Names the trail for assistive tech, so a page with more than one
   *  navigation region stays distinguishable. */
  label?: string;
  /** The level you are on is this page's heading. Set it where the trail stands
   *  in for a title, so generalizing a name into a path doesn't cost the page
   *  its heading. */
  heading?: boolean;
}

/** The separator, drawn on the same 14 grid as the chrome's glyphs. */
function Chevron(): ReactElement {
  return (
    <svg viewBox="0 0 14 14" width="11" height="11" aria-hidden="true">
      <path
        d="M5.4 3.6 8.8 7l-3.4 3.4"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.3"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

export function Breadcrumbs({ crumbs, label = "Breadcrumbs", heading }: BreadcrumbsProps): ReactElement | null {
  if (crumbs.length === 0) return null;
  const lastIndex = crumbs.length - 1;

  return (
    <nav className={s.crumbs} aria-label={label}>
      {crumbs.map((crumb, index) => {
        const here = index === lastIndex;
        return (
          <Fragment key={index}>
            {index > 0 ? (
              <span className={s.crumbsSep}>
                <Chevron />
              </span>
            ) : null}
            {here || !crumb.onGo ? (
              // A crumb that is not a control is not focusable, so a styled tip
              // could only be summoned by a pointer, and the current level is
              // the crumb most likely to be cut. The native title matches the
              // rail row's cut title and reaches a reader who never hovers.
              <Text
                as={here && heading ? "h1" : "span"}
                className={s.crumbsHere}
                data-here={here ? "" : undefined}
                aria-current={here ? "page" : undefined}
                title={crumb.label}
              >
                {crumb.label}
              </Text>
            ) : (
              <Text as="button" {...PLAIN_BUTTON} className={s.crumbsStep} tooltip="truncate" onClick={crumb.onGo}>
                {crumb.label}
              </Text>
            )}
          </Fragment>
        );
      })}
    </nav>
  );
}
