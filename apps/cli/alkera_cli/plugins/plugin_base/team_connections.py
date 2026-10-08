"""The member-side cache and state for team connections.

Cloud sync writes only this store, never a member's ``connections.json``.
Member state keys off the server row id, so deleting and recreating a
connection starts with clean local state.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

import structlog
from alkera_core.atomic_io import write_json_atomic
from alkera_core.connections import (
    Badge,
    Blocker,
    CredentialState,
    Outcome,
    StatusInputs,
    derive_badge,
)
from alkera_core.connectors.connection_form import FormField, ValueSlot
from alkera_core.naming import is_safe_handle
from alkera_core.versioning import VersionedModel
from pydantic import Field, field_validator

if TYPE_CHECKING:
    from alkera_core.connectors.connection import (
        Connection as BuiltConnection,
    )
    from alkera_core.connectors.connection import (
        ConnectionBuildResult,
    )

# Every ``team_lease`` reference minted here resolves through the scheme the lease
# client registers when it loads.
import alkera_cli.cloud_sync.shared_lease  # noqa: F401
from alkera_cli.contracts.tool_types import (
    CredentialMode,
    CredentialRef,
    Environment,
)
from alkera_cli.plugins.plugin_base.connection import Connection
from alkera_cli.plugins.plugin_base.connection_state import ConnectionStateRecord
from alkera_cli.plugins.plugin_base.tool import TEAM_RECORD_HANDLE, TEAM_RECORD_ID

logger = structlog.get_logger(__name__)

TEAM_CONNECTIONS_FILE = "team-connections.json"


def _migrate_record_v1_0_0_to_v2_0_0(doc: dict[str, Any]) -> dict[str, Any]:
    """1.0.0 → 2.0.0: drop the removed ``max_effect`` per-connection effect ceiling. Stamps
    the new version so the ladder advances (the loader keys off ``schema_version``)."""
    doc.pop("max_effect", None)
    doc["schema_version"] = "2.0.0"
    return doc


def _migrate_record_to_v3_0_0(doc: dict[str, Any]) -> dict[str, Any]:
    """2.x → 3.0.0: a record used to carry a connection the SERVER had built —
    ``attributes`` plus the ``dialect``, ``urn_namespace`` and ``environment``
    read off it. It now carries the admin's raw form input, which this machine
    builds itself.

    A built map cannot be turned back into the inputs that produced it, so the
    old one is dropped rather than guessed at. Every field here is the server's
    to state, and the next sync pass rewrites the record whole; until it lands,
    the connection simply doesn't materialize."""
    for built in ("attributes", "dialect", "urn_namespace", "environment"):
        doc.pop(built, None)
    doc["shared_values"] = {}
    doc["schema_version"] = "3.0.0"
    return doc


def _migrate_record_v3_0_0_to_v3_1_0(doc: dict[str, Any]) -> dict[str, Any]:
    """Mark historical shared bundles as primary because named custody did not exist."""
    doc["has_primary_secret"] = bool(doc.get("has_shared_secret"))
    doc["schema_version"] = "3.1.0"
    return doc


def _migrate_record_v3_1_0_to_v3_2_0(doc: dict[str, Any]) -> dict[str, Any]:
    """Mark named-role presence unknown to the old writer as safely absent."""
    doc["shared_named_credential_roles"] = []
    doc["schema_version"] = "3.2.0"
    return doc


