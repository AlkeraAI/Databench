import { useState, type FormEvent } from "react";

import { Callout, CheckIcon, Modal, Submenu, TextInput } from "@alkera/ui";

import { useMemberships, useSwitchOrg, type CurrentUser, type Membership } from "../api/auth";
import { refusalSentence, stepUpLoginUrl } from "../api/errors";
import {
  isPendingMembership,
  useCreateOrg,
  useJoinMembership,
  useMultiOrgEnabled,
} from "../api/orgs";
import { firstError, isDisplayName, required } from "../lib/validation";

type MenuUser = Pick<CurrentUser, "org_team_id" | "membership_count"> | null | undefined;

/**
 * The account menu's org rows, mounted only while the menu is open: one "Switch organization"
 * row whose flyout lists every org this person belongs to (the one this browser is in shown as
 * current, not offered) and, where the server runs with several orgs per person, ends with
 * "Create organization". There, too, the orgs that provisioned the person and wait for them to
 * join read "Pending" and offer Join, never a switch; finding a pending org is why the list is
 * read for a person in one org then, and only then.
 */
export function OrgMenuSection({
  user,
  close,
  onCreate,
}: {
  user: MenuUser;
  close: () => void;
  onCreate: () => void;
}) {
  const enabled = useMultiOrgEnabled();
  const multiOrg = (user?.membership_count ?? 1) > 1;
  const memberships = useMemberships({ enabled: multiOrg || enabled });
  const rows = memberships.data?.memberships ?? [];
  const pending = enabled ? rows.filter(isPendingMembership) : [];
  const offered = multiOrg || enabled;
  if (!offered && pending.length === 0) return null;

  return (
    <>
      {offered ? (
        <Submenu label="Switch organization" className="alk-accmenu__item">
          <OrgFlyoutRows
            orgs={rows.filter((m) => !isPendingMembership(m))}
            currentOrgId={user?.org_team_id}
            canCreate={enabled}
            close={close}
            onCreate={onCreate}
          />
        </Submenu>
      ) : null}
      {pending.length > 0 ? <PendingOrgRows pending={pending} /> : null}
      <div className="alk-accmenu__sep" role="separator" />
    </>
  );
}

/** The flyout's rows: every org, the current one marked and not offered, then Create. */
function OrgFlyoutRows({
  orgs,
  currentOrgId,
  canCreate,
  close,
  onCreate,
}: {
  orgs: readonly Membership[];
  currentOrgId: string | undefined;
  canCreate: boolean;
  close: () => void;
  onCreate: () => void;
}) {
  const switchOrg = useSwitchOrg();
  return (
    <>
      {orgs.map((m) => {
        const name = m.org_name || "Unnamed organization";
        if (m.org_team_id === currentOrgId) {
          return (
            <button
              key={m.org_team_id}
              type="button"
              role="menuitem"
              className="alk-accmenu__item alk-accmenu__item--current"
              disabled
              aria-current="true"
            >
              <span className="alk-accmenu__name">{name}</span>
              <CheckIcon size={14} aria-hidden="true" />
            </button>
          );
        }
        return (
          <button
            key={m.org_team_id}
            type="button"
            role="menuitem"
            className="alk-accmenu__item"
            disabled={switchOrg.isPending}
            onClick={() => {
              close();
              switchOrg.mutate({ orgTeamId: m.org_team_id });
            }}
          >
            {name}
          </button>
        );
      })}
      {canCreate ? (
        <>
          {orgs.length > 0 ? <div className="alk-accmenu__sep" role="separator" /> : null}
          <button type="button" role="menuitem" className="alk-accmenu__item" onClick={onCreate}>
            Create organization
          </button>
        </>
      ) : null}
    </>
  );
}

/** The orgs waiting for the person to join them. Joining keeps the menu open: the joined org
 *  moves into the switch flyout once the memberships refresh, which is where the switch is offered. */
function PendingOrgRows({ pending }: { pending: readonly Membership[] }) {
  const join = useJoinMembership();
  const joinFailed = join.isError && !stepUpLoginUrl(join.error) ? refusalSentence(join.error) : null;
  return (
    <div className="alk-accmenu__group" role="group" aria-label="Pending organizations">
      <span className="alk-theme__label">Pending</span>
      {pending.map((m) => {
        const name = m.org_name || "Unnamed organization";
        return (
          <button
            key={m.org_team_id}
            type="button"
            role="menuitem"
            className="alk-accmenu__item alk-accmenu__item--plan"
            aria-label={`Join ${name}`}
            disabled={join.isPending}
            onClick={() => join.mutate(m.org_team_id)}
          >
            {name}
            <span className="alk-accmenu__hint">Join</span>
          </button>
        );
      })}
      {joinFailed ? (
        <span className="alk-accmenu__error" role="alert">
          {joinFailed}
        </span>
      ) : null}
    </div>
  );
}

/** What a refused creation says, inline under the field. */
const CREATE_REFUSALS = { known: { 404: "Creating an organization isn't available." } };

/**
 * The "Create organization" dialog: one name field. On success the browser switches into the
 * new org (the switch reloads the app there).
 */
export function CreateOrgDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const create = useCreateOrg();
  const switchOrg = useSwitchOrg();
  const [name, setName] = useState("");
  const [shown, setShown] = useState(false);
  const fieldError = firstError(required("Name your organization")(name), isDisplayName()(name));
  const busy = create.isPending || switchOrg.isPending;
  const refused = create.isError
    ? refusalSentence(create.error, CREATE_REFUSALS)
    : switchOrg.isError && !stepUpLoginUrl(switchOrg.error)
      ? refusalSentence(switchOrg.error)
      : null;

  const close = () => {
    if (busy) return;
    create.reset();
    setName("");
    setShown(false);
    onClose();
  };
  const submit = (event?: FormEvent) => {
    event?.preventDefault();
    setShown(true);
    if (fieldError || busy) return;
    create.mutate(name.trim(), {
      onSuccess: (org) => switchOrg.mutate({ orgTeamId: org.org_team_id }),
    });
  };

  return (
    <Modal
      open={open}
      onClose={close}
      title="Create organization"
      size="sm"
      confirmLabel="Create"
      onConfirm={() => submit()}
      confirmBusy={busy}
    >
      <form noValidate onSubmit={submit}>
        {refused ? <Callout tone="danger">{refused}</Callout> : null}
        <TextInput
          label="Organization name"
          autoComplete="organization"
          autoFocus
          value={name}
          error={shown ? fieldError : null}
          onChange={(event) => setName(event.target.value)}
        />
      </form>
    </Modal>
  );
}
