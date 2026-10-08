/**
 * A text file edited live, everyone in it at once.
 *
 * The tab holds the file's live channel (one per file per browser tab, shared
 * with any other editor on it), and once the document has synced draws a
 * CodeMirror editor bound to it. What is typed is the file: the server writes
 * it back to the drive a moment later, so there is nothing to save. A person
 * who may only read sees it change as others type, in a read-only editor.
 *
 * When the file cannot be edited live (the server will not open it, the lane
 * is off, the browser cannot load Loro) the editor says so to its host, which
 * shows the file the ordinary way instead. Loaded only with a file tab that
 * shows text: CodeMirror and Loro stay out of every other page.
 *
 * Text the document could not keep (an edit the server refused, or what was
 * typed and never taken when the file stopped being editable live) is handed
 * to the host as a notice with the text, never dropped: the host outlives this
 * editor, so the offer stays on screen if the editor goes.
 */

import { defaultKeymap, indentWithTab } from "@codemirror/commands";
import { HighlightStyle, defaultHighlightStyle, syntaxHighlighting } from "@codemirror/language";
import { tags } from "@lezer/highlight";
import { Compartment, type Extension } from "@codemirror/state";
import {
  EditorView,
  drawSelection,
  highlightActiveLine,
  highlightActiveLineGutter,
  keymap,
  lineNumbers,
} from "@codemirror/view";
import { useEffect, useRef, useState } from "react";

import { LoroCodeMirrorBinding, fromDocument } from "@/api/realtime/crdt/codeMirrorBinding";
import { acquireLiveFile, type LiveFileState } from "@/api/realtime/crdt/liveFile";
import { EDITS_NOT_KEPT, type LiveNotice, type LiveSavingPaused, type LiveSocket } from "@/api/realtime/crdt/channel";
import type { LoroApi } from "@/api/realtime/crdt/loro";
import { getRealtimeClient } from "@/api/realtime/client";
import type { AccountScope } from "@/lib/accountScope";
import { hueOf } from "@/lib/personHue";
import { liveCarets } from "@/pages/workspace/chat/workspace/liveCarets";

import { languageLoaderFor, wrapsByDefault } from "./fileLanguages";

import "./liveFileEditor.css";

/** What the editor says while a reader may not type. */
export const VIEW_ONLY = "View only";

/** What the editor says while its edits wait on the server, still kept. */
export const NOT_SAVED_YET = "Not saved yet, retrying";

/** What the editor says while the server cannot save the file, by why. */
export function savingPausedMessage(reason: string): string {
  if (reason.startsWith("files.quota_") || reason === "files.user_quota_bytes") {
    return "Changes aren't saving because the storage quota is full.";
  }
  switch (reason) {
    case "leased":
      return "Changes aren't saving while another machine holds this folder.";
    case "no_writer":
      return "Changes aren't saving because nobody here can still edit this file.";
    case "gone":
      return "Changes aren't saving because this file is in the trash.";
    case "too_large":
    case "text_too_large":
      return "Changes aren't saving because the file on the drive is too large to edit here.";
    case "binary":
      return "Changes aren't saving because the file on the drive is no longer text.";
    case "quarantined_lost":
      return "Edits made since the last save were lost, and the drive keeps the last saved version.";
    case "files.frozen":
      return "Changes aren't saving because this drive is over its storage limit.";
    default:
      return "Changes aren't saving right now.";
  }
}

export interface LiveFileEditorProps {
  nodeId: string;
  /** The file's name, which picks its syntax. */
  name: string;
  /** The file cannot be edited live: show it another way. */
  onFallback: (reason: string) => void;
  /** Text the document could not keep, to show with a way to copy it. */
  onNotice: (notice: LiveNotice) => void;
  /** Who is signed in, in which org: what this tab has typed and the server
   *  not yet taken is kept for the next page under them. */
  account: AccountScope;
  /** Seams for tests: the socket and Loro (the portal's own by default). */
  socket?: LiveSocket;
  loadLoro?: () => Promise<LoroApi>;
  /** Soft wrap, as the reader set it in the tab's header; unset, prose wraps
   *  and code does not. */
  wrap?: boolean;
  /** The document's size in bytes (UTF-8), told as it changes: the header's
   *  size reading, which the drive's item lags behind while people type. */
  onBytes?: (bytes: number) => void;
  /** The reader typed into the file (somebody else's change does not count):
   *  the host keeps a preview tab once it has been edited. */
  onLocalEdit?: () => void;
}

/** Syntax colours from the app's own tokens, readable on light and dark. */
const tokenHighlight = HighlightStyle.define([
  { tag: [tags.heading, tags.strong], fontWeight: "600", color: "var(--alkPrimaryText)" },
  { tag: [tags.list, tags.processingInstruction, tags.contentSeparator], color: "var(--alkSecondaryText)" },
  { tag: [tags.keyword, tags.operatorKeyword], color: "var(--alkBrandText)" },
  { tag: [tags.string, tags.special(tags.string)], color: "var(--alkSuccessText)" },
  { tag: [tags.number, tags.bool, tags.atom], color: "var(--alkWarningText)" },
  { tag: [tags.comment, tags.meta], color: "var(--alkTertiaryText)", fontStyle: "italic" },
  { tag: [tags.link, tags.url], color: "var(--alkLinkText)" },
  { tag: tags.emphasis, fontStyle: "italic" },
]);