class TeamConnectionRecord(VersionedModel):
    """One preconfigured connection as the backend defines it — the member
    view (no secret material; presence fields and ``credential_version``
    describe the credential bundle instead)."""

    # 2.0.0: removed ``max_effect`` (the per-connection effect ceiling). The migration drops
    # the field from a 1.0.0 document; extra="allow" keeps it loadable regardless.
    # 2.1.0: added ``member_fields`` + ``shared_custody`` (additive, defaulted — an older
    # document reads as no member-supplied fields and legacy fetch-to-file custody).
    # 3.0.0: ``attributes`` (a server-built connection) became ``shared_values`` (the
    # admin's raw form input), and ``dialect`` / ``urn_namespace`` / ``environment`` went
    # with it — this machine builds them from the merged values.
    # 3.1.0: added ``has_primary_secret`` so named-only bundles remain distinct from a
    # compound bundle whose primary file is missing.
    # 3.2.0: added exact shared named-role presence so lease refs cannot name absent values.
    # 3.3.0: added the server's settled verification facts — ``last_outcome``,
    # ``last_verified_at``, ``badge`` and ``credential_state`` — which ``status``
    # used to flatten into one of three words. Additive and defaulted: an older
    # document reads as a row nothing has checked yet.
    # 3.4.0: added ``owner_user_id`` + ``created_by_name``. A row can now belong to
    # one person rather than to a team, and it syncs by exactly the same path;
    # additive and defaulted, so an older document reads as a team's row added by
    # nobody in particular, which is what every row before this was.
    SCHEMA_VERSION: ClassVar[str] = "3.4.0"
    MIGRATIONS: ClassVar[dict[str, Callable[[dict[str, Any]], dict[str, Any]]]] = {
        "1.0.0": _migrate_record_v1_0_0_to_v2_0_0,
        "2.0.0": _migrate_record_to_v3_0_0,
        "2.1.0": _migrate_record_to_v3_0_0,
        "3.0.0": _migrate_record_v3_0_0_to_v3_1_0,
        "3.1.0": _migrate_record_v3_1_0_to_v3_2_0,
    }

    id: str
    team_id: str = ""
    team_name: str = ""
    owner_user_id: str = ""
    """Set when this row is ONE person's own connection rather than a team's.
    Everything else about it — the sync, the lease, the build — is identical; the
    owner is who it belongs to, not how it works."""
    created_by_name: str = ""
    """Who added it, as the server prints them (their display name, else their
    email). Carried so a surface can say so without a second lookup."""
    plugin: str = ""
    handle: str = ""
    shared_values: dict[str, str] = Field(default_factory=dict)
    """The admin's raw form values for the inputs they distributed. Merged with
    this member's own answers and handed to the connector's build, once."""
    auth_mode: str = "shared"  # "shared" | "per_user"
    auth_method: str = ""
    member_fields: list[str] = Field(default_factory=list)
    """The form fields THIS member fills in locally — everything the admin held
    back, secrets included, and the deployment tier when they left that open."""
    auto_add: bool = False
    enabled: bool = True
    has_shared_secret: bool = False
    has_primary_secret: bool | None = None
    """Whether the bundle expects a primary credential; ``None`` preserves the
    primary-only interpretation of synthetic records that omit the new field."""
    shared_named_credential_roles: list[str] = Field(default_factory=list)
    """Exact named roles stored by the backend; values never ride this record."""
    credential_version: int = 0
    shared_custody: str = ""
    """How this member obtains shared credentials: ``lease`` for in-memory
    primary and named roles, or empty for older fetch-to-file backends."""
    status: str = Field(default="unverified", deprecated=True)
    """The pre-vocabulary verdict ("unverified" | "ok" | "error"). Superseded by
    ``last_outcome`` + ``badge``; kept for one release so a member running an
    older client against a newer server still reads something."""
    last_outcome: str = ""
    """The server's last SETTLED outcome for the shared credential, in the
    connection vocabulary (``ok``, ``invalid_credential``, ``permission``, …).
    Empty when nothing has settled."""
    last_verified_at: str = ""
    """When that outcome settled, ISO-8601. A string because it is carried, not
    computed with, on this side."""
    badge: str = ""
    """The badge the SERVER derived for the shared row. A member's own state can
    still outrank it (their session is dead, their machine cannot build the
    connection), which is what ``member_badge`` decides."""
    credential_state: str = "present"
    """The shared credential's axis: ``present``, or ``unreadable`` when the
    backend cannot open its own ciphertext — a row nobody can fix but an admin."""
    updated_at: datetime | None = None

    @field_validator(
        "owner_user_id",
        "created_by_name",
        "last_outcome",
        "last_verified_at",
        "badge",
        mode="before",
    )
    @classmethod
    def _absent_reads_as_empty(cls, value: object) -> object:
        """A JSON ``null`` is how the server spells "this row has no owner",
        "nobody is named as having added it" and "nothing has been checked here":
        every one of those columns is nullable, and the member view serializes
        them straight. Read it as the same absence an older document expresses by
        omitting the key, so any surface can build a record from a row off the
        wire without knowing to patch it first."""
        return "" if value is None else value

    @property
    def expects_primary_secret(self) -> bool:
        """Whether the shared bundle declares a primary role."""
        return self.has_shared_secret and self.has_primary_secret is not False


