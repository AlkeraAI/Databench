// The connection-form view model — ONE schema shape rendered by every surface
// (the VS Code webview's Plugins & Connections page AND the browser portal's
// team Preconfigured Connections). Mirrors the backend/daemon ConnectionFormSchema
// wire shape; a secret typed into the form lives only in the caller's transient
// draft, never in these types.

/** One input the connection form renders. */
export interface FormFieldView {
  name: string;
  label: string;
  /** "text" | "password" | "number" | "select" | "file" | "directory" | "bool" — drives the
   *  control; an unknown value renders as a plain text input (forward-compatible). */
  type: string;
  required: boolean;
  /** The longest value this input accepts; 0/absent = unbounded. Declared once on the
   *  field server-side; the submit gate refuses a value past it. */
  max_length?: number;
  /** The word to show for each stored enum value. A picker showing the token a
   *  connector stores reads terser than the table that lists the same value. */
  enum_labels?: Record<string, string>;
  /** True ⇒ masked, collected transiently, never echoed back. */
  secret: boolean;
  default: string;
  /** Options for a select field (empty ⇒ free input, never null on the wire). */
  enum_values: string[];
  /** Persistent guidance rendered BELOW the input (never inside it). */
  help: string;
  /** Ghost example text inside the empty input. */
  placeholder: string;
  /** Consecutive fields sharing a group render under one heading. */
  group: string;
}

/** The OAuth slot on an auth method — renders an "Authorize with …" button. */
export interface OAuthSpecView {
  provider: string;
  authorize_url?: string;
  scopes?: string[];
  /** True ⇒ the provider's OAuth client secret is org-held (BigQuery/Google): a
   *  per-user connection needs the admin to supply the org's OAuth client id +
   *  secret. False/absent ⇒ a public-client provider (Snowflake/Databricks) that
   *  needs no app. Set by the backend when it serves the connector catalog. */
  needs_org_client?: boolean;
}

/** One auth choice the user picks; the form shows its fields only. */
export interface AuthMethodView {
  name: string;
  label: string;
  fields: FormFieldView[];
  oauth: OAuthSpecView | null;
  /** Capability matrix (per auth method): where this method may be used. */
  local_capable?: boolean;
  team_capable?: boolean;
}

/** A plugin's declarative connection form. */
export interface ConnectionFormView {
  /** Top-of-form instruction banner; empty/null ⇒ none. */
  note: string | null;
  auth_methods: AuthMethodView[];
  shared_fields: FormFieldView[];
  /** Fields rendered after the optional tail, whatever their required flag.
   *  The team-connection catalog puts the deployment tier here so it reads as
   *  a closing question instead of interrupting the credential block. */
  trailing_fields?: FormFieldView[];
  implicit_team_capable?: boolean;
  /** The server-compiled completeness rule per auth method (a method-less form
   *  keys the empty string): each group names inputs of which AT LEAST ONE must
   *  be answered. The submit gate and the required/optional divider read these;
   *  the client never re-derives the rule from the fields. Absent on an older
   *  server ⇒ the gate falls back to each required field as its own group. */
  ask_groups?: Record<string, string[][]>;
}

/** One entry of a connection's values document: a form input carrying who
 *  answers it and the answer held so far. A secret slot never carries its
 *  value — `deferred` says something safer holds it (an encrypted column, a
 *  credential file, a sign-in) and it counts as answered. */
export interface ValueSlotView {
  name: string;
  label: string;
  value: string;
  /** "admin" (distributed) | "member" (this member supplies it). */
  owner: "admin" | "member";
  deferred: boolean;
  secret: boolean;
  /** The render surface, copied off the declaring field. */
  type: string;
  required: boolean;
  default: string;
  enum_values: string[];
  enum_labels: Record<string, string>;
  help: string;
  placeholder: string;
  group: string;
  max_length: number;
}

/** The transient form state a caller owns — handle + chosen method + values.
 *  Secrets live here only until submit and are never persisted by the UI. */
export interface ConnectionDraft {
  handle: string;
  method: string;
  values: Record<string, string>;
}

/** Host-provided native path picker (VS Code). Absent in the browser portal —
 *  file/directory fields degrade to a plain text input. */
export type PickPathFn = (opts: { directory: boolean }) => Promise<string | null>;
