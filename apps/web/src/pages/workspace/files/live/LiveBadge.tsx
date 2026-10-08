// The one line over a leased folder saying what the reader is looking at, who
// is working, and the way to the chat holding it.
//
// One line, because those were three stacked sentences over the same rows and
// two of them said what this one says. The judgement is `liveState` and the
// wording is `liveCopy`; this only renders them — and stamps the state on the
// element so the styling (and a test) can read the verdict rather than the
// sentence.

import { Link } from "react-router-dom";

import { livenessLine, livenessNotes } from "../liveRoot/liveCopy";
import type { ChatLease, FolderLiveness } from "../liveRoot/liveness";

import "./live.css";

export interface LiveBadgeProps {
  liveness: FolderLiveness;
  /** The event stream is not delivering. The badge must stop claiming live
   *  whatever the lease says — the browser has no way to learn about a save. */
  streamDown?: boolean;
  /** Pinned in tests so a timestamp is deterministic. */
  formatTime?: (iso: string) => string;
  /** The chat whose machine holds the folder, when one does. This line is the
   *  only thing said about that: which chat, and the way there for the people
   *  the chat's own policy admits. */
  chat?: ChatLease | null;
  /** The listing under the badge IS that chat's own files. */
  inChat?: boolean;
}

/** The badge. A dot marks the two states that are moving (live, handing back);
 *  a saved copy gets no dot, because nothing is happening to it. */
export function LiveBadge({
  liveness,
  streamDown = false,
  formatTime,
  chat = null,
  inChat = false,
}: LiveBadgeProps) {
  const line = livenessLine(liveness, { streamDown, formatTime, chat, inChat });
  const state = streamDown ? "persisted" : liveness.state;
  const notes = streamDown ? [] : livenessNotes(liveness);
  const subject = line.subject;
  return (
    <span className="alk-files-live" data-state={state} role="status">
      {state === "persisted" ? null : <span className="alk-files-live__dot" aria-hidden="true" />}
      <span className="alk-files-live__label">
        {line.lead}
        {subject === null ? null : subject.href === null ? (
          <span className="alk-files-live__subject">{subject.text}</span>
        ) : (
          <Link className="alk-files-live__subject" to={subject.href}>
            {subject.text}
          </Link>
        )}
      </span>
      {notes.map((note) => (
        <span className="alk-files-live__note" key={note}>
          {note}
        </span>
      ))}
    </span>
  );
}
