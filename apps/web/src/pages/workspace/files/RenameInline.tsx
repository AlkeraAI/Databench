/**
 * Inline rename, from the row (F2, or Enter on macOS) and from the right pane.
 *
 * The new name is on screen before the request leaves — `useRenameItem` patches
 * every cached copy of the row in `onMutate`. When the server refuses, the hook
 * restores the snapshot it took, and this component's job is the other half of
 * that contract: put the field back with the name the person typed and show the
 * API's own reason, so a 412 (someone else changed the row) or a name conflict
 * is a sentence they can act on rather than a silent revert.
 *
 * A row that IS an object renames the OBJECT, not the node. A chat arrives as a
 * `<Title>.alkerachat` folder whose name was minted once from the title and is a
 * filesystem name from then on; the row already renders the object's live title
 * (`displayNameOf`), so renaming the node here would edit a string nobody has
 * seen and leave the title the row shows untouched.
 *
 * A chat template renames through its own route rather than the generic object
 * one: the title is the template's, the folder's README is re-rendered from it,
 * and the generic route refuses the field outright.
 */

import { useCallback, useEffect, useRef, useState } from "react";

import { nameNote, validateChatTitle, validateName, type NameRefusal } from "@alkera/chat-model";

import { useRenameChatTemplate } from "@/api/chatTemplates";
import { isObjectBacked, isOperation, useRenameItem, type Item, type Operation } from "@/api/files";
import { renameRefusal, useRenameChat } from "@/api/objects";
import { isTemplateFolder } from "@/lib/files/chatFolder";
import { displayNameOf } from "@/lib/files/columns";
import { filesErrorCopy, type FilesErrorCopy } from "@/lib/files/errors";

export interface RenameInlineProps {
  driveId: string;
  item: Item;
  /** Closes the editor: committed successfully, or abandoned with Escape. */
  onDone: () => void;
  /** Told when the server ran the rename as an operation, so the page can put
   *  it on the undo stack. This component owns its own mutation, so without
   *  this seam the operation the server named would die here and Cmd+Z after a
   *  rename would have nothing to invert. */
  onOperation?: (operation: Operation) => void;
  /** Told when the rename was answered with the node instead — which is what
   *  `PATCH …/items/{id}` does for EVERY rename, since only an oversized move
   *  is ever queued as an operation. Without this seam `onOperation` never
   *  fires on a rename and the undo stack stays empty, which is why a rename
   *  had no toast and no Cmd+Z while a trash had both. `previousName` is what
   *  the inverse puts back; the node carries the version it fences on. */
  onRenamed?: (renamed: Item, previousName: string) => void;
  /** Labels the field for a screen reader when the row's own name cell is hidden. */
  label?: string;
}

/** What a refused rename says.
 *
 *  The sentence comes from the one table in `errors.ts`, so a lease, a stale
 *  precondition, a name collision and the quota read here exactly as they read
 *  on every other Files surface — the previous local prose quoted the server's
 *  raw message and had no row for a 409 at all. */
export function refusalCopy(refusal: NameRefusal): FilesErrorCopy {
  return { code: `name_${refusal.rule}`, title: refusal.message, retryable: true };
}

export function refusalReason(error: unknown): FilesErrorCopy | null {
  if (error === null || error === undefined) return null;
  return filesErrorCopy(error, { action: "rename" });
}

