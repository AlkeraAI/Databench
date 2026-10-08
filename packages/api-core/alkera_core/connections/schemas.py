"""Team/org Preconfigured Connections — wire shapes.

Secrets are WRITE-ONLY throughout: read shapes expose only ``has_*`` booleans;
an update omitting/nulling a secret keeps the stored ciphertext. The deliberate
exception is the member credential endpoint, which distributes the shared
primary credential and any independently stored named extras over TLS
(membership-gated, audited on every call).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from alkera_core.connections import (
    Badge,
    CredentialState,
    Outcome,
    Reauth,
    VerificationState,
)
from alkera_core.naming import handle_error, is_safe_handle

MAX_SECRET_CHARS = 6000
"""The longest credential whose Fernet ciphertext still fits the column that
holds it. Measured against ``shared_secret_encrypted``'s String(8192): 6079
fits, 6080 does not. Rounded down so a future format change has room. Both the
rotate schema below and the save rule in ``team_connection_service`` refuse at
this bound, so it has one owner — two copies would drift the two endpoints
apart the first time the cipher or the column changed."""


class MemberCredentialCounts(BaseModel):
    """How a per-user row's members stand with their own credentials — the one
    number an admin needs to know that the row works for everybody but Dana."""

    authorized: int = 0
    needs_reauth: int = 0


class TeamConnectionRead(BaseModel):
    """One preconfigured connection, admin/portal view. No secret material."""

    id: UUID
    team_id: UUID
    team_name: str = ""
    #: Set on a PERSONAL connection: the one member it belongs to. None means
    #: the row is the team's.
    owner_user_id: UUID | None = None
    #: Who added the row, and the name to print next to it — their display name
    #: when they have one, else their email. Read at serve time rather than
    #: stored, so a rename shows up everywhere at once.
    created_by_id: UUID | None = None
    created_by_name: str = ""
    #: Whether THIS caller may edit, rotate or remove the row: its owner on a
    #: personal one, an admin of the owning team or any team above it on a
    #: team one. Everyone entitled can still use it and re-check it.
    can_manage: bool = False
    plugin: str
    handle: str
    # The raw form values this connection distributes, by the connector's own
    # input names. Its complement is ``member_fields``.
    shared_values: dict[str, str] = Field(default_factory=dict, title="Admin Shared Values")
    auth_mode: Literal["shared", "per_user"]
    auth_method: str = ""
    member_fields: list[str] = Field(default_factory=list)
    # The values document — every form input as a slot carrying who answers it
    # and the distributed value (serialized ``ValueSlot``s, form order). The
    # stored columns above are its flattening; this is what clients read.
    values_doc: list[dict[str, Any]] = Field(default_factory=list, title="Admin Values Doc")
    # The compiled completeness rule: each group names inputs of which at least
    # one must be answered. Clients check the groups against the doc + typed
    # values; only the server ever reads required/satisfied_by together.
    ask_groups: list[list[str]] = Field(default_factory=list, title="Admin Ask Groups")
    # Who put a shared credential in front of the whole team, when, and over
    # which inputs. Absent on a row that distributes no secret.
    shared_consent: dict[str, Any] | None = None
    auto_add: bool = False
    enabled: bool = True
    has_shared_secret: bool = Field(
        default=False,
        description=(
            "Compatibility flag: true when any primary or named shared credential is stored."
        ),
    )
    has_primary_secret: bool = Field(
        default=False,
        description="True when the shared credential bundle includes a primary value.",
    )
    credential_version: int = 0
    oauth_client_id: str | None = None
    has_oauth_client_secret: bool = False
    oauth_config: dict[str, Any] | None = None
    # --- what every surface shows, derived once on the server ---
    #: The one derived status. Clients present it; they never re-derive it.
    badge: Badge = Badge.not_checked
    #: The sentence under the badge: the credential's reason when the credential
    #: axis decided the badge, else the last verification's own detail.
    badge_reason: str = ""
    #: What the last SETTLED verification found. None means none ever settled.
    outcome: Outcome | None = None
    last_detail: str = ""
    #: When the last settled verification finished, whatever it found — so
    #: "checked 3 days ago" is true of a failure too.
    last_verified_at: datetime | None = None
    #: Set only while a verification is in flight.
    verification_state: VerificationState | None = None
    credential_state: CredentialState = CredentialState.present
    #: How a needs-reauth credential is fixed; None when nothing is wrong.
    reauth: Reauth | None = None
    #: Per-user rows only: how many members have signed in and how many must
    #: sign in again. None on a shared row, where the question does not arise.
    members: MemberCredentialCounts | None = None
    created_at: datetime
    updated_at: datetime


class TeamConnectionUpsertRequest(BaseModel):
    """Create/update a preconfigured connection (team admins).

    ``fields`` is the COMPLETE raw form input — every value, including the ones
    the admin keeps to themselves — because the server can only verify a whole
    connection, and this is the one moment somebody holds one. It builds and
    test-dials that, then stores none of it: what persists is the subset named
    by ``shared_fields``, raw, plus each independently encrypted credential role
    that was shared. A shared secret left blank on edit preserves that same
    primary or named role; a role is retired when the edited build no longer
    declares it, including when an optional feature is disabled.
    ``oauth_client_secret`` is likewise write-only, with None meaning keep.
    """

    plugin: str = Field(max_length=64)
    handle: str = Field(max_length=128)
    fields: dict[str, str] = Field(default_factory=dict)
    # The inputs whose distribute switch is ON — the values every member's
    # machine receives. Everything else in the form is the member's to answer,
    # including the deployment tier. Who holds the credential follows from
    # whether the secret inputs are named here; it is not a separate question.
    shared_fields: list[str] = Field(default_factory=list)
    auth_method: str = Field(default="", max_length=64)
    auto_add: bool = False
    enabled: bool = True
    # Per-user OAuth app config for CONFIDENTIAL providers (e.g. BigQuery) —
    # the client belongs to the org; its secret never reaches a member machine.
    oauth_client_id: str | None = Field(default=None, max_length=512)
    # Bounded by what its column holds AFTER encryption, not before: Fernet output
    # runs about a third longer, so a value the request admitted could only fail as
    # a truncated insert. 3007 is the largest plaintext whose ciphertext fits
    # String(4096); rounded down for headroom.
    oauth_client_secret: str | None = Field(default=None, max_length=3000)
    oauth_config: dict[str, Any] | None = None
    #: The verification this payload passed. The save requires one — the server
    #: stores only a configuration a check has already answered for, and the
    #: record's payload hash binds that answer to THIS payload, so an edited
    #: field cannot ride in on an older verdict.
    #:
    #: Optional on the shape rather than on the save, because the SAME shape is
    #: what asks for a check in the first place: a request for a verification
    #: cannot carry the id of the verification it is asking for. A save that
    #: omits it is refused by the save, by name, with the reason a person can
    #: act on — not by body validation with a field name.
    verification_id: UUID | None = None

    @field_validator("handle")
    @classmethod
    def _handle_is_safe(cls, value: str) -> str:
        # The handle becomes a filesystem path component on every member
        # machine; an unsafe one would save fine and then reach nobody.
        if not is_safe_handle(value):
            raise ValueError(handle_error(value))
        return value


class TeamConnectionRotateSecretRequest(BaseModel):
    """Rotate only the primary shared credential.

    Named roles change through the connection edit that owns their endpoint.
    This distinct verb records primary rotations and bumps the bundle version.
    """

    # Admitting more turned an over-long credential into a 500 from the
    # database rather than the named refusal every other bad input here gets.
    shared_secret: str = Field(min_length=1, max_length=MAX_SECRET_CHARS)


class ConnectionMoveRequest(BaseModel):
    """Re-address one connection to a different owner.

    ``team_id`` names the team it becomes; ``None`` makes it the caller's own.
    Nothing else travels in the request: the configuration, the stored
    credential and the choice of which values members supply are the row's
    already, and a move that re-sent them could quietly change them.
    """

    team_id: UUID | None = None


class MemberTeamConnection(BaseModel):
    """The member sync-down shape — full definition, NO secrets. The member's
    daemon reconciles these into the workspace (suggested / auto-added)."""

    id: UUID
    team_id: UUID
    team_name: str
    #: Set on a PERSONAL connection: the one member it belongs to. None means
    #: the row is the team's.
    owner_user_id: UUID | None = None
    #: Who added the row, and the name to print next to it — their display name
    #: when they have one, else their email. Read at serve time rather than
    #: stored, so a rename shows up everywhere at once.
    created_by_id: UUID | None = None
    created_by_name: str = ""
    #: Whether THIS caller may edit, rotate or remove the row: its owner on a
    #: personal one, an admin of the owning team or any team above it on a
    #: team one. Everyone entitled can still use it and re-check it.
    can_manage: bool = False
    plugin: str
    handle: str
    # The admin's raw form values for the inputs they distributed. This member's
    # machine merges its own answers on top and calls the connector's build once
    # — which is why nothing built ever crosses this wire.
    shared_values: dict[str, str] = Field(default_factory=dict, title="Member Shared Values")
    auth_mode: Literal["shared", "per_user"]
    auth_method: str = ""
    # The form fields THIS member supplies, secrets included. Empty when the
    # admin distributed everything.
    member_fields: list[str] = Field(default_factory=list)
    # The values document + compiled ask groups (see ``TeamConnectionRead``).
    # Authoritative over the flattened columns above, which older clients read.
    values_doc: list[dict[str, Any]] = Field(default_factory=list, title="Member Values Doc")
    ask_groups: list[list[str]] = Field(default_factory=list, title="Member Ask Groups")
    auto_add: bool = False
    enabled: bool = True
    has_shared_secret: bool = Field(
        default=False,
        description=(
            "Compatibility flag: true when any primary or named shared credential is stored."
        ),
    )
    has_primary_secret: bool = Field(
        default=False,
        description="True when the shared credential bundle includes a primary value.",
    )
    credential_version: int = 0
    # How a member obtains shared credentials. Current backends lease every
    # primary and named role in memory; empty remains the older fetch-to-file path.
    shared_custody: Literal["", "lease"] = ""
    # Per-user OAuth: whether the app config is complete server-side (the client
    # id may be public; the secret never crosses).
    oauth_client_id: str | None = None
    #: The derived status, from THIS member's vantage: on a per-user row the
    #: credential axis is the member's own grant, so an admin whose connection
    #: is healthy and a member who must sign in again see different badges on
    #: the same row — which is the whole point of the distinction.
    badge: Badge = Badge.not_checked
    badge_reason: str = ""
    outcome: Outcome | None = None
    last_verified_at: datetime | None = None
    credential_state: CredentialState = CredentialState.present
    reauth: Reauth | None = None
    #: Why leasing this connection's credential for the person this set was
    #: answered for is refused, as one sentence; empty when a lease would be
    #: handed out. The box serving their chat or workspace meets exactly this,
    #: so a picker never offers what the box then refuses.
    lease_refusal: str = ""
    updated_at: datetime


class MemberTeamConnectionsResponse(BaseModel):
    connections: list[MemberTeamConnection] = Field(default_factory=list)


class TeamConnectionCredentialResponse(BaseModel):
    """The decrypted shared credential bundle for an entitled member.

    Each value is written to its own chmod-600 file locally. Every fetch is
    audited; ``credential_version`` keys the bundle's local copy.
    """

    secret: str = Field(
        description="Primary shared credential; empty when the bundle contains only named values."
    )
    named_secrets: dict[str, str] = Field(
        default_factory=dict,
        title="Fetched Named Secrets",
    )
    credential_version: int


class TeamConnectionCredentialLease(BaseModel):
    """A short-lived lease of a shared credential bundle.

    The daemon holds it in memory only and re-leases on expiry; the backend
    refuses the next lease when the connection is disabled, flipped off shared
    mode, or deleted. Every lease is audited.
    """

    secret: str = Field(
        description="Primary shared credential; empty when the bundle contains only named values."
    )
    named_secrets: dict[str, str] = Field(
        default_factory=dict,
        title="Leased Named Secrets",
    )
    credential_version: int
    expires_at: datetime


class VerificationStarted(BaseModel):
    """A verification was accepted — poll it by id for the verdict."""

    id: UUID


class VerificationEndpointOutcome(BaseModel):
    """One endpoint's verdict within a compound connection verification."""

    outcome: Outcome
    detail: str = ""
    service_identity: str = ""


