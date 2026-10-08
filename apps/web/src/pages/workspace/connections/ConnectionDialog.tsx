// The add/edit connection dialog, and the machinery around it.
//
// One dialog serves both owners. Its first control is the team picker: "Just me"
// saves through `/me/connections`, a team through that team's routes. Everything
// below it — the generic connector form, the verify-before-live machine — is the
// same code either way, because the two saves differ only in who ends up holding
// the row.
//
// A personal connection has no second person in it: every value is the owner's,
// so the prefill switches and the sharing summary are not rendered and the save
// hands the server the whole form. Those controls answer "what do members
// inherit?", and a connection with no members has nothing to answer.

import { useEffect, useMemo, useState } from "react";

import { Button, Callout, Checkbox, FieldShell, Inline, Modal, Skeleton, Stack, TextInput } from "@alkera/ui";
import { ConnectionFormFields, deriveTeamShare, isSafeHandle, isValid, LogoChip, presentBadge, SAFE_HANDLE_MESSAGE, withDefaults, type AuthMethodView, type BadgeSource, type ConnectionDraft, type ConnectionFormView, type ConnectionOutcome, type FormFieldView } from "@alkera/ui/connections";

import styles from "./connections.module.css";
import { MEMBER_PREFILL_ENABLED } from "./flags";

import { ApiError, refusalSentence } from "../../../api/errors";
import {
  useConnectionForms,
  useMoveConnectionOwner,
  useMyConnections,
  useTeamConnections,
  useUpsertMyConnection,
  useUpsertTeamConnection,
  verificationInFlight,
  type ConnectorFormDescriptor,
  type TeamConnection,
  type TeamConnectionUpsert,
} from "../../../api/connections";
import { useConnectionChecks, type CheckRecord } from "../../../api/connectionChecks";
import { TeamPicker, type PickableTeam, type TeamPickerValue } from "../../../components/TeamPicker";
import { useTeams } from "../../../api/teams";
import { useCurrentUser } from "../../../api/auth";

/** Who a dialog is saving for. The two halves of the API differ only here, so
 *  every hook below takes one of these instead of a team id and a flag. */
export type Owner = TeamPickerValue;

export const isMine = (owner: Owner): owner is { kind: "me" } => owner.kind === "me";


function emptyDraft(): ConnectionDraft {
  return { handle: "", method: "", values: {} };
}

/** What an empty credential box means on an edit, said in the box itself. */
const KEPT_SECRET_PLACEHOLDER = "Stored; leave blank to keep";

/** Every input a form asks for, whichever half of it declares them. */
function allFields(form: ConnectionFormView): FormFieldView[] {
  return [
    ...form.shared_fields,
    ...(form.trailing_fields ?? []),
    ...form.auth_methods.flatMap((m) => m.fields),
  ];
}

/** The credential boxes an edit may leave empty.
 *
 *  A secret never comes back from the server, so an edit opens with the box
 *  blank — and a blank required box made Save unreachable, which cost a rename,
 *  a re-host, a tier change or an owner move a warehouse credential the admin
 *  may not hold. The save's own rule is the opposite: a secret it does not carry
 *  preserves the stored role. So a row that already holds one has its answer,
 *  and only a row that holds none still has to be told. */
function keptSecretNames(
  form: ConnectionFormView | null,
  editing: TeamConnection | null,
): Set<string> {
  if (!form || !editing?.has_shared_secret) return new Set();
  return new Set(allFields(form).filter((f) => f.secret).map((f) => f.name));
}

/** The form as an edit reads it: a credential the row already holds asks to be
 *  replaced rather than supplied, so its box stops being required and says why
 *  it is empty. */
function relaxKeptSecrets(form: ConnectionFormView, kept: Set<string>): ConnectionFormView {
  if (kept.size === 0) return form;
  const relax = (f: FormFieldView): FormFieldView =>
    kept.has(f.name) ? { ...f, required: false, placeholder: KEPT_SECRET_PLACEHOLDER } : f;
  return {
    ...form,
    shared_fields: form.shared_fields.map(relax),
    trailing_fields: form.trailing_fields?.map(relax),
    auth_methods: form.auth_methods.map((m) => ({ ...m, fields: m.fields.map(relax) })),
  };
}

/** The same form as the SUBMIT GATE reads it: a group the stored credential
 *  already answers is answered. Kept apart from the rendered form because the
 *  required/optional divider reads the groups too — dropping one there would
 *  file the credential under session defaults. */
function withoutKeptGroups(form: ConnectionFormView, kept: Set<string>): ConnectionFormView {
  if (kept.size === 0) return form;
  return {
    ...form,
    ask_groups: Object.fromEntries(
      Object.entries(form.ask_groups ?? {}).map(([methodName, groups]) => [
        methodName,
        groups.filter((group) => !group.some((name) => kept.has(name))),
      ]),
    ),
  };
}

/** The word the picker shows for each stored enum value, across the whole form.
 *  The connector declares both halves, so nothing here can name a value the list
 *  in front of the admin does not offer. */