class TeamMemberState(VersionedModel):
    """This member's local disposition for one server record."""

    # 1.1.0: added ``muted``. 1.2.0: added ``oauth_custody`` + ``needs_reauth``.
    # 1.3.0: added ``member_attributes`` + ``has_member_secret`` (all additive,
    # defaulted — an older document reads as unmuted, no custody, not-needing-reauth,
    # with nothing member-supplied).
    SCHEMA_VERSION: ClassVar[str] = "1.3.0"

    added: bool = False
    """Live in this workspace (auto-added or member-accepted)."""
    dismissed: bool = False
    """Tombstone: never suggest or auto-add THIS server id again. A new server
    id (admin recreated the connection) starts fresh."""
    muted: bool = False
    """Member-local mute: stays configured (credential kept, still listed) but
    hidden from the agent's tool surface — the same semantics as muting a
    local connection, held here because sync must never write the member's
    own ``connections.json``."""
    local_handle: str = ""
    """The handle it materializes under locally — differs from the record's
    handle when a member-own ``(plugin, handle)`` already existed (never
    clobber; the team copy gets a ``-team`` suffix)."""
    fetched_credential_version: int = -1
    """``credential_version`` of the shared credential bundle on disk
    (-1 = no credential files). Trails the record's version until a fetch lands."""
    authorized: bool = False
    """Per-user connections only: this member completed their sign-in."""
    oauth_custody: str = ""
    """Per-user OAuth custody after authorize: ``local`` (public client — the
    token bundle lives on this machine) | ``relay`` (confidential — the backend
    holds the refresh token; the connector leases access tokens by record id).
    Empty until the member authorizes."""
    needs_reauth: bool = False
    """Set when a token refresh / relay lease reported the session is dead — the
    UI shows "Sign in again". Cleared by a successful re-authorize."""
    member_attributes: dict[str, str] = Field(default_factory=dict)
    """The member's own non-secret values for the record's ``member_fields``
    (their warehouse username). Overlaid on the admin's attributes at build
    time, so the shape stays the admin's and the identity stays theirs."""
    has_member_secret: bool = False
    """Whether this member's own secret for a per-user credential row sits in
    their team credential dir. Distinct from ``oauth_custody``, which tracks
    where a browser sign-in's TOKEN lives."""


class TeamConnectionsState(VersionedModel):
    """The whole store document: server records + this member's state."""

    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    records: dict[str, TeamConnectionRecord] = Field(default_factory=dict)
    member: dict[str, TeamMemberState] = Field(default_factory=dict)


class TeamConnectionsStore:
    """Atomic read/write of ``.alkera/team-connections.json``. A missing or
    corrupt file reads as the empty state (sync re-converges from the server;
    nothing member-authored lives ONLY here except dismissals, which are
    cheap to redo compared to serving a torn document).

    Each ``save`` is atomic, but a load→mutate→save is not — writers hold
    :meth:`locked` around the whole transaction so a member's dismiss landing
    between another writer's load and save is never reverted (and its deleted
    credential never rewritten). Plain reads stay lock-free.
    """

    def __init__(self, path: Path) -> None:
        self._path = Path(path)

    @property
    def path(self) -> Path:
        return self._path

    @contextmanager
    def locked(self) -> Iterator[None]:
        """Hold the store's cross-process write lock. Critical sections are
        microsecond-scale, so contention spins out via ``retrying_lock`` rather
        than failing fast; a live 5-second holder still surfaces as
        ``LockHeldError``. Never hold this across an await — the lock guards
        the synchronous load→mutate→save only."""
        from alkera_core.project.locking import FileLock, retrying_lock

        lock = FileLock(self._path.with_name(self._path.name + ".lock"))
        with retrying_lock(lock):
            yield

    def load(self) -> TeamConnectionsState:
        try:
            raw = self._path.read_text()
        except FileNotFoundError:
            return TeamConnectionsState()
        try:
            return TeamConnectionsState.model_validate_json(raw)
        except ValueError:
            return TeamConnectionsState()

    def save(self, state: TeamConnectionsState) -> None:
        write_json_atomic(self._path, state.model_dump(mode="json"))


def team_credential_dir(plugins_root: Path, plugin: str, local_handle: str) -> Path | None:
    """Where a team connection's credentials live, or ``None`` when the
    server-supplied ``plugin`` / ``local_handle`` is not safe as a path component
    or the directory it names escapes ``plugins_root``.

    Parallel to but ISOLATED from the member's own ``connections/<handle>/`` dirs,
    so reconcile removals can never touch a member-owned secret.

    Fails closed, and returns an option so every caller has to say what it does
    when the answer is no. Both halves arrive from the SERVER: a handle of
    ``/Users/victim/Documents`` collapses the whole join to that absolute path
    (pathlib drops the left side) and ``..`` walks out of the project, which would
    point mkdir / rmtree / rename / secret-write at an attacker-chosen directory on
    every member's workstation. The sync lane refuses such a record before storing
    it, but a record an older client already persisted reaches these sinks anyway."""
    if is_safe_handle(plugin) and is_safe_handle(local_handle):
        directory = Path(plugins_root) / plugin / "team-connections" / local_handle
        try:
            if directory.resolve().is_relative_to(Path(plugins_root).resolve()):
                return directory
        except (OSError, ValueError):
            pass
    logger.warning(
        "team_connections.unsafe_credential_dir", plugin=plugin, local_handle=local_handle
    )
    return None


