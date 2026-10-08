import type { KeyboardEvent, MouseEvent, ReactNode } from "react";

import { externalHref } from "./externalUrl";
import "./url.css";

export interface UrlLinkProps {
  url: string;
  children?: ReactNode;
  /** When set, the link is a role=link span that calls this (with the url) rather
   *  than navigating via an <a> — for nesting inside a card's header button, where
   *  an <a> is invalid markup. The activation never bubbles to a row toggle. */
  onOpen?: (url: string) => void;
  className?: string;
}

/** A pastel, theme-adaptive URL. An <a> that opens externally by default; a
 *  role=link span (routed through `onOpen`) when it must sit inside a button. */
export function UrlLink({ url, children, onOpen, className }: UrlLinkProps) {
  const classes = `alk-url${className ? ` ${className}` : ""}`;
  const label = children ?? url;
  // The URL is somebody else's text — a model's, a search result's, a file's.
  // One that may not become a navigation stays the text it is, in both shapes:
  // an href would execute in this origin on click, and handing it to the host
  // would only move the same string one call further along.
  const href = externalHref(url);
  if (href === null) {
    // Not a link, and not dressed as one: the reader is not the attacker, and a
    // string that looks clickable and answers a click with nothing is a dead
    // affordance. The click is still stopped where an anchor would have stopped
    // it, so selecting the text does not also toggle the card around it.
    return (
      <span
        className={classes}
        data-inert=""
        title={url}
        onClick={(event) => event.stopPropagation()}
      >
        {label}
      </span>
    );
  }
  if (!onOpen) {
    return (
      <a
        className={classes}
        href={href}
        target="_blank"
        rel="noreferrer noopener"
        onClick={(event) => event.stopPropagation()}
      >
        {label}
      </a>
    );
  }
  const open = (event: MouseEvent | KeyboardEvent) => {
    event.preventDefault();
    event.stopPropagation();
    onOpen(url);
  };
  return (
    <span
      className={classes}
      role="link"
      tabIndex={0}
      title={url}
      onClick={open}
      onKeyDown={(event) => {
        if (event.key === "Enter" || event.key === " ") open(event);
      }}
    >
      {label}
    </span>
  );
}
