/**
 * "Share" — who can reach one node, and how to change it.
 *
 * The dialog is deliberately self-sufficient: it takes a drive and a node id and
 * reads everything else itself. That is what lets a surface with no Files row in
 * hand — a chat header, say — open it with the same four props the browser
 * passes, and it is why the prop signature is treated as a contract rather than
 * an implementation detail.
 *
 * Three server facts shape what it renders, and none of them is re-decided here:
 *
 * - **The server decides every control.** The listing names the rungs the
 *   reader may hand out (`assignableRoles`, each with its label), and on every
 *   row what it is called (`roleLabel`), whether it is the owner (`isOwner`) and
 *   whether it may be moved or withdrawn here (`canChange`, `canRemove`), all
 *   from the policy that enforces the write. A missing answer is a no.
 * - **A grant bubbles down and can only be taken back where it was made.** An
 *   inherited row is therefore disabled with the folder it came from, and the
 *   server's own 409 (`files.inherited_grant`) says the same thing if anything
 *   ever gets past the disabled control. The dialog does not invent a second
 *   rule; it shows the one the tree already enforces.
 * - **Both listings are needed.** The direct grants carry the share id a revoke
 *   is addressed by; the effective set carries the ancestor a grant descended
 *   from and no id at all. A dialog that read only one could either not remove
 *   anything or not explain anything.
 *
 * Changing someone's role is ONE request. The grant route moves the standing
 * share to the new rung, so there is no old row to withdraw afterwards — and
 * nothing in between for a second click, a refusal or a closed laptop to land
 * in. The row's controls are held while that request is out and the select
 * shows the rung being asked for, because a control that springs back to the
 * old value invites the same change again.
 */

import { useCallback, useEffect, useId, useMemo, useState } from "react";
import {
  Avatar,
  Button,
  Callout,
  CopyIcon,
  IconChip,
  Identity,
  Modal,
  SEARCH_DEBOUNCE_MS,
  Select,
  TextInput,
  useDebouncedValue,
} from "@alkera/ui";

import type { ApiError } from "@/api/errors";
import { Icon } from "@/app/icons";
import {
  useGrant,
  useItem,
  usePermissions,
  useRevoke,
  useShareCandidates,
  type GrantList,
  type Item,
} from "@/api/files";

import { filesErrorCopy, type FilesErrorCopy } from "@/lib/files/errors";
import { CAP_REFUSAL, capabilityRefusal } from "./refusalCopy";

import { copyLinkTo, linkTargetsFor, type LinkTarget } from "@/lib/files/links";
import { displayNameOf } from "@/lib/files/columns";
import { granted } from "@/lib/capabilities";
// The dialog owns its sheet: it opens from chunks (the chat header, a chat's
// file tab) that never load the files browser's stylesheet.
import "./share-dialog.css";

/** One grant line as the wire spells it. */
type Grant = GrantList["value"][number];

/** A rung the reader may hand out here, as the server names it. */
type RoleOption = NonNullable<GrantList["assignableRoles"]>[number];

/** What a row's rung is called: the server's label, else the rung itself. */
function rowRoleLabel(grant: Grant): string {
  return grant.roleLabel ?? grant.role;
}

/** The rung a row is selected as: the offered rung the server reads it as
 *  (a withdrawn one shows as the rung it behaves like), else its own. */
function rowShownRole(grant: Grant): string {
  return grant.shownRole ?? grant.role;
}

/** How a row is addressed while a write against it is in flight. The principal
 *  rather than the share id: the id is what changes when a grant is re-made,
 *  and the person on the row is what does not. */
function rowKey(grant: Grant): string {
  return `${grant.principal.kind}:${grant.principal.id}`;
}

/** A grant this node did not make itself. The effective listing names the
 *  ancestor; a direct grant names this node or nothing at all. */
function isInherited(grant: Grant, nodeId: string | undefined): boolean {
  const from = grant.grantingNodeId;
  return typeof from === "string" && from !== nodeId;
}

/** What a row says when nothing on the wire names who the grant is for. */
const UNKNOWN_PRINCIPAL = "Unknown principal";

/** The principal kinds this build knows how to talk about. `kind` rides the wire
 *  as a bare string, so the set is open: a grant made to something else — a
 *  service, an agent, whatever the ladder grows — is still a real grant this
 *  dialog has to show. */
const NAMED_KINDS: ReadonlySet<string> = new Set(["user", "team"]);