export function RenameInline({
  driveId,
  item,
  onDone,
  onOperation,
  onRenamed,
  label = "New name",
}: RenameInlineProps) {
  const onObject = isObjectBacked(item);
  const onTemplate = isTemplateFolder(item);
  const shown = onObject ? displayNameOf(item) : item.nameDisplay || item.name;
  const [draft, setDraft] = useState(shown);
  const [reason, setReason] = useState<FilesErrorCopy | null>(null);
  const field = useRef<HTMLInputElement>(null);
  // Escape has already been answered, so the blur the focus restore below causes
  // must not be read as a commit — blur is where clicking away commits.
  const abandoned = useRef(false);
  const rename = useRenameItem();
  const renameChat = useRenameChat();
  const renameTemplate = useRenameChatTemplate();

  useEffect(() => {
    const input = field.current;
    if (!input) return;
    input.focus();
    // Select the stem, not the extension: renaming rarely means retyping ".tsx".
    // A title is not a filename, so it is selected whole.
    const dot = onObject ? -1 : input.value.lastIndexOf(".");
    input.setSelectionRange(0, dot > 0 ? dot : input.value.length);
  }, [onObject]);

  /** Close the editor with the name the row already had, and give the keyboard
   *  back to the row it was taken from.
   *
   *  Without that hand-back the field simply unmounted and `document.activeElement`
   *  fell to `BODY`, so the listing never saw the next keystroke: Cmd+A went to
   *  the browser and selected the text of the whole page instead of the rows. The
   *  row is focused BEFORE the editor is closed, while the element the field sits
   *  in is still on screen to be found. */
  const abandon = useCallback(() => {
    abandoned.current = true;
    field.current?.closest<HTMLElement>("[data-row-id]")?.focus();
    onDone();
  }, [onDone]);

  const pending = rename.isPending || renameChat.isPending || renameTemplate.isPending;

  const commit = useCallback(() => {
    // A rename in flight has already spent this name; a second Enter is not a
    // second request.
    if (pending) return;
    // The raw text, not a trimmed copy: a surrounding space is one of the rules,
    // and trimming it away silently would rename the row to something nobody typed.
    const name = draft;
    if (name === shown) {
      onDone();
      return;
    }
    // The shared table both this field and the server read. A name that breaks
    // it is refused here rather than sent: the filesystem the drive is mirrored
    // onto would refuse it too, and a request spent on that is a round trip a
    // person waits through to be told what the field already knew.
    const refusal = onObject ? validateChatTitle(name) : validateName(name);
    if (refusal !== null) {
      setReason(refusalCopy(refusal));
      return;
    }
    if (onObject) {
      setReason(null);
      if (onTemplate) {
        // The write is fenced on a version, and a listing row carries none: the
        // hook reads the live one. A version invented here would be right for
        // exactly one rename and a 409 for every one after it.
        renameTemplate.mutate(
          { templateId: item.object?.id ?? item.id, title: name },
          {
            onSuccess: () => onDone(),
            onError: (error) =>
              setReason({ code: "rename_refused", title: renameRefusal(error), retryable: true }),
          },
        );
        return;
      }
      renameChat.mutate(
        { chatId: item.object?.id ?? item.id, title: name },
        {
          onSuccess: () => onDone(),
          onError: (error) =>
            setReason({ code: "rename_refused", title: renameRefusal(error), retryable: true }),
        },
      );
      return;
    }
    setReason(null);
    rename.mutate(
      { driveId, itemId: item.id, etag: item.etag, name },
      {
        onSuccess: (result) => {
          if (isOperation(result)) onOperation?.(result);
          else onRenamed?.(result, shown);
          onDone();
        },
        // The hook has already rolled the optimistic patch back; the editor
        // stays open holding what was typed, next to the server's reason.
        onError: (error) => setReason(refusalReason(error)),
      },
    );
  }, [
    pending,
    draft,
    shown,
    onObject,
    onTemplate,
    item,
    driveId,
    rename,
    renameChat,
    renameTemplate,
    onDone,
    onOperation,
    onRenamed,
  ]);

  // Not a refusal: one drive is shared by machines that disagree about what a
  // name may be, so a name only Windows objects to is stored and flagged. The
  // sentence rides along while the name is still legal, and steps aside for a
  // real refusal — two lines under one field contradict each other. A title
  // has no filesystem under it, so it never earns one.
  const note = onObject || reason !== null ? null : nameNote(draft);

  return (
    <span className="alk-files-rename">
      <input
        ref={field}
        type="text"
        className="alk-files-rename__field"
        aria-label={label}
        aria-invalid={reason !== null}
        aria-describedby={
          reason !== null
            ? `alk-rename-error-${item.id}`
            : note !== null
              ? `alk-rename-note-${item.id}`
              : undefined
        }
        value={draft}
        // Read-only rather than disabled: a disabled field drops the keyboard,
        // so a refusal would come back to a field nobody is typing in.
        readOnly={pending}
        aria-busy={pending || undefined}
        onChange={(event) => {
          const next = event.target.value;
          setDraft(next);
          // A refusal answers the text it was raised on. Typing past it clears
          // it the moment the name is legal, so the sentence never contradicts
          // what is in the field.
          if (reason !== null && (onObject ? validateChatTitle(next) : validateName(next)) === null) {
            setReason(null);
          }
        }}
        onClick={(event) => event.stopPropagation()}
        onKeyDown={(event) => {
          event.stopPropagation();
          if (event.key === "Enter") {
            event.preventDefault();
            commit();
          } else if (event.key === "Escape") {
            event.preventDefault();
            abandon();
          }
        }}
        onBlur={() => {
          if (abandoned.current || pending || reason !== null) return;
          // Clicking away is not a commit of a name that cannot be used: the
          // editor closes and the row keeps the name it had. Enter is where a
          // broken name is answered with the rule it broke.
          if ((onObject ? validateChatTitle(draft) : validateName(draft)) !== null) {
            onDone();
            return;
          }
          commit();
        }}
      />
      {reason !== null && (
        <span
          id={`alk-rename-error-${item.id}`}
          className="alk-files-rename__error"
          role="alert"
          data-code={reason.code}
        >
          <strong>{reason.title}</strong>
          {reason.detail ? <span>{reason.detail}</span> : null}
        </span>
      )}
      {note !== null && (
        <span
          id={`alk-rename-note-${item.id}`}
          className="alk-files-rename__note"
          data-code={`name_${note.rule}`}
        >
          {note.message}
        </span>
      )}
    </span>
  );
}

export default RenameInline;