class VerificationRead(BaseModel):
    """One verification, exactly as the record holds it.

    The dialog reads the whole record, not a summary of it, because the three
    things it must tell apart — the server has not handed this to a worker yet,
    a worker has it and is dialling, nobody ever took it — are three different
    fields and no single word can carry them. ``dispatch_attempts`` and
    ``last_dispatch_error`` are the backend's own account of the hand-off and
    never contain driver text or a credential; ``detail`` is the connector's
    sentence about the connection itself.
    """

    id: UUID
    #: None on a draft: the payload has no row behind it yet.
    connection_id: UUID | None = None
    state: VerificationState
    outcome: Outcome | None = None
    detail: str = ""
    endpoint_results: dict[str, VerificationEndpointOutcome] = Field(default_factory=dict)
    requested_at: datetime
    dispatched_at: datetime | None = None
    dispatch_attempts: int = 0
    last_dispatch_error: str = ""
    started_at: datetime | None = None
    finished_at: datetime | None = None


class ConnectorFormDescriptor(BaseModel):
    """One connector's portal-facing form surface, served from the registered
    connector descriptor catalog."""

    name: str
    title: str
    # The full ConnectionFormSchema, serialized. The portal renders the SAME
    # generic form the workspace uses; team_capable/local_capable flags ride
    # inside the auth methods.
    form: dict[str, Any]
    team_capable_methods: list[str] = Field(default_factory=list)
    # The compiled completeness rule per team-capable auth method (the implicit
    # method keys the empty string): groups of input names of which at least
    # one must be answered. The portal's gate checks these structurally.
    ask_groups: dict[str, list[list[str]]] = Field(default_factory=dict)


