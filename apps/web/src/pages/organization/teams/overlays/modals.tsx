import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";

import { Button, Callout, EmptyState, Identity, IconChip, Modal, TextInput, cx } from "@alkera/ui";

import { Icon } from "../../../../app/icons";
import { ROLE_LABEL, type Person, type Role } from "../data/model";
import styles from "./modals.module.css";

// member / admin chooser — each option states what the role can do, so the picker documents
// the grant. Role is named + described, never colour alone. `locked` disables the choice when
// descent has already decided it (the reason is stated beside the group, not implied by greying).
function RolePick({ value, onChange, locked }: { value: Role; onChange: (r: Role) => void; locked?: boolean }) {
  const opts: { role: Role; blurb: string }[] = [
    { role: "member", blurb: "Use the team's chats and knowledge" },
    { role: "admin", blurb: "Also manage members and sub-teams" },
  ];
  return (
    <div style={{ display: "flex", gap: "var(--alkSpace3)" }} role="radiogroup" aria-label="Role" aria-disabled={locked || undefined}>
      {opts.map((o) => (
        <button
          key={o.role}
          type="button"
          role="radio"
          aria-checked={value === o.role}
          className={styles.rolepickOpt}
          data-on={value === o.role || undefined}
          disabled={locked}
          onClick={() => onChange(o.role)}
        >
          <Icon name={o.role === "admin" ? "shield" : "user"} size={18} />
          <span className={styles.rolepickTxt}>
            {ROLE_LABEL[o.role]}
            <small>{o.blurb}</small>
          </span>
        </button>
      ))}
    </div>
  );
}

// The footer button row for the shared Modal's `footer` slot (Cancel + the action) — the Modal wraps
// it in its own .alk-modal__foot layout, so this is just the content.
function Foot({ onCancel, children }: { onCancel: () => void; children: ReactNode }) {
  return (
    <>
      <Button variant="secondary" fill="ghost" onClick={onCancel}>
        Cancel
      </Button>
      {children}
    </>
  );
}

export function CreateTeamModal({
  open,
  parentName,
  onClose,
  onSubmit,
}: {
  open: boolean;
  parentName?: string;
  onClose: () => void;
  onSubmit: (name: string) => void;
}) {
  const [name, setName] = useState("");
  const nameRef = useRef<HTMLInputElement>(null);
  useEffect(() => {
    if (open) setName("");
  }, [open]);

  const submit = () => {
    if (name.trim()) onSubmit(name.trim());
  };

  return (
    <Modal
      open={open}
      onClose={onClose}
      size="sm"
      initialFocusRef={nameRef}
      title={parentName ? `New sub-team under ${parentName}` : "Create a team"}
      sub={parentName ? undefined : "A top-level team in your organization."}
      footer={
        <Foot onCancel={onClose}>
          <Button onClick={submit} disabled={!name.trim()}>
            Create team
          </Button>
        </Foot>
      }
    >
      <form
        onSubmit={(e) => {
          e.preventDefault();
          submit();
        }}
      >
        <TextInput
          ref={nameRef}
          label="Team name"
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="e.g. Observability"
          autoComplete="off"
        />
      </form>
    </Modal>
  );
}

/** A syntactically complete address — the bar for OFFERING an invitation. Deliberately loose (the
 *  server is the authority on deliverability), but it must never fire mid-typing: no local part, no
 *  `@`, or a domain with no dot leaves the plain "no one matches" state. */
const EMAIL_RE = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;

