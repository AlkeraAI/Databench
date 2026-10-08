"""The backend client for the Preconfigured-Connections lane.

Same shape as the KB sync client (stored Bearer token, one request per call),
with one load-bearing rule baked into the return type: ``fetch_records``
returns ``None`` for ANYTHING that isn't an authoritative 200 — a 401, a 5xx,
an unreachable backend. ``None`` tells the reconcile planner "no answer", and
the planner mutates nothing. Only a real 200 (even an empty list) speaks for
membership.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx
import structlog

from alkera_cli.account.auth_file import ProfileResolutionError, org_headers
from alkera_cli.account.binding import profile_for_project, profile_for_sync
from alkera_cli.cloud_sync.lease_cache import _lease_expiry
from alkera_cli.host.http_error import response_error_detail
from alkera_cli.plugins.plugin_base.team_connections import TeamConnectionRecord

if TYPE_CHECKING:
    from alkera_core.project import ProjectDirectory

logger = structlog.get_logger(__name__)

_TIMEOUT_SECONDS = 30.0


def _values_from_slots(slots: list[Any]) -> tuple[dict[str, str], list[str]]:
    """Compile the server's values document to the stored columns: owner → who
    answers, value → what the admin distributed. A secret never rides a slot,
    and the compiled groups are re-derived locally from the same catalog, so
    the record never caches a stale compile."""
    entries = [s for s in slots if isinstance(s, dict) and str(s.get("name") or "")]
    shared = {
        str(s["name"]): str(s.get("value") or "")
        for s in entries
        if s.get("owner") != "member" and not s.get("secret")
    }
    member = [str(s["name"]) for s in entries if s.get("owner") == "member"]
    return shared, member


def _legacy_shared_values(raw: object) -> dict[str, str]:
    """An older backend without the values document serves the flattened
    columns directly."""
    return {str(k): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}


def _shared_named_roles_from_slots(plugin: str, auth_method: str, slots: list[Any]) -> list[str]:
    """Compile exact encrypted named-role presence from deferred admin secret slots."""
    from alkera_core.connectors.catalog import get_descriptor, team_form_fields

    try:
        specs = team_form_fields(get_descriptor(plugin).form_schema(), auth_method)
    except KeyError:
        return []
    roles = {spec.name: spec.credential_role for spec in specs if spec.credential_role}
    return sorted(
        {
            roles[str(slot.get("name") or "")]
            for slot in slots
            if isinstance(slot, dict)
            and slot.get("owner") != "member"
            and slot.get("secret")
            and slot.get("deferred")
            and str(slot.get("name") or "") in roles
        }
    )


def _record_from_wire(raw: dict[str, Any]) -> TeamConnectionRecord:
    """Map one backend member-view row onto the local record model — from the
    values document when it rides the row, else from the legacy flattened
    columns.

    The shared values are stringified defensively: the wire carries JSON, so a
    connector form that ever collects a number would arrive as one, and the
    record types them as strings. Letting that reach validation would raise, and
    the caller reads a raise as "the server gave no answer" — so one odd value
    in one row would quietly freeze this member's whole team-connection set,
    removals included, looking exactly like being offline."""
    doc = dict(raw)
    doc["id"] = str(doc.get("id", ""))
    doc["team_id"] = str(doc.get("team_id", ""))
    # A row with an owner is that person's own. It rides the same route and
    # reconciles the same way; carrying the owner keeps a surface from having to
    # ask the server which kind it is looking at.
    doc["owner_user_id"] = str(doc.get("owner_user_id") or "")
    doc["created_by_name"] = str(doc.get("created_by_name") or "")
    slots = doc.pop("values_doc", None)
    doc.pop("ask_groups", None)
    if isinstance(slots, list) and slots:
        doc["shared_values"], doc["member_fields"] = _values_from_slots(slots)
        doc["shared_named_credential_roles"] = _shared_named_roles_from_slots(
            str(doc.get("plugin") or ""), str(doc.get("auth_method") or ""), slots
        )
    else:
        doc["shared_values"] = _legacy_shared_values(doc.get("shared_values"))
    doc.pop("schema_version", None)  # the wire is not a persisted document
    _map_verification_fields(doc)
    return TeamConnectionRecord.model_validate(doc)


def _map_verification_fields(doc: dict[str, Any]) -> None:
    """Fold the server's verification facts onto the record, old wire or new.

    The backend is moving from one ``status`` word to the settled outcome plus
    the badge it derived. Both shapes are read, and neither is required: a member
    on a new client against an old server still gets a badge (derived from
    ``status``), and one on an old client against a new server still gets
    ``status``. A field the wire omits is left at its default rather than
    stamped, so a partial row never blanks what this member already knows.
    """

    outcome = doc.pop("outcome", None)
    if outcome is None:
        outcome = doc.get("last_outcome")
    if outcome is not None:
        doc["last_outcome"] = str(outcome or "")
    verified = doc.get("last_verified_at")
    doc["last_verified_at"] = str(verified) if verified else ""
    badge = doc.get("badge")
    if badge is not None:
        doc["badge"] = str(badge or "")
    credential_state = doc.get("credential_state")
    if credential_state:
        doc["credential_state"] = str(credential_state)
    else:
        doc.pop("credential_state", None)


class TeamConnectionsClient:
    """Fetches the member's team-connection set + shared credentials."""

    def __init__(
        self,
        *,
        api_url: str,
        token: str,
        org_id: str = "",
        transport: httpx.AsyncBaseTransport | None = None,
        auth: httpx.Auth | None = None,
    ) -> None:
        self._base = api_url.rstrip("/")
        self._token = token
        self._org_id = org_id
        self._transport = transport
        #: Signs each request when the bearer is replaced while the process
        #: runs (an org worker's): it outranks the headers built from ``token``.
        self._request_auth = auth

    @property
    def _auth(self) -> dict[str, str]:

        return org_headers(self._token, self._org_id)

    @property
    def identity_key(self) -> str:
        """A stable, non-reversible stand-in for WHOSE session this is, for
        callers that cache per-identity. A digest rather than the token so a
        cache key is never a credential."""
        return hashlib.sha256(self._token.encode()).hexdigest()[:16]

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=_TIMEOUT_SECONDS, transport=self._transport, auth=self._request_auth
        )

    async def fetch_records(self) -> list[TeamConnectionRecord] | None:
        """The member's visible team connections, or ``None`` when there is no
        authoritative answer (offline, auth failure, server error)."""
        try:
            async with self._client() as client:
                resp = await client.get(
                    f"{self._base}/api/v1/me/team-connections", headers=self._auth
                )
        except httpx.HTTPError:
            logger.warning("cloud_sync.connections.pull_unreachable", exc_info=True)
            return None
        if resp.status_code != 200:
            # A 401 is an AUTH answer, not a membership answer — treat it like
            # silence. Membership loss arrives as a 200 that omits the record.
            logger.warning("cloud_sync.connections.pull_failed", status=resp.status_code)
            return None
        try:
            rows = resp.json().get("connections") or []
            return [_record_from_wire(row) for row in rows if isinstance(row, dict)]
        except (ValueError, TypeError):
            logger.warning("cloud_sync.connections.pull_unparseable", exc_info=True)
            return None

    async def records_for_workspace(self, workspace_id: str) -> frozenset[str]:
        """The record ids a workspace's notebooks may use, on a box run on a
        person's login: the person's own set, which their door answers. The
        server reads a workspace's connections for a person on that door and
        never on the workspace's, so this is the set the box's sync
        materialized and its agent's tools query. Raises, for the caller to
        fail closed on, when the read gets no answer."""
        records = await self.fetch_records()
        if records is None:
            raise RuntimeError(f"the connections of workspace {workspace_id} could not be read")
        return frozenset(record.id for record in records)

    async def fetch_credential(self, record_id: str) -> tuple[str, int, dict[str, str]]:
        """Fetch one decrypted primary-and-named credential bundle.

        Raises on failure; the applier isolates each record and retries later.
        """
        async with self._client() as client:
            resp = await client.get(
                f"{self._base}/api/v1/me/team-connections/{record_id}/credential",
                headers=self._auth,
            )
        if resp.status_code != 200:
            raise RuntimeError(f"credential fetch returned HTTP {resp.status_code}")
        body = resp.json()
        named = body.get("named_secrets")
        named_secrets = (
            {str(name): str(secret) for name, secret in named.items()}
            if isinstance(named, dict)
            else {}
        )
        return str(body["secret"]), int(body.get("credential_version", 0)), named_secrets

    async def lease_credential_bundle(
        self, record_id: str, *, chat_id: str | None = None, workspace_id: str | None = None
    ) -> tuple[dict[str, str], float | None]:
        """A time-bounded lease of the complete shared credential bundle, plus the epoch second
        it stops being valid (``None`` when the server reports no expiry). ``chat_id``
        and ``workspace_id`` are ignored: a person's lease is decided for the person.

        The caller holds it in memory only. A 404 here is the whole point of
        leasing: the connection was disabled, deleted, or flipped to per-user,
        and this member stops being able to use it."""
        async with self._client() as client:
            resp = await client.post(
                f"{self._base}/api/v1/me/team-connections/{record_id}/credential-lease",
                headers=self._auth,
            )
        if resp.status_code == 404:
            raise RuntimeError("this preconfigured connection is no longer available to you")
        if resp.status_code != 200:
            raise RuntimeError(f"credential lease returned HTTP {resp.status_code}")
        body = resp.json()
        return _credential_bundle(body), _lease_expiry(body.get("expires_at"))

    async def lease_for_schema_read(self, record_id: str) -> tuple[str | None, dict[str, str]]:
        """The bundle a schema read of the connection runs under, and the chat
        it was leased for: a person's lease is for nobody but the person."""
        bundle, _expires = await self.lease_credential_bundle(record_id)
        return None, bundle


