"""The declarative connection-form schema (PLUGINS — the Plugins & Connections UI).

A plugin that needs typed credentials/config to add a connection declares a
``ConnectionFormSchema`` so the UI renders the input fields GENERICALLY — the
framework never hard-codes a per-plugin form. A plugin with no schema keeps pure
detect-then-add (handle only, no form) — exactly what local/zero-auth connectors
(DuckDB, dbt) need, so the schema is fully backward-compatible.

These are in-flight wire shapes (computed from the static plugin + sent to the UI),
never persisted — plain ``BaseModel``. Secrets are never part of the SCHEMA; a
secret-flagged field's VALUE is collected, stored via the ``CredentialManager`` at
the add boundary, and never echoed back.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class OAuthSpec(BaseModel):
    """The reserved OAuth slot. A plugin's auth method declares this so the generic
    form can render an "Authorize with …" button instead of secret fields. The shared,
    framework-owned backend-broker ↔ daemon-loopback handshake that CONSUMES it is
    deferred (built once, later) — reserving the slot now keeps the form API
    OAuth-ready with zero per-plugin redirect/callback code. Local connectors
    (DuckDB/dbt) never set it."""

    provider: str
    """The provider id the shared handshake routes through (e.g. "snowflake")."""
    authorize_url: str = ""
    scopes: list[str] = Field(default_factory=list)
    needs_org_client: bool = False
    """Whether the ORG must register its own OAuth client with this provider and
    supply the id and secret, which the backend keeps encrypted and members never
    see (BigQuery/Google). False where the provider ships a built-in client
    (Snowflake, Databricks), so an admin has nothing to register. Stamped per
    provider when the catalog is served; the static schema leaves it False."""


class FormField(BaseModel):
    """One input the connection form renders."""

    name: str
    label: str
    type: Literal["text", "password", "file", "directory", "number", "select", "bool"] = "text"
    """``file`` / ``directory`` collect a project-relative path. The web UI renders a native
    path picker; the TUI collects the path via a text input today (a native picker is a future
    enhancement). ``file`` is a single file (a ``.duckdb`` / ``.sqlite``); ``directory`` is a
    folder (a dbt project dir)."""
    required: bool = True
    satisfied_by: list[str] = Field(default_factory=list)
    """Other inputs that answer this one in its place. A required field whose
    alternative is filled is not missing — Snowflake's account identifier is
    answered by an Account URL, and the connector builds from either. Without
    this the shared presence check refuses a connection its own connector would
    accept."""
    secret: bool = False
    """True ⇒ collected, stored via ``CredentialManager``, never echoed back. The UI
    masks it; the value never lands on the ``Connection`` or in agent context."""
    credential_role: str = ""
    """Independent named credential role, or empty for the primary credential."""
    max_length: int = 0
    """The longest value this input accepts (0 = unbounded). Declared once here;
    every surface enforces it through the shared compile in ``catalog`` rather
    than keeping a bound of its own."""
    default: str = ""
    enum_labels: dict[str, str] = Field(default_factory=dict)
    """The word to show for each ``enum_values`` entry. A connector stores a terse
    token; the picker should read the way the rest of the product prints it."""
    enum_values: list[str] = Field(default_factory=list)
    """The options for a ``select`` field (empty ⇒ a free input, not a dropdown).

    These are non-``None`` by construction so the wire shape the UI consumes never
    carries ``null`` for them — a JSON ``null`` here would crash the generic form's
    field renderer (``enum_values.length``). Absence is the empty value, not null."""
    help: str = ""
    """Persistent guidance rendered BELOW the input (always visible — never inside
    the input, where it would vanish on focus). Empty ⇒ no help line."""
    placeholder: str = ""
    """Ghost text inside the empty input (an example value like
    ``xy12345.us-east-1``), distinct from ``help`` which stays visible while
    typing. Empty ⇒ no placeholder."""
    group: str = ""
    """Optional visual section label; consecutive fields sharing a ``group`` render
    under one heading (e.g. "Session defaults"). Empty ⇒ ungrouped."""


class ValueSlot(BaseModel):
    """One entry of a connection's values document — a form input carrying who
    answers it and what the answer is.

    The document is the single shape every surface reads: the backend serves it
    for stored rows, the daemon serves it for team records, and clients gate on
    it structurally (a compiled ask group is satisfied when some slot in it has
    a non-blank ``value`` or is ``deferred``). A secret slot never carries its
    value here — ``deferred`` says it is answered somewhere safer (an encrypted
    column, a credential file, a sign-in)."""

    name: str
    label: str
    value: str = ""
    owner: Literal["admin", "member"] = "admin"
    """Who answers this slot: the admin distributed a value, or the member
    supplies their own."""
    deferred: bool = False
    """Answered outside the document — a stored shared secret, a member's
    credential file, a completed sign-in. Counts as an answer."""
    secret: bool = False

    # The render surface, copied off the declaring ``FormField`` so a slot is
    # enough to draw the input that collects it.
    type: str = "text"
    required: bool = False
    default: str = ""
    enum_values: list[str] = Field(default_factory=list)
    enum_labels: dict[str, str] = Field(default_factory=dict)
    help: str = ""
    placeholder: str = ""
    group: str = ""
    max_length: int = 0


class AuthMethodSchema(BaseModel):
    """One auth choice the user picks (pat / user_pass / key_pair / oauth / …); the
    UI shows the fields for the chosen method only."""

    name: str
    label: str
    fields: list[FormField] = Field(default_factory=list)
    oauth: OAuthSpec | None = None
    """Set ⇒ this method authorizes via the (deferred) shared OAuth handshake — the
    form renders an "Authorize with …" button rather than secret fields."""

    # --- capability matrix (per auth method, NOT per plugin) ---
    local_capable: bool = True
    """A member may create a connection with this method anew in the LOCAL form.
    False for methods whose config belongs to the org (e.g. BigQuery's OAuth
    client — members never paste a client id/secret)."""
    team_capable: bool = False
    """A team/org admin may preconfigure a connection with this method (a
    Preconfigured Connection). False for machine-local auth that has nothing
    storable to distribute (e.g. BigQuery ADC — each machine's gcloud login)."""
    auto_add_override: bool | None = None
    """Auto-add eligibility is DERIVED — a preconfigured connection may be
    auto-added iff it needs zero member interaction, i.e. shared mode with the
    admin holding the full credential. This slot force-overrides the derivation
    for exotic methods; ``None`` (the default) means derive."""


class ConnectionFormSchema(BaseModel):
    """A plugin's declarative connection form: its auth methods + any fields shared
    across all of them (e.g. host/port). ``None`` from a plugin = no form (pure
    detect-then-add)."""

    note: str = ""
    """An optional plain-text instruction the UI shows at the TOP of the form, before any
    field — for setup a field can't capture. BigQuery uses it to say "sign in first with
    ``gcloud auth application-default login``" since its auth isn't a form secret (it
    resolves via Application Default Credentials at the I/O boundary). Empty ⇒ no banner."""
    auth_methods: list[AuthMethodSchema] = Field(default_factory=list)
    shared_fields: list[FormField] = Field(default_factory=list)
    trailing_fields: list[FormField] = Field(default_factory=list)
    """Fields rendered AFTER the optional tail, whatever their required flag.
    A connector never declares these — the team-connection catalog appends the
    deployment tier here so it reads as a closing question rather than landing
    in the middle of the credential block."""
    implicit_team_capable: bool = False
    """Capability slot for forms with NO auth methods (the secret is a shared
    field — ClickHouse/Trino/generic-SQL): whether an admin filling the whole
    form (including that secret) may preconfigure it at team/org level. The
    implicit method's ``auth_method`` is stored as the empty string."""


__all__ = [
    "AuthMethodSchema",
    "ConnectionFormSchema",
    "FormField",
    "OAuthSpec",
    "ValueSlot",
]