def lease_locator(record: TeamConnectionRecord, role: str = "primary") -> str:
    """Return a role-qualified locator for one bundle generation."""
    if not is_safe_handle(role):
        raise ValueError(f"unsafe credential role {role!r}")
    return f"{record.id}@{record.credential_version}#{role}"


def _team_specs(record: TeamConnectionRecord) -> list[FormField]:
    """The whole team form for this record's connector, or nothing when this
    client's catalog doesn't know the plugin.

    The shared catalog is the only source, deliberately. A plugin injected
    through the registry's ``extra_plugins`` seam is invisible here and its rows
    read as unsupported, which cannot strand a real member: the server refuses
    to save a team connection for a plugin outside the catalog, so no such row
    reaches anyone. Preconfiguring a connector therefore means adding it to the
    catalog, not registering it at runtime."""
    from alkera_core.connectors.catalog import get_descriptor, team_form_fields

    try:
        schema = get_descriptor(record.plugin).form_schema()
    except KeyError:
        return []
    return team_form_fields(schema, record.auth_method)


def member_field_specs(record: TeamConnectionRecord) -> list[FormField]:
    """The inputs THIS member must fill, resolved from the shared connector
    catalog in form order. One resolver so the webview, the TUI and the accept
    flow render and validate the same inputs — no surface keeps its own catalog
    lookup."""
    if not record.member_fields:
        return []
    by_name = {f.name: f for f in _team_specs(record)}
    return [by_name[name] for name in record.member_fields if name in by_name]


def _shared_deferred_fields(
    record: TeamConnectionRecord, specs: list[FormField], member_names: set[str]
) -> set[str]:
    """Return admin-owned secret fields backed by an explicitly stored role."""
    if not record.has_shared_secret or not specs:
        return set()
    from alkera_core.connectors.catalog import (
        admin_answered_secret_fields,
        credential_custody,
        get_descriptor,
    )

    stored_roles = set(record.shared_named_credential_roles)
    if record.expects_primary_secret:
        stored_roles.add("primary")
    custody = credential_custody(
        get_descriptor(record.plugin),
        record.auth_method,
        [field.name for field in specs if field.name not in member_names],
        stored_roles,
    )
    return admin_answered_secret_fields(specs, stored_roles, custody)


def connection_doc(record: TeamConnectionRecord, member: TeamMemberState) -> list[ValueSlot]:
    """The one values document for this connection on THIS machine: every form
    input as a slot carrying who answers it and the answer held here — the
    admin's distributed value or this member's own. A secret never rides; a
    stored shared secret or this member's credential file answers its slot as
    ``deferred``. Every surface reads this document; none re-merges the halves."""
    from alkera_core.connectors.catalog import values_doc

    specs = _team_specs(record)
    member_names = set(record.member_fields)
    values = {
        **record.shared_values,
        **{k: v for k, v in member.member_attributes.items() if k in member_names},
    }
    deferred = _shared_deferred_fields(record, specs, member_names)
    if member.has_member_secret:
        deferred |= {f.name for f in specs if f.secret and f.name in member_names}
    return values_doc(specs, values=values, member_names=member_names, deferred=deferred)


def admin_prefills(record: TeamConnectionRecord, member: TeamMemberState) -> list[tuple[str, str]]:
    """The admin's answered values, labeled, in form order — what a member reads
    before handing over their own login. A member asked for a warehouse password
    without being shown which warehouse cannot tell whether they are answering
    the right connection at all. Secrets never appear; nothing here is editable,
    which is what makes it a summary rather than a second copy of the form."""
    return [
        (slot.label, slot.value)
        for slot in connection_doc(record, member)
        if slot.owner == "admin" and not slot.secret and slot.value.strip()
    ]


def record_ask_groups(record: TeamConnectionRecord) -> list[list[str]]:
    """The compiled completeness rule for this record's whole form: groups of
    input names of which at least one must be answered."""
    from alkera_core.connectors.catalog import ask_groups

    return ask_groups(_team_specs(record))


def open_member_groups(record: TeamConnectionRecord, member: TeamMemberState) -> list[list[str]]:
    """The ask groups assigned to THIS member that no answer closes yet.

    A group is the member's when its leading required input is theirs to
    answer; it closes on any non-blank value in the document — the admin's
    declared alternative included — or a deferred slot (their secret file)."""
    from alkera_core.connectors.catalog import ask_groups

    doc = connection_doc(record, member)
    answered = {s.name for s in doc if s.value.strip() or s.deferred}
    return [
        group
        for group in ask_groups(member_field_specs(record))
        if not any(name in answered for name in group)
    ]


