// Who may use a machine: the whole org, teams (with their sub-teams), people.
//
// The grants are listed as chips; one row below adds another, picking the kind
// first (Org, Team, Member) the way the billing page picks where credit lands.
// What may be named follows what the reader may grant: an org admin any team
// and any member, a team admin only their teams, the teams under them and
// their members. The server checks the same.

import { useMemo, useState } from "react";

import { Button, Inline, Pill, Select, Stack } from "@alkera/ui";

import type { AudienceGrant } from "../../../api/machines";
import { useOrgMembers } from "../../../api/orgMembers";
import { useTeamRosters, useTeams } from "../../../api/teams";
import { Icon } from "../../../app/icons";

import { COPY, type MachineViewer } from "./model";
import styles from "./machines.module.css";

/** A grant with the words it is shown in. */
export type AudienceDraft = AudienceGrant & { label: string };

export const grantKey = (g: AudienceGrant): string => `${g.kind}:${g.team_id ?? ""}:${g.user_id ?? ""}`;

/** The grants as the server takes them. */
export const toGrants = (drafts: readonly AudienceDraft[]): AudienceGrant[] =>
  drafts.map(({ kind, team_id, user_id }) => ({ kind, team_id: team_id ?? null, user_id: user_id ?? null }));

type Kind = AudienceGrant["kind"];

export interface AudiencePickerProps {
  value: readonly AudienceDraft[];
  onChange: (next: AudienceDraft[]) => void;
  viewer: MachineViewer;
  disabled?: boolean;
}

/** The teams a reader may name: every team under the org root for an org admin
 *  (the root itself is the org grant), their own teams for a team admin. */
function useNameableTeams(viewer: MachineViewer) {
  const teams = useTeams(viewer.mayManage);
  return useMemo(
    () =>
      (teams.data ?? [])
        .filter((t) => (viewer.isOrgAdmin ? !t.is_root : viewer.adminTeamIds.has(t.id)))
        .sort((a, b) => a.name.localeCompare(b.name)),
    [teams.data, viewer.isOrgAdmin, viewer.adminTeamIds],
  );
}

/** The people a reader may name, by id, with the name they are shown by. */
function useNameablePeople(viewer: MachineViewer, teamIds: readonly string[]) {
  const org = useOrgMembers(viewer.isOrgAdmin);
  const rosters = useTeamRosters(viewer.isOrgAdmin ? [] : teamIds, !viewer.isOrgAdmin);
  return useMemo(() => {
    const people = new Map<string, string>();
    if (viewer.isOrgAdmin) {
      for (const m of org.data ?? []) if (m.is_active) people.set(m.user_id, m.display_name || m.email);
    } else {
      for (const m of rosters.members) people.set(m.user_id, m.display_name || m.email);
    }
    return [...people.entries()].map(([id, name]) => ({ id, name })).sort((a, b) => a.name.localeCompare(b.name));
  }, [viewer.isOrgAdmin, org.data, rosters.members]);
}

export function AudiencePicker({ value, onChange, viewer, disabled = false }: AudiencePickerProps) {
  const teams = useNameableTeams(viewer);
  const people = useNameablePeople(viewer, teams.map((t) => t.id));
  const kinds: [Kind, string][] = [
    ...(viewer.isOrgAdmin ? ([["org", "Org"]] as [Kind, string][]) : []),
    ["team", "Team"],
    ["user", "Member"],
  ];
  const [kind, setKind] = useState<Kind>(viewer.isOrgAdmin ? "org" : "team");
  const [pick, setPick] = useState("");

  const taken = new Set(value.map(grantKey));
  const draft: AudienceDraft | null =
    kind === "org"
      ? { kind: "org", label: COPY.everyone }
      : kind === "team"
        ? (() => {
            const team = teams.find((t) => t.id === pick);
            return team ? { kind: "team", team_id: team.id, label: team.name } : null;
          })()
        : (() => {
            const person = people.find((p) => p.id === pick);
            return person ? { kind: "user", user_id: person.id, label: person.name } : null;
          })();
  const addable = draft !== null && !taken.has(grantKey(draft));

  const add = () => {
    if (!draft || !addable) return;
    onChange([...value, draft]);
    setPick("");
  };

  return (
    <Stack gap={3} align="stretch">
      {value.length === 0 ? (
        <p className="alk-meta">{COPY.noAudience}</p>
      ) : (
        <ul className={styles.chips} aria-label="Who can use it">
          {value.map((g) => (
            <li key={grantKey(g)}>
              <Pill shape="rect" variant="outline">
                {g.label}
                <Button
                  iconOnly
                  aria-label={`Remove ${g.label}`}
                  size="sm"
                  variant="secondary"
                  fill="ghost"
                  disabled={disabled}
                  onClick={() => onChange(value.filter((x) => grantKey(x) !== grantKey(g)))}
                >
                  <Icon name="close" size={12} />
                </Button>
              </Pill>
            </li>
          ))}
        </ul>
      )}
      <Inline role="group" aria-label="Add who can use it" gap={2} wrap align="flex-end">
        {kinds.map(([key, label]) => (
          <Pill
            key={key}
            interactive
            selected={kind === key}
            variant="outline"
            shape="rect"
            size="lg"
            disabled={disabled}
            onClick={() => {
              setKind(key);
              setPick("");
            }}
          >
            {label}
          </Pill>
        ))}
        {kind === "team" ? (
          <Select label="Team" value={pick} onChange={(e) => setPick(e.target.value)} disabled={disabled} rootStyle={{ minWidth: 200 }}>
            <option value="" disabled>
              Select a team
            </option>
            {teams.map((t) => (
              <option key={t.id} value={t.id} disabled={taken.has(grantKey({ kind: "team", team_id: t.id }))}>
                {t.name}
              </option>
            ))}
          </Select>
        ) : null}
        {kind === "user" ? (
          <Select label="Member" value={pick} onChange={(e) => setPick(e.target.value)} disabled={disabled} rootStyle={{ minWidth: 200 }}>
            <option value="" disabled>
              Select a member
            </option>
            {people.map((p) => (
              <option key={p.id} value={p.id} disabled={taken.has(grantKey({ kind: "user", user_id: p.id }))}>
                {p.name}
              </option>
            ))}
          </Select>
        ) : null}
        <Button variant="secondary" disabled={disabled || !addable} onClick={add}>
          {kind === "org" ? "Add everyone" : "Add"}
        </Button>
      </Inline>
    </Stack>
  );
}
