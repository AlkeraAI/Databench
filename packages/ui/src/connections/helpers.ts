// Pure form logic shared by every connection-form surface: field ordering
// (required → method → OPTIONAL LAST, with the "Optional" divider — the pinned
// UX contract), method-switch value retention, and submit validation.

import type {
  AuthMethodView,
  ConnectionDraft,
  ConnectionFormView,
  FormFieldView,
  ValueSlotView,
} from "./types";

/** The chosen auth method, defaulting to the first (a single-method form never
 *  shows a picker but still submits its method name). */
export function selectedMethod(
  form: ConnectionFormView,
  methodName: string,
): AuthMethodView | undefined {
  return form.auth_methods.find((m) => m.name === methodName) ?? form.auth_methods[0];
}

/** The subset of typed values to keep when the user switches auth method: everything for a
 *  SHARED field (account / host / user — method-independent) is retained; a value keyed to any
 *  auth method's own field is dropped. This keeps a mis-picked method from wiping the shared
 *  inputs, while guaranteeing one method's secret never rides along into another's submit. */
export function retainedValuesOnMethodSwitch(
  form: ConnectionFormView,
  values: Record<string, string>,
): Record<string, string> {
  // Trailing fields are method-independent too — the deployment tier belongs to
  // the warehouse, not to how you sign in to it — so they keep their answers.
  const kept = new Set(
    [...form.shared_fields, ...(form.trailing_fields ?? [])].map((f) => f.name),
  );
  return Object.fromEntries(Object.entries(values).filter(([name]) => kept.has(name)));
}

/** Required shared fields first, then the picked method's, then the optional shared tail.
 *  This puts the credential fields (e.g. token / password) immediately after the identity
 *  fields (account / user) and before the optional session defaults — so a user types
 *  user → password, not user → warehouse → database → password. */
/** Whether a field belongs above the "Optional" divider. A field the server compiled
 *  into some ask group is not a session default, whatever its own flag says. Left in
 *  the optional tail it read as a contradiction: Account is required, its help says
 *  to use Account URL instead, and Account URL sat under a divider announcing that
 *  nothing below it is needed. Group membership is method-independent for shared
 *  fields, so the union over methods is the right divider for all of them. */
export function requiredBlockTest(form: ConnectionFormView): (f: FormFieldView) => boolean {
  const grouped = new Set(
    Object.values(form.ask_groups ?? {})
      .flat()
      .flat(),
  );
  return (f) => f.required || grouped.has(f.name);
}

export function orderedFields(
  form: ConnectionFormView,
  methodFields: FormFieldView[],
): FormFieldView[] {
  const inRequiredBlock = requiredBlockTest(form);
  const requiredShared = form.shared_fields.filter(inRequiredBlock);
  const optionalShared = form.shared_fields.filter((f) => !inRequiredBlock(f));
  // A method's OWN fields get the same required-first split the shared ones get.
  // A connector is free to declare an optional field between two required ones —
  // AWS puts the SSO region between the start URL and the account id — and
  // without this the "Optional" rule opened above two fields the form still
  // refuses to save without.
  const requiredMethod = methodFields.filter(inRequiredBlock);
  const optionalMethod = methodFields.filter((f) => !inRequiredBlock(f));
  return [
    ...requiredShared,
    ...requiredMethod,
    ...optionalMethod,
    ...optionalShared,
    ...(form.trailing_fields ?? []),
  ];
}

/** The values a submit should actually carry: what the user typed, plus every
 *  connector default they left alone.
 *
 *  The renderer shows `f.default` in an untouched box, so a member reads a
 *  filled field. Without this the draft never holds that value and the field
 *  arrives empty — filled on screen, blank on the wire. Resolving it here means
 *  what submits is what was shown. */
export function withDefaults(
  form: ConnectionFormView,
  methodFields: FormFieldView[],
  values: Record<string, string>,
): Record<string, string> {
  const resolved = { ...values };
  for (const f of orderedFields(form, methodFields)) {
    if ((resolved[f.name] ?? "") === "" && f.default) resolved[f.name] = f.default;
  }
  return resolved;
}

/** A `FormFieldView` decorated with the section heading (if any) it should render UNDER —
 *  set only on the first field of a run that shares a `group`, so the panel draws one heading
 *  per group. `divider` marks the boundary into the optional-defaults tail. */
export interface OrderedRow {
  field: FormFieldView;
  /** A group heading to render above this field, or null to render none. */
  heading: string | null;
  /** True on the first optional field with no explicit group — the panel draws an
   *  "Optional" divider so required credentials read as one block. */
  optionalDivider: boolean;
}

/** Annotate the ordered fields with group headings + the optional divider, without changing
 *  the field order itself. A group heading is emitted once, on the first field of each run of
 *  the same non-empty `group`. The optional divider is emitted once, before the first optional
 *  field that isn't already opening a group. */
