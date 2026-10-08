/**
 * A chat template's own page — the brief, where it came from, and the two ways
 * out of it.
 *
 * A template is two things the drive keeps together: a ROW carrying the prose
 * its author wrote for whoever starts the next chat, and a FOLDER carrying the
 * files that chat opens with. Files can show the folder and the rail can list
 * the row, but only this page has both in hand, which is why the brief is
 * edited here and nowhere else.
 *
 * Three server facts shape it, and none is re-decided in the page:
 *
 * - **An edit names the version it read.** Two people editing one brief is a
 *   409 the page re-reads from, never a silent overwrite; the text the person
 *   typed survives the refusal, because that is the only part of the exchange
 *   they cannot get back.
 * - **The edit rung lives on the folder.** Reading the row is not permission to
 *   rewrite it, so the editor is withheld from a reader the folder says may not
 *   write — and the server refuses anyway if anything gets past that.
 * - **The row and the folder are different nodes.** `files_node_id` is the
 *   template folder: a new chat starts from it and a share is made over it, so
 *   the grant covers everything inside. The files a reader browses are one level
 *   in, at the working folder the server names on the folder's facet.
 *
 * A template that is not this reader's to see gets the same page as one that
 * does not exist. The refusal and the absence are deliberately indistinguishable:
 * a page that said "you may not read this" would confirm the template exists.
 */