export function AddMemberModal({
  open,
  directory,
  loading,
  searchable = true,
  excludeIds,
  coveredBy,
  pendingInvites,
  teamName,
  onClose,
  onSubmit,
  onInvite,
}: {
  open: boolean;
  /** The org user directory (the root team's roster) — who can be added. */
  directory: Person[];
  /** True while the directory is still loading. */
  loading: boolean;
  /** False when the viewer cannot read the org directory (only an org admin can), so the dialog
   *  works by address alone and claims nothing about who is or isn't in the organization. */
  searchable?: boolean;
  /** Ids holding a row on this team (its direct members) — not offered again. */
  excludeIds: Set<string>;
  /** People who reach this team as admin by descent without a row here, keyed to the name of the
   *  team the standing comes from. Offered, but only as a direct admin: a member row would display
   *  a role they don't hold, and the server refuses it. */
  coveredBy: Map<string, string>;
  /** Addresses with a pending invitation to this team — a second invite is refused. */
  pendingInvites: string[];
  teamName: string;
  onClose: () => void;
  onSubmit: (personId: string, role: Role) => void;
  /** Send the email invitation. Resolves to the server's refusal to show in place, or null on
   *  success (the caller confirms and closes). */
  onInvite: (email: string, role: Role) => Promise<string | null>;
}) {
  const [query, setQuery] = useState("");
  const [pickedId, setPickedId] = useState<string | null>(null);
  const [role, setRole] = useState<Role>("member");
  const [sending, setSending] = useState(false);
  const [sendError, setSendError] = useState<string | null>(null);
  const [refusedEmail, setRefusedEmail] = useState<string | null>(null);
  const searchRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (open) {
      setQuery("");
      setPickedId(null);
      setRole("member");
      setSending(false);
      setSendError(null);
      setRefusedEmail(null);
    }
  }, [open]);

  // Org people not holding a row on this team. Someone reaching it by descent is still offered —
  // a direct admin row is the one thing not yet true of them — and is marked as covered.
  const candidates = useMemo<Person[]>(() => {
    const q = query.trim().toLowerCase();
    return directory.filter(
      (p) => !excludeIds.has(p.id) && (!q || p.name.toLowerCase().includes(q) || p.email.toLowerCase().includes(q)),
    );
  }, [directory, excludeIds, query]);

  // Descent decides the role of a covered pick: locked to admin, with the fact stated.
  const pickedCoveredFrom = pickedId ? coveredBy.get(pickedId) : undefined;
  const effectiveRole: Role = pickedCoveredFrom ? "admin" : role;

  // What the typed address IS, resolved against everything the client can know. The org directory is
  // the whole org (membership materializes onto the root), so an address found there belongs to a
  // person who can simply be added — inviting them by email would make the backend auto-accept the
  // invitation silently, with no email and a role the admin only thinks they chose. The remaining
  // cross-org case is unknowable here and is the server's 409 to raise.
  const typed = query.trim();
  const typedEmail = EMAIL_RE.test(typed) ? typed.toLowerCase() : null;
  const inOrg = typedEmail ? directory.find((p) => p.email.toLowerCase() === typedEmail) : undefined;
  const alreadyInvited = typedEmail != null && pendingInvites.some((e) => e.toLowerCase() === typedEmail);
  // An address the server has already refused is no longer on offer: repeating the send only earns
  // the same refusal, and leaving "they'll join ..." on screen beside it states something the
  // server has just denied. Cleared as soon as the admin edits the address.
  const refused = sendError !== null && refusedEmail === typedEmail;
  const offer = typedEmail && !inOrg && !alreadyInvited && !refused ? typedEmail : null;

  const send = async () => {
    if (!offer || sending) return;
    setSending(true);
    setSendError(null);
    const failure = await onInvite(offer, role);
    setSending(false);
    setRefusedEmail(failure ? offer : null);
    setSendError(failure);
  };

  const emptyState = (): ReactNode => {
    if (loading) return <EmptyState size="sm" title="Loading your organization's people..." />;
    if (offer && !searchable) {
      return (
        <EmptyState
          size="sm"
          icon={<Icon name="mail" size={28} />}
          title={
            <>
              Invite <strong>{offer}</strong>
            </>
          }
          body={`Someone already in your organization joins ${teamName} at once; anyone else joins when they accept.`}
        />
      );
    }
    if (offer) {
      return (
        <EmptyState
          size="sm"
          icon={<Icon name="mail" size={28} />}
          title={
            <>
              <strong>{offer}</strong> isn’t in your organization yet.
            </>
          }
          body={`Invite them by email. They’ll join ${teamName} as ${role === "admin" ? "an Admin" : "a Member"} when they accept.`}
        />
      );
    }
    if (refused) {
      return (
        <EmptyState
          size="sm"
          icon={<Icon name="mail" size={28} />}
          title={`${typedEmail} wasn’t invited.`}
          body="Edit the address to try a different one."
        />
      );
    }
    if (alreadyInvited) {
      return (
        <EmptyState
          size="sm"
          icon={<Icon name="mail" size={28} />}
          title="Already invited, pending"
          body={`${typedEmail} has an invitation to ${teamName} that hasn’t been accepted yet.`}
        />
      );
    }
    if (inOrg) {
      // In the org, but already holding a row on this team — so neither the row nor the offer is shown.
      return <EmptyState size="sm" title={`${inOrg.name} is already a direct member of ${teamName}.`} />;
    }
    // Every address-shaped query is answered above — it is offered, already invited, or
    // already a member — so text reaching here is text that is not an address. The dialog
    // is a people picker that falls through to invite-by-email, and the next move from
    // here is to type a whole address; "No one matches your search." sent the admin
    // looking for a person instead of at what they had typed.
    if (typed) return <EmptyState size="sm" title="Enter a full email address to invite someone." />;
    if (!searchable) return <EmptyState size="sm" title="Type an email address to add someone." />;
    return <EmptyState size="sm" title="Everyone in your organization is already a direct member of this team." />;
  };

  return (
    <Modal
      open={open}
      onClose={onClose}
      initialFocusRef={searchRef}
      title={`Add member to ${teamName}`}
      sub={searchable ? "Add someone from your organization, or invite them by email." : "Add someone by email address."}
      footer={
        <Foot onCancel={onClose}>
          {offer ? (
            <Button onClick={() => void send()} loading={sending}>
              Send invite
            </Button>
          ) : (
            <Button disabled={!pickedId} onClick={() => pickedId && onSubmit(pickedId, effectiveRole)}>
              {pickedCoveredFrom ? "Add as direct admin" : "Add member"}
            </Button>
          )}
        </Foot>
      }
    >
      <div className={styles.field}>
        <TextInput
          ref={searchRef}
          type="search"
          rootStyle={{ width: "100%" }}
          value={query}
          onChange={(e) => {
            setQuery(e.target.value);
            setSendError(null);
          }}
          placeholder={searchable ? "Name or email address" : "Email address"}
          aria-label="Search people or type an email address"
        />
      </div>
      {sendError ? (
        <div className={styles.field}>
          <Callout tone="danger">{sendError}</Callout>
        </div>
      ) : null}
      <div className={styles.picklist} role="group" aria-label="People">
        {candidates.length === 0 ? (
          emptyState()
        ) : (
          candidates.map((p) => {
            const from = coveredBy.get(p.id);
            return (
              <button
                key={p.id}
                type="button"
                aria-pressed={pickedId === p.id}
                className={styles.pickrow}
                data-on={pickedId === p.id || undefined}
                onClick={() => setPickedId(p.id)}
              >
                <Identity
                  initials={p.initials}
                  name={p.name}
                  secondary={from ? `${p.email} · Admin here by descent from ${from}` : p.email}
                  trailing={<span className={styles.pickrowCheck}>{pickedId === p.id ? <Icon name="check" size={16} /> : null}</span>}
                />
              </button>
            );
          })
        )}
      </div>
      <div className={styles.field}>
        <span className="alk-field__label">Role</span>
        <RolePick value={effectiveRole} onChange={setRole} locked={Boolean(pickedCoveredFrom)} />
        {pickedCoveredFrom ? (
          <p className={cx("alk-meta")} style={{ margin: "var(--alkSpace2) 0 0" }}>
            Already an admin of {teamName} by descent from {pickedCoveredFrom}, so the role is set there. Adding them
            writes a direct admin row here as well.
          </p>
        ) : null}
      </div>
    </Modal>
  );
}

