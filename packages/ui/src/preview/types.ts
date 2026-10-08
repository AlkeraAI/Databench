// The preview system: how a file's bytes become something a person can look at.
//
// One registry serves every surface that shows a file — a tab beside a chat, the
// large modal on the Files page, the page a share link lands on — so a type is
// taught to render ONCE and every surface learns it. A renderer declares what it
// matches and what it needs (text, a blob URL, a frame, or nothing at all); the
// host fetches that and hands it back as `content`. A renderer this library ships
// never fetches: bytes cross an authorized boundary, and only the host knows how
// to mint that. A renderer the HOST registers is host code, and may read more of
// the file than its bytes through the host's own API, keyed by `file`.
//
// The types live apart from the registry so a renderer can be declared without
// importing the registry's module state.

import type { ComponentType, ReactNode } from "react";

import type { ChatFilesResolver } from "../primitives/render/Markdown/ChatFiles";

/** What the SERVER knows about the file — the sniffed mime (never the extension),
 *  the name as stored, and the size in bytes. The decision inputs, nothing else. */
export interface PreviewFacts {
  /** The mime the server sniffed from the bytes. */
  mime: string;
  /** The file's name, used only to choose between renderers of the same mime
   *  (a `.md` under `text/plain` reads as markdown, a `.txt` does not). */
  name: string;
  /** Size in bytes. Shown on the card; it never decides who draws the file. */
  size: number;
  /** Whether the file's bytes exist yet — a head version has been committed.
   *  `false` on a node an agent or an upload has created but not written back.
   *
   *  It is NOT "size > 0": a genuinely empty file is synced, and a node adopted
   *  before its bytes commit is not, whatever its size or timestamps say. Absent
   *  means synced — nothing claims a file is still arriving without evidence. */
  synced?: boolean;
  /** The machine a file still on its way is being fetched from. Absent where
   *  nothing is fetching it: no lease, or a machine that is not answering. */
  machine?: string | null;
}

/** What a renderer needs the host to fetch before it can draw.
 *  - `text` — the decoded body as a string.
 *  - `blob` — an object URL for the bytes (an `<img>` / `<video>` source).
 *  - `frame` — a URL a sandboxed frame can load (the bytes never enter this origin).
 *  - `none` — nothing; the renderer draws from the facts alone. */
export type PreviewNeed = "text" | "blob" | "frame" | "none";

/** One pluggable way to show a file. Registered once at module load; the surfaces
 *  never learn the list. */
export interface PreviewRenderer {
  /** Registry key. Registering the same id again REPLACES the earlier renderer,
   *  which is how a host overrides a built-in. */
  id: string;
  /** Higher wins a contested file. A tie goes to the newest registration. The
   *  catch-all fallback sits below everything at -1. */
  priority: number;
  /** Whether this renderer claims the file. Keys on the sniffed mime first; the
   *  name only where two renderers share a mime. */
  match(facts: PreviewFacts): boolean;
  /** What the host must fetch for this file. */
  needs(facts: PreviewFacts): PreviewNeed;
  /** The view settings this renderer offers, declared so a HOST can draw them in
   *  chrome of its own. A surface with room for a second bar (the Files modal)
   *  ignores the list and lets the renderer draw its own; a surface with no room
   *  for one — a tab in a pane the reader drags narrow — reads the list, draws
   *  each setting as a control it owns and asks the renderer to draw none. A new
   *  setting is declared here and every host that reads the list picks it up. */
  viewSettings?: readonly PreviewViewSetting[];
  Component: ComponentType<PreviewProps>;
}

/** One thing about a preview a person may switch — soft wrap on a text file, the
 *  zoom on an image. It names the key it occupies inside the renderer's
 *  `viewState`, which is what lets a host drive it without knowing which renderer
 *  is drawing. */