import { useCallback, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";

import { ConfirmDialog, EmptyState } from "@alkera/ui";

import {
  useChatTemplate,
  useDeleteChatTemplate,
  useRenameChatTemplate,
  useUpdateChatTemplate,
  type ChatTemplateRead,
} from "@/api/chatTemplates";
import { ApiError, refusalSentence } from "@/api/errors";
import { useDrive, useItem } from "@/api/files";
import { useSpecificTitle } from "@/app/documentTitle";
import { PERMISSION_MODE_OPTIONS } from "@/lib/preferences/fields";

import { BROWSE_FILES, browseTargetOf } from "@/lib/files/openTarget";
import { formatModified } from "@/lib/files/columns";
import { ShareDialog } from "../files/ShareDialog";

import "./template-page.css";

/** The statuses that mean the server has ANSWERED about this template rather
 *  than failed to: a read refused and a read of something that is not there are
 *  the same page, so neither can be told from the other. */
const ANSWERED = [401, 403, 404];

/** What a refused write says. The server writes its refusals for a person, so
 *  its own sentence is kept; only the conflict gets copy of its own, because the
 *  reader has to know their text is still here and what pressing Save again now
 *  does. */
export function writeRefusal(error: unknown): string {
  if (!(error instanceof ApiError)) {
    return "The connection dropped before the server answered. Try again.";
  }
  if (error.status === 409 || error.code === "version_conflict") {
    return "Someone else changed this template. Save again to keep your version.";
  }
  return refusalSentence(error, { fallback: "That did not go through." });
}

/** The stance a chat started from here runs in, named the way the preferences
 *  page names it rather than as the wire's own token. */
function permissionModeLabel(mode: string): string {
  return PERMISSION_MODE_OPTIONS.find((option) => option.value === mode)?.label ?? mode;
}

function Fact({ label, value }: { label: string; value: string }) {
  return (
    <div className="template-page__fact">
      <dt>{label}</dt>
      <dd>{value}</dd>
    </div>
  );
}

/** The provenance a reader wants before they start from a template: when it was
 *  saved, when its brief last moved, and the stance it carries. */
function Facts({ record }: { record: ChatTemplateRead }) {
  return (
    <section className="template-page__facts" role="group" aria-label="Facts">
      <dl>
        <Fact label="Saved" value={formatModified({ at: record.created_at, actor: null })} />
        <Fact label="Updated" value={formatModified({ at: record.updated_at, actor: null })} />
        <Fact label="Permission mode" value={permissionModeLabel(record.permission_mode)} />
        {record.model?.display_name ? (
          <Fact label="Model" value={record.model.display_name} />
        ) : null}
      </dl>
    </section>
  );
}

export function ChatTemplatePage() {
  const { templateId } = useParams<{ templateId: string }>();
  const navigate = useNavigate();

  const template = useChatTemplate(templateId);
  const record = template.data;
  useSpecificTitle(record?.title);

  // The folder behind the row: it names the files a reader browses, and it is
  // the node a share is made over. Held back until the row names it, so a page
  // that has not loaded asks the drive for nothing.
  const drive = useDrive();
  const driveId = drive.data?.id;
  const nodeId = record?.files_node_id ?? undefined;
  const node = useItem(driveId, nodeId);

  // `null` while the brief on screen is the server's. Typing takes ownership of
  // it; a landed save hands it back, so a brief edited elsewhere appears here
  // without the reader's own text being replaced under their cursor.
  const [draft, setDraft] = useState<string | null>(null);
  // The name being typed, or `null` when the heading is just the heading.
  const [newName, setNewName] = useState<string | null>(null);
  const [refusal, setRefusal] = useState<string | null>(null);
  const [sharing, setSharing] = useState(false);
  const [confirmingDelete, setConfirmingDelete] = useState(false);

  const update = useUpdateChatTemplate();
  const rename = useRenameChatTemplate();
  const remove = useDeleteChatTemplate();

  const brief = draft ?? record?.brief ?? "";
  // Withheld only on a folder that SAYS no. A template whose folder this reader
  // cannot see — Files switched off, a node read that failed — still offers the
  // editor: the server decides the write, and hiding the field on a missing
  // answer would lock an author out of their own brief.
  const mayEdit = node.data?.capabilities?.can_write !== false;

  const saveBrief = useCallback(() => {
    if (templateId === undefined || record === undefined) return;
    setRefusal(null);
    update.mutate(
      { templateId, brief, expectedVersion: record.version },
      {
        onSuccess: () => setDraft(null),
        onError: (error) => {
          setRefusal(writeRefusal(error));
          // Read the row again so the next save is fenced on the version that
          // actually exists. The typed text stays exactly where it is.
          if (error.status === 409) void template.refetch();
        },
      },
    );
  }, [templateId, record, brief, update, template]);

  const saveName = useCallback(() => {
    if (templateId === undefined || record === undefined || newName === null) return;
    const named = newName.trim();
    // Nothing typed, or nothing changed: the same row written back would spend a
    // version and a README re-render on no edit at all.
    if (named === "" || named === record.title) {
      setNewName(null);
      return;
    }
    setRefusal(null);
    rename.mutate(
      { templateId, title: named, expectedVersion: record.version },
      {
        onSuccess: () => setNewName(null),
        onError: (error) => {
          setRefusal(writeRefusal(error));
          if (error.status === 409) void template.refetch();
        },
      },
    );
  }, [templateId, record, newName, rename, template]);

  const deleteTemplate = useCallback(() => {
    if (templateId === undefined) return;
    setRefusal(null);
    remove.mutate(
      { templateId },
      {
        onSuccess: () => {
          setConfirmingDelete(false);
          navigate("/files");
        },
        onError: (error) => {
          setConfirmingDelete(false);
          setRefusal(writeRefusal(error));
        },
      },
    );
  }, [templateId, remove, navigate]);

  if (template.isError && ANSWERED.includes(template.error.status)) {
    return (
      <EmptyState
        title="This isn’t here, or isn’t shared with you."
        action={
          <button type="button" className="alk-btn" onClick={() => navigate("/files")}>
            Go to Files
          </button>
        }
      />
    );
  }
  if (template.isError) {
    return (
      <EmptyState
        tone="alert"
        title="This template could not be loaded."
        details={refusalSentence(template.error, { fallback: "" }) || undefined}
        action={
          <button type="button" className="alk-btn" onClick={() => void template.refetch()}>
            Try again
          </button>
        }
      />
    );
  }
  if (record === undefined) return <p className="alk-caption">Loading…</p>;

  return (
    <div className="template-page">
      <header className="template-page__head">
        {newName === null ? (
          <h1 className="alk-h2">{record.title}</h1>
        ) : (
          <form
            className="template-page__rename"
            onSubmit={(event) => {
              event.preventDefault();
              saveName();
            }}
          >
            <input
              type="text"
              aria-label="Name"
              autoComplete="off"
              autoFocus
              value={newName}
              onChange={(event) => setNewName(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Escape") setNewName(null);
              }}
            />
            <button type="submit" className="alk-btn" data-variant="primary">
              Save
            </button>
            <button
              type="button"
              className="alk-btn"
              data-variant="secondary"
              data-fill="outline"
              onClick={() => setNewName(null)}
            >
              Cancel
            </button>
          </form>
        )}
        <div className="template-page__actions">
          <button
            type="button"
            className="alk-btn"
            data-variant="primary"
            // The template's own folder, which is what the chat surface takes as
            // its source: it holds the brief AND the files, and copying starts
            // from there.
            onClick={() => navigate(`/chat?source=${encodeURIComponent(nodeId ?? record.id)}`)}
          >
            New chat
          </button>
          <button
            type="button"
            className="alk-btn"
            data-variant="secondary"
            data-fill="outline"
            // One level in: the working folder the server named on the facet,
            // which is what a chat started from here begins with.
            onClick={() =>
              navigate(`/files/${node.data ? browseTargetOf(node.data) : (nodeId ?? "")}`)
            }
            disabled={nodeId === undefined}
          >
            {BROWSE_FILES}
          </button>
          <button
            type="button"
            className="alk-btn"
            data-variant="secondary"
            data-fill="outline"
            onClick={() => setSharing(true)}
            disabled={nodeId === undefined || driveId === undefined}
          >
            Share…
          </button>
          {mayEdit && newName === null ? (
            <button
              type="button"
              className="alk-btn"
              data-variant="secondary"
              data-fill="outline"
              onClick={() => setNewName(record.title)}
            >
              Rename
            </button>
          ) : null}
          <button
            type="button"
            className="alk-btn"
            data-variant="destructive"
            data-fill="outline"
            onClick={() => setConfirmingDelete(true)}
          >
            Delete…
          </button>
        </div>
      </header>

      {refusal !== null ? (
        <p className="template-page__error alk-danger" role="alert">
          {refusal}
        </p>
      ) : null}

      <section className="template-page__brief" aria-labelledby="template-brief">
        <h2 id="template-brief" className="alk-h4">
          Brief
        </h2>
        {mayEdit ? (
          <>
            <textarea
              aria-label="Brief"
              className="template-page__editor"
              rows={10}
              value={brief}
              placeholder="No brief yet. Write what a new chat should be told."
              onChange={(event) => setDraft(event.target.value)}
            />
            <div className="template-page__save">
              <button
                type="button"
                className="alk-btn"
                data-variant="primary"
                disabled={update.isPending || draft === null}
                onClick={saveBrief}
              >
                {update.isPending ? "Saving…" : "Save brief"}
              </button>
            </div>
          </>
        ) : (
          <p className="template-page__read-only">{record.brief || "No brief yet."}</p>
        )}
      </section>

      <Facts record={record} />

      {sharing && driveId !== undefined && nodeId !== undefined ? (
        <ShareDialog
          driveId={driveId}
          nodeId={nodeId}
          // The folder is stored as `<Title>.alkerachat.template`, so the dialog's
          // own read would title it with a filesystem name.
          subjectName={record.title}
          open
          onClose={() => setSharing(false)}
        />
      ) : null}

      <ConfirmDialog
        open={confirmingDelete}
        onClose={() => setConfirmingDelete(false)}
        title={`Delete ${record.title}?`}
        consequence="It and its files go away for everyone it is shared with. Chats already started from it are not affected."
        confirmLabel="Delete template"
        tone="destructive"
        busy={remove.isPending}
        onConfirm={deleteTemplate}
      />
    </div>
  );
}

export default ChatTemplatePage;