export function RenameModal({
  open,
  currentName,
  onClose,
  onSubmit,
}: {
  open: boolean;
  currentName: string;
  onClose: () => void;
  onSubmit: (name: string) => void;
}) {
  const [name, setName] = useState(currentName);
  const nameRef = useRef<HTMLInputElement>(null);
  useEffect(() => {
    if (open) setName(currentName);
  }, [open, currentName]);

  const changed = name.trim() && name.trim() !== currentName;

  return (
    <Modal
      open={open}
      onClose={onClose}
      size="sm"
      initialFocusRef={nameRef}
      title={`Rename ${currentName}`}
      footer={
        <Foot onCancel={onClose}>
          <Button onClick={() => changed && onSubmit(name.trim())} disabled={!changed}>
            Rename
          </Button>
        </Foot>
      }
    >
      <form
        onSubmit={(e) => {
          e.preventDefault();
          if (changed) onSubmit(name.trim());
        }}
      >
        <TextInput ref={nameRef} label="Team name" value={name} onChange={(e) => setName(e.target.value)} autoComplete="off" />
      </form>
    </Modal>
  );
}

export interface TeamChoice {
  id: string;
  name: string;
  depth: number;
  members: number;
}

// Destination picker for moving a team or a member — an indented list of valid target teams.
export function TeamPickModal({
  open,
  title,
  sub,
  confirmLabel,
  choices,
  onClose,
  onSubmit,
}: {
  open: boolean;
  title: string;
  sub?: string;
  confirmLabel: string;
  choices: TeamChoice[];
  onClose: () => void;
  onSubmit: (teamId: string) => void;
}) {
  const [pickedId, setPickedId] = useState<string | null>(null);
  useEffect(() => {
    if (open) setPickedId(null);
  }, [open]);

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={title}
      sub={sub}
      footer={
        <Foot onCancel={onClose}>
          <Button disabled={!pickedId} onClick={() => pickedId && onSubmit(pickedId)}>
            {confirmLabel}
          </Button>
        </Foot>
      }
    >
      <div className={styles.picklist} role="group" aria-label="Destination team">
        {choices.length === 0 ? (
          <EmptyState size="sm" title="No valid destination." />
        ) : (
          choices.map((c) => (
            <button
              key={c.id}
              type="button"
              aria-pressed={pickedId === c.id}
              className={styles.pickrow}
              data-on={pickedId === c.id || undefined}
              style={{ paddingLeft: 12 + c.depth * 18 }}
              onClick={() => setPickedId(c.id)}
            >
              <Identity
                leading={
                  <IconChip size="md" tone="neutral">
                    <Icon name="branch" size={16} />
                  </IconChip>
                }
                name={c.name}
                trailing={
                  <>
                    <span className={cx("alk-meta", "alk-num")} style={{ marginLeft: "auto", flex: "0 0 auto" }}>{c.members}</span>
                    <span className={styles.pickrowCheck}>{pickedId === c.id ? <Icon name="check" size={16} /> : null}</span>
                  </>
                }
              />
            </button>
          ))
        )}
      </div>
    </Modal>
  );
}

export function MoveMemberModalPick({
  open,
  who,
  choices,
  onClose,
  onSubmit,
}: {
  open: boolean;
  who: string;
  choices: TeamChoice[];
  onClose: () => void;
  onSubmit: (teamId: string) => void;
}) {
  return (
    <TeamPickModal
      open={open}
      title={`Move ${who}`}
      sub="They become a direct member of the team you choose."
      confirmLabel="Move member"
      choices={choices}
      onClose={onClose}
      onSubmit={onSubmit}
    />
  );
}
