/**
 * The details pane: what one selected item is, who can reach it, and — behind a
 * toggle — where its bytes actually live.
 *
 * Two reads back it: `usePermissions(effective)` for the sharing summary, so a
 * grant inherited from an ancestor is named with the ancestor it came from, and
 * `useVersions` for the count that opens the file's history. Both are keyed on
 * the node, so selecting another row swaps the pane's data rather than merging
 * it.
 *
 * The store key and `ino` are developer facts, not product facts: they stay
 * behind a toggle so the pane reads as a file manager's inspector by default.
 */

import type {
  KeyboardEvent as ReactKeyboardEvent,
  MouseEvent as ReactMouseEvent,
  ReactNode,
} from "react";
import { useState } from "react";
import { Link } from "react-router-dom";

import { anchorOfElement, type ContextMenuState } from "@alkera/ui";

import { usePermissions, useVersions, type GrantList, type Item } from "@/api/files";

import { isChatFolder, isFolderObject, isTemplateFolder } from "@/lib/files/chatFolder";
import { BROWSE_FILES, pageDoorOf } from "@/lib/files/openTarget";
import { FileVersionsDialog } from "./FileVersionsDialog";
import { LeaseBadge, LeaseFacetPane } from "./LeaseBadge";
import {
  displayNameOf,
  displayPath,
  formatModified,
  formatSize,
  kindLabel,
  modifiedOf,
  sizeOf,
} from "@/lib/files/columns";
import { OwnerCell } from "./OwnerCell";
import { objectRoute } from "@/lib/files/objectRoute";
import { ShareDialog } from "./ShareDialog";
import { capabilityRefusal } from "./refusalCopy";

/** The two menu openers a host spreads on the element that owns the rows — a
 *  right-click and Shift+F10 — as the actions layer already composes them.
 *  Restated structurally so the pane does not depend on the module that builds
 *  them: it is handed the behaviour, not the wiring. */
export interface PaneMenuTriggers {
  onContextMenu: (event: ReactMouseEvent<HTMLElement>) => void;
  onKeyDown: (event: ReactKeyboardEvent<HTMLElement>) => void;
}

export interface RightPaneProps {
  driveId: string | undefined;
  /** The selected item, or undefined when the selection is empty or plural. */
  item: Item | undefined;
  /** How many rows are selected, so the pane can say so instead of showing one. */
  selectedCount?: number;
  /** The two ways into a folder that is also a page: a chat, a workspace or a
   *  template.
   *  The pane offers exactly what the row's menu and its double-click offer, so
   *  a person who works from the pane is not shown a smaller product. Absent for
   *  every other kind of node. */
  onOpenChat?: (item: Item) => void;
  onViewFiles?: (item: Item) => void;
  /** The row menu's two openers. A person working from the pane rather than the
   *  list still has to reach every action, and reaching them by keyboard is the
   *  whole reason Shift+F10 exists. */
  triggerProps?: PaneMenuTriggers;
  /** Open that same menu at a point — what the pane's "…" button asks for. The
   *  pointer has no right-click target in the pane's header, so the button is
   *  the third opener of the ONE menu, never a second list of actions. */
  openMenuAt?: ContextMenuState["openAt"];
}

/** The brief a chat template's author wrote, as the listing carries it. Read
 *  defensively off the object facet's open metadata bag: a template saved before
 *  the facet carried one has none, and the pane says so rather than rendering an
 *  empty block. */
export function briefOf(item: Item): string | null {
  const brief = item.object?.metadata?.["brief"];
  return typeof brief === "string" && brief.trim() !== "" ? brief : null;
}

/** A file's content hash; a folder has none. */
function hashOf(item: Item): string | null {
  const hash = item.file?.content_hash;
  return typeof hash === "string" && hash !== "" ? hash : null;
}

/** The object key the bytes were written under. Additive on the file facet (the
 *  wire models allow unknown fields), so it is read defensively. */
export function storeKeyOf(item: Item): string | null {
  const facet = (item.file ?? {}) as unknown as Record<string, unknown>;
  for (const name of ["storeKey", "store_key", "key"]) {
    const value = facet[name];
    if (typeof value === "string" && value !== "") return value;
  }
  return null;
}

/** One line per principal who can reach the node, naming the ancestor a grant
 *  descended from — the difference between "shared with Dana" and "Dana can see
 *  this because she can see the folder above it".
 *
 *  Both halves are the server's own words: the grantee's name comes resolved on
 *  the listing (a uuid is not a thing a person can read) and the rung is shown
 *  as the product's label rather than the internal role. A grant whose principal
 *  no longer resolves keeps its id, which is still true and still actionable in
 *  the share dialog. */