def _credential_bundle(body: dict[str, Any]) -> dict[str, str]:
    """Normalize one primary-and-named lease response into role-addressed values."""
    named = body.get("named_secrets")
    bundle = (
        {str(name): str(secret) for name, secret in named.items()}
        if isinstance(named, dict)
        else {}
    )
    primary = str(body.get("secret") or "")
    if primary:
        bundle["primary"] = primary
    return bundle


#: What a plugin is told when no chat this box holds may lease the connection's
#: credential: the box holds no chat, or every held chat's owner is refused it
#: (the opaque 404 — not theirs, disabled, per-user, no stored secret).
NO_HELD_CHAT_MAY_USE = "no chat on this machine may use this connection"

#: What the legacy file-custody fetch answers on a box: an older daemon writes
#: that credential to a file, and a shared machine never keeps one on disk.
FILE_CUSTODY_NOT_ON_MACHINE = "a shared machine never keeps a connection's credential on disk"

#: What a lease answers when no chat's door could be reached at all.
LEASE_UNREACHABLE = "the credential lease could not reach the server"

#: What a lease on a shared machine answers when no chat was named for it: a
#: credential there is leased for the chat whose tool call asked, never for
#: whichever held chat happens to be allowed.
LEASE_NEEDS_A_CHAT = "a shared machine leases a connection's credential only for a named chat"