export function orderedRows(form: ConnectionFormView, methodFields: FormFieldView[]): OrderedRow[] {
  const fields = orderedFields(form, methodFields);
  // The same test the ordering used, so the divider lands where the block actually
  // ends rather than on the first field whose own flag reads optional.
  const inRequiredBlock = requiredBlockTest(form);
  const hasRequired = fields.some(inRequiredBlock);
  let prevGroup = "";
  let optionalSectionOpened = false;
  return fields.map((field) => {
    const group = field.group?.trim() ?? "";
    const heading = group !== "" && group !== prevGroup ? group : null;
    prevGroup = group;
    // The "Optional" divider is drawn once, at the FIRST optional field — and only when
    // that field isn't already opening its own group heading (which serves as the separator).
    let optionalDivider = false;
    if (!inRequiredBlock(field) && !optionalSectionOpened && hasRequired) {
      optionalSectionOpened = true;
      optionalDivider = heading === null;
    }
    return { field, heading, optionalDivider };
  });
}

/** Whether a field collects a PEM document — a certificate, a CA bundle, a private
 *  key — rather than a single-line value.
 *
 *  A PEM carries line breaks, so a one-line box cannot hold one: pasting into it
 *  either flattens the newlines or stops at the first. The form schema has no flag
 *  for this today, so the name is what tells us; the shape is checked so a picker
 *  or a toggle about certificates ("verify_certs") is never mistaken for the
 *  certificate itself. Once the schema grows a multi-line flag, read that instead
 *  and leave this as the fallback for a connector that hasn't declared one. */
const PEM_FIELD_RE =
  /(^|_)(pem|cert|certs|cacert|cacerts|ca_bundle|sslrootcert|sslcert|sslkey|private_key)($|_)/;

export function isPemField(field: FormFieldView): boolean {
  if (field.enum_values.length > 0) return false;
  if (field.type !== "text" && field.type !== "password") return false;
  return PEM_FIELD_RE.test(field.name.toLowerCase());
}

/** A handle becomes a directory component on every member's machine, so it must be a safe slug.
 *  This is the same rule the server enforces at the save boundary — `SAFE_HANDLE_RE` in
 *  `packages/api-core/alkera_core/naming.py`. Nothing generates one side from the other, so
 *  widening either one means editing both by hand, or a name this form accepts gets refused.
 *
 *  `$` is the faithful port of Python's `\Z`: with no `m` flag it anchors at the true end of the
 *  string, where Python's own `$` would additionally admit a trailing newline. */
const SAFE_HANDLE_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/;

export function isSafeHandle(value: string): boolean {
  return SAFE_HANDLE_RE.test(value);
}

/** What an admin reads when the name they typed can't be used. */
export const SAFE_HANDLE_MESSAGE =
  "The name becomes a folder on every member's machine, so it can't contain spaces or slashes. " +
  "Start it with a letter or digit, then use only letters, digits, dots, underscores, and dashes.";

/** Submit gate: a handle, every server-compiled ask group answered, and every typed
 *  value inside its declared bound. Purely structural — the rule (which fields answer
 *  which, how long a value may run) arrives compiled from the server; this only checks
 *  the draft against it. A form may legitimately have NO auth methods (the secret is a
 *  shared field, or auth is out-of-band); an OAuth method is valid once its non-secret
 *  required fields are filled — the handshake supplies the credential. Without
 *  compiled groups (an older server) each required field gates alone. */
export function isValid(draft: ConnectionDraft, form: ConnectionFormView | null): boolean {
  if (!form || !draft.handle.trim()) return false;
  const method = selectedMethod(form, draft.method);
  const fields = orderedFields(form, method?.fields ?? []);
  const valueOf = (name: string): string => {
    const field = fields.find((f) => f.name === name);
    return (draft.values[name] ?? field?.default ?? "").trim();
  };
  if (fields.some((f) => (f.max_length ?? 0) > 0 && valueOf(f.name).length > (f.max_length ?? 0)))
    return false;
  // The server's compiled ask groups for the picked method; an older
  // daemon/backend sent none, so each required field falls back to a group of
  // its own.
  const groups =
    form.ask_groups?.[method?.name ?? ""] ??
    fields.filter((f) => f.required).map((f) => [f.name]);
  return groups.every((group) => group.some((name) => valueOf(name) !== ""));
}

/** The member-owned slots of a values document, as renderable form fields. */
export function slotFields(doc: ValueSlotView[]): FormFieldView[] {
  return doc
    .filter((s) => s.owner === "member")
    .map((s) => ({
      name: s.name,
      label: s.label,
      type: s.type,
      required: s.required,
      secret: s.secret,
      max_length: s.max_length,
      default: s.default,
      enum_values: s.enum_values,
      enum_labels: s.enum_labels,
      help: s.help,
      placeholder: s.placeholder,
      group: s.group,
    }));
}

/** What the admin filled in, labeled and in document order — shown read-only so a
 *  member sees which warehouse they are signing in to. Secrets never carry a value. */