class ConnectionFormsResponse(BaseModel):
    connectors: list[ConnectorFormDescriptor] = Field(default_factory=list)
    checked_before_save: bool = False
    """Whether a save must name a settled check of the same payload (start one
    at ``.../connections/verifications``). False where the deployment checks
    nothing, and the form saves directly."""


class OAuthRelaySpec(BaseModel):
    """The PUBLIC half of a confidential per-user OAuth spec — everything the
    member's daemon needs to run the browser + loopback locally. The client
    secret NEVER appears here; the exchange happens on the relay."""

    provider: str
    authorize_url: str
    token_url: str
    client_id: str
    scope: str = ""
    extra_authorize_params: dict[str, str] = Field(default_factory=dict)
    confidential: bool = True
    # Loopback binding the daemon must reproduce (all public): a provider whose
    # registered client demands a fixed redirect (databricks-cli → :8020) or a
    # specific path needs these; ephemeral providers leave them at the defaults.
    loopback_host: str = "127.0.0.1"
    loopback_port: int = 0
    loopback_path: str = ""


class OAuthExchangeRequest(BaseModel):
    """The daemon hands over the code it caught on its loopback plus its own
    PKCE verifier; the relay adds the client secret and exchanges."""

    code: str
    redirect_uri: str
    code_verifier: str


class OAuthTokenLease(BaseModel):
    """A short-lived access token for the member's daemon. The refresh token
    stays on the backend — leasing again is how you 'refresh'."""

    access_token: str
    expires_at: datetime | None = None
    scope: str = ""