BUILD_UNSUPPORTED = "unsupported"
"""This client's connector catalog has no such plugin — an Alkera older than the
one the admin set the connection up with."""

BUILD_INCOMPLETE = "incomplete"
"""The values on hand don't make a connection — the shared half is missing or
malformed, which is what a member sees while the record still holds what an
older writer stored."""

BUILD_REFUSED = "refused"
"""The values build, but into a connection that would reach the machine opening
it (a local file, a loopback or metadata address) rather than a server. The
server refuses to save one; a record that carries one anyway (saved before that
check, or answered by a member) never materializes on any machine."""


def _assemble_result(
    record: TeamConnectionRecord, member: TeamMemberState
) -> tuple[ConnectionBuildResult | None, str]:
    """The connector's build over the admin's values plus this member's own, and
    why it failed when it did."""
    from alkera_core.connectors.catalog import build_result, distribution_refusal, get_descriptor

    try:
        descriptor = get_descriptor(record.plugin)
    except KeyError:
        return None, BUILD_UNSUPPORTED
    try:
        result = build_result(
            descriptor,
            member.local_handle or record.handle,
            record.auth_method,
            merged_values(record, member),
        )
    except ValueError:
        return None, BUILD_INCOMPLETE
    if distribution_refusal(descriptor, result.connection.attributes) is not None:
        return None, BUILD_REFUSED
    return result, ""


def assemble(
    record: TeamConnectionRecord, member: TeamMemberState
) -> tuple[BuiltConnection | None, str]:
    """Build the connection shape used by team-row status and display surfaces."""
    result, blocker = _assemble_result(record, member)
    return (result.connection if result is not None else None), blocker


def build_blocker(record: TeamConnectionRecord, member: TeamMemberState) -> str:
    """Why this machine cannot assemble the record into a connection, or ``""``
    when it can.

    The admin's browser built this connection somewhere else, against its own
    catalog and its own values, so no flag either side stores answers whether it
    builds HERE. Every surface that claims a team row works has to attempt it:
    without that, a row reads Connected off the admin's server-side probe while
    nothing on this machine can reach the warehouse."""
    return assemble(record, member)[1]


def build_refusal(blocker: str) -> str:
    """What to tell a member whose connection won't assemble, as a fragment the
    caller's own sentence carries. One wording, because a member who meets this
    on Add, on Test connection and on the badge is meeting one problem."""
    if blocker == BUILD_UNSUPPORTED:
        return "this connection needs a newer version. Update and try again"
    if blocker == BUILD_REFUSED:
        return (
            "the preconfigured connection points at a local file or at the machine "
            "itself rather than a database server, so ask an admin to set it up again"
        )
    return (
        "the preconfigured details don't make a working connection, so "
        "ask an admin to set it up again"
    )


def member_completion(record: TeamConnectionRecord) -> str:
    """What this member must still do before the row connects: ``oauth`` to sign
    in, ``credentials`` to answer the fields the admin held back, or ``""`` when
    the admin left them nothing.

    Keyed off the member half, never off who holds the credential. An admin who
    distributes the password but holds back a database name leaves a SHARED row
    with a member half, and a surface reading ``auth_mode`` here would offer no
    form for it and strand the member on a row they can never add."""
    from alkera_core.connectors.catalog import get_descriptor, is_oauth_method

    try:
        schema = get_descriptor(record.plugin).form_schema()
    except KeyError:
        # No descriptor to ask, so read the record's own shape instead: a
        # per-user row that owes no values got that way through a sign-in.
        if record.member_fields:
            return "credentials"
        return "oauth" if record.auth_mode == "per_user" else ""
    if is_oauth_method(schema, record.auth_method):
        return "oauth"
    return "credentials" if record.member_fields else ""


