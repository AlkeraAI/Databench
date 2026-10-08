import { useState, type ReactNode } from "react";

import { Button, DateInput, Select, TextInput } from "@alkera/ui";

import type { AuditFilters } from "@/api/orgAdminAudit";

/**
 * The audit-log filter row shared by the organization's log and the platform log: an action
 * choice, the actor's exact email, and a From/To day range, with Clear once anything is set.
 * Each log supplies its own action choices; the wire shape is the same on both routes.
 */

/** The filter controls as the reader set them; `auditFiltersToWire` converts to query params. */
export type AuditFilterDraft = { action: string; actor: string; from: string; to: string };

export const EMPTY_AUDIT_FILTERS: AuditFilterDraft = { action: "", actor: "", from: "", to: "" };

/** Picked days widen to their full local range. */
export function auditFiltersToWire(draft: AuditFilterDraft): AuditFilters {
  return {
    action: draft.action || undefined,
    actor_email: draft.actor || undefined,
    created_after: draft.from ? new Date(`${draft.from}T00:00:00`).toISOString() : undefined,
    created_before: draft.to ? new Date(`${draft.to}T23:59:59.999`).toISOString() : undefined,
  };
}

export function AuditFilterBar({
  draft,
  active,
  onPatch,
  actionOptions,
  actionIcons,
}: {
  draft: AuditFilterDraft;
  active: boolean;
  onPatch: (patch: Partial<AuditFilterDraft>) => void;
  /** The `<option>`s after "All actions". */
  actionOptions: ReactNode;
  actionIcons?: Record<string, ReactNode>;
}) {
  // Child order is load-bearing: `.alk-audit-filters` (portal.css) sizes its five grid tracks
  // positionally — Action, Actor, From, To, Clear — and pairs From/To on the 2x2 row.
  // The actor field commits on Enter or blur, not per keystroke — it holds its own text until then.
  const [actor, setActor] = useState(draft.actor);
  const commitActor = () => onPatch({ actor: actor.trim() });
  const clear = () => {
    setActor("");
    onPatch(EMPTY_AUDIT_FILTERS);
  };
  return (
    <div className="alk-audit-filters">
      <Select
        label="Action"
        size="sm"
        optionIcons={actionIcons}
        value={draft.action}
        onChange={(e) => onPatch({ action: e.target.value })}
      >
        <option value="">All actions</option>
        {actionOptions}
      </Select>
      <TextInput
        label="Actor"
        size="sm"
        placeholder="exact email"
        value={actor}
        onChange={(e) => setActor(e.target.value)}
        onBlur={commitActor}
        onKeyDown={(e) => (e.key === "Enter" ? commitActor() : undefined)}
      />
      <DateInput label="From" size="sm" value={draft.from} onChange={(from) => onPatch({ from })} />
      <DateInput label="To" size="sm" value={draft.to} onChange={(to) => onPatch({ to })} />
      {active ? (
        <Button className="alk-audit-filters__clear" variant="secondary" fill="ghost" size="sm" onClick={clear}>
          Clear filters
        </Button>
      ) : null}
    </div>
  );
}
