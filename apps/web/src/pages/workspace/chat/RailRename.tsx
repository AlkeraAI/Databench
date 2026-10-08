// Renaming a chat where its name is drawn: the name swaps for a field holding
// the whole title, Enter keeps it and Escape abandons it.
//
// The same mechanism the chat header's own title uses, so a rename asked for
// from the rail and a rename asked for from the header behave identically. A
// refusal is not swallowed and is not shown here either: the field reports the
// server's own sentence to whoever drew the row, which reads it out under the
// name rather than letting the name silently spring back.

import { useLayoutEffect, useRef, useState, type ReactElement } from "react";

import { refusalSentence } from "../../../api/errors";

/** What a rejection with nothing to say reads as. The write normally rejects
 *  with the server's own sentence; this is the floor under one that carries
 *  none. */
export const RENAME_REFUSED = "This chat could not be renamed.";

/** A rename the caller already turned into the sentence the reader sees. The
 *  row reads `sentence` out as given; any other rejection goes through
 *  `refusalSentence`. */
export class RenameRefusedError extends Error {
  readonly sentence: string;

  constructor(sentence: string) {
    super(sentence);
    this.name = "RenameRefusedError";
    this.sentence = sentence;
  }
}

export interface RailRenameProps {
  /** The name being retyped. The field opens holding all of it, selected. */
  title: string;
  /** Resolves when the new name has landed; rejects with the reason it did
   *  not, as a `RenameRefusedError` when the caller has worded it. */
  onRename: (title: string) => Promise<void>;
  /** The edit is over, whichever way it ended: kept, abandoned or refused. */
  onDone: () => void;
  /** The reason the write was refused, for the row to read out. */
  onRefused: (reason: string) => void;
  /** What the field is called to a screen reader. */
  fieldLabel?: string;
}

export function RailRename({
  title,
  onRename,
  onDone,
  onRefused,
  fieldLabel = "Chat title",
}: RailRenameProps): ReactElement {
  const [draft, setDraft] = useState(title);
  const [pending, setPending] = useState(false);
  const field = useRef<HTMLInputElement | null>(null);

  // Selected on open: a long name is then one keystroke from being replaced,
  // and the caret still lands inside the field for anyone who types instead.
  useLayoutEffect(() => {
    field.current?.select();
  }, []);

  const commit = (): void => {
    if (pending) return;
    const next = draft.trim();
    // A name emptied is not a rename request: the chat keeps the name it had.
    if (next === "" || next === title) {
      onDone();
      return;
    }
    setPending(true);
    void onRename(next).then(
      () => {
        setPending(false);
        onDone();
      },
      (error: unknown) => {
        setPending(false);
        onRefused(error instanceof RenameRefusedError ? error.sentence : refusalSentence(error, { fallback: RENAME_REFUSED }));
        onDone();
      },
    );
  };

  return (
    <div className="chat-page__row-edit">
      <input
        ref={field}
        type="text"
        className="chat-page__row-field"
        aria-label={fieldLabel}
        autoFocus
        readOnly={pending}
        value={draft}
        onChange={(event) => setDraft(event.target.value)}
        onBlur={commit}
        onKeyDown={(event) => {
          if (event.key === "Enter") {
            event.preventDefault();
            commit();
          } else if (event.key === "Escape") {
            event.preventDefault();
            onDone();
          }
        }}
      />
    </div>
  );
}
