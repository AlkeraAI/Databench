import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";

import { Button, ConfirmDialog, GitHubMark, GoogleMark, TextInput, type ConfirmTone } from "@alkera/ui";

import { Icon } from "../../../app/icons";
import { useCurrentUser } from "../../../api/auth";
import { useIdentityDashboard } from "../../../api/dashboard";
import { useOrgSettings, useUpdateOrgSettings, type OrgSettings } from "../../../api/org";
import { ORG_SETTINGS_SECTIONS } from "../../../app/extensions/portal";
import { ActionRow, LoadError, ReadingField, SettingsSection, SettingsSkeleton, ToggleRow, errText } from "./fields";
import { LeaveOrgRow } from "./LeaveOrgRow";
import shell from "./shell.module.css";
import type { Notify } from "../../../app/notify";

/** What a locked control on this page tells a member: who to ask, in one line. */
const ADMIN_ONLY = "Only an org admin can change this.";

/**
 * Organization — the org-admin controls, wired to the real backend:
 *  - General: read-only identity from /api/v1/dashboard (org name + id + member count) and a pointer
 *    to the Teams page, which owns the roster;
 *  - Sign-in methods: the two provider toggles (PUT /api/v1/org/settings) — every flip is confirmed
 *    through the shared ConfirmDialog, and turning off the last enabled one is confirmed harder,
 *    since it can lock the org out;
 *  - every section an installed extension adds through ORG_SETTINGS_SECTIONS.
 */

export function OrgBody({ notify }: { notify: Notify }) {
  const dash = useIdentityDashboard();
  const me = useCurrentUser();
  const settings = useOrgSettings();

  const loading = dash.isLoading || settings.isLoading;
  const error = dash.isError || settings.isError;

  if (loading) return <SettingsSkeleton sections={3} />;
  if (error || !dash.data || !settings.data) {
    return (
      <LoadError
        message="We couldn't load your organization settings."
        onRetry={() => {
          void dash.refetch();
          void settings.refetch();
        }}
      />
    );
  }

  return (
    <OrgForm
      orgName={dash.data.org.name}
      orgId={dash.data.org.id}
      memberCount={dash.data.org.member_count}
      isAdmin={dash.data.is_org_admin}
      // The Teams page opens on a team the reader administers; for someone who administers none it
      // is a "you don't manage this team" dead end, so the link is not offered to them.
      managesTeams={dash.data.is_org_admin || (me.data?.admin_team_ids?.length ?? 0) > 0}
      settings={settings.data}
      notify={notify}
    />
  );
}

