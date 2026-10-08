// Portal shell + nav glyphs — the flask's single-weight, rounded-cap line vernacular (no fills,
// no glows). App-local chrome icons; @alkera/ui owns the component-internal glyph set.

import type { CSSProperties, ReactNode } from "react";

const GLYPHS = {
  overview: (
    <>
      <rect x="4" y="4" width="7" height="7" rx="1.4" />
      <rect x="13" y="4" width="7" height="7" rx="1.4" />
      <rect x="4" y="13" width="7" height="7" rx="1.4" />
      <rect x="13" y="13" width="7" height="7" rx="1.4" />
    </>
  ),
  chats: (
    <>
      <path d="M4 6.5A2.5 2.5 0 016.5 4h11A2.5 2.5 0 0120 6.5v7a2.5 2.5 0 01-2.5 2.5H9l-4 4z" />
      <path d="M8 9h8M8 12h5" />
    </>
  ),
  knowledge: (
    <>
      <path d="M5 5.5A1.5 1.5 0 016.5 4H18a1 1 0 011 1v13a1 1 0 01-1 1H6.5A1.5 1.5 0 005 20.5z" />
      <path d="M5 17.5A1.5 1.5 0 016.5 16H19" />
    </>
  ),
  lineage: (
    <>
      <circle cx="6" cy="6" r="2.2" />
      <circle cx="18" cy="6" r="2.2" />
      <circle cx="12" cy="18" r="2.2" />
      <path d="M7.6 7.6l3 8M16.4 7.6l-3 8" />
    </>
  ),
  teams: (
    <>
      <circle cx="9" cy="8" r="3" />
      <path d="M3.5 19a5.5 5.5 0 0111 0" />
      <path d="M16 5.5a3 3 0 010 5.5" />
      <path d="M20.5 19a5.5 5.5 0 00-4-5.3" />
    </>
  ),
  analytics: (
    <>
      <path d="M4 4v15a1 1 0 001 1h15" />
      <path d="M7.5 14.5l3.2-4 2.8 2.2 4.5-6" />
    </>
  ),
  usage: (
    <>
      <path d="M4 20h16" />
      <path d="M7 20v-5M12 20v-10M17 20v-7" />
    </>
  ),
  coins: (
    <>
      <ellipse cx="12" cy="6.6" rx="6" ry="2.6" />
      <path d="M6 6.6v4.2c0 1.44 2.69 2.6 6 2.6s6-1.16 6-2.6V6.6" />
      <path d="M6 10.8v4.2c0 1.44 2.69 2.6 6 2.6s6-1.16 6-2.6v-4.2" />
    </>
  ),
  table: (
    <>
      <rect x="4" y="5" width="16" height="14" rx="1.5" />
      <path d="M4 10h16M10 10v9" />
    </>
  ),
  settings: (
    <>
      <path d="M4 8h9M17 8h3M4 16h3M11 16h9" />
      <circle cx="14.5" cy="8" r="2.3" />
      <circle cx="8.5" cy="16" r="2.3" />
    </>
  ),
  panel: (
    <>
      <rect x="3.5" y="5" width="17" height="14" rx="2" />
      <path d="M9.5 5v14" />
    </>
  ),
  plus: <path d="M12 5v14M5 12h14" />,
  chevron: <path d="M6 9.5l6 6 6-6" />,
  signout: (
    <>
      <path d="M9 4H6a2 2 0 00-2 2v12a2 2 0 002 2h3" />
      <path d="M14 16l4-4-4-4M18 12H9" />
    </>
  ),

  // ---- shared concern glyphs (the flask's single-weight, rounded-cap line) ----
  // centred on y12 so it sits true beside its trigger text.
  chevronDown: <path d="M6 9l6 6 6-6" />,
  // a vertical overflow control (distinct from the horizontal `more`).
  dotsV: (
    <>
      <circle cx="12" cy="5" r="1.4" />
      <circle cx="12" cy="12" r="1.4" />
      <circle cx="12" cy="19" r="1.4" />
    </>
  ),
  mail: (
    <>
      <rect x="3.5" y="5.5" width="17" height="13" rx="2.2" />
      <path d="M4.5 7.5l7.5 5.2 7.5-5.2" />
    </>
  ),
  // Open envelope — the read state of the mail toggle. The flap lifts to a peak; the inner V is the
  // letter showing through, distinguishing it from `mail` (flat top, flap folded down).
  mailOpen: (
    <>
      <path d="M3.5 10l8.5-5.5L20.5 10v6.5a2 2 0 01-2 2H5.5a2 2 0 01-2-2z" />
      <path d="M3.5 10l8.5 5.5L20.5 10" />
    </>
  ),
  // Admin — a shield with an assay check. Role is named + glyphed, never colour alone.
  shield: (
    <>
      <path d="M12 3.9l7 2.4v5.4c0 4.2-3 7.1-7 8.4-4-1.3-7-4.2-7-8.4V6.3z" />
      <path d="M9 12l2.2 2.2L15 10.4" />
    </>
  ),
  user: (
    <>
      <circle cx="12" cy="8.2" r="3.4" />
      <path d="M5.5 19.2a6.5 6.5 0 0113 0" />
    </>
  ),
  // Settings glyphs — drawn in the same single-weight, rounded-cap flask line as the set above.
  users: (
    <>
      <circle cx="9" cy="8" r="3" />
      <path d="M3.5 19a5.5 5.5 0 0111 0" />
      <path d="M16 5.5a3 3 0 010 5.5" />
      <path d="M20.5 19a5.5 5.5 0 00-4-5.3" />
    </>
  ),
  key: (
    <>
      <circle cx="8" cy="8" r="3.6" />
      <path d="M10.6 10.6L19 19M16 16l2.5-2.5M14 14l2 2" />
    </>
  ),
  shieldCheck: (
    <>
      <path d="M12 3.5l7 2.5v5.2c0 4.3-3 7-7 9-4-2-7-4.7-7-9V6z" />
      <path d="M8.8 12l2.3 2.3 4.1-4.6" />
    </>
  ),
  building: (
    <>
      <path d="M5 20V6.5A1.5 1.5 0 016.5 5h7A1.5 1.5 0 0115 6.5V20" />
      <path d="M15 9.5h2.5A1.5 1.5 0 0119 11v9" />
      <path d="M4 20h16" />
      <path d="M8 9h4M8 12.5h4M8 16h4" />
    </>
  ),
  monitor: (
    <>
      <rect x="3.5" y="5" width="17" height="11" rx="2" />
      <path d="M9 20h6M12 16v4" />
    </>
  ),
  device: (
    <>
      <rect x="3" y="5.5" width="14" height="10" rx="1.6" />
      <path d="M2 18.5h16" />
      <rect x="18" y="9" width="4" height="9.5" rx="1.2" />
    </>
  ),
  logout: (
    <>
      <path d="M14 5.5H6.5A1.5 1.5 0 005 7v10a1.5 1.5 0 001.5 1.5H14" />
      <path d="M10.5 12H20M17 9l3 3-3 3" />
    </>
  ),
  alert: (
    <>
      <path d="M12 4.5l8.5 14.5H3.5z" />
      <path d="M12 10v4M12 16.6v.2" />
    </>
  ),
  userPlus: (
    <>
      <circle cx="10" cy="8.1" r="3.2" />
      <path d="M4 19.1a6 6 0 0110.2-4.2" />
      <path d="M17.5 14.1v5M15 16.6h5" />
    </>
  ),
  // Re-parent / move a team — two opposed arrows.
  move: (
    <>
      <path d="M8 8h10M15 5l3 3-3 3" />
      <path d="M16 16H6M9 13l-3 3 3 3" />
    </>
  ),
  // Branch — a team splitting into sub-teams (genealogy fork): a parent over two children.
  branch: (
    <>
      <circle cx="12" cy="5.9" r="1.7" />
      <path d="M12 7.6v2.4M6 16.5v-2.6a1.5 1.5 0 011.5-1.5h9a1.5 1.5 0 011.5 1.5V16.5" />
      <circle cx="6" cy="18.1" r="1.7" />
      <circle cx="18" cy="18.1" r="1.7" />
    </>
  ),
  // The directional descent stamps — membership RISES from a floor, permission DESCENDS from a
  // ceiling. Drawn symmetric about the viewBox mid-line so they sit true beside their text.
  rise: (
    <>
      <path d="M5 18.5h14" />
      <path d="M12 18.5v-9" />
      <path d="M7.5 10L12 5.5l4.5 4.5" />
    </>
  ),
  descend: (
    <>
      <path d="M5 5.5h14" />
      <path d="M12 5.5v9" />
      <path d="M7.5 14L12 18.5l4.5-4.5" />
    </>
  ),
  // Descent — the parentage line: a node descending an elbow to its child.
  descent: (
    <>
      <path d="M7 5v8.5A2.5 2.5 0 009.5 16H17" />
      <path d="M14 13l3 3-3 3" />
    </>
  ),
  up: (
    <>
      <path d="M4 16l5-5 4 4 7-7" />
      <path d="M16 8h4v4" />
    </>
  ),
  ascend: (
    <>
      <path d="M12 19V5" />
      <path d="M5 12l7-7 7 7" />
    </>
  ),
  down: (
    <>
      <path d="M4 8l5 5 4-4 7 7" />
      <path d="M16 16h4v-4" />
    </>
  ),
  download: (
    <>
      <path d="M12 4v10" />
      <path d="M8 10l4 4 4-4" />
      <path d="M5 15.5V18a2 2 0 002 2h10a2 2 0 002-2v-2.5" />
    </>
  ),
  // Named by a trend's own direction (`up` / `down` / `flat`), so this key is only ever reached
  // through that value — never as a literal.
  flat: <path d="M5 12h14" />,
  close: <path d="M6 6l12 12M18 6L6 18" />,
  search: (
    <>
      <circle cx="10.5" cy="10.5" r="6" />
      <path d="M15 15l4.5 4.5" />
    </>
  ),
  check: <path d="M5 12.5l4.5 4.5L19 7" />,
  pencil: (
    <>
      <path d="M4 20h4l10-10-4-4L4 16z" />
      <path d="M13.5 6.5l4 4" />
    </>
  ),
  more: (
    <>
      <circle cx="5" cy="12" r="1.4" />
      <circle cx="12" cy="12" r="1.4" />
      <circle cx="19" cy="12" r="1.4" />
    </>
  ),
  eyeoff: (
    <>
      <path d="M4 4l16 16" />
      <path d="M9.6 5.2A9.3 9.3 0 0112 5c5 0 9 4.5 9 7a11 11 0 01-2.3 3.1M6.5 7.2C4 8.6 2.4 10.9 2 12c.6 1.7 4 6 10 6a9.6 9.6 0 003.4-.6" />
      <path d="M9.9 10a3 3 0 004.1 4.1" />
    </>
  ),
  eye: (
    <>
      <path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7-10-7-10-7Z" />
      <circle cx="12" cy="12" r="3" />
    </>
  ),
  back: <path d="M15 5l-7 7 7 7" />,
  trash: (
    <>
      <path d="M5 7h14" />
      <path d="M9 7V5.5A1.5 1.5 0 0110.5 4h3A1.5 1.5 0 0115 5.5V7" />
      <path d="M6.5 7l.8 11a2 2 0 002 1.9h5.4a2 2 0 002-1.9l.8-11" />
    </>
  ),
  copy: (
    <>
      <rect x="9" y="9" width="11" height="11" rx="2" />
      <path d="M5 15V6a2 2 0 012-2h9" />
    </>
  ),
  refresh: (
    <>
      <path d="M20 6.5V11h-4.5" />
      <path d="M19 11a7 7 0 10-1.2 6.8" />
    </>
  ),
  flask: (
    <>
      <path d="M9 3h6" />
      <path d="M10 3v6.2L5.4 17a2 2 0 001.8 3h9.6a2 2 0 001.8-3L14 9.2V3" />
      <path d="M8.2 14h7.6" />
    </>
  ),
  // A graduated vessel (empty) — the bench's "nothing measured yet" instrument for empty states.
  vessel: (
    <>
      <path d="M8 3h8" />
      <path d="M9.2 3v15.5A2.5 2.5 0 0012 21a2.5 2.5 0 002.8-2.5V3" />
      <path d="M11 8.5h2M11 12h3M11 15.5h2" />
    </>
  ),
  books: (
    <>
      <path d="M4 19h16" />
      <rect x="5.5" y="5" width="3.2" height="14" rx="0.8" />
      <rect x="10" y="8" width="3.2" height="11" rx="0.8" />
      <path d="M15.4 6.6l3-.8 2.2 12.2-3 .8z" />
    </>
  ),
  code: <path d="M9 8l-4 4 4 4M15 8l4 4-4 4" />,
  bulb: (
    <>
      <path d="M9.5 17h5M10.5 20h3" />
      <path d="M12 3a6 6 0 00-3.4 10.9c.5.4.9 1 .9 1.6v.5h5v-.5c0-.6.4-1.2.9-1.6A6 6 0 0012 3z" />
    </>
  ),
  note: (
    <>
      <path d="M6 4h8l5 5v11H6z" />
      <path d="M14 4v5h5M9 13h6M9 16.5h4" />
    </>
  ),
  layers: (
    <>
      <path d="M12 4l8 4-8 4-8-4z" />
      <path d="M4 12l8 4 8-4M4 16l8 4 8-4" />
    </>
  ),
  // A connected plug -- the mark the editor extension gives its own Plugins
  // surface, traced from Tabler's plug-connected (MIT) so the two agree.
  plug: (
    <>
      <path d="M7 12l5 5l-1.5 1.5a3.536 3.536 0 1 1 -5 -5l1.5 -1.5" />
      <path d="M17 12l-5 -5l1.5 -1.5a3.536 3.536 0 1 1 5 5l-1.5 1.5" />
      <path d="M3 21l2.5 -2.5M18.5 5.5l2.5 -2.5" />
      <path d="M10 11l-2 2M13 14l-2 2" />
    </>
  ),
  clock: (
    <>
      <circle cx="12" cy="12" r="7.5" />
      <path d="M12 8v4.4l3 1.8" />
    </>
  ),
  sortAz: <path d="M5 7h11M5 12h7M5 17h4" />,
  funnel: <path d="M4 5h16l-6.2 8v5l-3.6-2v-3z" />,
  folder: <path d="M4 7a1 1 0 011-1h4l2 2h8a1 1 0 011 1v9a1 1 0 01-1 1H5a1 1 0 01-1-1z" />,
  // A workspace: a folder several chats share, drawn as the folder with lines
  // of work on it, so it reads as a place to open rather than a page.
  workspaceFolder: (
    <>
      <path d="M4 7a1 1 0 011-1h4l2 2h8a1 1 0 011 1v9a1 1 0 01-1 1H5a1 1 0 01-1-1z" />
      <path d="M9 12.5h6M9 15.5h4" />
    </>
  ),
  // A chat template: the brief and the files a new chat starts from, drawn as a
  // page with a second one behind it — the thing is a pattern to copy, not a
  // document to read.
  template: (
    <>
      <path d="M9.5 4H14l5 5v9a1.5 1.5 0 01-1.5 1.5h-8A1.5 1.5 0 018 18V5.5A1.5 1.5 0 019.5 4z" />
      <path d="M14 4v5h5" />
      <path d="M5 8v9.5A2.5 2.5 0 007.5 20H15" />
    </>
  ),
  // A closed padlock — the one mark that reads as "nobody else" without a word beside it.
  lock: (
    <>
      <rect x="4.5" y="10.5" width="15" height="9" rx="1.6" />
      <path d="M8 10.5V8a4 4 0 118 0v2.5" />
    </>
  ),
  // Files: a drive platter — the storage surface the Files page browses.
  drive: (
    <>
      <rect x="3.5" y="5" width="17" height="6" rx="1.6" />
      <rect x="3.5" y="13" width="17" height="6" rx="1.6" />
      <path d="M7 8h.01M7 16h.01" />
    </>
  ),
  circleFull: <circle cx="12" cy="12" r="6.5" fill="currentColor" stroke="none" />,
  circleHalf: (
    <>
      <circle cx="12" cy="12" r="6.5" />
      <path d="M12 5.5a6.5 6.5 0 010 13z" fill="currentColor" stroke="none" />
    </>
  ),
  circleRing: <circle cx="12" cy="12" r="6.5" />,
  // A triangle read by its fill: hollow, barred, solid.
  triHollow: <path d="M12 4.2 19.6 19 4.4 19Z" />,
  triBarred: (
    <>
      <path d="M12 4.2 19.6 19 4.4 19Z" />
      <path d="M8.1 14.6h7.8" />
    </>
  ),
  triSolid: <path d="M12 4.2 19.6 19 4.4 19Z" fill="currentColor" stroke="none" />,

  // The knowledge catalog's trust seals: an empty ring when nobody has vouched, the machine
  // itself while only the agent has, a check sealed in a ring once a person or a known source
  // did. The three silhouettes part at the 20px a row renders them with hue removed, which the
  // assay triangle above does not -- it grades lineage, a different concept, and keeps that
  // surface.
  sealUnverified: <circle cx="12" cy="12" r="8.5" />,
  // Material Symbols `robot` (Apache-2.0), authored on Google's 960-unit grid and carried whole
  // rather than redrawn: the group maps that grid onto this set's 24px frame, so the shape is the
  // vendor's own outline. It is the one filled mark in the set, which is what lets a head read at
  // the 16px a row renders it.
  sealAgent: (
    <g transform="translate(0 24) scale(0.025)" fill="currentColor" stroke="none">
      <path d="M160-120v-200q0-33 23.5-56.5T240-400h480q33 0 56.5 23.5T800-320v200H160Zm200-320q-83 0-141.5-58.5T160-640q0-83 58.5-141.5T360-840h240q83 0 141.5 58.5T800-640q0 83-58.5 141.5T600-440H360ZM240-200h480v-120H240v120Zm120-320h240q50 0 85-35t35-85q0-50-35-85t-85-35H360q-50 0-85 35t-35 85q0 50 35 85t85 35Zm28.5-91.5Q400-623 400-640t-11.5-28.5Q377-680 360-680t-28.5 11.5Q320-657 320-640t11.5 28.5Q343-600 360-600t28.5-11.5Zm240 0Q640-623 640-640t-11.5-28.5Q617-680 600-680t-28.5 11.5Q560-657 560-640t11.5 28.5Q583-600 600-600t28.5-11.5ZM480-200Zm0-440Z" />
    </g>
  ),
  sealVerified: (
    <>
      <circle cx="12" cy="12" r="8.5" />
      <path d="M8.4 12.2l2.4 2.4 4.8-5.2" />
    </>
  ),
  // A record's attachment: a link that holds, and the same link broken where the named asset
  // is not in the lineage graph.
  link: (
    <>
      <path d="M10.5 13.5a3.5 3.5 0 0 0 5 0l3-3a3.5 3.5 0 0 0-5-5l-1.5 1.5" />
      <path d="M13.5 10.5a3.5 3.5 0 0 0-5 0l-3 3a3.5 3.5 0 0 0 5 5l1.5-1.5" />
    </>
  ),
  linkOff: (
    <>
      <path d="M15 8.5l1.5-1.5a3.5 3.5 0 0 1 5 5l-2 2" />
      <path d="M9 15.5L7.5 17a3.5 3.5 0 0 1-5-5l2-2" />
      <line x1="3" y1="3" x2="21" y2="21" />
    </>
  ),

  // ---- chat concern glyphs (the flask's single-weight, rounded-cap line) ----
  // The survey map — three vessels descending a watershed, edges flowing down.
  graph: (
    <>
      <rect x="3.5" y="3.5" width="6" height="4.2" rx="1" />
      <rect x="14.5" y="3.5" width="6" height="4.2" rx="1" />
      <rect x="9" y="16.3" width="6" height="4.2" rx="1" />
      <path d="M6.5 7.7v3.3a2 2 0 002 2H12M17.5 7.7v3.3a2 2 0 01-2 2H12M12 13v3.3" />
    </>
  ),
  // The specimen shelf — distillates on a ruled plate.
  artifact: (
    <>
      <path d="M5 4h9l5 5v11H5z" />
      <path d="M14 4v5h5" />
      <path d="M8.5 13h7M8.5 16.2h4.5" />
    </>
  ),
  // The pipette — add a drop of context to the assay (the composer's attach).
  pipette: (
    <>
      <path d="M14.5 4.6l4.9 4.9" />
      <path d="M12.4 6.7l-7.5 7.5a1 1 0 00-.27.5L4 18.4a.6.6 0 00.7.7l3.7-.86a1 1 0 00.5-.27l7.5-7.5z" />
      <path d="M11 8.1l4.9 4.9" />
    </>
  ),
  // Send — a measured arrow up.
  arrowUp: <path d="M12 19V6M6.5 11.5L12 6l5.5 5.5" />,
  // Stop — halt the assay.
  stop: <rect x="6.5" y="6.5" width="11" height="11" rx="2.2" />,
  // Resume — a play triangle, the counterpart to stop (disable ↔ enable a route).
  resume: <path d="M8 6.4 18 12 8 17.6Z" />,
  // Expand into a stacked plate.
  expand: <path d="M14 4h6v6M20 4l-7 7M10 20H4v-6M4 20l7-7" />,
  chevronRight: <path d="M9.5 6l6 6-6 6" />,
  // A settled assay — a check within a ring, drawn in the flask line.
  checkCircle: (
    <>
      <circle cx="12" cy="12" r="8.5" />
      <path d="M8.4 12.2l2.4 2.4 4.8-5.2" />
    </>
  ),
  // A struck assay — a graduated vessel with a slash across it; status never rides on hue alone.
  broke: (
    <>
      <path d="M8 4.5h8M9 4.5v5.2L6.4 16a2 2 0 001.8 2.9h7.6a2 2 0 001.8-2.9L15 9.7V4.5" />
      <path d="M7 7.5l10 9.5" />
    </>
  ),
  // The retort — the model the bench is running.
  retort: (
    <>
      <path d="M9 4h4v4.5l4.4 7.2a2 2 0 01-1.7 3H7.3a2 2 0 01-1.7-3L10 8.5" />
      <path d="M13 4h2" />
    </>
  ),
  // A drafting compass — the graph toolbar's "fit to view".
  compass: (
    <>
      <circle cx="12" cy="5.5" r="1.6" />
      <path d="M11 7l-4.5 12M13 7l4.5 12" />
      <path d="M9.3 13.5h5.4" />
    </>
  ),
  // A reading that leaves the portal — a GitHub commit, a pull request.
  external: (
    <>
      <path d="M18.5 13.5V18a1.5 1.5 0 01-1.5 1.5H6A1.5 1.5 0 014.5 18V7A1.5 1.5 0 016 5.5h4.5" />
      <path d="M14 4.5h5.5V10" />
      <path d="M19.5 4.5L11.5 12.5" />
    </>
  ),
} satisfies Record<string, ReactNode>;

export type IconName = keyof typeof GLYPHS;

export function Icon({
  name,
  size = 18,
  style,
}: {
  name: IconName;
  size?: number;
  style?: CSSProperties;
}) {
  return (
    <svg
      className="alk-ic"
      style={style}
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.7}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
    >
      {GLYPHS[name]}
    </svg>
  );
}