function enumLabels(form: ConnectionFormView | null): Map<string, string> {
  const labels = new Map<string, string>();
  if (!form) return labels;
  for (const field of allFields(form)) {
    for (const [value, label] of Object.entries(field.enum_labels ?? {})) {
      if (label) labels.set(value, label);
    }
  }
  return labels;
}

/** A refusal in the words the form in front of the admin uses.
 *
 *  A connector states its rules in the driver's own tokens — the TLS guard says
 *  'disable' cannot be used and to use 'verify-full' — and the picker never
 *  shows either: it offers "No encryption" and "Verify certificate and
 *  hostname". So a quoted token that IS one of the form's enum values is swapped
 *  for that picker's label, unquoted, and anything else is left exactly as the
 *  server wrote it. */
function inPickerWords(message: string, form: ConnectionFormView | null): string {
  const labels = enumLabels(form);
  if (labels.size === 0) return message;
  return message.replace(
    /'([A-Za-z0-9][\w.-]*)'/g,
    (quoted, value: string) => labels.get(value) ?? quoted,
  );
}

/** The add-dialog's lifecycle — verify-before-live, matching the workspace form:
 *  the dialog stays open while the server checks and only settles on a RECORD.
 *  NOTHING is persisted until a record settles `ok` (or `unsupported`, where
 *  there was nothing to dial): `editing` → (save) → `verifying` (a server-side
 *  verification of the PAYLOAD, polled by its id) → passed? save the real row +
 *  close : `failed` (the record's own reading, inline and actionable; fix + try
 *  again, or cancel — either way no connection ever touched the team's list). */
type ModalPhase = "editing" | "verifying" | "failed";

/** How long the browser keeps asking about a record that never reaches a terminal
 *  state. The server's own recovery abandons a queued record at 90 s and a silent
 *  running one at 60 s, so past two minutes the answer is not coming to THIS page
 *  — stop polling and say so, rather than spin until the tab is closed. */
const VERIFY_CAP_MS = 120_000;

/** A queued record older than this is being RETRIED towards the worker, not
 *  simply fresh — the wait is worth explaining. */
const SLOW_DISPATCH_MS = 5_000;

/** What a failed settlement is called, in the words every other Alkera surface
 *  uses for the same outcome — so a refused add and the row it would have created
 *  never read as two different problems. */
function outcomeBadge(outcome: ConnectionOutcome | string): BadgeSource {
  switch (outcome) {
    case "invalid_credential":
      return { badge: "needs_reauth", reauth: "reenter", outcome };
    case "permission":
      return { badge: "no_access", outcome };
    case "unreachable":
    case "timeout":
      return { badge: "unreachable", outcome };
    case "infrastructure":
      return { badge: "error", outcome: "infrastructure" };
    default:
      return { badge: "error", outcome: "error" };
  }
}

export interface VerificationNotice {
  tone: "info" | "warning" | "danger";
  title?: string;
  body: string;
  /** Whether this reading is the end of the record — the dialog offers a retry
   *  and lets the form be edited again. */
  terminal: boolean;
}

/**
 * What the admin is told about a check, read off the RECORD and nothing else.
 *
 * Three different waits, and only one of them is the worker's fault. The server
 * writes its dispatch facts onto the record — how many times it has tried to hand
 * the check over and what went wrong — so a server still retrying says exactly
 * that instead of a browser-side stopwatch guessing "the worker may not be
 * running" while the worker sits healthy and idle. Only a record nobody ever took
 * (`abandoned`) points there.
 */
export function verificationNotice(record: CheckRecord, now: number): VerificationNotice {
  if (record.state === "abandoned") {
    return {
      tone: "danger",
      title: "The worker didn't pick this up",
      body: "Nothing checked this connection. Try again; nothing has been saved.",
      terminal: true,
    };
  }
  if (record.state === "running") {
    return { tone: "info", body: "Checking the connection…", terminal: false };
  }
  if (record.state === "queued") {
    const age = now - Date.parse(record.requested_at);
    const retrying = record.dispatch_attempts > 1 || (Number.isFinite(age) && age > SLOW_DISPATCH_MS);
    if (!retrying) return { tone: "info", body: "Starting the check…", terminal: false };
    // The dispatch error the record carries is the server's own plumbing and
    // names nothing the admin can act on, so the wait says only that it is a
    // wait. The title above it already says which wait.
    return {
      tone: "warning",
      title: "Still reaching the background worker",
      body: "Waiting for credential check.",
      terminal: false,
    };
  }
  // Settled, and in the clear. The save is the last leg of the check and the
  // dialog stays open for the whole of it, so this is what the admin reads in
  // the seconds before the row appears: it has to say what is happening rather
  // than fall through the refusal tail below, which read every settled record as
  // a failure and told them "The check failed." about a connection that had just
  // passed — in red, with a retry, immediately before the row arrived Connected.
  if (record.outcome === "ok" || record.outcome === "unsupported") {
    return { tone: "info", body: "Saving the connection…", terminal: false };
  }
  const outcome = record.outcome ?? "error";
  if (outcome === "infrastructure") {
    return {
      tone: "warning",
      title: "Couldn't run the check",
      body: record.detail || "Try again.",
      terminal: true,
    };
  }
  const view = presentBadge(outcomeBadge(outcome));
  return {
    tone: "danger",
    title: view.label,
    body: record.detail || view.note || "The connection couldn't be verified.",
    terminal: true,
  };
}

