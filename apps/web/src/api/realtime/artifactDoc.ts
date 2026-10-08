// The artifact document — a knowledge entry's title and body — as pure functions.
//
// State on the wire: `{kb_version, fields: {title|body: {value, ts, peer_id}}}`. An op of
// intent `set_fields` carries `{field: {value, ts}}`; the writing peer is the envelope's
// `peer_id`. The server keeps the last writer per field by `(ts, peer_id)` — a total order,
// so every peer converges to the same value whatever the delivery order — and this module
// applies exactly that rule on the client, so a reducer here and the server never disagree.
//
// `ts` is a seconds-since-epoch float, because that is the clock the server seeds a field
// with (`content_updated_at` of the row). A client stamping milliseconds would win every
// comparison against a REST edit forever; a test pins the unit.

import type { OpPayload } from "./docSync";

export const ARTIFACT_FIELDS = ["title", "body"] as const;
export type ArtifactField = (typeof ARTIFACT_FIELDS)[number];

export interface ArtifactFieldWrite {
  value: string;
  ts: number;
  peer_id: string;
}

export interface ArtifactDocState {
  kb_version: number;
  fields: Partial<Record<ArtifactField, ArtifactFieldWrite>>;
}

export function isArtifactField(value: string): value is ArtifactField {
  return (ARTIFACT_FIELDS as readonly string[]).includes(value);
}

/** The state a snapshot carries, tolerant of anything a newer server adds. Unknown fields
 *  and malformed writes are dropped rather than thrown on. */
export function parseArtifactState(raw: unknown): ArtifactDocState {
  const state: ArtifactDocState = { kb_version: 0, fields: {} };
  if (typeof raw !== "object" || raw === null) return state;
  const record = raw as Record<string, unknown>;
  if (typeof record.kb_version === "number") state.kb_version = record.kb_version;
  const fields = record.fields;
  if (typeof fields !== "object" || fields === null) return state;
  for (const [name, write] of Object.entries(fields as Record<string, unknown>)) {
    if (!isArtifactField(name)) continue;
    if (typeof write !== "object" || write === null) continue;
    const w = write as Record<string, unknown>;
    if (typeof w.value !== "string" || typeof w.ts !== "number") continue;
    state.fields[name] = { value: w.value, ts: w.ts, peer_id: typeof w.peer_id === "string" ? w.peer_id : "" };
  }
  return state;
}

/** Whether `candidate` was written later than `current`: by `ts`, then by `peer_id` (the
 *  server's tie-break, compared as plain strings the way Python compares them). */
export function laterWrite(
  candidate: { ts: number; peer_id: string },
  current: { ts: number; peer_id: string } | undefined,
): boolean {
  if (current === undefined) return true;
  if (candidate.ts !== current.ts) return candidate.ts > current.ts;
  return candidate.peer_id > current.peer_id;
}

/** Apply a `set_fields` op from `peerId`. Returns the new state and the fields that changed;
 *  a write that lost to the field's current writer changes nothing, as on the server. */
export function applyArtifactOp(
  state: ArtifactDocState,
  op: Pick<OpPayload, "intent" | "fields">,
  peerId: string,
): { state: ArtifactDocState; changed: ArtifactField[] } {
  if (op.intent !== "set_fields" || !op.fields) return { state, changed: [] };
  const fields = { ...state.fields };
  const changed: ArtifactField[] = [];
  for (const [name, write] of Object.entries(op.fields)) {
    if (!isArtifactField(name)) continue;
    if (typeof write.value !== "string" || typeof write.ts !== "number") continue;
    const candidate = { value: write.value, ts: write.ts, peer_id: peerId };
    if (!laterWrite(candidate, fields[name])) continue;
    fields[name] = candidate;
    changed.push(name);
  }
  return changed.length === 0 ? { state, changed } : { state: { ...state, fields }, changed };
}

/** The writer clock for an edit made now: seconds since the epoch, as the server stamps. */
export function artifactWriteTs(nowMs: number = Date.now()): number {
  return nowMs / 1000;
}

/** The op that writes the given fields at one clock. */
export function setFieldsOp(
  values: Partial<Record<ArtifactField, string>>,
  ts: number,
): { intent: "set_fields"; fields: Record<string, { value: string; ts: number }> } {
  const fields: Record<string, { value: string; ts: number }> = {};
  for (const name of ARTIFACT_FIELDS) {
    const value = values[name];
    if (value !== undefined) fields[name] = { value, ts };
  }
  return { intent: "set_fields", fields };
}

/** The plain value per field, for a form to read. */
export function artifactValues(state: ArtifactDocState): Partial<Record<ArtifactField, string>> {
  const out: Partial<Record<ArtifactField, string>> = {};
  for (const name of ARTIFACT_FIELDS) {
    const write = state.fields[name];
    if (write) out[name] = write.value;
  }
  return out;
}