function OrgForm({
  orgName,
  orgId,
  memberCount,
  isAdmin,
  managesTeams,
  settings,
  notify,
}: {
  orgName: string;
  orgId: string;
  memberCount: number;
  isAdmin: boolean;
  managesTeams: boolean;
  settings: OrgSettings;
  notify: Notify;
}) {
  const navigate = useNavigate();
  const updateSettings = useUpdateOrgSettings();
  const [sections] = useState(() => ORG_SETTINGS_SECTIONS.items());

  const [allowGoogle, setAllowGoogle] = useState(settings.allow_login_google);
  const [allowGithub, setAllowGithub] = useState(settings.allow_login_github);
  const [pendingMethod, setPendingMethod] = useState<PendingMethod | null>(null);

  // The controls above seed off this document, so a refetch that brings back someone else's
  // change (or this admin's own save) has to re-seed them — otherwise they keep the values they
  // mounted with and the next save writes those stale values back. React Query hands back the same
  // object when a refetch changed nothing, so each effect runs only when its document really moved.
  // Re-seeding in place rather than remounting on a key leaves an open sign-in confirm, the extension
  // sections below and keyboard focus exactly where they were.
  useEffect(() => {
    setAllowGoogle(settings.allow_login_google);
    setAllowGithub(settings.allow_login_github);
  }, [settings]);

  // Apply a sign-in-method toggle: set optimistically, persist, and on failure revert + toast. The
  // success toast waits for the server ack (onSuccess), matching every other control on the page.
  const persistGoogle = (on: boolean) => {
    setAllowGoogle(on);
    updateSettings.mutate(
      { allow_login_google: on },
      {
        onSuccess: () => notify.success(`Google sign-in ${on ? "enabled" : "disabled"}.`),
        onError: (e) => {
          setAllowGoogle(!on);
          notify.error(errText(e, "Could not save sign-in methods."));
        },
      },
    );
  };
  const persistGithub = (on: boolean) => {
    setAllowGithub(on);
    updateSettings.mutate(
      { allow_login_github: on },
      {
        onSuccess: () => notify.success(`GitHub sign-in ${on ? "enabled" : "disabled"}.`),
        onError: (e) => {
          setAllowGithub(!on);
          notify.error(errText(e, "Could not save sign-in methods."));
        },
      },
    );
  };
  // Who may sign in is not a preference, so no flip of these switches applies on click: each one
  // asks first and only the answer runs the persist path above. Turning off the LAST enabled
  // method can lock the org out, so that one asks harder.
  const flipGoogle = (on: boolean) => setPendingMethod({ method: "google", on, lockout: !on && !allowGithub });
  const flipGithub = (on: boolean) => setPendingMethod({ method: "github", on, lockout: !on && !allowGoogle });
  const applyPending = () => {
    if (!pendingMethod) return;
    const { method, on } = pendingMethod;
    if (method === "google") persistGoogle(on);
    else persistGithub(on);
    setPendingMethod(null);
  };

  return (
    <>
      <SettingsSection id="org-general" icon="building" title="General">
        <TextInput
          label="Organization name"
          value={orgName}
          disabled
          readOnly
          className={shell.capped}
        />
        <ReadingField label="Organization ID" value={orgId} />
        {/* The org's member count (every member holds a row on the root). A team count is not
            given: the dashboard lists only the reader's own teams, not the org's. */}
        <ActionRow label="Members" help={`${memberCount} ${memberCount === 1 ? "member" : "members"}`}>
          {managesTeams ? (
            <Button variant="secondary" fill="ghost" leftSection={<Icon name="users" size={16} />} onClick={() => navigate("/teams")}>
              Manage in Teams
            </Button>
          ) : null}
        </ActionRow>
        <LeaveOrgRow orgName={orgName} />
      </SettingsSection>

      <SettingsSection id="org-signin" icon="shieldCheck" title="Sign-in methods">
        {/* A member reads which methods their org allows; only an admin moves them, which is what
            the server answers too (a member's PUT is a 403). A live switch that snaps back is the
            worst of both: it offers the write and then refuses it without saying who may. */}
        <ToggleRow
          label="Allow sign-in with Google"
          logo={<GoogleMark size={18} />}
          help={isAdmin ? undefined : ADMIN_ONLY}
          checked={allowGoogle}
          disabled={!isAdmin}
          onChange={flipGoogle}
        />
        <ToggleRow
          label="Allow sign-in with GitHub"
          logo={<GitHubMark size={18} />}
          help={isAdmin ? undefined : ADMIN_ONLY}
          checked={allowGithub}
          disabled={!isAdmin}
          onChange={flipGithub}
        />
      </SettingsSection>

      {sections.map(({ key, Section }) => (
        <Section key={key} isAdmin={isAdmin} notify={notify} />
      ))}

      <ConfirmDialog
        open={pendingMethod !== null}
        onClose={() => setPendingMethod(null)}
        onConfirm={applyPending}
        title={pendingMethod ? methodPrompt(pendingMethod).title : ""}
        consequence={pendingMethod ? methodPrompt(pendingMethod).consequence : undefined}
        confirmLabel={pendingMethod ? methodPrompt(pendingMethod).verb : ""}
        tone={pendingMethod ? methodPrompt(pendingMethod).tone : "default"}
        requireTyped={pendingMethod?.lockout ? METHOD_NAME[pendingMethod.method] : undefined}
      />
    </>
  );
}

/** A sign-in-method change waiting on its confirmation. */
type PendingMethod = { method: "google" | "github"; on: boolean; lockout: boolean };

const METHOD_NAME: Record<PendingMethod["method"], string> = { google: "Google", github: "GitHub" };

/** What the confirmation says about a pending change. Three shapes: turning a method on is
 *  reversible, turning one off costs the members who use it, and turning off the last one can
 *  leave nobody able to get back in. */
function methodPrompt(pending: PendingMethod): {
  title: string;
  consequence: string;
  verb: string;
  tone: ConfirmTone;
} {
  const name = METHOD_NAME[pending.method];
  if (pending.on) {
    return {
      title: `Allow sign-in with ${name}?`,
      consequence: `Anyone with a ${name} account on a member's email address can sign in.`,
      verb: "Allow",
      tone: "default",
    };
  }
  if (pending.lockout) {
    return {
      title: "Disable the last sign-in method?",
      consequence: `This is the only sign-in method left, so turning it off can lock out every member who has no password, including you.`,
      verb: "Disable it anyway",
      tone: "destructive",
    };
  }
  return {
    title: `Stop allowing sign-in with ${name}?`,
    consequence: `Members who sign in with ${name} need another method from their next sign-in.`,
    verb: "Stop allowing",
    tone: "warning",
  };
}