def credential_satisfied(record: TeamConnectionRecord, member: TeamMemberState) -> bool:
    """Whether the connection has everything it needs to actually connect.

    A shared record under lease custody needs nothing locally. An older
    fetch-custody bundle needs its complete generation on disk. A passwordless
    shared record needs nothing; a per-user
    record needs this member's own sign-in. An added-but-unsatisfied connection
    stays off the live surface until the next lane run or authorization.

    Signing in once is not enough on a row whose member half can GROW: an admin
    who withdraws a field from the shared half leaves every member holding a
    connection that silently lacks it. So the check reads the values document
    fresh each time: every ask group assigned to this member must hold an
    answer — a value in the document (the admin's declared alternative counts,
    because the document carries both halves) or a deferred slot (this member's
    secret file stands in for the value that never rides). An open group drops
    the row back to the suggested lane, which is where the form to close it
    lives. A member-supplied value past its declared bound reads as no answer
    at all — the connector's own build would refuse it."""
    from alkera_core.connectors.catalog import overlong_values

    if record.auth_mode == "per_user" and not member.authorized:
        return False
    if open_member_groups(record, member):
        return False
    doc = connection_doc(record, member)
    member_values = {s.name: s.value for s in doc if s.owner == "member"}
    if overlong_values(member_field_specs(record), member_values):
        return False
    if record.auth_mode == "per_user":
        return True
    if record.has_shared_secret and record.shared_custody != "lease":
        return member.fetched_credential_version >= 0
    return True


def is_held(record: TeamConnectionRecord, member: TeamMemberState) -> bool:
    """Whether this member still holds the record: offered by the team, accepted
    here, not dismissed, not muted, credential in hand.

    Ownership, not workability — the health sweep asks this one, because "is
    this still the row I started on" survives an admin-side change that makes
    the row stop building, and a sweep that conflated the two would delete a
    member's token bundle over it."""
    return (
        record.enabled
        and member.added
        and not member.dismissed
        and not member.muted
        and credential_satisfied(record, member)
    )


def is_live(record: TeamConnectionRecord, member: TeamMemberState) -> bool:
    """Whether this record materializes as a LIVE connection right now.

    The assembly attempt is what settles it. A held row whose values no longer
    build is not live however healthy its stored flags look, because the thing a
    caller would go on to use cannot be made."""
    return is_held(record, member) and not build_blocker(record, member)


def is_configured(record: TeamConnectionRecord, member: TeamMemberState) -> bool:
    """Whether this record belongs in the member's CONNECTIONS LIST — accepted
    and workable: live, or muted-but-otherwise-live (listed so it can be
    unmuted). A credential-pending row is NOT configured yet — it stays in the
    suggested lane, where the retry action lives."""
    return (
        record.enabled
        and member.added
        and not member.dismissed
        and credential_satisfied(record, member)
    )


def is_suggested(record: TeamConnectionRecord, member: TeamMemberState) -> bool:
    """Whether this record surfaces in the suggested lane (visible, not yet
    workable, not dismissed). A muted row is configured — it must never
    re-surface as a suggestion."""
    return (
        record.enabled
        and not member.dismissed
        and not member.muted
        and not is_configured(record, member)
    )


def member_badge(
    record: TeamConnectionRecord,
    member: TeamMemberState,
    state: ConnectionStateRecord | None = None,
    *,
    muted: bool = False,
    now: datetime | None = None,
) -> Badge:
    """The badge for a CONFIGURED team row, as THIS member experiences it.

    One derivation, three sources of fact, in the order that answers "what does
    this person have to do next?":

    A row that won't assemble here reads that way first, ahead of every stored
    verdict — signing in again does not give a connector to a client that lacks
    one, and the admin's probe passed on the admin's machine. Then this member's
    own session: a refused refresh is theirs to fix whatever the shared
    credential is doing. Then the outcome, preferring what THIS machine observed
    (a query or a Test that just failed here) over what the server settled, since
    the member is looking at the row because of what they just saw.
    """
    blocker = build_blocker(record, member)
    credential = CredentialState.present
    if member.needs_reauth:
        credential = CredentialState.needs_reauth
    elif record.credential_state == CredentialState.unreadable.value:
        credential = CredentialState.unreadable
    elif state is not None and state.credential_state == CredentialState.needs_reauth.value:
        credential = CredentialState.needs_reauth

    outcome: Outcome | None = None
    verified_at: datetime | None = None
    if state is not None and state.outcome:
        with suppress(ValueError):
            outcome = Outcome(state.outcome)
        verified_at = state.verified_at
    if outcome is None:
        outcome = _server_outcome(record, member)
        verified_at = _parse_moment(record.last_verified_at)
    if verified_at is not None and verified_at.tzinfo is None:
        verified_at = verified_at.replace(tzinfo=UTC)
    # A refused record is the admin's to fix, exactly like one that does not build.
    build: Blocker = (
        "unsupported"
        if blocker == BUILD_UNSUPPORTED
        else ("incomplete" if blocker in (BUILD_INCOMPLETE, BUILD_REFUSED) else "")
    )
    return derive_badge(
        StatusInputs(
            enabled=record.enabled,
            muted=muted,
            blocker=build,
            credential_state=credential,
            last_outcome=outcome,
            last_verified_at=verified_at,
        ),
        now=now or datetime.now(tz=UTC),
    )


