import { useState, type CSSProperties } from "react";

import {
  ConfirmDialog,
  Identity,
  SidePanel,
  Skeleton,
  Switch,
  Tooltip,
  cx,
} from "@alkera/ui";

import { refusalSentence } from "../../../../api/errors";
import { useIdentityDashboard } from "../../../../api/dashboard";
import { useOrgMembers, useSetMemberActive, useSetSsoExemption, type OrgMember } from "../../../../api/orgMembers";
import { MEMBER_DRAWER_SECTIONS } from "../../../../app/extensions/portal";
import type { Person } from "../data/model";
import styles from "./MemberDrawer.module.css";

// One-off flex wrappers for the drawer layout — inline, not page classes (they name no reusable concept).
const colGap7: CSSProperties = { display: "flex", flexDirection: "column", gap: "var(--alkSpace7)" };
const colGap5: CSSProperties = { display: "flex", flexDirection: "column", gap: "var(--alkSpace5)" };
const colGap2: CSSProperties = { display: "flex", flexDirection: "column", gap: "var(--alkSpace2)" };
const section: CSSProperties = { ...colGap5, borderTop: "1px solid var(--alkDivider)", paddingTop: "var(--alkSpace7)" };

// The member drawer, opened from a roster row: the member, then the sections an extension
// registers (MEMBER_DRAWER_SECTIONS; billing adds the member's plan). An ORG admin additionally
// gets the account controls (deactivation, SSO break-glass).

export interface PlanTarget {
  teamId: string;
  teamName: string;
  person: Person;
}

type Notify = (text: string, ok: boolean) => void;

const failMessage = (e: unknown): string => refusalSentence(e);


export function MemberDrawer({
  target,
  onClose,
  onNotify,
}: {
  target: PlanTarget | null;
  onClose: () => void;
  /** Report a mutation's outcome (the page owns the toasts). */
  onNotify?: Notify;
}) {
  const open = target !== null;
  const [sections] = useState(() => MEMBER_DRAWER_SECTIONS.items());

  // The org-admin extras — the account controls. Gated on the same signal as
  // the org-admin routes (`is_org_admin` on the dashboard payload); the server
  // enforces the same gate on every endpoint. Fetched only while the drawer is open.
  const dashboard = useIdentityDashboard().data;
  const isOrgAdmin = Boolean(dashboard?.is_org_admin);
  // SSO break-glass is Enterprise-only (the server 403s the PUT); the switch greys
  // out with an upsell tip instead of failing on click. Self-hosted installs always
  // report enterprise features enabled, so this only gates hosted non-Enterprise orgs.
  const ssoGated = dashboard ? !dashboard.enterprise_features_enabled : false;
  const members = useOrgMembers(open && isOrgAdmin);
  const row = target ? members.data?.find((m) => m.user_id === target.person.id) : undefined;

  const notify: Notify = (text, ok) => onNotify?.(text, ok);

  return (
    <SidePanel open={open} onClose={onClose} anchor="viewport" mode="modal" width={400} eyebrow="Member" title={target?.person.name ?? "Member"}>
      {target ? (
        <div style={colGap7}>
          <Identity
            className={styles.planWho}
            initials={target.person.initials}
            name={target.person.name}
            secondary={target.person.email}
          />

          {sections.map(({ key, Section }) => (
            <Section key={key} teamId={target.teamId} userId={target.person.id} open={open} />
          ))}

          {isOrgAdmin && members.isLoading ? (
            // Access: eyebrow, then two switch + two-line-note groups.
            <section style={section} aria-hidden="true">
              <Skeleton width={52} height={12} />
              <div style={colGap2}>
                <Skeleton width={140} height={21} />
                <Skeleton width="92%" height={15} />
                <Skeleton width="64%" height={15} />
              </div>
              <div style={colGap2}>
                <Skeleton width={168} height={21} />
                <Skeleton width="88%" height={15} />
                <Skeleton width="56%" height={15} />
              </div>
            </section>
          ) : null}
          {isOrgAdmin && members.isError ? (
            <p className={cx("alk-body", "alk-muted")}>We couldn’t load the account controls. Close the panel and try again.</p>
          ) : null}
          {/* Keyed per member so the section's local state resets when the target changes. */}
          {isOrgAdmin && row ? <AccessSection key={`access-${row.user_id}`} row={row} ssoGated={ssoGated} notify={notify} /> : null}
        </div>
      ) : null}
    </SidePanel>
  );
}

/** Org-admin account controls: deactivation (offboard without data loss) and the SSO
 *  break-glass exemption. Deactivating is destructive — it confirms first; the rest apply
 *  immediately. `ssoGated` greys out the
 *  break-glass toggle with an Enterprise upsell tip (SSO is an Enterprise feature). */
function AccessSection({ row, ssoGated, notify }: { row: OrgMember; ssoGated: boolean; notify: Notify }) {
  const setActive = useSetMemberActive();
  const setExempt = useSetSsoExemption();
  const [confirmOff, setConfirmOff] = useState(false);
  const name = row.display_name || row.email;
  const fail = (e: unknown) => notify(failMessage(e), false);

  return (
    <section style={section} aria-label="Access">
      <span className="alk-eyebrow">Access</span>
      <div style={colGap2}>
        <Switch
          label="Active"
          checked={row.is_active}
          disabled={setActive.isPending}
          onChange={(e) => {
            if (e.currentTarget.checked) {
              setActive.mutate(
                { userId: row.user_id, active: true },
                { onSuccess: () => notify(`${name} reactivated.`, true), onError: fail },
              );
            } else {
              setConfirmOff(true);
            }
          }}
        />
        <span className={cx("alk-meta", "alk-muted")}>
          Deactivating blocks password sign-in and SSO immediately without deleting the user's data.
        </span>
      </div>
      <div style={colGap2}>
        {ssoGated ? (
          <Tooltip label="Available on Enterprise plan">
            {(t) => (
              // A focus stop of its own — the disabled switch's input isn't focusable,
              // so the wrapper carries hover + keyboard focus for the upsell tip. The
              // group role + name give assistive tech something to attach the
              // aria-describedby description to.
              <span
                {...t}
                tabIndex={0}
                role="group"
                aria-label="SSO break-glass"
                style={{ display: "inline-flex", width: "fit-content" }}
              >
                <Switch label="SSO break-glass" checked={row.sso_exempt} disabled />
              </span>
            )}
          </Tooltip>
        ) : (
          <Switch
            label="SSO break-glass"
            checked={row.sso_exempt}
            disabled={setExempt.isPending}
            onChange={(e) => {
              const exempt = e.currentTarget.checked;
              setExempt.mutate(
                { userId: row.user_id, exempt },
                {
                  onSuccess: () => notify(exempt ? `${name} can sign in with a password.` : `${name} must use single sign-on.`, true),
                  onError: fail,
                },
              );
            }}
          />
        )}
        <span className={cx("alk-meta", "alk-muted")}>Lets this account sign in with a password even when single sign-on is required.</span>
      </div>

      <ConfirmDialog
        open={confirmOff}
        onClose={() => setConfirmOff(false)}
        title={`Deactivate ${name}?`}
        consequence="They immediately lose access but keep their data. Reactivate anytime to restore it."
        confirmLabel="Deactivate"
        tone="warning"
        busy={setActive.isPending}
        onConfirm={() =>
          setActive.mutate(
            { userId: row.user_id, active: false },
            {
              onSuccess: () => {
                setConfirmOff(false);
                notify(`${name} deactivated.`, true);
              },
              onError: (e) => {
                setConfirmOff(false);
                fail(e);
              },
            },
          )
        }
      />
    </section>
  );
}