class ChatConnectionsClient(TeamConnectionsClient):
    """The connections a box reads for the chats it holds, on its own machine
    credential.

    A machine is a member of no org, so the person's door refuses it; it reads
    ``GET /chats/{id}/connections`` once per chat it currently holds — the set
    each chat's OWNER may use, resolved on the server — and reconciles the
    union into the workspace store, the way a member's daemon reconciles their
    own list. A chat that answers 404 (left this box, deleted, never held)
    contributes nothing; a box holding no chat holds no connections, and says
    so authoritatively so the store empties. ``None`` — no answer — is kept for
    a pass in which no held chat answered at all.

    A credential reaches the box only as a lease through the same chat door:
    ``POST /chats/{id}/connections/{record}/credential-lease`` answers the
    bundle when that chat's owner may use the connection and this box holds
    the chat, and the opaque 404 otherwise. A tool call's lease asks only the
    chat it is for; the box's own schema read asks the held chats one at a
    time — first the ones whose last listing carried the record — and is told
    which chat answered. The caller holds a bundle in memory, nothing here
    writes it. The legacy file-custody fetch stays refused: a shared machine
    never keeps a connection's credential on disk.
    """

    def __init__(
        self,
        *,
        api_url: str,
        token: str,
        chats: Callable[[], Iterable[str]],
        workspaces: Callable[[], Iterable[str]] = tuple,
        transport: httpx.AsyncBaseTransport | None = None,
        token_source: Callable[[], str] | None = None,
        auth: httpx.Auth | None = None,
    ) -> None:
        super().__init__(api_url=api_url, token=token, transport=transport, auth=auth)
        #: Where the box's current bearer is read at each request, when it is
        #: replaced while the process runs (an org worker's is, every few
        #: minutes); ``token`` alone otherwise.
        self._token_source = token_source
        self._chats = chats
        #: The workspaces this box holds (their sandboxes run notebook
        #: kernels); their owners' connections join the store even when no
        #: chat of theirs is on the box.
        self._workspaces = workspaces
        #: Which record ids each held chat listed on its last read, so a lease
        #: asks the chat most likely to answer first. Replaced by every pull,
        #: kept in memory only.
        self._listed: dict[str, frozenset[str]] = {}

    @property
    def _auth(self) -> dict[str, str]:
        token = self._token_source() if self._token_source is not None else self._token
        return org_headers(token or self._token, self._org_id)

    async def _chat_records(
        self, client: httpx.AsyncClient, chat_id: str
    ) -> list[TeamConnectionRecord] | None:
        """One chat's answer: its rows, ``[]`` for the opaque 404 (the box does
        not hold it — another box's, unbound, deleted), ``None`` for no answer
        (unreachable, refused otherwise, unparseable)."""
        try:
            resp = await client.get(
                f"{self._base}/api/v1/chats/{chat_id}/connections", headers=self._auth
            )
        except httpx.HTTPError:
            logger.warning("cloud_sync.connections.pull_unreachable", chat_id=chat_id)
            return None
        if resp.status_code == 404:
            return []
        if resp.status_code != 200:
            logger.warning(
                "cloud_sync.connections.pull_failed", chat_id=chat_id, status=resp.status_code
            )
            return None
        try:
            rows = resp.json().get("connections") or []
            return [_record_from_wire(row) for row in rows if isinstance(row, dict)]
        except (ValueError, TypeError):
            logger.warning("cloud_sync.connections.pull_unparseable", chat_id=chat_id)
            return None

    async def fetch_records(self) -> list[TeamConnectionRecord] | None:
        chat_ids = [str(c) for c in self._chats()]
        workspace_ids = [str(w) for w in self._workspaces()]
        if not chat_ids and not workspace_ids:
            self._listed = {}
            return []
        records: dict[str, TeamConnectionRecord] = {}
        listed: dict[str, frozenset[str]] = {}
        answered = False
        async with self._client() as client:
            for chat_id in chat_ids:
                parsed = await self._chat_records(client, chat_id)
                if parsed is None:
                    continue
                answered = True
                listed[chat_id] = frozenset(record.id for record in parsed)
                for record in parsed:
                    records.setdefault(record.id, record)
            for workspace_id in workspace_ids:
                rows = await self._listing(
                    client, f"{self._base}/api/v1/workspaces/{workspace_id}/connections"
                )
                if rows is None:
                    continue
                answered = True
                for record in rows:
                    records.setdefault(record.id, record)
        if not answered:
            return None
        self._listed = listed
        return list(records.values())

    async def _listing(
        self, client: httpx.AsyncClient, url: str
    ) -> list[TeamConnectionRecord] | None:
        """One door's rows, ``[]`` for its opaque 404, ``None`` for no answer."""
        try:
            resp = await client.get(url, headers=self._auth)
        except httpx.HTTPError:
            logger.warning("cloud_sync.connections.pull_unreachable", url=url)
            return None
        if resp.status_code == 404:
            return []
        if resp.status_code != 200:
            logger.warning("cloud_sync.connections.pull_failed", url=url, status=resp.status_code)
            return None
        try:
            rows = resp.json().get("connections") or []
            return [_record_from_wire(row) for row in rows if isinstance(row, dict)]
        except (ValueError, TypeError, AttributeError):
            logger.warning("cloud_sync.connections.pull_unparseable", url=url)
            return None

    async def records_for_chat(self, chat_id: str) -> frozenset[str]:
        """The record ids the server answers for ONE chat, as the machine — a
        chat's connection scope. This is what narrows a chat's view of the
        workspace's union store to its owner's own set: the server decides on
        the chat, so a chat this box does not hold (the opaque 404) has no
        scope, and a read that gets no answer raises for the caller to fail
        closed on, rather than answering an empty set that would read as
        authoritative."""
        async with self._client() as client:
            parsed = await self._chat_records(client, str(chat_id))
        if parsed is None:
            raise RuntimeError(f"the connections of chat {chat_id} could not be read")
        ids = frozenset(record.id for record in parsed)
        self._listed[str(chat_id)] = ids
        return ids

    async def records_for_workspace(self, workspace_id: str) -> frozenset[str]:
        """The record ids attached to ONE workspace this box holds, as the
        machine: the shared connections a notebook kernel in the workspace's
        sandbox may query (``GET /workspaces/{id}/connections``). Raises, for
        the caller to fail closed on and say why, when the server answers that
        this box does not hold the workspace (the opaque 404) and when the
        read gets no answer."""
        url = f"{self._base}/api/v1/workspaces/{workspace_id}/connections"
        async with self._client() as client:
            try:
                resp = await client.get(url, headers=self._auth)
            except httpx.HTTPError as exc:
                raise RuntimeError(
                    f"the connections of workspace {workspace_id} could not be read"
                ) from exc
        if resp.status_code == 404:
            raise RuntimeError(f"this machine does not hold workspace {workspace_id}")
        if resp.status_code != 200:
            raise RuntimeError(
                f"the connections of workspace {workspace_id} answered HTTP {resp.status_code}"
            )
        try:
            rows = resp.json().get("connections") or []
            return frozenset(_record_from_wire(row).id for row in rows if isinstance(row, dict))
        except (ValueError, TypeError, AttributeError) as exc:
            raise RuntimeError(
                f"the connections of workspace {workspace_id} answered unreadably"
            ) from exc

    async def fetch_credential(self, record_id: str) -> tuple[str, int, dict[str, str]]:
        raise RuntimeError(FILE_CUSTODY_NOT_ON_MACHINE)

    async def _lease_for_workspace(
        self, record_id: str, workspace_id: str
    ) -> tuple[dict[str, str], float | None]:
        """One connection of a held workspace's owner, leased for that
        workspace alone (``POST /workspaces/{id}/connections/{rid}/credential-lease``):
        a shared row's bundle, or a per-user row's owner grant as its primary."""
        url = (
            f"{self._base}/api/v1/workspaces/{workspace_id}/connections/"
            f"{record_id}/credential-lease"
        )
        try:
            async with self._client() as client:
                resp = await client.post(url, headers=self._auth)
        except httpx.HTTPError as exc:
            raise RuntimeError(LEASE_UNREACHABLE) from exc
        if resp.status_code == 404:
            raise RuntimeError(NO_HELD_CHAT_MAY_USE)
        if resp.status_code != 200:
            detail = response_error_detail(resp)
            raise RuntimeError(detail or f"credential lease returned HTTP {resp.status_code}")
        try:
            body = resp.json()
        except ValueError as exc:
            raise RuntimeError("the credential lease answered unreadably") from exc
        if not isinstance(body, dict):
            raise RuntimeError("the credential lease answered unreadably")
        return _credential_bundle(body), _lease_expiry(body.get("expires_at"))

    def _chats_to_ask(self, record_id: str) -> list[str]:
        """The held chats, those whose last listing carried the record first;
        each once, in the order the box holds them."""
        held = list(dict.fromkeys(str(c) for c in self._chats()))
        listed = [c for c in held if record_id in self._listed.get(c, frozenset())]
        return listed + [c for c in held if c not in listed]

    async def lease_credential_bundle(
        self, record_id: str, *, chat_id: str | None = None, workspace_id: str | None = None
    ) -> tuple[dict[str, str], float | None]:
        """Lease for ``workspace_id`` when a notebook statement names the
        workspace it ran in (only that workspace's door is asked), else for
        ``chat_id``, the chat whose tool call asked; only that chat is asked,
        so a chat whose share was revoked is refused rather than served
        through another. With neither named the lease is refused without
        asking: a credential on a shared machine is never leased for whichever
        held chat happens to be allowed.

        The bundle is returned to the caller and written nowhere; the person's
        door is never asked, a machine has no standing there."""
        if workspace_id is not None:
            return await self._lease_for_workspace(record_id, workspace_id)
        if chat_id is None:
            raise RuntimeError(LEASE_NEEDS_A_CHAT)
        async with self._client() as client:
            answer = await self._lease_as(client, str(chat_id), record_id)
        if answer.lease is not None:
            return answer.lease
        raise RuntimeError(answer.failure or NO_HELD_CHAT_MAY_USE)

    async def lease_for_a_held_chat(
        self, record_id: str
    ) -> tuple[str, dict[str, str], float | None]:
        """The box's own read of a connection's schema, which no tool call
        made: lease through the first held chat whose owner may use the
        connection, and say which chat that was, so the read that follows is
        made as that chat. A chat's 404 is that chat's answer, not the box's:
        the next held chat is asked. Any other failure is remembered and the
        next chat asked too, except a 400 or 401, which is about this box's own
        request and no other chat changes. When no chat answered, the error
        names the failure seen, or that no held chat may use the connection."""
        failure: str | None = None
        async with self._client() as client:
            for asked in self._chats_to_ask(record_id):
                answer = await self._lease_as(client, asked, record_id)
                if answer.lease is not None:
                    return asked, *answer.lease
                failure = answer.failure or failure
                if answer.about_this_box:
                    break
        raise RuntimeError(failure or NO_HELD_CHAT_MAY_USE)

    async def lease_for_schema_read(self, record_id: str) -> tuple[str | None, dict[str, str]]:
        chat_id, bundle, _expires = await self.lease_for_a_held_chat(record_id)
        return chat_id, bundle

    async def _lease_as(
        self, client: httpx.AsyncClient, chat_id: str, record_id: str
    ) -> _LeaseAnswer:
        """One chat's answer to one lease."""
        url = f"{self._base}/api/v1/chats/{chat_id}/connections/{record_id}/credential-lease"
        try:
            resp = await client.post(url, headers=self._auth)
        except httpx.HTTPError:
            logger.warning(
                "cloud_sync.connections.lease_unreachable", chat_id=chat_id, exc_info=True
            )
            return _LeaseAnswer(failure=LEASE_UNREACHABLE)
        if resp.status_code == 404:
            return _LeaseAnswer()
        if resp.status_code != 200:
            logger.warning(
                "cloud_sync.connections.lease_failed", chat_id=chat_id, status=resp.status_code
            )
            return _LeaseAnswer(
                failure=f"credential lease returned HTTP {resp.status_code}",
                about_this_box=resp.status_code in (400, 401),
            )
        try:
            body = resp.json()
        except ValueError:
            logger.warning("cloud_sync.connections.lease_unparseable", chat_id=chat_id)
            return _LeaseAnswer(failure=LEASE_UNREADABLE)
        if not isinstance(body, dict):
            return _LeaseAnswer(failure=LEASE_UNREADABLE)
        return _LeaseAnswer(lease=(_credential_bundle(body), _lease_expiry(body.get("expires_at"))))