def _server_outcome(record: TeamConnectionRecord, member: TeamMemberState) -> Outcome | None:
    """The settled outcome behind a team row when this machine observed none.

    A per-user row's SERVER status stays "unverified" forever — there is no
    shared credential a worker could probe — so the member's own sign-in is the
    verdict: they authorized, which means their own probe passed at authorize
    time. A shared row reads the server's outcome, from the new field or from the
    one word the old wire had for it.
    """
    if record.auth_mode == "per_user":
        return Outcome.ok if member.authorized else None
    if record.last_outcome:
        try:
            return Outcome(record.last_outcome)
        except ValueError:
            return None
    # Read out of the field store rather than off the attribute: ``status`` is
    # deprecated on purpose and this IS its one remaining reader (a server that
    # has not been taught the new fields yet), so the warning is noise a whole
    # test run would carry.
    legacy = str(record.__dict__.get("status", ""))
    if legacy == "ok":
        return Outcome.ok
    if legacy == "error":
        return Outcome.error
    return None


def _parse_moment(text: str) -> datetime | None:
    if not text:
        return None
    with suppress(ValueError):
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    return None


def merged_values(record: TeamConnectionRecord, member: TeamMemberState) -> dict[str, str]:
    """The complete non-secret form input for this connection on THIS machine,
    read off the values document — the shape the connector's build takes.

    The document already settled who answers what: a member value lands only on
    a slot the admin left them, so a value typed before the admin reclaimed the
    field never overrides what the admin now supplies."""
    return {
        s.name: s.value
        for s in connection_doc(record, member)
        if not s.secret and (s.owner == "admin" or s.value)
    }


def to_connection(
    record: TeamConnectionRecord, member: TeamMemberState, *, plugins_root: Path
) -> Connection | None:
    """Build the live ``Connection`` a team record materializes as, or ``None``
    when it cannot be built here.

    This is where the connector finally sees a whole connection: the admin's
    shared values plus this member's own, handed to the same ``build`` the
    admin's browser ran. That is what lets a connector rename its own inputs —
    an account URL becomes a host, one database URL becomes six attributes —
    and still land on the shape ``connect()`` expects, whichever half of the
    form each value came from.

    ``None`` means this machine cannot make a connection out of the record: an
    unknown connector, values that don't build, or a plugin/handle pair that
    names no credential directory this machine will write. Callers drop it rather
    than materialize something half-formed; the next sync brings a corrected
    record. Secrets are deliberately absent from the build; primary and named
    references resolve them only at the I/O boundary."""
    local_handle = member.local_handle or record.handle
    result, _ = _assemble_result(record, member)
    if result is None:
        return None
    built = result.connection
    # A whole-record refusal, not a per-branch one: a locator built from an unsafe
    # pair points the credential read at an attacker-chosen path, and the same pair
    # is this connection's identity everywhere else it travels.
    credential_dir = team_credential_dir(plugins_root, record.plugin, local_handle)
    if credential_dir is None:
        return None

    from alkera_core.connectors.connection import with_named_credential_refs

    named_refs = _named_credential_refs(record, result, credential_dir)
    credential_ref = _credential_ref_for(record, member, credential_dir)

    connection = Connection(
        handle=local_handle,
        plugin=record.plugin,
        dialect=built.dialect or record.plugin,
        environment=Environment(str(built.environment)),
        # The connector names the warehouse it actually reached, so two teams
        # publishing the same handle never collapse into one identity. The
        # fallback keys off the local handle (the one collision resolution
        # already made unique) for a connector that mints no namespace.
        urn_namespace=built.urn_namespace or f"{record.plugin}://{local_handle}",
        credential_ref=credential_ref,
        credential_mode=(
            CredentialMode.PER_USER if record.auth_mode == "per_user" else CredentialMode.SHARED
        ),
        enabled=True,
        attributes=dict(built.attributes),
    )
    connection._runtime_bindings[TEAM_RECORD_ID] = record.id
    connection._runtime_bindings[TEAM_RECORD_HANDLE] = record.handle
    return with_named_credential_refs(connection, named_refs)


def _primary_credential_file(credential_dir: Path) -> CredentialRef:
    """Return the primary role's file reference."""
    return CredentialRef(scheme="file", locator=str(credential_dir / "credential"))


def _per_user_primary_ref(
    record: TeamConnectionRecord,
    member: TeamMemberState,
    credential_dir: Path,
    *,
    member_primary: bool,
) -> CredentialRef | None:
    """Resolve member OAuth or primary-file custody without a shared fallback."""
    if member.has_member_secret and member_primary:
        return _primary_credential_file(credential_dir)
    if member.oauth_custody == "relay":
        return CredentialRef(scheme="oauth_relay", locator=record.id)
    if member.oauth_custody == "local":
        return CredentialRef(scheme="oauth", locator=str(credential_dir / "oauth.json"))
    return None


