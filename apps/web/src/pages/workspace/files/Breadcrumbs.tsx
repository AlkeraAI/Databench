/**
 * The path trail above the browser.
 *
 * Every segment is a drop target: dragging a selection onto an ancestor moves it there,
 * which is the shortest path "up one level" a file manager has. The drag wiring itself
 * belongs to the drag-and-drop layer, so this component only exposes the seam
 * (`onDropToSegment`) and paints the target state the layer asks for.
 *
 * A segment is cut with an ellipsis when the line it shares is narrower than the
 * name, so every segment carries its whole name on hover — a trail that reads
 * `Q3 rev…` is only useful if the rest of it is one pointer away. The mark a
 * segment wears sits on that same line: it is the name's, not a row of its own,
 * so the name is a box of its own for the cut to happen inside.
 *
 * The trail is the path the session walked, so a deep link has one segment and
 * nothing above it to press. The way up therefore stands apart from the trail,
 * as a control the surface drives from the folder's own parent, and a trail
 * deeper than three folds its middle so the folder the reader is in keeps its
 * name on a narrow line; the fold opens in place on request.
 */

import { useEffect, useState, type DragEvent } from "react";

import { HomeIcon } from "@alkera/ui";

import { Icon, type IconName } from "@/app/icons";

export interface Crumb {
  /** Node id of the folder this segment addresses. */
  readonly id: string;
  /** What the user sees; already the decoded display name. */
  readonly name: string;
  /** The portal glyph this segment wears when the folder IS something the
   *  portal has a page for — a chat, walked into for its files. Absent for an
   *  ordinary folder, which carries no mark at all. */
  readonly icon?: IconName;
  /** The folder is a member's home: it wears the home mark, and `name` is its owner's. */
  readonly home?: boolean;
}

/** The way up, as the surface offers it: what it is called, and what pressing it
 *  does. `go` absent means there is nothing above — the drive's root, the chat's
 *  own working directory — and the control stays, disabled, so the line reads the
 *  same at every depth. */
export interface BreadcrumbsUp {
  readonly title: string;
  readonly go?: () => void;
}

/** The drive's three root containers are stored lowercase — the trail read `home` where the rail
 *  said `Home`, two spellings of one place. This is the single spelling of the product's own
 *  names; a folder a person made is never re-cased. */
const ROOT_PLACE_NAMES: Readonly<Record<string, string>> = {
  home: "Home",
  shared: "Shared",
  teams: "Teams",
};

/** How the product names a root container. Anything else is the person's own name, verbatim. */
export function displayPlaceName(name: string): string {
  return ROOT_PLACE_NAMES[name] ?? name;
}

/** A trail longer than this folds the segments between its first and its last. */
export const TRAIL_FOLDS_PAST = 3;

export interface BreadcrumbsProps {
  /** Root first, current folder last. The last segment is the page, not a link. */
  segments: readonly Crumb[];
  onNavigate?: (id: string) => void;
  /** Called when something is dropped on a segment. Absent, segments accept no drop. */
  onDropToSegment?: (id: string, event: DragEvent<HTMLElement>) => void;
  /** A drag is over a segment / has left it, so the layer can paint the target. */
  onDragOverSegment?: (id: string, event: DragEvent<HTMLElement>) => void;
  onDragLeaveSegment?: (id: string, event: DragEvent<HTMLElement>) => void;
  /** The segment the drag layer says is currently under the pointer. */
  activeDropId?: string | null;
  label?: string;
  /** The way up. Absent, the trail carries no such control (a dialog's trail). */
  up?: BreadcrumbsUp;
}

/** The fold's place in the list. Not a crumb: it addresses no folder. */
const FOLD = Symbol("fold");

export function Breadcrumbs({
  segments,
  onNavigate,
  onDropToSegment,
  onDragOverSegment,
  onDragLeaveSegment,
  activeDropId = null,
  label = "Breadcrumb",
  up,
}: BreadcrumbsProps) {
  const [opened, setOpened] = useState(false);
  const lastId = segments[segments.length - 1]?.id;
  // A fold opened for one folder closes when the reader moves to another: the
  // fold is about this line, not a preference.
  useEffect(() => {
    setOpened(false);
  }, [lastId]);

  const folded = !opened && segments.length > TRAIL_FOLDS_PAST;
  const hidden = folded ? segments.slice(1, -1) : [];
  const first = segments[0];
  const last = segments[segments.length - 1];
  const shown: ReadonlyArray<Crumb | typeof FOLD> =
    folded && first !== undefined && last !== undefined ? [first, FOLD, last] : segments;
  const droppable = onDropToSegment !== undefined;

  return (
    <nav className="alk-files-crumbs" aria-label={label}>
      {up ? (
        <button
          type="button"
          className="alk-files-crumbs__up"
          aria-label={up.title}
          title={up.title}
          disabled={up.go === undefined}
          onClick={up.go}
        >
          <Icon name="ascend" size={14} />
        </button>
      ) : null}
      <ol className="alk-files-crumbs__list">
        {shown.map((segment) => {
          if (segment === FOLD) {
            return (
              <li key="fold" className="alk-files-crumbs__item">
                <button
                  type="button"
                  className="alk-files-crumbs__fold"
                  aria-expanded={false}
                  aria-label={`Show the ${hidden.length} folders between`}
                  title={hidden.map((crumb) => crumb.name).join(" / ")}
                  onClick={() => setOpened(true)}
                >
                  …
                </button>
              </li>
            );
          }
          const isLast = segment.id === lastId;
          // Only the drive's own root is re-cased; a nested folder called `home` is the
          // person's, and keeps the name they gave it.
          const name =
            segment.id === first?.id && !segment.home ? displayPlaceName(segment.name) : segment.name;
          return (
            <li
              key={segment.id}
              className="alk-files-crumbs__item"
              data-drop-target={droppable ? "folder" : undefined}
              data-drop-active={activeDropId === segment.id ? "true" : undefined}
              data-node-id={segment.id}
              onDragEnter={droppable ? (event) => onDragOverSegment?.(segment.id, event) : undefined}
              onDragOver={
                droppable
                  ? (event) => {
                      event.preventDefault();
                      onDragOverSegment?.(segment.id, event);
                    }
                  : undefined
              }
              onDragLeave={droppable ? (event) => onDragLeaveSegment?.(segment.id, event) : undefined}
              onDrop={droppable ? (event) => onDropToSegment(segment.id, event) : undefined}
            >
              {isLast ? (
                <span className="alk-files-crumbs__current" aria-current="page" title={name}>
                  <CrumbMark segment={segment} />
                  <span className="alk-files-crumbs__name">{name}</span>
                </span>
              ) : (
                <button
                  type="button"
                  className="alk-files-crumbs__link"
                  title={name}
                  onClick={() => onNavigate?.(segment.id)}
                >
                  <CrumbMark segment={segment} />
                  <span className="alk-files-crumbs__name">{name}</span>
                </button>
              )}
            </li>
          );
        })}
      </ol>
    </nav>
  );
}

/** The mark a segment wears: the home mark for a member's home, the portal glyph for
 *  something the portal has a page for, nothing for an ordinary folder. */
function CrumbMark({ segment }: { segment: Crumb }) {
  if (segment.home) return <HomeIcon size={14} data-home="true" />;
  return segment.icon ? <Icon name={segment.icon} size={14} /> : null;
}

export default Breadcrumbs;