/** Who a grant is for, as a person reads it: the resolved name, else the raw id
 *  — never blank, because a row with no label is a row nobody can act on.
 *
 *  A kind this build cannot name has no id worth printing as a name: a bare uuid
 *  reads as somebody's handle and tells the person nothing. That row says what it
 *  is instead, keeps the id beside it, and keeps Remove live — access nobody can
 *  identify is exactly the access that has to stay withdrawable. */
function granteeLabel(grant: Grant): string {
  const named = grant.principalName;
  if (typeof named === "string" && named.trim() !== "") return named;
  return NAMED_KINDS.has(grant.principal.kind) ? grant.principal.id : UNKNOWN_PRINCIPAL;
}

/** How a control on the row is addressed by assistive tech. Two unnameable
 *  principals would otherwise carry the same accessible name, so those rows —
 *  and only those — carry the id as well. */
function granteeAria(grant: Grant): string {
  const label = granteeLabel(grant);
  return label === UNKNOWN_PRINCIPAL ? `${label} ${grant.principal.id}` : label;
}

/** Why an inherited row's controls are held, said on the controls themselves. */
const INHERITED_NOTE = "This access is inherited. Change it on the folder it comes from.";

/** The monogram for a person's disc: first and last initial, or the first two
 *  letters of a one-word name. */
function initialsOf(label: string): string {
  const parts = label.trim().split(/\s+/).filter(Boolean);
  if (parts.length === 0) return "?";
  const first = parts[0]!;
  const last = parts[parts.length - 1]!;
  const letters = parts.length > 1 ? `${first[0]}${last[0]}` : first.slice(0, 2);
  return letters.toUpperCase();
}

/** The leading mark on a row or a match: a team reads as a team, anything else
 *  as the disc every member list in the app uses. */
function Mark({ kind, label }: { kind: string; label: string }) {
  if (kind === "team") {
    return (
      <IconChip size={32} tone="neutral" aria-hidden="true">
        <Icon name="teams" size={16} />
      </IconChip>
    );
  }
  return <Avatar initials={label === UNKNOWN_PRINCIPAL ? "?" : initialsOf(label)} />;
}

/** Someone a grant can be made to. Users and teams are the two kinds the share
 *  route accepts from a person, and both are picked from the org directory the
 *  server searches for a caller who may share this node — never by probing the
 *  grant route with a typed id. */
interface Candidate {
  kind: "user" | "team";
  id: string;
  name: string;
  /** The second line in the picker: an email, or what a team is. */
  hint: string;
}

export interface ShareDialogProps {
  driveId: string | undefined;
  /** The node being shared. */
  nodeId: string | undefined;
  open: boolean;
  onClose: () => void;
  /** What to call the thing being shared, when the node's own name is not what
   *  the reader knows it by.
   *
   *  A file IS its name, so the browser passes nothing and the dialog reads the
   *  node. An object-backed node is not: a chat is stored as
   *  `<uuid>.alkerachat`, so a header that opens this dialog over a chat was
   *  asking somebody to share a 36-character id and trust they picked the right
   *  one. The caller that knows the human name says it here. */
  subjectName?: string;
  /** Sharing a workspace (or a chat, a workspace of one) shares every
   *  connection its owner may use: the dialog says so to the sharer. */
  sharesConnections?: boolean;
}

export const SHARES_CONNECTIONS_NOTE = "People you share this with can query data through your connections.";

