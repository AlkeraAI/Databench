// Naming a chat's copy before it is made.
//
// The copy is opened the moment it exists, so the name has to be decided
// first: a name asked for afterwards would mean renaming a chat already on
// screen, and a copy that silently took a name nobody chose left two rows in
// the rail a reader had to tell apart by reading the transcript.
//
// The field opens on the name the server would have given — "<title> (copy)"
// for the owner, the source's own title for anyone copying into their drive —
// so pressing the one key straight through is the same copy it always was.

import { useState, type FormEvent, type ReactElement } from "react";

import { Modal, TextInput } from "@alkera/ui";

export const DUPLICATE_CHAT_TITLE = "Duplicate this chat";
export const COPY_CHAT_TITLE = "Copy this chat to my drive";

export interface DuplicateChatDialogProps {
  /** Whose copy this is: the owner duplicates beside the original, anyone else
   *  takes a copy into their own drive. It decides the heading and the key. */
  owner: boolean;
  /** The name the field opens on, selected, so it is one keystroke from gone. */
  suggestedName: string;
  /** The server's own words when it refused the last attempt. */
  error: string | null;
  pending: boolean;
  onCancel: () => void;
  onSubmit: (name: string) => void;
}

export function DuplicateChatDialog({
  owner,
  suggestedName,
  error,
  pending,
  onCancel,
  onSubmit,
}: DuplicateChatDialogProps): ReactElement {
  const [name, setName] = useState(suggestedName);
  const named = name.trim();
  const submit = (event?: FormEvent): void => {
    event?.preventDefault();
    if (named === "" || pending) return;
    onSubmit(named);
  };

  return (
    <Modal
      open
      onClose={onCancel}
      title={owner ? DUPLICATE_CHAT_TITLE : COPY_CHAT_TITLE}
      sub={
        owner
          ? "A second chat holding this one's transcript. You will be taken into it."
          : "A copy in your own drive, holding this chat's transcript. You will be taken into it."
      }
      size="sm"
      confirmLabel="Duplicate"
      onConfirm={() => submit()}
      confirmDisabled={named === "" || pending}
      confirmBusy={pending}
    >
      <form className="duplicate-chat" onSubmit={submit}>
        <TextInput
          label="Name"
          value={name}
          autoFocus
          onChange={(event) => setName(event.target.value)}
        />
        {error ? (
          <p className="duplicate-chat__error" role="alert">
            {error}
          </p>
        ) : null}
        {/* The visible key lives in the modal's footer; this keeps Enter in the
            name field submitting rather than doing nothing. */}
        <button type="submit" hidden aria-hidden="true" tabIndex={-1} />
      </form>
    </Modal>
  );
}