const theme = EditorView.theme({
  "&": {
    height: "100%",
    color: "var(--alkPrimaryText)",
    backgroundColor: "var(--alkPageBg)",
    fontSize: "var(--alkTextSm)",
  },
  ".cm-scroller": { fontFamily: "var(--alkFontMono)", lineHeight: "var(--alkLeadingRelaxed)" },
  ".cm-gutters": {
    backgroundColor: "var(--alkPageBg)",
    color: "var(--alkTertiaryText)",
    borderRight: "1px solid var(--alkDivider)",
  },
  ".cm-activeLine": { backgroundColor: "var(--alkHoverBg)" },
  ".cm-activeLineGutter": { backgroundColor: "var(--alkHoverBg)" },
  "&.cm-focused": { outline: "none" },
  "&.cm-focused .cm-cursor": { borderLeftColor: "var(--alkPrimaryText)" },
});

export default function LiveFileEditor({
  nodeId,
  name,
  onFallback,
  onNotice,
  account,
  socket,
  loadLoro,
  wrap,
  onBytes,
  onLocalEdit,
}: LiveFileEditorProps) {
  const toldBytes = useRef(onBytes);
  toldBytes.current = onBytes;
  const toldEdit = useRef(onLocalEdit);
  toldEdit.current = onLocalEdit;
  const wraps = wrap ?? wrapsByDefault(name);
  const wrapping = useRef(new Compartment());
  const view = useRef<EditorView | null>(null);
  const wrapsNow = useRef(wraps);
  wrapsNow.current = wraps;
  const [state, setState] = useState<LiveFileState>({ kind: "pending" });
  const [canWrite, setCanWrite] = useState(false);
  const [paused, setPaused] = useState<LiveSavingPaused | null>(null);
  const [unsaved, setUnsaved] = useState(false);
  const host = useRef<HTMLDivElement | null>(null);
  const fallback = useRef(onFallback);
  fallback.current = onFallback;
  const notify = useRef(onNotice);
  notify.current = onNotice;

  useEffect(() => {
    const { file, release } = acquireLiveFile(nodeId, { socket: socket ?? getRealtimeClient(), loadLoro, account });
    // From the moment the file is held, not only once it is live: an offer
    // the channel held while no editor was open is handed over here.
    const unlisten = file.channel.listen({
      notice: (notice) => {
        if (notice !== null) notify.current(notice);
      },
    });
    const stop = file.subscribe((next) => {
      setState(next);
      if (next.kind !== "fallback") return;
      if (next.unacknowledged) notify.current({ message: EDITS_NOT_KEPT, restorable: next.unacknowledged });
      fallback.current(next.reason);
    });
    return () => {
      unlisten();
      stop();
      release();
    };
  }, [account, loadLoro, nodeId, socket]);

  const live = state.kind === "live" ? state : null;

  useEffect(() => {
    if (live === null || host.current === null) return;
    const binding = new LoroCodeMirrorBinding({ channel: live.channel, loro: live.loro, hueOf, carets: liveCarets });
    const language = new Compartment();
    const encoder = new TextEncoder();
    const tellBytes = (text: string) => toldBytes.current?.(encoder.encode(text).length);
    const extensions: Extension = [
      EditorView.updateListener.of((update) => {
        if (update.docChanged) tellBytes(update.state.doc.toString());
        if (update.transactions.some((tr) => tr.docChanged && tr.annotation(fromDocument) !== true)) {
          toldEdit.current?.();
        }
      }),
      binding.keys(),
      lineNumbers(),
      highlightActiveLineGutter(),
      highlightActiveLine(),
      drawSelection(),
      keymap.of([...defaultKeymap, indentWithTab]),
      syntaxHighlighting(tokenHighlight),
      syntaxHighlighting(defaultHighlightStyle, { fallback: true }),
      EditorView.contentAttributes.of({ "aria-label": name }),
      wrapping.current.of(wrapsNow.current ? EditorView.lineWrapping : []),
      language.of([]),
      theme,
    ];
    const editor = new EditorView({ state: binding.createState(extensions), parent: host.current });
    view.current = editor;
    tellBytes(editor.state.doc.toString());
    setCanWrite(live.channel.canWrite);
    setPaused(live.channel.savingPaused);
    setUnsaved(live.channel.unsaved);
    const unlisten = live.channel.listen({
      writable: (writable) => setCanWrite(writable),
      saving: (next) => setPaused(next),
      unsaved: (next) => setUnsaved(next),
    });
    let cancelled = false;
    const load = languageLoaderFor(name);
    if (load !== null) {
      void load()
        .then((support) => {
          if (!cancelled) editor.dispatch({ effects: language.reconfigure(support) });
        })
        .catch(() => undefined);
    }
    return () => {
      cancelled = true;
      unlisten();
      binding.dispose();
      editor.destroy();
      view.current = null;
    };
  }, [live, name]);

  useEffect(() => {
    view.current?.dispatch({ effects: wrapping.current.reconfigure(wraps ? EditorView.lineWrapping : []) });
  }, [wraps]);

  if (state.kind === "fallback") return null;
  return (
    <div className="alk-live-file" data-state={state.kind}>
      {live !== null && (paused !== null || unsaved || !canWrite) ? (
        <div className="alk-live-file__status">
          {paused !== null ? (
            <span className="alk-live-file__saving" role="status">
              {savingPausedMessage(paused.reason)}
            </span>
          ) : unsaved ? (
            <span className="alk-live-file__saving" role="status">
              {NOT_SAVED_YET}
            </span>
          ) : (
            <span className="alk-live-file__mode">{VIEW_ONLY}</span>
          )}
        </div>
      ) : null}
      <div className="alk-live-file__editor" ref={host} aria-busy={live === null} />
    </div>
  );
}