export interface PreviewViewSetting {
  /** The `viewState` key whose value says whether the setting is on. */
  key: string;
  /** What the control is called — the host's accessible name and its tooltip. */
  label: string;
  /** The glyph a host draws for it. */
  icon?: ReactNode;
  /** The state written when the setting is switched on, and when it is switched
   *  off, merged over what the host is holding. They default to `{[key]: true}`
   *  and `{[key]: false}`; a setting that is not a bare flag — an image's framing,
   *  where turning "actual size" off also drops the wheel's zoom — spells both
   *  out. `key`'s value inside `on` is also what reads back as on. */
  on?: Readonly<Record<string, unknown>>;
  off?: Readonly<Record<string, unknown>>;
}

/** Decoded text, whole or as the windows of it that have landed so far.
 *
 *  A large file is read a window at a time: `loaded` is how many of its `total`
 *  bytes have arrived, and `more()` brings the next window and resolves once it
 *  has landed (it rejects when that read failed, leaving `text` as it was).
 *  Absent, the text is the whole file. The host cuts every window at a line (a
 *  markdown file at a blank line), so `text` never ends inside one.
 *
 *  `whole()` reads the entire file in one request, for an action that means the
 *  file rather than what is on screen — a Copy. */
export interface PreviewText {
  kind: "text";
  text: string;
  loaded?: number;
  total?: number;
  more?: () => Promise<void>;
  whole?: () => Promise<string>;
}

/** The bytes the host fetched, in the shape the plan asked for. */
export type PreviewContent =
  | PreviewText
  | { kind: "blob"; url: string; mime: string }
  | {
      kind: "frame";
      url: string;
      sandboxed: boolean;
      /** The sandboxed document may run its own scripts (an HTML report). It
       *  stays in an opaque origin either way; the content origin's CSP gives it
       *  no network. */
      scripts?: boolean;
      title: string;
    }
  | { kind: "none" };

/** Where a file's preview is in its life.
 *  `gone` — the file is no longer there.
 *  `pending` — the machine holding the folder has not written this version back yet. */
export type PreviewStatus = "loading" | "ready" | "error" | "gone" | "pending";

/** Where a file lives on its drive. */
export interface PreviewFileRef {
  driveId: string;
  itemId: string;
}

export interface PreviewProps {
  facts: PreviewFacts;
  /** Where the file lives, for a renderer the host registered (a notebook's
   *  saved outputs are read beside it). Library renderers never read it. */
  file?: PreviewFileRef;
  content: PreviewContent;
  /** The item's etag. A change means new bytes: a renderer reloads on it. */
  version: string;
  status: PreviewStatus;
  /** Why the preview failed, where the host knows. */
  error?: string;
  /** What the card says while the bytes are `pending`, when the host knows more
   *  than that they are on their way — the machine bringing them is offline, or
   *  busy. Absent, the card says what the facts alone support. */
  pendingReason?: string;
  /** One line the host puts above bytes it DID draw, when those bytes are not
   *  the newest there are: the drive served an older copy than the machine
   *  holds. Absent on a copy that is current. */
  notice?: string;
  onDownload(): void;
  /** Open the file whole, outside the preview — absent where the host has nowhere
   *  to open it. */
  onOpenExternal?(): void;
  /** Resolves paths relative to the file's own folder, so a rendered document can
   *  reach the images and media stored beside it. */
  resolver?: ChatFilesResolver;
  /** Per-renderer view state the host persists across a remount (a sort column, a
   *  zoom, a soft-wrap toggle). Opaque to everyone but the renderer that wrote it. */
  viewState?: unknown;
  onViewState?(state: unknown): void;
  /** Who draws the renderer's own view settings. `"renderer"` (the default) — the
   *  renderer draws its own bar. `"host"` — the host has already drawn them
   *  somewhere of its own, so the renderer draws no bar and reads each setting
   *  out of `viewState` on every render rather than keeping a copy that would
   *  then disagree with the control the person actually used. */
  viewControls?: "renderer" | "host";
}

/** What `planPreview` decided: who draws the file and what the host must fetch.
 *  No file is refused for its size. */
export interface PreviewPlan {
  renderer: PreviewRenderer;
  need: PreviewNeed;
  /** The bytes have not arrived yet, so no renderer could draw them whatever the
   *  type is. A surface deciding whether a file is worth opening must treat this
   *  as "openable" — the card explains the wait — rather than as "nothing here". */
  unsynced: boolean;
}