export function ShareDialog({
  driveId,
  nodeId,
  open,
  onClose,
  subjectName,
  sharesConnections = false,
}: ShareDialogProps) {
  const node = useItem(driveId, open ? nodeId : undefined);
  // Both listings, because they answer different halves of the dialog: the direct
  // one carries the share id a revoke needs, the effective one the ancestor an
  // inherited grant came from.
  const direct = usePermissions(driveId, open ? nodeId : undefined);
  const effective = usePermissions(driveId, open ? nodeId : undefined, { effective: true });
  const grant = useGrant();
  const revoke = useRevoke();

  const [query, setQuery] = useState("");
  /** The suggestion the arrow keys are on; Enter picks it. */
  const [active, setActive] = useState(0);
  const listId = useId();
  const inheritedNoteId = useId();
  const [picked, setPicked] = useState<Candidate | null>(null);
  /** The rung picked for a new person; empty means the weakest on offer. */
  const [role, setRole] = useState<string>("");
  const [refusal, setRefusal] = useState<FilesErrorCopy | null>(null);
  /** Which link is on the clipboard, and whether the last attempt was refused.
   *  Two pieces of state rather than one enum because a dialog offering two
   *  links has to say WHICH one landed there. */
  const [copied, setCopied] = useState<string | null>(null);
  const [copyFailed, setCopyFailed] = useState(false);
  /** Rows with a write of their own in flight, each carrying the rung to show
   *  meanwhile — `null` for a removal, which asks for no rung. A row is held by
   *  its own write only: one person's change is no reason to freeze another's. */
  const [inFlight, setInFlight] = useState<Readonly<Record<string, string | null>>>({});

  useEffect(() => {
    if (open) {
      setQuery("");
      setPicked(null);
      setRole("");
      setRefusal(null);
      setCopied(null);
      setCopyFailed(false);
      setInFlight({});
    }
  }, [open]);

  const settle = useCallback((key: string) => {
    setInFlight((held) => {
      const rest: Record<string, string | null> = { ...held };
      delete rest[key];
      return rest;
    });
  }, []);

  const item: Item | undefined = node.data;
  const canShare = item?.capabilities?.can_share === true;
  // The directory is searched by the server, as a share of THIS node: a member
  // who may share their own file may name anyone in the org, and nobody needs
  // to be an org admin to do it. The query waits out a burst of typing.
  const [settledQuery, caughtUp] = useDebouncedValue(query.trim(), SEARCH_DEBOUNCE_MS);
  const directory = useShareCandidates(driveId, nodeId, settledQuery, open && canShare);
  // Why not, in the one wording every surface uses for the server's reason
  // (`no_reshare`, say, or the rung the reader holds) — never the bare code.
  const shareRefusal =
    item === undefined || canShare
      ? undefined
      : (capabilityRefusal(item, "can_share") ?? CAP_REFUSAL.can_share);

  /** Every row the dialog shows: the direct grants first, then the grants this
   *  node only inherits, which are shown for context and cannot be edited here. */
  const rows = useMemo(() => {
    const directRows = direct.data?.value ?? [];
    const inherited = (effective.data?.value ?? []).filter((row) => isInherited(row, nodeId));
    return [
      ...directRows.map((row) => ({ grant: row, inherited: false })),
      ...inherited.map((row) => ({ grant: row, inherited: true })),
    ];
  }, [direct.data, effective.data, nodeId]);

  /** Who is not on the node yet. A principal that already has a direct grant is
   *  off the picker: their row is the place to change what they have. The owner
   *  never reaches it: the server leaves them out of the candidates. A grant
   *  inherited from above stays offerable, since a direct one here may raise it. */
  const candidates = useMemo<Candidate[]>(() => {
    const taken = new Set(
      (direct.data?.value ?? []).map((row) => `${row.principal.kind}:${row.principal.id}`),
    );
    const found: Candidate[] = [];
    for (const one of directory.data?.value ?? []) {
      // Only the two kinds a person may share with; a kind the directory grows
      // later is left out rather than offered as something the grant refuses.
      const kind = one.principal.kind;
      if (kind !== "user" && kind !== "team") continue;
      found.push({
        kind,
        id: one.principal.id,
        name: one.name,
        hint:
          kind === "user"
            ? (one.email ?? "")
            : one.isOrg === true
              ? "Everyone in the organization"
              : "Team",
      });
    }
    return found.filter((one) => !taken.has(`${one.kind}:${one.id}`)).slice(0, 8);
  }, [directory.data, direct.data]);

  /** The rungs the reader may hand out here, weakest first, as the server says. */
  const offered: readonly RoleOption[] = direct.data?.assignableRoles ?? [];
  const newRole = role !== "" ? role : (offered[0]?.role ?? "");

  const suggesting = picked === null && query.trim() !== "";
  /** What the suggestion area says instead of a list. A refused or failed search
   *  is said as such — "nobody matches" would tell someone their colleague is
   *  not in the org when the truth is the question was never answered. */
  const searchNote: { text: string; state: "searching" | "failed" | "empty" } | null =
    !suggesting || candidates.length > 0
      ? null
      : directory.isError
        ? { text: "People and teams could not be searched.", state: "failed" }
        : !caughtUp || directory.isFetching || directory.data === undefined
          ? { text: "Searching…", state: "searching" }
          : { text: `Nobody here matches “${query.trim()}”.`, state: "empty" };
  const pick = useCallback((one: Candidate) => {
    setPicked(one);
    setQuery("");
    setActive(0);
  }, []);

  /** The node's version, read back after a write moved it.
   *
   * Every Files mutation is fenced on `If-Match`, and a grant bumps the counter
   * — so the etag this dialog opened with is spent the moment the first write
   * lands. A role change, which is two writes, asks the server for the version
   * the second one must name rather than reusing the first one's.
   */
  const freshEtag = useCallback(async (): Promise<string | undefined> => {
    const again = await node.refetch();
    return again.data?.etag;
  }, [node]);

  const explain = useCallback(
    (error: unknown, grantedBy?: string) =>
      setRefusal(filesErrorCopy(error as ApiError, grantedBy ? { grantedBy } : {})),
    [],
  );

  /** A grant the dialog sent at a version the node has already left.
   *
   * The dialog holds the etag it opened with, and anything at all — another
   * person sharing, the chat behind the node writing a turn — moves it. Both
   * refusals mean the same thing to the person clicking Share: not "you may
   * not", but "say which version again".
   */
  const isStale = (error: unknown): boolean => {
    const status = (error as ApiError | undefined)?.status;
    return status === 412 || status === 428;
  };

  /** Send one grant write at the version the dialog holds, and once more at
   *  the node's current version if that one was spent. Once, and only once: a
   *  second stale answer is a node under active change, and retrying forever
   *  would hide that behind a spinner. Every write in the dialog goes through
   *  here, so a role change or a removal is never refused for an edit that
   *  landed after the dialog opened. */
  const atFreshVersion = useCallback(
    async (send: (etag: string) => Promise<unknown>, opened: string): Promise<void> => {
      try {
        await send(opened);
      } catch (error) {
        if (!isStale(error)) throw error;
        const etag = await freshEtag();
        if (etag === undefined) throw error;
        await send(etag);
      }
    },
    [freshEtag],
  );

  const addPerson = useCallback(async () => {
    if (!driveId || !nodeId || !picked || !item || newRole === "") return;
    setRefusal(null);
    try {
      await atFreshVersion(
        (etag) =>
          grant.mutateAsync({
            driveId,
            itemId: nodeId,
            etag,
            principal: { kind: picked.kind, id: picked.id },
            role: newRole,
          }),
        item.etag,
      );
      setPicked(null);
      setQuery("");
    } catch (error) {
      explain(error);
    }
  }, [driveId, nodeId, picked, item, grant, newRole, atFreshVersion, explain]);

  const changeRole = useCallback(
    async (row: Grant, next: string) => {
      if (!driveId || !nodeId || !item || rowShownRole(row) === next) return;
      const key = rowKey(row);
      // A row already writing sends nothing more: the second request would name
      // the same spent version, and two grants admitted at once would leave
      // the person on two rows.
      if (key in inFlight) return;
      setRefusal(null);
      setInFlight((held) => ({ ...held, [key]: next }));
      try {
        // One request. The grant route moves the share this person already
        // holds here, so there is no second row to withdraw.
        await atFreshVersion(
          (etag) =>
            grant.mutateAsync({
              driveId,
              itemId: nodeId,
              etag,
              principal: { kind: row.principal.kind, id: row.principal.id },
              role: next,
            }),
          item.etag,
        );
      } catch (error) {
        explain(error);
      } finally {
        settle(key);
      }
    },
    [driveId, nodeId, item, grant, inFlight, settle, explain, atFreshVersion],
  );

  const removePerson = useCallback(
    async (row: Grant) => {
      const shareId = row.id;
      if (!driveId || !nodeId || !item || typeof shareId !== "string") return;
      const key = rowKey(row);
      if (key in inFlight) return;
      setRefusal(null);
      setInFlight((held) => ({ ...held, [key]: null }));
      try {
        await atFreshVersion(
          (etag) => revoke.mutateAsync({ driveId, itemId: nodeId, etag, shareId }),
          item.etag,
        );
      } catch (error) {
        explain(error);
      } finally {
        settle(key);
      }
    },
    [driveId, nodeId, item, revoke, inFlight, settle, explain, atFreshVersion],
  );

  /** Every link this node offers, best first.
   *
   *  A row is not always one thing: a chat is a conversation AND a folder of
   *  files, so the person sharing it has to be given both and told which is
   *  which. The list comes from the link registry rather than a branch here, so
   *  an object type added later gets its links without editing this dialog.
   *
   *  Before the node read lands there is still an address to copy — the dialog
   *  was opened on a node id — so the link section is never empty while the rest
   *  of the dialog loads. */
  const targets = useMemo<LinkTarget[]>(() => {
    if (item) return linkTargetsFor(item);
    if (nodeId === undefined) return [];
    return [{ id: "files", label: "Copy link", href: `${window.location.origin}/files/${nodeId}` }];
  }, [item, nodeId]);

  /** Put one link on the clipboard and say what happened. "Link copied" is
   *  reported only once the write has RESOLVED: a clipboard write is
   *  permission-gated and asynchronous, and a button that flips on the call
   *  tells someone their link is on the clipboard when the browser has just
   *  refused it — so they paste the previous one somewhere it does not belong. */
  const copyLink = useCallback(async (target: LinkTarget): Promise<void> => {
    const outcome = await copyLinkTo(target.href);
    setCopied(outcome === "copied" ? target.id : null);
    setCopyFailed(outcome === "failed");
  }, []);

  // The caller's name for the subject outranks the node's own: it is the one a
  // reader recognises, and it is known before the node read lands.
  const subject = subjectName ?? (item ? displayNameOf(item) : "");
  const title = subject ? `Share “${subject}”` : "Share";

  return (
    <Modal open={open} onClose={onClose} title={title} size="md" className="alk-files-share">
      <div className="alk-files-share__body">
        {shareRefusal !== undefined && !canShare ? (
          <Callout tone="neutral" role="status">
            {shareRefusal}
          </Callout>
        ) : null}

        {canShare ? (
          <div className="alk-files-share__add">
            <div className="alk-files-share__controls">
              <TextInput
                label="Add people and teams"
                type="search"
                size="md"
                rootClassName="alk-files-share__search"
                autoComplete="off"
                spellCheck={false}
                placeholder="Name, email or team"
                value={picked ? picked.name : query}
                role="combobox"
                aria-autocomplete="list"
                aria-expanded={suggesting}
                aria-controls={suggesting ? listId : undefined}
                aria-activedescendant={
                  suggesting && candidates[active] ? `${listId}-${active}` : undefined
                }
                onChange={(event) => {
                  setPicked(null);
                  setActive(0);
                  setQuery(event.target.value);
                }}
                onKeyDown={(event) => {
                  if (!suggesting || candidates.length === 0) return;
                  if (event.key === "ArrowDown") {
                    event.preventDefault();
                    setActive((at) => Math.min(at + 1, candidates.length - 1));
                  } else if (event.key === "ArrowUp") {
                    event.preventDefault();
                    setActive((at) => Math.max(at - 1, 0));
                  } else if (event.key === "Enter") {
                    const one = candidates[active];
                    if (one === undefined) return;
                    event.preventDefault();
                    pick(one);
                  }
                }}
              />
              <Select
                aria-label="Role for new people"
                size="md"
                rootClassName="alk-files-share__role-new"
                value={newRole}
                onChange={(event) => setRole(event.target.value)}
              >
                {offered.map((one) => (
                  <option key={one.role} value={one.role}>
                    {one.label}
                  </option>
                ))}
              </Select>
              <Button
                size="md"
                disabled={picked === null || newRole === "" || grant.isPending}
                onClick={() => void addPerson()}
              >
                Share
              </Button>
            </div>
            {suggesting ? (
              searchNote !== null ? (
                <p
                  className="alk-files-share__suggestions alk-files-share__empty"
                  role={searchNote.state === "failed" ? "alert" : "status"}
                  data-state={searchNote.state}
                >
                  {searchNote.text}
                </p>
              ) : (
                <ul
                  id={listId}
                  role="listbox"
                  className="alk-files-share__suggestions"
                  aria-label="Matches"
                >
                  {candidates.map((one, index) => (
                    <li
                      key={`${one.kind}:${one.id}`}
                      id={`${listId}-${index}`}
                      role="option"
                      aria-selected={index === active}
                      className="alk-files-share__suggestion"
                      // Keep the keyboard in the field: the option is picked, the
                      // field is where typing goes on.
                      onMouseDown={(event) => event.preventDefault()}
                      onMouseEnter={() => setActive(index)}
                      onClick={() => pick(one)}
                    >
                      <Identity
                        leading={<Mark kind={one.kind} label={one.name} />}
                        name={one.name}
                        // A member with no display name is named by their
                        // email; the second line would only say it again.
                        secondary={one.hint && one.hint !== one.name ? one.hint : undefined}
                      />
                    </li>
                  ))}
                </ul>
              )
            ) : null}
          </div>
        ) : null}

        {refusal ? (
          <div className="alk-files-share__error" data-code={refusal.code}>
            <Callout tone="danger" role="alert" title={refusal.title}>
              {refusal.detail || undefined}
            </Callout>
          </div>
        ) : null}

        {sharesConnections && canShare ? (
          <p className="alk-files-share__note">{SHARES_CONNECTIONS_NOTE}</p>
        ) : null}

        <section className="alk-files-share__section" aria-label="People with access">
          <h3 className="alk-files-share__heading">People with access</h3>
          {rows.length === 0 ? (
            <p className="alk-files-share__empty">
              {direct.isPending ? "Loading…" : "Only you."}
            </p>
          ) : (
            <ul className="alk-files-share__rows">
              {rows.map(({ grant: row, inherited }) => {
                // The owner holds the item; there is nothing to change or take
                // away here, and "Inherited" beside them only raised the question
                // of from where. The server marks them.
                const isOwner = row.isOwner === true;
                const from = row.grantingNodeId;
                const key = rowKey(row);
                const writing = key in inFlight;
                const label = granteeLabel(row);
                // The second line says only what the row cannot say otherwise: the
                // id behind a principal nobody could name, and that the access
                // comes from further up.
                const secondary = [
                  label === UNKNOWN_PRINCIPAL ? row.principal.id : null,
                  inherited && !isOwner ? "Inherited" : null,
                ]
                  .filter((part): part is string => part !== null)
                  .join(" · ");
                const describedBy = inherited ? inheritedNoteId : undefined;
                return (
                  <li
                    key={`${row.principal.kind}:${row.principal.id}:${row.id ?? from ?? "direct"}`}
                    className="alk-files-share__row"
                    data-origin={inherited ? "inherited" : "direct"}
                  >
                    <Identity
                      leading={<Mark kind={row.principal.kind} label={label} />}
                      name={label}
                      secondary={secondary || undefined}
                      trailing={
                        isOwner ? (
                          <span className="alk-files-share__role--readonly">
                            {rowRoleLabel(row)}
                          </span>
                        ) : canShare ? (
                          <span
                            className="alk-files-share__row-controls"
                            title={inherited ? INHERITED_NOTE : undefined}
                          >
                            <Select
                              size="sm"
                              rootClassName="alk-files-share__role"
                              aria-label={`Role for ${granteeAria(row)}`}
                              aria-describedby={describedBy}
                              value={inFlight[key] ?? rowShownRole(row)}
                              disabled={!granted(row.canChange) || writing}
                              onChange={(event) => void changeRole(row, event.target.value)}
                            >
                              {/* Whatever the row already holds is always an option, even a
                                  rung the reader may not hand out: a select whose value is
                                  missing from its options renders empty. */}
                              {offered.some((one) => one.role === rowShownRole(row)) ? null : (
                                <option value={rowShownRole(row)}>{rowRoleLabel(row)}</option>
                              )}
                              {offered.map((one) => (
                                <option key={one.role} value={one.role}>
                                  {one.label}
                                </option>
                              ))}
                            </Select>
                            <Button
                              size="sm"
                              variant="secondary"
                              fill="ghost"
                              aria-label={`Remove ${granteeAria(row)}`}
                              aria-describedby={describedBy}
                              disabled={!granted(row.canRemove) || writing}
                              onClick={() => void removePerson(row)}
                            >
                              Remove
                            </Button>
                          </span>
                        ) : (
                          // A reader who may not share sees who has access, not
                          // controls they cannot use.
                          <span className="alk-files-share__role--readonly">
                            {rowRoleLabel(row)}
                          </span>
                        )
                      }
                    />
                  </li>
                );
              })}
            </ul>
          )}
          {/* The one sentence every inherited row's controls point at. */}
          <span id={inheritedNoteId} hidden>
            {INHERITED_NOTE}
          </span>
        </section>

        <section className="alk-files-share__links" aria-label="Links">
          <div className="alk-files-share__link-buttons">
            {targets.map((target) => (
              <Button
                key={target.id}
                size="md"
                variant="secondary"
                fill="outline"
                leftSection={<CopyIcon size={16} />}
                data-link={target.id}
                title={target.href}
                onClick={() => void copyLink(target)}
              >
                {/* One link needs no telling apart, so the button names the action.
                    Two do, so each names the thing it points at. */}
                {copied === target.id
                  ? "Link copied"
                  : targets.length === 1
                    ? "Copy link"
                    : target.label}
              </Button>
            ))}
          </div>
          <p className="alk-files-share__note">
            Only people who already have access can open this link.
          </p>
          {copyFailed ? (
            <p className="alk-files-share__note" role="status" data-state="copy-failed">
              The link was not copied.
            </p>
          ) : null}
        </section>
      </div>
    </Modal>
  );
}

export default ShareDialog;
