/**
 * The one row at the foot of the chat's Files tab saying what is happening to
 * the folder above it.
 *
 * A status bar rather than a notice: the folder is being written by a machine
 * for the whole time the chat is awake, so the fact is permanent and belongs
 * where permanent facts go — pinned under the listing, at the size of a
 * footnote, out of the way of the rows a reader came to look at. A line that
 * appeared over the table, moved it down and then vanished was the opposite of
 * that.
 *
 * Every part sits in its own slot in a fixed order — state, machine, time,
 * counts — and a slot with nothing to say renders nothing. The bar is never a
 * sentence, so it cannot grow into three lines when the panel is dragged
 * narrow; and it never shows an id, because what the reader can act on is the
 * machine's name, not the uuid the lease fences on.
 *
 * Because it is permanent, the bar itself is not what a screen reader listens
 * to. Its counts move on every lease frame and its clock re-spells itself every
 * thirty seconds, and a live region over all of that reads a ticking clock at
 * the reader for the life of the conversation. The announcement is a hidden
 * node beside it carrying the state alone, and only when the state changes.
 */

import { useEffect, useRef, useState } from "react";

import { Pill, type PillTone } from "@alkera/ui";

import { liveStatus, type LiveStatusState } from "@/pages/workspace/files/liveRoot/liveCopy";
import type { FolderLiveness } from "@/pages/workspace/files/liveRoot/liveness";

/** What each state is worth on sight: work in progress reads as healthy, a
 *  hand-back as a change under way, a stream that stopped delivering as the one
 *  state a reader may need to do something about. */
const TONES: Record<LiveStatusState, PillTone> = {
  live: "success",
  landing: "success",
  "handing-back": "info",
  persisted: "neutral",
  offline: "warning",
  "stream-down": "warning",
};

/** The floor between two announcements of the same bar. Below it the change is
 *  still on screen; it is only the speech that is dropped. A lease that flaps
 *  would otherwise read the bar out on every frame it flapped in. */
export const ANNOUNCE_EVERY_MS = 5_000;

export interface FilesStatusBarProps {
  liveness: FolderLiveness;
  /** The event stream is not delivering. Nothing the browser holds is this
   *  second's, counts included. */
  streamDown?: boolean;
  /** The browser says it has no network. */
  offline?: boolean;
  /** How long ago the copy on screen was written ("3 min ago"), when the drive
   *  knows. Spelled by the caller, which owns the clock. */
  savedAgo?: string | null;
}

export function FilesStatusBar({
  liveness,
  streamDown = false,
  offline = false,
  savedAgo = null,
}: FilesStatusBarProps) {
  const status = liveStatus(liveness, { streamDown, offline, savedAgo });

  // The state the reader was last told, and when. The first render tells them
  // nothing: they asked for the panel and the bar is part of what they got.
  const spoken = useRef<string | null>(null);
  const spokenAt = useRef<number>(Number.NEGATIVE_INFINITY);
  const counter = useRef(0);
  const [announcement, setAnnouncement] = useState<{ text: string; seq: number } | null>(null);
  const label = status.label;
  useEffect(() => {
    const previous = spoken.current;
    spoken.current = label;
    if (previous === null || previous === label) return;
    const now = Date.now();
    if (now - spokenAt.current < ANNOUNCE_EVERY_MS) return;
    spokenAt.current = now;
    counter.current += 1;
    setAnnouncement({ text: label, seq: counter.current });
  }, [label]);

  return (
    <>
      {/* Keyed on the count, not the text: repeating a state a reader has
          already been told is how a region goes unheard, and the key is what
          makes the repeat a new node when it IS worth saying again. */}
      <p className="alk-ws-status__said" data-vh role="status">
        {announcement ? <span key={announcement.seq}>{announcement.text}</span> : null}
      </p>
      <div className="alk-ws-status" data-state={status.state}>
        <Pill className="alk-ws-status__state" tone={TONES[status.state]} variant="plain" dot>
          {status.label}
        </Pill>
        {status.machine === "" ? null : (
          <span className="alk-ws-status__machine" title={status.machine}>
            {status.machine}
          </span>
        )}
        {status.when === "" ? null : <span className="alk-ws-status__when">{status.when}</span>}
        {status.chips.length === 0 ? null : (
          <span className="alk-ws-status__chips">
            {status.chips.map((chip) => (
              <Pill key={chip} tone="neutral">
                {chip}
              </Pill>
            ))}
          </span>
        )}
      </div>
    </>
  );
}

export default FilesStatusBar;