#: What a lease answers when the server's reply could not be read.
LEASE_UNREADABLE = "the credential lease answered unreadably"


@dataclass(frozen=True)
class _LeaseAnswer:
    """One chat's answer to a lease: the bundle and its expiry, or why not.
    Neither is the chat's opaque 404. ``about_this_box`` marks a refusal of
    the box's own request (a 400 or 401), which no other chat answers
    differently."""

    lease: tuple[dict[str, str], float | None] | None = None
    failure: str | None = None
    about_this_box: bool = False


_installed: Callable[[], TeamConnectionsClient | None] | None = None


def install_connections_client(provider: Callable[[], TeamConnectionsClient | None] | None) -> None:
    """Make ``provider`` answer :func:`resolve_connections_client` for this
    process instead of the stored login. A box on its machine credential has
    no login on disk and must never read one; it installs the client it reads
    its chats' connections through, so a plugin leasing a credential meets the
    same door. ``None`` restores the default."""
    global _installed
    _installed = provider


def serves_chats_by_machine_credential() -> bool:
    """Whether this process reads its connections through a machine credential's
    chat-scoped door: a box serving the chats of many people, where nothing on
    the machine itself (a login, a cloud CLI's cache) belongs to any one of them."""
    provider = _installed
    return provider is not None and isinstance(provider(), ChatConnectionsClient)