def _shared_primary_ref(record: TeamConnectionRecord, credential_dir: Path) -> CredentialRef | None:
    """Resolve an admin-owned primary role from its declared bundle custody."""
    if not record.expects_primary_secret:
        return None
    if record.shared_custody == "lease":
        return CredentialRef(scheme="team_lease", locator=lease_locator(record, "primary"))
    return _primary_credential_file(credential_dir)


def _credential_ref_for(
    record: TeamConnectionRecord,
    member: TeamMemberState,
    credential_dir: Path,
) -> CredentialRef | None:
    """Resolve the primary role at the I/O boundary from its field owner."""
    member_primary = any(
        spec.secret and not spec.credential_role for spec in member_field_specs(record)
    )
    if record.auth_mode == "per_user":
        return _per_user_primary_ref(record, member, credential_dir, member_primary=member_primary)
    if member_primary:
        return _primary_credential_file(credential_dir)
    return _shared_primary_ref(record, credential_dir)


def _named_credential_ref(
    record: TeamConnectionRecord,
    name: str,
    member_roles: set[str],
    credential_dir: Path,
) -> CredentialRef | None:
    """Resolve one named role only when its custody owner proves a secret exists."""
    file_owned = name in member_roles or record.shared_custody != "lease"
    if file_owned:
        path = credential_dir / "credentials" / name
        return CredentialRef(scheme="file", locator=str(path)) if path.is_file() else None
    if name not in record.shared_named_credential_roles:
        return None
    return CredentialRef(scheme="team_lease", locator=lease_locator(record, name))


def _named_credential_refs(
    record: TeamConnectionRecord,
    result: ConnectionBuildResult,
    credential_dir: Path,
) -> dict[str, CredentialRef]:
    """Build refs for every active named role under the row's custody mode."""
    refs: dict[str, CredentialRef] = {}
    member_roles = {
        spec.credential_role for spec in member_field_specs(record) if spec.credential_role
    }
    for name in result.named_credentials or {}:
        if not is_safe_handle(name):
            continue
        ref = _named_credential_ref(record, name, member_roles, credential_dir)
        if ref is not None:
            refs[name] = ref
    return refs


def pending_reason(record: TeamConnectionRecord, member: TeamMemberState) -> str | None:
    """Why this shared row is not on the live surface yet, or ``None`` when the
    row is live (or deliberately off: dismissed, muted, disabled by the admin).

    A synced-but-unaccepted row looks exactly like no row at all to every
    consumer of :func:`is_live`, which is how a member who has just been shared
    a connection gets an empty lineage graph with nothing to read. The two
    states a member can actually clear are separated here — accept it, or
    finish signing it in — because the action differs.
    """
    if not record.enabled or member.dismissed or member.muted:
        return None
    if not member.added:
        return "not accepted yet; accept it in Connections"
    if not credential_satisfied(record, member):
        return "not verified yet; finish signing it in under Connections"
    return None


def pending_hints(state: TeamConnectionsState) -> list[str]:
    """One line per shared connection a member still has to act on.

    The lineage seed reports a count, and a count of zero reads the same whether
    the graph is genuinely empty or every connection it would have read is
    waiting on an accept. These lines are what tells the two apart, so they are
    built from the store rather than from whatever the failing seed happened to
    raise.
    """
    hints: list[str] = []
    for record_id, record in sorted(state.records.items()):
        member = state.member.get(record_id) or TeamMemberState()
        reason = pending_reason(record, member)
        if reason is None:
            continue
        handle = (member.local_handle or record.handle) or record_id
        hints.append(f"connection {handle} is {reason}")
    return hints


__all__ = [
    "BUILD_INCOMPLETE",
    "BUILD_REFUSED",
    "BUILD_UNSUPPORTED",
    "TEAM_CONNECTIONS_FILE",
    "TeamConnectionRecord",
    "TeamConnectionsState",
    "TeamConnectionsStore",
    "TeamMemberState",
    "admin_prefills",
    "assemble",
    "build_blocker",
    "build_refusal",
    "connection_doc",
    "credential_satisfied",
    "is_configured",
    "is_held",
    "is_live",
    "is_suggested",
    "lease_locator",
    "member_badge",
    "member_field_specs",
    "merged_values",
    "open_member_groups",
    "pending_hints",
    "pending_reason",
    "record_ask_groups",
    "team_credential_dir",
    "to_connection",
]
