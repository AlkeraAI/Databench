/**
 * "Save as template…" — turn a chat into something a person can start again.
 *
 * The reusable unit is the chat, not a folder format: saving one copies the
 * files it worked in and carries the brief, so a new chat started from the
 * template opens with those files already in place. Two fields are all a person
 * decides — what to call it, and what the brief should say — and both are
 * optional: left alone, the server titles the template after the chat and
 * writes the brief from the transcript itself.
 *
 * The refusals are the point of the dialog rather than the fields. A chat that
 * never had a working directory has nothing to save, and a drive with no room
 * left refuses before anything is written; both answer with a code, and both
 * are sentences a person can act on rather than a silent dialog that will not
 * close.
 *
 * The write itself lives in the portal's templates API module; this surface
 * knows the fields and the sentences, never a URL.
 */

import { useEffect, useState } from "react";

import { Callout, Modal, TextInput, Textarea } from "@alkera/ui";

import { useSaveAsTemplate, type ChatTemplateRead } from "@/api/chatTemplates";
import { ApiError, refusalSentence } from "@/api/errors";
import type { Item } from "@/api/files";

import { displayNameOf } from "@/lib/files/columns";
import { QUOTA_STATUS } from "@/lib/files/errors";
// The dialog owns its sheet: the chat header and the chat rail open it from
// chunks that never load the files browser's stylesheet.
import "./save-as-template-dialog.css";

/** The sentence one refusal gets. Only the two this dialog can actually provoke
 *  have copy of their own; everything else keeps the server's own words, which
 *  are written for a person. */
export function saveRefusal(error: unknown): string {
  if (!(error instanceof ApiError)) {
    return "The connection dropped before the server answered. Try again.";
  }
  if (error.code === "chat.no_working_directory") {
    return "This chat has no files yet, so there is nothing to save as a template.";
  }
  if (error.code === "files.user_quota_bytes") {
    return "You are out of storage, so the template's files could not be copied. Ask your org or team admin for more room.";
  }
  if (error.code === "files.quota_bytes" || error.status === QUOTA_STATUS) {
    return "Your organization is out of storage, so the template's files could not be copied.";
  }
  return refusalSentence(error, { fallback: "That did not go through." });
}

export interface SaveAsTemplateDialogProps {
  open: boolean;
  /** The chat row being saved. Its object facet names the chat the server
   *  copies from; the node id is not what the route takes. */
  chat: Item | undefined;
  onClose: () => void;
  /** Told with the template the server made, so the caller can send the person
   *  to it. */
  onSaved?: (template: ChatTemplateRead) => void;
}

export function SaveAsTemplateDialog({
  open,
  chat,
  onClose,
  onSaved,
}: SaveAsTemplateDialogProps) {
  const suggested = chat ? displayNameOf(chat) : "";
  const [title, setTitle] = useState(suggested);
  const [brief, setBrief] = useState("");
  const [refusal, setRefusal] = useState<string | null>(null);
  const save = useSaveAsTemplate();

  // Reopening starts from the chat on screen, never from what was typed the
  // last time the dialog was open over a different row.
  useEffect(() => {
    if (open) {
      setTitle(suggested);
      setBrief("");
      setRefusal(null);
    }
  }, [open, suggested]);

  const sourceChatId = chat?.object?.id ?? chat?.id;

  return (
    <Modal
      open={open}
      onClose={onClose}
      title="Save as template"
      size="md"
      confirmLabel={save.isPending ? "Saving…" : "Save template"}
      confirmDisabled={sourceChatId === undefined || save.isPending}
      onConfirm={() => {
        if (sourceChatId === undefined) return;
        const named = title.trim();
        const written = brief.trim();
        setRefusal(null);
        save.mutate(
          {
            source_chat_id: sourceChatId,
            ...(named !== "" && named !== suggested ? { title: named } : {}),
            ...(written !== "" ? { brief: written } : {}),
          },
          {
            onSuccess: (template) => {
              onSaved?.(template);
              onClose();
            },
            onError: (error) => setRefusal(saveRefusal(error)),
          },
        );
      }}
      footerDivided
    >
      <div className="alk-files-template__body">
        <p className="alk-files-template__lede">
          A template keeps this chat&rsquo;s files and its brief. Starting a chat from it opens a
          new chat with a copy of those files already in place.
        </p>
        <TextInput
          label="Name"
          size="md"
          autoComplete="off"
          value={title}
          placeholder={suggested}
          onChange={(event) => setTitle(event.target.value)}
        />
        <Textarea
          label="Brief"
          size="md"
          rows={6}
          value={brief}
          placeholder="Left empty, the brief is written from this chat."
          onChange={(event) => setBrief(event.target.value)}
        />
        {refusal !== null ? (
          <Callout tone="danger" role="alert">
            {refusal}
          </Callout>
        ) : null}
      </div>
    </Modal>
  );
}

export default SaveAsTemplateDialog;