export function describeSharing(grants: GrantList | undefined, nodeId: string): string[] {
  // A page that has not read the grants yet, and an answer without a page of
  // them, both mean the same thing here: nothing to say about who can access
  // this. Neither may take the pane down with it.
  if (!grants?.value) return [];
  return grants.value.map((grant) => {
    const who = grant.principalName || grant.principal.id;
    const role = grant.roleLabel ?? grant.role;
    const inherited = grant.grantingNodeId !== null && grant.grantingNodeId !== nodeId;
    return inherited ? `${who} · ${role} · via a parent folder` : `${who} · ${role}`;
  });
}

function Field({
  label,
  value,
  mono,
}: {
  label: string;
  // A node, not a string: the Owner line renders the same cell the listing does.
  value: ReactNode;
  mono?: boolean;
}) {
  return (
    <div className="alk-files-pane__field">
      <dt className="alk-files-pane__label">{label}</dt>
      <dd
        className={
          mono ? "alk-files-pane__value alk-files-pane__value--mono" : "alk-files-pane__value"
        }
      >
        {value}
      </dd>
    </div>
  );
}

export function RightPane({
  driveId,
  item,
  selectedCount = item ? 1 : 0,
  onOpenChat,
  onViewFiles,
  triggerProps,
  openMenuAt,
}: RightPaneProps) {
  const [developer, setDeveloper] = useState(false);
  const [shareOpen, setShareOpen] = useState(false);
  const [versionsOpen, setVersionsOpen] = useState(false);

  const nodeId = item?.id;
  const permissions = usePermissions(driveId, nodeId, { effective: true });
  // Only a file has a history of bytes, and asking for a folder's is a request
  // the server has to refuse. The read is skipped rather than refused.
  const versions = useVersions(driveId, item?.kind === "file" ? nodeId : undefined);

  if (selectedCount > 1) {
    return (
      <aside className="alk-files-pane" aria-label="Details" {...triggerProps}>
        <p className="alk-files-pane__empty">{selectedCount} items selected</p>
      </aside>
    );
  }

  if (!item || !driveId) {
    return (
      <aside className="alk-files-pane" aria-label="Details" {...triggerProps}>
        <p className="alk-files-pane__empty">Select an item to see its details.</p>
      </aside>
    );
  }

  const modified = modifiedOf(item);
  const created = item.attrs?.birthtime ?? null;
  const hash = hashOf(item);
  const storeKey = storeKeyOf(item);
  // A node at the top of a drive has no path above it, and the server sends the
  // empty string for it rather than nothing at all. Both mean "there is none",
  // and a field that renders the empty one reads as a value that failed to load.
  const path = displayPath(item);
  const name = displayNameOf(item);
  const sharing = describeSharing(permissions.data, item.id);
  const templatePage = isTemplateFolder(item) ? objectRoute(item) : null;
  const versionCount = versions.data?.versions?.length ?? null;

  return (
    <aside className="alk-files-pane" aria-label="Details" {...triggerProps}>
      <header className="alk-files-pane__head">
        <h2 className="alk-files-pane__name">{name}</h2>
        {openMenuAt ? (
          <button
            type="button"
            className="alk-files-pane__action"
            aria-haspopup="menu"
            aria-label={`Actions for ${name}`}
            onClick={(event) =>
              openMenuAt(anchorOfElement(event.currentTarget), event.currentTarget)
            }
          >
            …
          </button>
        ) : null}
        {/* {renaming ? (
          <RenameInline driveId={driveId} item={item} onDone={() => setRenaming(false)} />
        ) : (
          <button
            type="button"
            className="alk-files-pane__action"
            onClick={() => setRenaming(true)}
          >
            Rename
          </button>
        )} */}
      </header>

      {isFolderObject(item) ? (
        <div className="alk-files-pane__ways" role="group" aria-label="Open">
          <button
            type="button"
            className="alk-files-pane__action"
            onClick={() => onOpenChat?.(item)}
          >
            {pageDoorOf(item)?.label ?? "Open"}
          </button>
          <button
            type="button"
            className="alk-files-pane__action"
            onClick={() => onViewFiles?.(item)}
          >
            {BROWSE_FILES}
          </button>
        </div>
      ) : null}

      {/* The lease surface reads, and only reads: who has the folder out and on
          which machine, with the whole facet under it. The writes about a lease
          are the row menu's, so the pane states the fact and offers no control
          over it. Drawn only when there is a lease to describe. */}
      {item.lease ? (
        <section className="alk-files-pane__section" aria-labelledby={`alk-pane-lease-${item.id}`}>
          <h3 id={`alk-pane-lease-${item.id}`} className="alk-files-pane__heading">
            Lease
          </h3>
          <LeaseBadge item={item} variant="pane" />
          <LeaseFacetPane item={item} />
        </section>
      ) : null}

      {isChatFolder(item) ? (
        <p className="alk-files-pane__note">
          Trashing this folder trashes the chat&rsquo;s files; the chat itself is deleted from the
          chat page.
        </p>
      ) : null}

      <dl className="alk-files-pane__fields">
        <Field label="Kind" value={kindLabel(item)} />
        <Field label="Size" value={formatSize(sizeOf(item))} />
        <Field label="Created" value={formatModified({ at: created, actor: null })} />
        <Field label="Modified" value={formatModified(modified)} />
        <Field label="Owner" value={<OwnerCell item={item} />} />
        <Field label="Path" value={path === undefined || path === "" ? "—" : path} mono />
        {isTemplateFolder(item) ? (
          <div className="alk-files-pane__field">
            <dt className="alk-files-pane__label">Brief</dt>
            <dd className="alk-files-pane__value">
              {/* The brief is written and read on the template's own page, not
                  typed over in a details pane, so this is a link and not a field. */}
              <span className="alk-files-pane__brief">{briefOf(item) ?? "No brief yet."}</span>
              {templatePage === null ? null : <Link to={templatePage}>Edit</Link>}
            </dd>
          </div>
        ) : null}
        <Field label="Hash" value={hash ?? "—"} mono />
        {item.kind === "file" ? (
          <div className="alk-files-pane__field">
            <dt className="alk-files-pane__label">Versions</dt>
            <dd className="alk-files-pane__value">
              {/* The count is the way in, not a fact on its own: there is no
                  versions route to link to, and the history is read against the
                  node's live version rather than a page's url. */}
              {versionCount === null ? (
                "—"
              ) : (
                <button
                  type="button"
                  className="alk-files-pane__link"
                  onClick={() => setVersionsOpen(true)}
                >
                  {versionCount === 1 ? "1 version" : `${versionCount} versions`}
                </button>
              )}
            </dd>
          </div>
        ) : null}
      </dl>

      <FileVersionsDialog
        driveId={driveId}
        nodeId={item.id}
        open={versionsOpen}
        onClose={() => setVersionsOpen(false)}
        subjectName={name}
      />

      <section className="alk-files-pane__section" aria-labelledby={`alk-pane-sharing-${item.id}`}>
        <h3 id={`alk-pane-sharing-${item.id}`} className="alk-files-pane__heading">
          Who can access
        </h3>
        {sharing.length === 0 ? (
          <p className="alk-files-pane__empty">Only you.</p>
        ) : (
          <ul className="alk-files-pane__list">
            {sharing.map((line) => (
              <li key={line}>{line}</li>
            ))}
          </ul>
        )}
        {/* The summary above is a reading; this is the way to change it. The
            button is present even when the caller may not share, disabled with
            the server's own reason, so the refusal is legible instead of the
            control simply being absent. */}
        <button
          type="button"
          className="alk-files-pane__action"
          disabled={item.capabilities?.can_share !== true}
          title={capabilityRefusal(item, "can_share")}
          onClick={() => setShareOpen(true)}
        >
          Share
        </button>
        <ShareDialog
          driveId={driveId}
          nodeId={item.id}
          open={shareOpen}
          onClose={() => setShareOpen(false)}
          // The heading a person has been reading, not the filesystem name
          // behind it: a chat is stored as `<uuid>.alkerachat`.
          subjectName={name}
        />
      </section>

      {/* Developer facts are for a dev build only: the store key and the inode are
          nothing a customer acts on, so a production build never renders the control. */}
      {import.meta.env.DEV ? (
        <section className="alk-files-pane__section">
          <button
            type="button"
            className="alk-files-pane__action"
            aria-expanded={developer}
            onClick={() => setDeveloper((on) => !on)}
          >
            Developer details
          </button>
          {developer && (
            <dl className="alk-files-pane__fields">
              <Field label="Store key" value={storeKey ?? "—"} mono />
              <Field label="ino" value={String(item.ino)} mono />
            </dl>
          )}
        </section>
      ) : null}
    </aside>
  );
}

export default RightPane;