def resolve_connections_client(
    project: ProjectDirectory | None = None, *, pin: bool = False
) -> TeamConnectionsClient | None:
    """The installed client, else one acting as ``project``'s profile, else
    ``None`` when logged out.

    A project pinned to another org than every usable profile also answers
    ``None``: to the reconcile that is "no answer", so it mutates nothing and
    org A's connections are never swapped for org B's in the same project.
    ``pin`` pins an unpinned project to the profile's org (the reconcile is a
    sync). Without a project (a connector leasing at query time) it is the
    profile resolved for the process; the server holds every lease to the
    token's own org."""
    if _installed is not None:
        return _installed()

    try:
        auth = profile_for_sync(project) if pin else profile_for_project(project)
    except ProfileResolutionError as exc:
        logger.info("cloud_sync.connections.refused", reason=str(exc))
        return None
    if auth is None:
        return None
    return TeamConnectionsClient(api_url=auth.api_url, token=auth.token, org_id=auth.org_team_id)


__all__ = [
    "FILE_CUSTODY_NOT_ON_MACHINE",
    "LEASE_NEEDS_A_CHAT",
    "LEASE_UNREACHABLE",
    "LEASE_UNREADABLE",
    "NO_HELD_CHAT_MAY_USE",
    "ChatConnectionsClient",
    "TeamConnectionsClient",
    "install_connections_client",
    "resolve_connections_client",
    "serves_chats_by_machine_credential",
]