/** A settled record the save may proceed on. `unsupported` passes because it
 *  means the server has no way to dial this connector at all — a fact about the
 *  connector, never a reason to refuse an admin's configuration. */
function verificationPassed(record: CheckRecord | undefined): boolean {
  return record?.state === "settled" && (record.outcome === "ok" || record.outcome === "unsupported");
}

/** The connector choice: everything the catalog offers that a team can
 *  preconfigure. */
function ConnectorPicker({
  loading,
  descriptors,
  onPick,
}: {
  loading: boolean;
  descriptors: ConnectorFormDescriptor[];
  onPick: (d: ConnectorFormDescriptor) => void;
}) {
  // The catalog loads as the shape it will become, so the dialog does not jump
  // from one line of text to ten rows.
  if (loading) {
    return (
      <Stack gap="0" align="stretch">
        {[0, 1, 2, 3, 4, 5].map((i) => (
          <div key={i} className={styles.pick}>
            <Skeleton width={24} height={24} />
            <Skeleton width={110} height={14} />
          </div>
        ))}
      </Stack>
    );
  }
  return (
    <Stack gap="0" align="stretch">
      {descriptors.map((d: ConnectorFormDescriptor) => (
        <button key={d.name} type="button" className={styles.pick} onClick={() => onPick(d)}>
          <LogoChip pluginId={d.name} size={24} />
          {d.title}
        </button>
      ))}
    </Stack>
  );
}

/** The verify-before-live machine (see `ModalPhase`): save() asks the server for a
 *  verification of the payload and persists only once that record settles in the
 *  clear, naming the record on the save so the server can refuse a save of
 *  anything else. Nothing is persisted on any other path.
 *
 *  `collides` is re-read at the moment of persisting, not only while typing: the
 *  team's list moves under an open dialog (another admin, another tab), and a
 *  save that skipped the second look would silently overwrite the row that
 *  appeared while this check was out. */