export function slotPrefills(doc: ValueSlotView[]): Array<[string, string]> {
  return doc
    .filter((s) => s.owner === "admin" && !s.secret && s.value.trim() !== "")
    .map((s) => [s.label, s.value]);
}

/** The member gate over the one document: every ask group holds an answer — a
 *  non-blank slot value (the admin's declared alternative counts, because the
 *  document carries both halves), a deferred slot, or what the member typed — and
 *  every typed value fits its slot's bound. Structural only: nothing here merges
 *  the halves or re-derives the rule. Without compiled groups (an older daemon)
 *  each required member slot gates alone. */
export function docComplete(
  doc: ValueSlotView[],
  groups: string[][],
  typed: Record<string, string>,
): boolean {
  const byName = new Map(doc.map((s) => [s.name, s]));
  const typedOf = (name: string): string => (typed[name] ?? "").trim();
  if (doc.some((s) => s.owner === "member" && s.max_length > 0 && typedOf(s.name).length > s.max_length))
    return false;
  const effective = groups.length
    ? groups
    : doc.filter((s) => s.owner === "member" && s.required).map((s) => [s.name]);
  return effective.every((group) => group.some((name) => slotAnswered(byName, typed, name)));
}

function slotAnswered(
  byName: Map<string, ValueSlotView>,
  typed: Record<string, string>,
  name: string,
): boolean {
  const slot = byName.get(name);
  const typedValue = (typed[name] ?? "").trim();
  return Boolean(typedValue || (slot && (slot.value.trim() !== "" || slot.deferred)));
}

/** The label of the first open group the member cannot close from this form:
 *  none of its names is a member-owned slot, so only an admin recreating the
 *  connection can supply it. Null when every open group has a member input.
 *  Reachable only on rows saved before the server validated completeness. */
export function docBlockedOn(
  doc: ValueSlotView[],
  groups: string[][],
  typed: Record<string, string>,
): string | null {
  const byName = new Map(doc.map((s) => [s.name, s]));
  for (const group of groups) {
    if (group.some((name) => slotAnswered(byName, typed, name))) continue;
    if (group.some((name) => byName.get(name)?.owner === "member")) continue;
    const first = byName.get(group[0]);
    return first ? first.label : (group[0] ?? null);
  }
  return null;
}

/** Everything the team preconfigure dialog derives from one method choice plus
 *  the per-field prefill switches: who ends up holding the credential, whether
 *  the row can arrive ready to use, and — when it can't — why, in the admin's
 *  own terms. Pure; the rendered controls and the save payload read one object. */
export interface TeamShareDerivation {
  method: AuthMethodView | undefined;
  /** An OAuth method always binds per-user — the member authorizes in their browser. */
  oauthMethod: boolean;
  /** A CONFIDENTIAL per-user provider needs the org's OAuth app (client id + secret). */
  needsOAuthApp: boolean;
  authMethod: string;
  /** Every input the chosen method collects, the deployment tier included. */
  methodFields: FormFieldView[];
  secretFields: FormFieldView[];
  /** Who ends up holding the credential is the switch on the secret: hand the
   *  password over and the team queries as one identity; keep it back and each
   *  member signs in as themselves. */
  sharesACredential: boolean;
  perUser: boolean;
  autoAddable: boolean;
  /** Why auto-add is unavailable — shown beside the switch rather than the
   *  switch vanishing and leaving the admin to work it out. */
  autoAddNeeds: string;
}

export function deriveTeamShare(
  form: ConnectionFormView | null,
  teamCapableMethods: string[],
  draftMethod: string,
  withheld: ReadonlySet<string>,
): TeamShareDerivation {
  const teamCapable = new Set(teamCapableMethods);
  const method = form
    ? selectedMethod(
        { ...form, auth_methods: form.auth_methods.filter((m) => teamCapable.has(m.name)) },
        draftMethod,
      )
    : undefined;
  const oauthMethod = !!method?.oauth;
  const needsOAuthApp = oauthMethod && !!method?.oauth?.needs_org_client;
  const authMethod = method?.name ?? (teamCapable.has("") ? "" : draftMethod);
  const methodFields = form ? orderedFields(form, method?.fields ?? []) : [];
  const secretFields = methodFields.filter((f) => f.secret);
  const sharesACredential =
    secretFields.length > 0 && secretFields.every((f) => !withheld.has(f.name));
  const perUser = oauthMethod || (secretFields.length > 0 && !sharesACredential);
  const autoAddable = !oauthMethod && withheld.size === 0;
  const autoAddNeeds = oauthMethod
    ? "Each member signs in through their browser first, so this connection can't arrive ready to use."
    : `Members still answer ${withheld.size === 1 ? "the field" : `${withheld.size} fields`} you switched off, so this connection can't arrive ready to use.`;
  return {
    method,
    oauthMethod,
    needsOAuthApp,
    authMethod,
    methodFields,
    secretFields,
    sharesACredential,
    perUser,
    autoAddable,
    autoAddNeeds,
  };
}
