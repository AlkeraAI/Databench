/**
 * One line at the top of an open file — everything the tab says about the file,
 * and every door out of it.
 *
 * The pane beside a conversation is a column the reader drags narrow, so a
 * second row of controls costs the file itself a quarter of its height. There
 * is one row here: the name, the two readings a person checks (how big, how
 * recently written), what the machine is doing to it, and the actions as
 * glyphs. The renderer's own settings — soft wrap on a text file, actual size on
 * an image — ride the same row rather than a bar of their own; the renderer
 * declares them and this header draws them, so a new one appears here without
 * this file learning about it.
 *
 * Narrow is answered by folding, not by wrapping. The row is measured, and the
 * actions that no longer fit move — from the right, in the order they were
 * given — into a menu behind one glyph. Nothing is dropped: an action the pane
 * is too narrow to show is still an action the reader can reach, which is what
 * separates a fold from a truncation.
 */

import { IconDots } from "@tabler/icons-react";
import { useMemo, useRef, type ReactNode } from "react";

import { Button, Dropdown, DropdownItem, Tooltip } from "@alkera/ui";

import { usePaneWidth } from "./usePaneWidth";

import "./workspace.css";

/** One door out of the file, or one thing about how it is drawn. */
export interface FileTabAction {
  id: string;
  /** The accessible name and the tooltip — they are the same words, so what a
   *  screen reader says and what a pointer reveals can never disagree. */
  label: string;
  icon: ReactNode;
  run: () => void;
  /** A setting reads its state; a command has none. */
  pressed?: boolean;
}

/** The width one glyph button takes, plus the gap before it. */
const ACTION_SLOT = 30;
/** The narrowest the name is allowed to get before actions start folding — below
 *  it a file is a column of ellipsis and the row has stopped naming anything. */
const NAME_FLOOR = 108;
/** Under this the readings come off the line and join the name's title, because
 *  a size and a date between them cost more room than the name itself. */
export const FACTS_FLOOR = 320;

/**
 * How many actions the row can show whole.
 *
 * An unmeasured row (the first paint, or a test with no layout) shows all of
 * them: the first frame a reader sees must never be the narrowest one. Once the
 * row does not fit, one slot is spent on the menu that holds the rest.
 */
export function visibleActionCount(width: number, total: number): number {
  if (width <= 0) return total;
  const room = Math.floor((width - NAME_FLOOR) / ACTION_SLOT);
  if (room >= total) return total;
  return Math.max(0, room - 1);
}

export interface FileTabHeaderProps {
  /** The name as the reader sees it. */
  name: string;
  /** The whole chat-relative path, on the name's `title`. */
  path: string;
  /** The readings — size, when it last changed. */
  facts: readonly string[];
  /** What the machine is doing to this file right now. */
  chip?: ReactNode;
  /** The switch between the ways the file can be shown, where it has more
   *  than one. It is never folded: it is what the body below is. */
  views?: ReactNode;
  actions: readonly FileTabAction[];
}

/** The width the view switch takes on the row. */
export const VIEWS_SLOT = 128;

export function FileTabHeader({ name, path, facts, chip, views, actions }: FileTabHeaderProps) {
  const bar = useRef<HTMLDivElement | null>(null);
  const width = usePaneWidth(bar);

  const roomForFacts = width === 0 || width >= FACTS_FLOOR + (views ? VIEWS_SLOT : 0);
  const shown = visibleActionCount(width === 0 || !views ? width : Math.max(1, width - VIEWS_SLOT), actions.length);
  const folded = actions.slice(shown);

  // The readings are never lost to a narrow pane — they move onto the name,
  // where the same hover that spells out the path spells them out too.
  const title = useMemo(
    () => (roomForFacts || facts.length === 0 ? path : `${path} · ${facts.join(" · ")}`),
    [facts, path, roomForFacts],
  );

  return (
    <div className="alk-ws-file__bar" ref={bar} data-compact={roomForFacts ? undefined : ""}>
      <span className="alk-ws-file__name" title={title}>
        {name}
      </span>
      {roomForFacts
        ? facts.map((fact) => (
            <span className="alk-ws-file__fact" key={fact}>
              {fact}
            </span>
          ))
        : null}
      {chip}
      {views ? <div className="alk-ws-file__views">{views}</div> : null}
      <div className="alk-ws-file__actions" role="group" aria-label="File actions">
        {actions.slice(0, shown).map((action) => (
          <Tooltip key={action.id} label={action.label}>
            {(tip) => (
              <Button
                {...tip}
                iconOnly
                aria-label={action.label}
                aria-pressed={action.pressed}
                variant="secondary"
                fill="ghost"
                size="sm"
                onClick={action.run}
              >
                {action.icon}
              </Button>
            )}
          </Tooltip>
        ))}
        {folded.length > 0 ? (
          <Dropdown
            label="More actions"
            trigger={{
              kind: "icon",
              icon: <IconDots size={15} stroke={1.8} aria-hidden />,
              ariaLabel: "More actions",
              variant: "secondary",
              fill: "ghost",
              size: "sm",
            }}
            tickSide="right"
          >
            {folded.map((action) => (
              <DropdownItem
                key={action.id}
                icon={action.icon}
                // A setting reads as a switch in the menu; a command reads as a
                // command, so a screen reader never announces "Download, not
                // checked".
                {...(action.pressed === undefined ? {} : { selected: action.pressed })}
                onSelect={action.run}
              >
                {action.label}
              </DropdownItem>
            ))}
          </Dropdown>
        ) : null}
      </div>
    </div>
  );
}

export default FileTabHeader;
