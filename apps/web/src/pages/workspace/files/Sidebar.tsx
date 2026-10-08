import { NavLink } from "react-router-dom";

import { Icon, type IconName } from "@/app/icons";

/** The Files rail: the drive's three root containers, then the saved views that are filters over
 *  the same listing, then Trash. The rail is presentation only — the page owns where a place
 *  goes, because a root container is addressed by node id and only the listing knows it yet. */

export type FilesPlaceId = "shared" | "home" | "teams" | "recent" | "starred" | "sharedWithMe" | "leases" | "trash";

export interface FilesPlace {
  id: FilesPlaceId;
  label: string;
  icon: IconName;
  /** A place with a fixed address links; one addressed by node id is a button the page routes. */
  to?: string;
  /** A feed place answers from a drive-scoped route of its own, so it needs no node id and is
   *  reachable the moment the drive is. Only a node-addressed place waits for the listing. */
  feed?: boolean;
}

/** Group one: the drive's root. Group two: saved views. Trash stands alone at the foot. */
export const FILES_PLACES: readonly (readonly FilesPlace[])[] = [
  [
    // Home first: it is where /files lands, so it is the place a person reads first.
    { id: "home", label: "Home", icon: "folder" },
    // Shared and Teams are not on the rail, but the places (and the root containers
    // behind them) exist, so a link or a crumb that reaches them still resolves.
  ],
  [
    { id: "recent", label: "Recent", icon: "clock", feed: true },
    { id: "sharedWithMe", label: "Shared with me", icon: "users", feed: true },
    // Answered by the drive-scoped lease route rather than by a node, like the
    // two feeds above it, so it is reachable the moment the drive is.
    { id: "leases", label: "My leases", icon: "monitor", feed: true },
  ],
  [{ id: "trash", label: "Trash", icon: "trash", to: "/files/trash" }],
];

export interface SidebarProps {
  /** The place the browser is showing, so the rail can mark it current. */
  current?: FilesPlaceId;
  /** Where a node-addressed place goes. Absent while the listing has not resolved it yet, which
   *  is what disables the entry rather than letting a click go nowhere. */
  onSelect?: (place: FilesPlaceId) => void;
  /** The places `onSelect` can actually reach right now. */
  reachable?: readonly FilesPlaceId[];
  /** True once the drive itself has loaded, which is all a feed place needs. */
  driveReady?: boolean;
}

/** Why a place cannot be gone to, or `undefined` when it can.
 *
 *  A rail entry that is greyed out and says nothing teaches a person that the
 *  product is broken. Every refusal here has a sentence, and the sentence is
 *  rendered beside the entry rather than hidden in a `title`, so it reaches a
 *  touch screen and a screen reader as well as a mouse. */
export function placeReason(
  place: FilesPlace,
  options: { onSelect: boolean; reachable: readonly FilesPlaceId[]; driveReady: boolean },
): string | undefined {
  if (place.to !== undefined) return undefined;
  if (!options.onSelect) return "This view is not available here.";
  if (options.reachable.includes(place.id)) return undefined;
  if (place.feed === true) {
    return options.driveReady ? undefined : "Waiting for your drive to load.";
  }
  return "Waiting for your drive to load.";
}

export function Sidebar({ current, onSelect, reachable, driveReady = false }: SidebarProps) {
  const reasonFor = (place: FilesPlace): string | undefined =>
    placeReason(place, {
      onSelect: onSelect !== undefined,
      reachable: reachable ?? [],
      driveReady,
    });

  return (
    <nav className="alk-files-rail" aria-label="Places" aria-busy={driveReady ? undefined : true}>
      {FILES_PLACES.map((group, index) => (
        <ul key={index} className="alk-files-rail__group">
          {group.map((place) => {
            const reason = reasonFor(place);
            return (
            <li key={place.id}>
              {place.to !== undefined ? (
                <NavLink
                  to={place.to}
                  className="alk-files-rail__item"
                  aria-current={current === place.id ? "page" : undefined}
                >
                  <Icon name={place.icon} />
                  <span>{place.label}</span>
                </NavLink>
              ) : (
                <button
                  type="button"
                  className="alk-files-rail__item"
                  aria-current={current === place.id ? "page" : undefined}
                  disabled={reason !== undefined}
                  aria-describedby={reason === undefined || !driveReady ? undefined : `alk-place-why-${place.id}`}
                  onClick={() => onSelect?.(place.id)}
                >
                  <Icon name={place.icon} />
                  <span>{place.label}</span>
                </button>
              )}
              {/* While the drive itself is still loading, the listing's "Opening your files…"
                  already says why nothing can be opened; a copy of it under every place was the
                  second loading line on the same screen. */}
              {reason === undefined || !driveReady ? null : (
                <p className="alk-meta" id={`alk-place-why-${place.id}`}>
                  {reason}
                </p>
              )}
            </li>
            );
          })}
        </ul>
      ))}
    </nav>
  );
}