function useVerifyThenSave({
  owner,
  checkedBeforeSave,
  payload,
  collides,
  beforeSave,
  onSaved,
}: {
  owner: Owner;
  /** The server's word on whether a save must name a settled check. When it
   *  checks nothing, the save goes straight to the upsert. */
  checkedBeforeSave: boolean;
  payload: () => TeamConnectionUpsert;
  collides: () => boolean;
  /** Run before the check, when the save needs the row to be somewhere else
   *  first — the owner picker moved. What it throws is what the dialog shows,
   *  and nothing is checked or stored afterwards. */
  beforeSave?: () => Promise<unknown>;
  onSaved: () => void;
}) {
  // Both halves are wired up and one of them is used: a hook cannot be called
  // conditionally, and the unused mutation never runs. The team hooks take the
  // empty id in the personal case, where their mutationFn is never called.
  const teamId = isMine(owner) ? "" : owner.teamId;
  const { checks } = useConnectionChecks();
  const startTeam = checks.useStartTeam(teamId);
  const startMine = checks.useStartMine();
  const upsertTeam = useUpsertTeamConnection(teamId);
  const upsertMine = useUpsertMyConnection();
  const start = isMine(owner) ? startMine : startTeam;
  const upsert = isMine(owner) ? upsertMine : upsertTeam;
  const [phase, setPhase] = useState<ModalPhase>("editing");
  const [verificationId, setVerificationId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  // The browser gave up on this record. The record itself is untouched on the
  // server; this only stops THIS page asking.
  const [capped, setCapped] = useState(false);

  // The record, by its own id — a dedicated query, no coupling to any list.
  // Nothing exists server-side but the verification; the connection is not saved.
  const pollId = capped ? null : verificationId;
  const teamPoll = checks.usePollTeam(teamId, isMine(owner) ? null : pollId);
  const minePoll = checks.usePollMine(isMine(owner) ? pollId : null);
  const verification = isMine(owner) ? minePoll : teamPoll;
  const record = verification.data;
  // The instant the server last answered about this record, which is the clock
  // the record's age is read against. A record that is only growing older comes
  // back with every field unchanged, so react-query hands back the same object
  // and nothing re-renders: an age taken from `Date.now()` during render would
  // be evaluated once, on the first answer, and the wait would never get its
  // explanation however long it went on. This moves on every poll, and it is
  // also the truer reading — how old the record was when the server described
  // it, rather than how long this tab has been open since.
  const recordReadAt = verification.dataUpdatedAt;

  const fail = (err: unknown, fallback: string) => {
    setPhase("failed");
    setError(refusalSentence(err, { fallback }));
  };

  // The save. Where the server checks connections it is only ever called after a
  // record settled in the clear, carrying that record's id so the server can bind
  // the row to what it checked; where it checks nothing, it carries no id.
  const persist = (id: string | null) => {
    upsert.mutate(
      { ...payload(), verification_id: id },
      {
        onSuccess: onSaved,
        onError: (err) => {
          if (err instanceof ApiError && err.code === "verification_required") {
            // The record no longer covers this payload (edited since, expired,
            // already spent). Not a failure of the connection — ask for a fresh
            // check rather than showing the server's internal reason.
            setVerificationId(null);
            fail(null, "The check no longer matches what you're saving. Validate again.");
            return;
          }
          fail(err, "Couldn't save the connection.");
        },
      },
    );
  };

  const startCheck = () => {
    start.mutate(payload(), {
      onSuccess: ({ id }) => {
        setVerificationId(id);
        setPhase("verifying");
      },
      onError: (err) => fail(err, "Couldn't start the check."),
    });
  };

  const save = () => {
    setError(null);
    setCapped(false);
    setVerificationId(null);
    void (async () => {
      // The move goes first: the save is an upsert by (owner, plugin, handle),
      // so checking and storing at the new owner before the row got there would
      // write a second row and leave the first one where it was.
      if (beforeSave) {
        try {
          await beforeSave();
        } catch (err) {
          fail(err, "Couldn't change who this connection is for.");
          return;
        }
      }
      if (!checkedBeforeSave) {
        persist(null);
        return;
      }
      startCheck();
    })();
  };

  // React to the settled record. A pass persists; anything else leaves the dialog
  // editable with the record's own reading on screen (nothing was persisted, so
  // there is nothing to clean up).
  const settled = record?.state === "settled";
  const passed = verificationPassed(record);
  const verifying = phase === "verifying";
  useEffect(() => {
    if (!verifying || !record) return;
    if (record.state === "abandoned") {
      setPhase("failed");
      return;
    }
    if (!settled) return;
    if (!passed) {
      setPhase("failed");
      return;
    }
    // The last look at the name, taken against the list as it is NOW: the check
    // took seconds, and the team's connections are not frozen while it ran.
    if (collides()) {
      setVerificationId(null);
      setPhase("editing");
      return;
    }
    if (verificationId) persist(verificationId);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- persist/collides close over the current draft, which is frozen while verifying
  }, [verifying, settled, passed, record?.state]);

  // The safety cap. Measured from the request, so it bounds the whole wait rather
  // than any one leg of it.
  useEffect(() => {
    if (!verifying || !verificationId) return;
    const timer = window.setTimeout(() => {
      setCapped(true);
      setPhase("failed");
    }, VERIFY_CAP_MS);
    return () => window.clearTimeout(timer);
  }, [verifying, verificationId]);

  const returnToEditing = () => {
    setPhase("editing");
    setError(null);
  };

  const resetAll = () => {
    setPhase("editing");
    setVerificationId(null);
    setError(null);
    setCapped(false);
  };

  return {
    phase,
    error,
    /** The record's own reading, or the cap's. Absent while there is nothing to say. */
    notice:
      capped
        ? {
            tone: "danger" as const,
            title: "The worker didn't pick this up",
            body: "Nothing checked this connection. Try again; nothing has been saved.",
            terminal: true,
          }
        : record
          ? verificationNotice(record, recordReadAt)
          : undefined,
    /** The poll itself failed — the check's status is unknown, which is not the
     *  same as the check having failed. */
    pollFailed: !capped && verification.isError,
    pollError: verification.error?.message ?? "",
    retryPoll: () => void verification.refetch(),
    // A record that settled in the clear is not the end of the wait: the save it
    // leads to is still to come, and handing the form back in between would
    // re-arm the save button on a payload already on its way to the server.
    validating:
      (verifying &&
        !capped &&
        (record === undefined || verificationInFlight(record.state) || passed)) ||
      upsert.isPending,
    startPending: start.isPending,
    checked: checkedBeforeSave,
    save,
    returnToEditing,
    resetAll,
  };
}

/** The add / edit connection dialog — the owner picker, the shared generic form,
 *  and (for a team) the preconfiguration controls (what members inherit,
 *  auto-add). Editing opens the same dialog on an existing row: the save is an
 *  upsert by identity either way, so there is one code path and one set of
 *  refusals rather than an add dialog and an edit dialog that drift apart. */
export function ConnectionDialog({
  open,
  owner,
  onOwnerChange,
  editing,
  onClose,
}: {
  open: boolean;
  owner: Owner;
  onOwnerChange: (owner: Owner) => void;
  /** The row being edited, or null when adding. Its connector is fixed; its
   *  owner is not — picking a different one moves the row through the move
   *  route before the save, because the save upserts by (owner, plugin, handle)
   *  and would otherwise write a second row at the new owner. */
  editing: TeamConnection | null;
  onClose: () => void;
}) {
  const forms = useConnectionForms(open);
  const teams = useTeams(open);
  const me = useCurrentUser();
  const adminTeamIds = useMemo(
    () => new Set(me.data?.admin_team_ids ?? []),
    [me.data?.admin_team_ids],
  );
  const teamId = isMine(owner) ? "" : owner.teamId;
  const teamRows = useTeamConnections(teamId || null, open && !isMine(owner));
  const myRows = useMyConnections(open && isMine(owner));
  // Only this owner's rows can collide: the identity key includes the owner, so
  // a member's "prod" and the team's "prod" are two different rows.
  const existingRows: TeamConnection[] = isMine(owner)
    ? (myRows.data ?? []).filter((c) => c.owner_user_id)
    : (teamRows.data ?? []);
  const [connector, setConnector] = useState<string>("");
  const [draft, setDraft] = useState<ConnectionDraft>(emptyDraft());
  // The org's OAuth app for a CONFIDENTIAL per-user provider (BigQuery/Google) —
  // the admin supplies it here; it's stored encrypted server-side and members
  // never see it. Empty for public-client providers (Snowflake/Databricks).
  const [oauthApp, setOauthApp] = useState({ clientId: "", clientSecret: "" });
  // Fields the admin fills in to verify the connection but keeps back from
  // members, who answer them on their own machines. Everything is prefilled by
  // default, which is why the empty set is the starting point.
  const [withheld, setWithheld] = useState<Set<string>>(new Set());

  const descriptors = useMemo(
    () => (forms.data?.connectors ?? []).filter((d) => (d.team_capable_methods ?? []).length > 0),
    [forms.data],
  );

  // Opening on an existing row seeds the form from what was stored. A secret
  // does not come back — the server never returns one — and a blank one keeps
  // what is stored, which is the same contract the save has always had.
  useEffect(() => {
    if (!open || !editing) return;
    setConnector(editing.plugin);
    setDraft({
      handle: editing.handle,
      method: editing.auth_method ?? "",
      values: { ...(editing.shared_values ?? {}) },
    });
    setWithheld(new Set(editing.member_fields ?? []));
    setOauthApp({ clientId: editing.oauth_client_id ?? "", clientSecret: "" });
  }, [open, editing]);
  const descriptor = descriptors.find((d) => d.name === connector) ?? null;
  const baseForm = (descriptor?.form ?? null) as ConnectionFormView | null;
  // The compiled ask groups ride the descriptor beside the form; the view
  // carries them together so the submit gate and the divider read one object.
  const catalogForm: ConnectionFormView | null =
    baseForm && descriptor ? { ...baseForm, ask_groups: descriptor.ask_groups ?? {} } : baseForm;
  // A credential this row already holds is not an empty required box.
  const keptSecrets = keptSecretNames(catalogForm, editing);
  const form: ConnectionFormView | null =
    catalogForm && relaxKeptSecrets(catalogForm, keptSecrets);
  // What Save reads. The rendered form keeps every group so the credential keeps
  // its place above the optional tail; only the gate treats a stored one as the
  // answer it is.
  const gateForm: ConnectionFormView | null = form && withoutKeptGroups(form, keptSecrets);
  // The org's OAuth client secret behaves like every other stored credential: the
  // server never returns it, and a save that does not carry one keeps what it holds.
  // So an edit of a row that already has one leaves its box empty on purpose, and
  // that box neither asks to be filled nor holds Save shut.
  const keptOAuthSecret = Boolean(editing?.has_oauth_client_secret);
  const teamCapable = new Set(descriptor?.team_capable_methods ?? []);
  const methodFilter = (m: AuthMethodView) => teamCapable.has(m.name);
  const {
    method,
    oauthMethod,
    needsOAuthApp,
    authMethod,
    methodFields,
    secretFields,
    perUser,
    autoAddable,
  } = deriveTeamShare(form, descriptor?.team_capable_methods ?? [], draft.method, withheld);

  // The admin fills in the WHOLE connection whatever they hand over, so the
  // server validates and dials something real. The switch beside each field
  // decides only what descends to members.
  const destinationName = isMine(owner)
    ? "you"
    : ((teams.data ?? []).find((t) => t.id === owner.teamId)?.name ?? "that team");
  const teamName = isMine(owner)
    ? ""
    : ((teams.data ?? []).find((t) => t.id === owner.teamId)?.name ?? "this team");

  const shareControl = (f: FormFieldView) => (
    <label className={styles.prefill}>
      <Checkbox
        checked={!withheld.has(f.name)}
        disabled={machine.validating}
        aria-label={`Prefill ${f.label} for members`}
        onChange={(e) =>
          setWithheld((prev) => {
            const next = new Set(prev);
            if (e.target.checked) next.delete(f.name);
            else next.add(f.name);
            return next;
          })
        }
      />
    </label>
  );

  // A handle that collides with a PRE-EXISTING connection must not save from
  // the add dialog — the server upserts by identity, so it would silently
  // overwrite an existing connection's config.
  //
  // Neither name check is scoped to a phase. A failed check returns the form to
  // an editable state without returning `phase` to "editing", so a check that
  // only held while editing went blind exactly when the admin was retyping.
  // Editing a row is not colliding with itself: the save is an upsert by that
  // same identity.
  const handleCollides = existingRows.some(
    (c) =>
      c.id !== editing?.id &&
      c.plugin === descriptor?.name &&
      c.handle === draft.handle.trim(),
  );

  // The server refuses a handle that isn't a safe slug, so catch it here rather
  // than spend a round trip on it. An empty name already blocks the save through
  // `isValid`, and a field nobody has filled in yet has nothing to correct.
  const handleUnsafe = draft.handle.trim() !== "" && !isSafeHandle(draft.handle.trim());

  // Both name problems belong ON the name field. Carried to the bottom of the
  // dialog they landed below the fold, so an admin typing an invalid name saw an
  // unchanged field and a Save button that had quietly stopped working. An unsafe
  // name outranks a collision: it can never save, and the collision is one the
  // admin could still resolve by removing the other row.
  const handleProblem = handleUnsafe
    ? SAFE_HANDLE_MESSAGE
    : handleCollides
      ? `${isMine(owner) ? "You already have" : "This team already preconfigures"} a ${descriptor?.title ?? "connection"} named ${draft.handle.trim()}. Pick another name, or remove the existing one first.`
      : undefined;

  const payload = (): TeamConnectionUpsert => ({
    plugin: descriptor?.name ?? "",
    handle: draft.handle.trim(),
    // The start-a-check call and the save share one request shape; only the save
    // names the record (`persist` adds `verification_id`). The server refuses a
    // save that quotes none, and refuses a check request that quotes one.
    // The complete connection, always — including the values the admin keeps.
    // The server can only verify a whole connection, and this is the moment
    // somebody holds one. It dials this, then stores only what descends.
    fields: form ? withDefaults(form, method?.fields ?? [], draft.values) : draft.values,
    auth_method: authMethod,
    // Every switch that is on. Who holds the credential and whether the tier is
    // inherited both follow from this list, so neither is sent separately.
    shared_fields: methodFields
      .map((f) => f.name)
      .filter((n) => isMine(owner) || !withheld.has(n)),
    // Every connection saved from the portal arrives ready to use WHERE IT CAN.
    // The portal asks nobody about it, but it cannot ask for it either where the
    // server would refuse the whole save: a team row that still leaves a member
    // something to answer — a switched-off field, or a browser sign-in every
    // member performs themselves — stores as a suggestion instead. A personal
    // row has no members, so it is always ready.
    auto_add: isMine(owner) ? true : autoAddable,
    enabled: true,
    // The org OAuth app rides its own encrypted slots (never a form field), and
    // only where the provider obliges the org to register one.
    ...(needsOAuthApp
      ? {
          oauth_client_id: oauthApp.clientId.trim(),
          // Trimmed like the id: a pasted secret's trailing newline would save
          // silently and only fail later at member sign-in (no check covers it).
          // A blank box on a row that already holds a secret carries nothing — the
          // save reads an absent secret as "keep the stored one".
          ...(oauthApp.clientSecret.trim()
            ? { oauth_client_secret: oauthApp.clientSecret.trim() }
            : {}),
        }
      : {}),
  });

  const move = useMoveConnectionOwner();
  // Where a move this dialog already performed left the row. `editing` is the
  // snapshot the page took when the dialog opened and never re-reads, so once
  // the move lands it names the old owner; a check that fails afterwards leaves
  // the dialog open re-offering a move that has already happened.
  const [movedOwner, setMovedOwner] = useState<Owner | null>(null);
  // Where the row is now, as the picker spells it — so "changed" is one
  // comparison rather than three special cases.
  const storedOwner: Owner | null = !editing
    ? null
    : (movedOwner ??
      (editing.owner_user_id ? { kind: "me" } : { kind: "team", teamId: editing.team_id }));
  const ownerChanged =
    storedOwner !== null &&
    (isMine(owner) !== isMine(storedOwner) ||
      (!isMine(owner) && !isMine(storedOwner) && owner.teamId !== storedOwner.teamId));

  const machine = useVerifyThenSave({
    owner,
    checkedBeforeSave: forms.data?.checked_before_save ?? true,
    payload,
    // Read at settle time, not captured: the list refetches under an open dialog.
    collides: () => handleCollides,
    beforeSave:
      editing && ownerChanged
        ? async () => {
            const destination: Owner = isMine(owner)
              ? { kind: "me" }
              : { kind: "team", teamId: owner.teamId };
            const moved = await move.mutateAsync({
              connectionId: editing.id,
              teamId: isMine(owner) ? null : owner.teamId,
            });
            setMovedOwner(destination);
            return moved;
          }
        : undefined,
    onSaved: () => close(),
  });

  const close = () => {
    setConnector("");
    setDraft(emptyDraft());
    setOauthApp({ clientId: "", clientSecret: "" });
    setWithheld(new Set());
    setMovedOwner(null);
    machine.resetAll();
    onClose();
  };

  const save = () => {
    if (!descriptor || handleCollides || handleUnsafe) return;
    machine.save();
  };

  // One box for the whole column. Partly-checked reads as partly-checked rather
  // than guessing a side, and clicking it prefills everything.
  const allPrefilled = withheld.size === 0;
  const prefillAll = (
    <label className={styles.prefill}>
      <Checkbox
        ref={(el: HTMLInputElement | null) => {
          if (el) el.indeterminate = !allPrefilled && withheld.size < methodFields.length;
        }}
        checked={allPrefilled}
        disabled={machine.validating}
        aria-label="Prefill every value for members"
        onChange={(e) => setWithheld(e.target.checked ? new Set() : new Set(methodFields.map((f) => f.name)))}
      />
    </label>
  );

  return (
    <Modal
      open={open}
      onClose={close}
      size="xl"
      title={editing ? `Edit ${editing.handle}` : "Add a connection"}
    >
      <Stack gap="var(--alkSpaceLg)" align="stretch">
      <FieldShell
        htmlFor=""
        label="Who this connection is for"
        // Only the consequence of the change, and only once it has been made:
        // a caption under an untouched picker is a paragraph everyone reads
        // past, and the reader has nothing to do about it.
        description={
          ownerChanged
            ? isMine(owner)
              ? "Saving moves this connection to you. Everyone who reached it through the team loses it, along with any sign-in they did for it."
              : `Saving moves this connection to ${destinationName}. Everyone there gets it with the credential stored on it; whoever reaches it today and isn't on that team loses it.`
            : undefined
        }
      >
        <TeamPicker
          size="sm"
          value={owner}
          onChange={onOwnerChange}
          teams={(teams.data ?? []) as PickableTeam[]}
          adminTeamIds={adminTeamIds}
          disabled={machine.validating}
        />
      </FieldShell>
      {!descriptor ? (
        <ConnectorPicker
          loading={forms.isLoading}
          descriptors={descriptors}
          onPick={(d) => {
            setConnector(d.name);
            const dform = d.form as unknown as ConnectionFormView;
            const first = dform.auth_methods.find((m) =>
              (d.team_capable_methods ?? []).includes(m.name),
            );
            // Picking seeds the draft with the first team-capable method and
            // its declared defaults, exactly as the workspace form starts.
            setDraft({
              ...emptyDraft(),
              method: first?.name ?? "",
              values: withDefaults(dform, first?.fields ?? [], {}),
            });
            setOauthApp({ clientId: "", clientSecret: "" });
          }}
        />
      ) : (
        <Stack gap="var(--alkSpaceLg)" align="stretch">
          <ConnectionFormFields
            form={form as ConnectionFormView}
            draft={draft}
            // A field name is only valid under the method that declares it, so
            // a switched method starts the sharing choices over.
            onDraftChange={(next) => {
              if (next.method !== draft.method) {
                setWithheld(new Set());
              }
              setDraft(next);
            }}
            disabled={machine.validating}
            methodFilter={methodFilter}
            hideNote
            // A browser sign-in has nothing to hand back: every input on the form
            // is a coordinate, the member supplies only their identity, and no
            // screen ever asks them for the rest. Offering the switch here would
            // promise a step that never comes.
            fieldAccessory={
              !MEMBER_PREFILL_ENABLED || oauthMethod || isMine(owner) ? undefined : shareControl
            }
            accessoryHeader={!MEMBER_PREFILL_ENABLED || isMine(owner) ? undefined : prefillAll}
            accessoryLabel={
              !MEMBER_PREFILL_ENABLED || isMine(owner) ? undefined : "Prefill for members"
            }
            handleError={handleProblem}
            belowMethodSlot={
              oauthMethod ? (
                <>
                  <Callout tone="info" icon={null}>
                    Members sign in themselves. Each authorizes in their browser, so their own
                    warehouse permissions apply. Secret credentials never leave this admin config.
                    Every value you enter below reaches them as you entered it. Signing in is the
                    only step they take, so there is nowhere for them to answer a field you keep
                    back.
                  </Callout>
                  {needsOAuthApp ? (
                    <Stack gap={3} align="stretch">
                      <TextInput
                        label="OAuth client ID"
                        description="From the OAuth app you registered for this connector (a Google Cloud OAuth 2.0 “Desktop app” client). Members never see it."
                        required
                        autoComplete="off"
                        value={oauthApp.clientId}
                        disabled={machine.validating}
                        onChange={(e) =>
                          setOauthApp((s) => ({ ...s, clientId: e.target.value }))
                        }
                      />
                      <TextInput
                        type="password"
                        label="OAuth client secret"
                        description="Stored encrypted for your org and used only to sign members in. It never reaches a member’s machine."
                        required={!keptOAuthSecret}
                        placeholder={keptOAuthSecret ? KEPT_SECRET_PLACEHOLDER : undefined}
                        autoComplete="off"
                        value={oauthApp.clientSecret}
                        disabled={machine.validating}
                        onChange={(e) =>
                          setOauthApp((s) => ({ ...s, clientSecret: e.target.value }))
                        }
                      />
                    </Stack>
                  ) : null}
                </>
              ) : null
            }
          />
          {isMine(owner) ? null : (
            <SharingSummary
              perUser={perUser}
              oauthMethod={oauthMethod}
              hasSecretField={secretFields.length > 0}
              withheldCount={withheld.size}
              teamName={teamName}
            />
          )}
          <Stack gap={0} align="stretch">
            {/* One explanation at a time, in the order of what the admin can do
                about it: a thrown failure (the save was refused, the check never
                started) outranks the record's reading, and a poll that itself
                failed says so rather than blaming the far end for a status
                nobody could read. */}
            {machine.error ? (
              <Callout tone="danger" title="Couldn't save">
                {inPickerWords(machine.error, form)}
              </Callout>
            ) : machine.pollFailed ? (
              <Callout tone="danger" title="Couldn't read the check's status">
                <Stack gap={2} align="stretch">
                  <span>
                    {machine.pollError ||
                      "The check may still be running. Try reading it again."}
                  </span>
                  <div>
                    <Button variant="secondary" fill="ghost" onClick={machine.retryPoll}>
                      Try again
                    </Button>
                  </div>
                </Stack>
              </Callout>
            ) : machine.notice ? (
              <Callout
                tone={machine.notice.tone === "info" ? "info" : machine.notice.tone}
                icon={machine.notice.tone === "info" ? null : undefined}
                title={machine.notice.title}
              >
                {/* The body can be the connector's own sentence, which runs to
                    several lines and says why on the last one — so it wraps and
                    keeps its line breaks instead of being clipped. */}
                {machine.notice.terminal ? (
                  <Stack gap={2} align="stretch">
                    <span className={styles.driverDetail}>
                      {inPickerWords(machine.notice.body, form)}
                    </span>
                    <div>
                      <Button variant="secondary" fill="ghost" onClick={() => save()}>
                        Try again
                      </Button>
                    </div>
                  </Stack>
                ) : (
                  <span className={styles.driverDetail}>
                    {inPickerWords(machine.notice.body, form)}
                  </span>
                )}
              </Callout>
            ) : null}
            <Inline gap={2} justify="flex-end" style={{ marginTop: "var(--alkSpace3)" }}>
              {/* Cancel is always safe: nothing exists until a verified save,
                  so there is never anything to clean up. */}
              <Button variant="secondary" fill="ghost" onClick={close}>
                Cancel
              </Button>
              <Button
                variant="primary"
                loading={machine.startPending || machine.validating || move.isPending}
                disabled={
                  !isValid(draft, gateForm as ConnectionFormView) ||
                  handleCollides ||
                  handleUnsafe ||
                  machine.validating ||
                  // The move is not idempotent, so the key is dead while it is
                  // out: a second click would move the row a second time.
                  move.isPending ||
                  (needsOAuthApp &&
                    (!oauthApp.clientId.trim() ||
                      (!oauthApp.clientSecret.trim() && !keptOAuthSecret)))
                }
                onClick={() => {
                  machine.returnToEditing();
                  save();
                }}
              >
                {machine.validating
                  ? machine.checked
                    ? "Checking…"
                    : "Saving…"
                  : machine.phase === "failed"
                    ? machine.checked
                      ? "Save & validate again"
                      : "Save connection"
                    : "Save connection"}
              </Button>
            </Inline>
          </Stack>
        </Stack>
      )}
      </Stack>
    </Modal>
  );
}

/** Who ends up holding the credential, in a sentence — the one consequence of
 *  the form the admin cannot read off the fields in front of them. A team
 *  credential of its own says nothing: the box is right above the line. */
function SharingSummary({
  perUser,
  oauthMethod,
  hasSecretField,
  withheldCount,
  teamName,
}: {
  perUser: boolean;
  oauthMethod: boolean;
  hasSecretField: boolean;
  withheldCount: number;
  teamName: string;
}) {
  let credential: string;
  if (oauthMethod) {
    credential = `Each member of ${teamName} signs in through their browser, so their own warehouse permissions apply.`;
  } else if (!hasSecretField) {
    // Nothing in this form is a credential (AWS reads the SSO token cache on the
    // member's own machine), so the sharing sentence must not claim one.
    credential = `This connector signs in on each member's own machine, so ${teamName} shares the connection but no credential.`;
  } else if (perUser) {
    credential = `Each member of ${teamName} signs in with their own credential, so the warehouse sees who ran what.`;
  } else {
    // A credential of the team's own says nothing back, whether the admin typed
    // one or left the box empty: the box is on screen above this line.
    return null;
  }
  // What members still answer for themselves, which exists only while the
  // switches that decide it do.
  const rest =
    !MEMBER_PREFILL_ENABLED || withheldCount === 0
      ? ""
      : withheldCount === 1
        ? " They also fill in the one field you switched off."
        : ` They also fill in the ${withheldCount} fields you switched off.`;
  // The dialog's second read, so it holds the body size and the secondary ink
  // rather than the tertiary caption every field description already uses. It is
  // the verdict on every switch above it, and the last thing read before Save.
  return (
    <p className="alk-muted">
      {credential}
      {rest}
    </p>
  );
}
