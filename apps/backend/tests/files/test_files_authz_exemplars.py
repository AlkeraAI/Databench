"""Every Files route that decides through ``authorize()``, with the rows it files.

The exemplar shape of ``apps/backend/tests/test_authz_exemplars.py``, applied to Files: each
case drives the *real* route through ``ASGITransport`` against real Postgres,
pins the status contract, and then pins the ``authz.decision`` row the outbox
holds afterwards — an ALLOW that rode the request's own transaction, a DENY
that survived the refused request's rollback.

The second half of every case is the redaction contract: a decision row is read
by platform staff who may have no business knowing what a customer called a
file, so the payload's ``attrs`` carry ids and shapes and nothing else.
"""

from __future__ import annotations

import secrets
import uuid
from typing import TYPE_CHECKING, Any

import pytest
from _files_kit import NOT_FOUND, refusal
from alkera_core.authz.decision import NEVER_AUDITED_KEYS
from alkera_core.authz.headers import agent_headers
from alkera_core.authz.policies.files import FORBIDDEN, NO_SUCH_NODE
from alkera_core.authz.principal import ActingContext
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.files import acl
from alkera_core.files.authz.decider import NO_DOWNLOAD_BIT
from alkera_core.files.authz.grants import Principal
from alkera_core.files.authz.ladder import ROLE_OWNER, ROLE_READER
from alkera_core.models import EventOutbox
from alkera_core.models.files.acl import FileShare
from alkera_core.models.files.tree import FileNode
from backend.services.org import teams as team_service
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from tests.conftest import app_client, login, make_member

if TYPE_CHECKING:  # the fixtures live in a conftest, which is not an importable package
    from .conftest import FilesFixtures, FilesOrgFixture

pytestmark = pytest.mark.asyncio

BASE = "/api/v1/files"
AUTHZ_TYPE = "authz.decision"
FILE_NODE = "file_node"
OPAQUE = NOT_FOUND

#: The name and path bytes every fixture node in this module carries. Spelled
#: once so the redaction assertion can look for exactly these strings.
SECRET_NAME = "acquisition-memo-project-hydra.txt"


async def _decisions(org_id: uuid.UUID) -> list[EventOutbox]:
    """Every Files decision row this org has, oldest first."""
    async with AsyncSessionLocal() as session:
        rows = await session.execute(
            select(EventOutbox)
            .where(
                EventOutbox.org_id == org_id,
                EventOutbox.type == AUTHZ_TYPE,
                EventOutbox.entity == FILE_NODE,
            )
            .order_by(EventOutbox.id)
        )
        return list(rows.scalars().all())


def _for_node(rows: list[EventOutbox], node: FileNode) -> list[dict[str, Any]]:
    """The payloads of the rows that name ``node`` as their resource."""
    return [row.payload for row in rows if row.entity_id == str(node.id)]


def _assert_ids_only(payload: dict[str, Any], node: FileNode) -> None:
    """The row's ``attrs`` may carry ids and shapes, never customer content.

    Two separate hazards: a key the platform has banned outright, and a *value*
    that happens to be the node's name or path — the two pieces of a Files node
    that are the customer's words rather than ours.
    """
    attrs = payload["attrs"]
    assert set(attrs) <= set(attrs) - NEVER_AUDITED_KEYS
    forbidden = {
        node.name.decode(),
        node.name_display,
        node.path_ids,
        node.name_key.decode() if isinstance(node.name_key, bytes) else str(node.name_key),
    }
    flat: list[str] = []
    for value in attrs.values():
        if isinstance(value, str):
            flat.append(value)
        elif isinstance(value, (list, tuple, set, frozenset)):
            flat.extend(str(item) for item in value)
    assert not (set(flat) & forbidden), f"attrs leaked node content: {attrs}"


async def _reader_client(
    client: AsyncClient,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
    node: FileNode,
    *,
    role: str = ROLE_READER,
) -> AsyncClient:
    """A logged-in org member holding exactly ``role`` on ``node``.

    A plain member has no standing on a Files node at all, so without this the
    only reachable denial is the opaque one; the grant is what makes the
    *visible* 403 branch — "you can see it, you still may not do that" —
    reachable through a real route.
    """
    ctx = ActingContext.for_user(
        user_id=files_org.org.admin_id, org_id=files_org.org.org_id, email="fixture@test"
    )
    async with fx.repo.transaction():
        await acl.grant(fx.repo, ctx, node, Principal(kind="user", id=files_org.member.id), role)
    await fx.repo.session.commit()
    member_client = app_client()
    return await login(member_client, files_org.member.email, files_org.member_password)


async def _other_org(client: AsyncClient) -> tuple[AsyncClient, uuid.UUID]:
    """A logged-in member of a completely different org, and that org's id.

    The id is what lets a cross-org case read the *outsider's* audit stream and
    say which branch refused them, rather than only that something did.
    """
    async with AsyncSessionLocal() as session:
        org, _admin = await team_service.create_org_with_admin(
            session,
            org_name=f"Other Org {secrets.token_hex(4)}",
            admin_email=f"other-admin-{secrets.token_hex(6)}@alkera.dev",
            admin_first_name="Other",
            admin_last_name="Admin",
            admin_password="pw-1234567890",
        )
        await session.commit()
        member, password = await make_member(session, org_id=org.id, verified=True)
    outsider = app_client()
    return await login(outsider, member.email, password or ""), org.id


async def _other_org_client(client: AsyncClient) -> AsyncClient:
    """A logged-in member of a completely different org."""
    outsider, _org_id = await _other_org(client)
    return outsider


def _code(response: Any) -> str:
    """The machine-readable code of a refusal, whichever envelope carried it.

    The Files handler answers ``{"code": ...}``; a denial the platform engine
    raises leaves as an ``HTTPException`` through the observability handler, as
    ``{"error": {"code": ...}}``. The status and the code are the contract here;
    the envelope difference itself is pinned — and currently failing — in
    :func:`test_a_policy_denial_is_indistinguishable_from_a_missing_node`.
    """
    body = response.json()
    if "code" in body:
        return str(body["code"])
    return str(body["error"]["code"])


async def _drive_id(client: AsyncClient) -> str:
    response = await client.get(f"{BASE}/drives")
    assert response.status_code == 200, response.text
    return str(response.json()["id"])


# =========================================================================== #
# the allow — 200 and an ALLOW row in the request's own transaction
# =========================================================================== #


@pytest.mark.parametrize(
    ("method", "suffix", "body", "action"),
    [
        pytest.param("GET", "", None, "read", id="get_item"),
        pytest.param("PATCH", "", {"name": "renamed.txt"}, "write", id="patch_rename"),
        pytest.param("GET", "/content", None, "export", id="get_content"),
    ],
)
async def test_an_org_admin_is_allowed_and_the_allow_is_on_record(
    files_client: AsyncClient,
    files_on: None,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
    idem: Any,
    method: str,
    suffix: str,
    body: dict[str, Any] | None,
    action: str,
) -> None:
    """The org admin is floored at manager everywhere in their own org, so each
    of the four verbs is allowed — and each files exactly one ALLOW row naming
    the action it decided."""
    node = await fx.node(SECRET_NAME.encode())
    await fx.version(node)
    drive = await _drive_id(files_client)
    url = f"{BASE}/drives/{drive}/items/{node.id}{suffix}"
    headers = {**idem(), "If-Match": str(node.etag)}
    response = await files_client.request(method, url, json=body, headers=headers)
    assert response.status_code in {200, 204, 302}, response.text

    payloads = _for_node(await _decisions(files_org.org.org_id), node)
    assert [p["effect"] for p in payloads] == ["allow"]
    assert payloads[-1]["action"] == action
    assert payloads[-1]["policy"] == "files.access"
    _assert_ids_only(payloads[-1], node)


async def test_the_permanent_delete_needs_the_owner_rung_a_manager_does_not_reach(
    files_client: AsyncClient,
    files_on: None,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
    idem: Any,
) -> None:
    """The org-admin descent floors an admin at *manager*, and ``DELETE`` sits
    one rung above that — so the same admin who may trash a node is refused the
    purge that would make it unrecoverable, and is allowed the moment an owner
    grant lands. Both halves file a row naming the same action."""
    # The grant lands on the folder and reaches the node by descent, which is
    # also what keeps the purge off the folder the grant's own descent
    # operation still references.
    folder = await fx.node(b"owned", kind="folder")
    node = await fx.node(SECRET_NAME.encode(), parent=folder)
    drive = await _drive_id(files_client)
    refused = await files_client.delete(
        f"{BASE}/drives/{drive}/items/{node.id}?permanent=true",
        headers={**idem(), "If-Match": str(node.etag)},
    )
    assert refused.status_code == 403, refused.text

    ctx = ActingContext.for_user(
        user_id=files_org.org.admin_id, org_id=files_org.org.org_id, email="fixture@test"
    )
    async with fx.repo.transaction():
        await acl.grant(
            fx.repo, ctx, folder, Principal(kind="user", id=files_org.org.admin_id), ROLE_OWNER
        )
    await fx.repo.session.commit()
    await fx.repo.session.refresh(node)
    allowed = await files_client.delete(
        f"{BASE}/drives/{drive}/items/{node.id}?permanent=true",
        headers={**idem(), "If-Match": str(node.etag)},
    )
    assert allowed.status_code == 204, allowed.text

    payloads = _for_node(await _decisions(files_org.org.org_id), node)
    deletes = [p for p in payloads if p["action"] == "delete"]
    assert [p["effect"] for p in deletes] == ["deny", "allow"]
    _assert_ids_only(deletes[0], node)


# =========================================================================== #
# the visible deny — 403 with a code, surviving the refused request's rollback
# =========================================================================== #


@pytest.mark.parametrize(
    ("method", "suffix", "body", "action"),
    [
        pytest.param("PATCH", "", {"name": "renamed.txt"}, "write", id="patch_rename"),
        pytest.param("DELETE", "?permanent=true", None, "delete", id="permanent_delete"),
    ],
)
async def test_a_reader_is_refused_with_a_code_and_the_deny_survives(
    client: AsyncClient,
    files_on: None,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
    idem: Any,
    method: str,
    suffix: str,
    body: dict[str, Any] | None,
    action: str,
) -> None:
    """A caller who may read the node already knows it is there, so naming the
    refusal costs nothing — and the row is written in a session of its own, so
    it is still there after the 403 rolled the request back."""
    node = await fx.node(SECRET_NAME.encode())
    await fx.version(node)
    reader = await _reader_client(client, fx, files_org, node)
    drive = await _drive_id(reader)
    url = f"{BASE}/drives/{drive}/items/{node.id}{suffix}"
    headers = {**idem(), "If-Match": str(node.etag)}
    response = await reader.request(method, url, json=body, headers=headers)
    assert response.status_code == 403, response.text
    assert _code(response) == FORBIDDEN

    payloads = _for_node(await _decisions(files_org.org.org_id), node)
    denies = [p for p in payloads if p["effect"] == "deny"]
    assert [p["reason"] for p in denies] == ["action_not_allowed"]
    assert denies[-1]["action"] == action
    assert denies[-1]["as_not_found"] is False
    assert denies[-1]["error_code"] == FORBIDDEN
    _assert_ids_only(denies[-1], node)


async def test_a_no_download_flag_refuses_the_content_route_visibly(
    client: AsyncClient,
    files_on: None,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
) -> None:
    """The flag narrows every role, reader included: metadata still reads, the
    bytes do not — and the row names the flag rather than the node."""
    node = await fx.node(SECRET_NAME.encode(), flags=NO_DOWNLOAD_BIT)
    await fx.version(node)
    reader = await _reader_client(client, fx, files_org, node)
    drive = await _drive_id(reader)
    assert (await reader.get(f"{BASE}/drives/{drive}/items/{node.id}")).status_code == 200
    response = await reader.get(f"{BASE}/drives/{drive}/items/{node.id}/content")
    assert response.status_code == 403, response.text
    assert _code(response) == FORBIDDEN

    denies = [
        p for p in _for_node(await _decisions(files_org.org.org_id), node) if p["effect"] == "deny"
    ]
    assert denies[-1]["action"] == "export"
    assert "no_download" in denies[-1]["attrs"]["flags"]
    _assert_ids_only(denies[-1], node)


# =========================================================================== #
# the opaque deny — 404, and a row that still says what was refused
# =========================================================================== #


async def test_a_member_with_no_grant_gets_the_opaque_404_with_a_deny_on_record(
    client: AsyncClient,
    files_on: None,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
) -> None:
    """An unreadable node is refused as though it did not exist — the caller
    learns nothing — but the refusal is still on record, and the reason names
    the opaque branch rather than the ordinary one."""
    node = await fx.node(SECRET_NAME.encode())
    stranger = app_client()
    await login(stranger, files_org.member.email, files_org.member_password)
    drive = await _drive_id(stranger)
    response = await stranger.get(f"{BASE}/drives/{drive}/items/{node.id}")
    assert response.status_code == 404
    assert _code(response) == "not_found"

    denies = [
        p for p in _for_node(await _decisions(files_org.org.org_id), node) if p["effect"] == "deny"
    ]
    assert denies[-1]["reason"] == "action_not_allowed_unreadable"
    assert denies[-1]["as_not_found"] is True
    assert denies[-1]["error_code"] is None
    _assert_ids_only(denies[-1], node)


async def test_a_cross_org_read_is_opaque_and_files_nothing_under_the_callers_org(
    client: AsyncClient,
    files_on: None,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
) -> None:
    """Another org's node is not this caller's business, and the row must not
    make it one: nothing at all is planted in the *owning* org's stream, and the
    row the caller's own stream keeps says the node was never there for them —
    the same thing an id that was never issued files, so an outsider who can
    read their own audit log still cannot learn that the node exists.
    """
    node = await fx.node(SECRET_NAME.encode())
    outsider, outsider_org = await _other_org(client)
    other_drive = await _drive_id(outsider)
    response = await outsider.get(f"{BASE}/drives/{other_drive}/items/{node.id}")
    assert response.status_code == 404
    assert refusal(response) == OPAQUE

    assert _for_node(await _decisions(files_org.org.org_id), node) == [], (
        "an outsider's read planted a row in the owning org's audit stream"
    )
    mine = _for_node(await _decisions(outsider_org), node)
    assert [row["reason"] for row in mine] == [NO_SUCH_NODE]
    assert mine[-1]["as_not_found"] is True
    assert mine[-1]["attrs"]["exists"] is False

    unissued = uuid.uuid4()
    missing = await outsider.get(f"{BASE}/drives/{other_drive}/items/{unissued}")
    assert (missing.status_code, refusal(missing)) == (response.status_code, refusal(response))
    async with AsyncSessionLocal() as session:
        rows = await session.execute(
            select(EventOutbox).where(
                EventOutbox.type == AUTHZ_TYPE,
                EventOutbox.entity == FILE_NODE,
                EventOutbox.entity_id == str(unissued),
                EventOutbox.org_id == outsider_org,
            )
        )
        never = [row.payload for row in rows.scalars().all()]
    assert [row["reason"] for row in never] == [row["reason"] for row in mine], (
        "the two classes file different reasons: the audit log is the oracle"
    )


async def test_a_policy_denial_is_indistinguishable_from_a_missing_node(
    client: AsyncClient,
    files_on: None,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
) -> None:
    """The whole point of the opaque branch: a real node the caller may not read
    and an id that was never allocated must answer the *same bytes*. Anything
    that differs — a code, a key, an envelope — is an existence oracle."""
    node = await fx.node(SECRET_NAME.encode())
    stranger = app_client()
    await login(stranger, files_org.member.email, files_org.member_password)
    drive = await _drive_id(stranger)
    unreadable = await stranger.get(f"{BASE}/drives/{drive}/items/{node.id}")
    nonexistent = await stranger.get(f"{BASE}/drives/{drive}/items/{uuid.uuid4()}")
    assert unreadable.status_code == nonexistent.status_code == 404
    assert refusal(unreadable) == refusal(nonexistent) == OPAQUE


async def test_no_decision_row_ever_carries_a_banned_key(
    files_client: AsyncClient,
    files_on: None,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
    idem: Any,
) -> None:
    """The declared allowlist is checked at registration, but the payload is
    what platform staff actually read — so pin it on the rows themselves,
    across a mixed run of allowed and refused requests.
    """
    node = await fx.node(SECRET_NAME.encode())
    await fx.version(node)
    drive = await _drive_id(files_client)
    await files_client.get(f"{BASE}/drives/{drive}/items/{node.id}")
    await files_client.patch(
        f"{BASE}/drives/{drive}/items/{node.id}",
        json={"name": "renamed.txt"},
        headers={**idem(), "If-Match": str(node.etag)},
    )
    await files_client.get(f"{BASE}/drives/{drive}/items/{uuid.uuid4()}")

    rows = await _decisions(files_org.org.org_id)
    assert rows, "the run filed no decision rows at all"
    for row in rows:
        assert not (set(row.payload["attrs"]) & NEVER_AUDITED_KEYS)
        assert SECRET_NAME not in str(row.payload["attrs"])


async def test_a_disabled_deployment_decides_nothing(
    files_client: AsyncClient,
    files_off: None,
    real_session: AsyncSession,
    files_org: FilesOrgFixture,
) -> None:
    """With the flag off every route is the opaque 404 *before* a policy runs —
    so a dark deployment leaves no decision rows to mine either.

    The switch is pinned by ``files_off``, not inherited: read from the ambient
    process this case runs against whatever the dotenv — or the last sibling
    that flipped the module-global settings object — happened to leave behind,
    and on a LIVE deployment the route answers the same 404 for a node that is
    not there while filing the very decision row the case says never exists.
    """
    response = await files_client.get(f"{BASE}/drives/{uuid.uuid4()}/items/{uuid.uuid4()}")
    assert response.status_code == 404
    assert refusal(response) == OPAQUE
    assert await _decisions(files_org.org.org_id) == []


# =========================================================================== #
# sharing — the one action a role must climb to, and the one an agent never has
# =========================================================================== #


def _share_body(principal_id: uuid.UUID, role: str = ROLE_READER) -> dict[str, Any]:
    return {"principal": {"kind": "user", "id": str(principal_id)}, "role": role}


async def _grants_on(node: FileNode) -> list[tuple[uuid.UUID, str]]:
    """The live direct grants on ``node``, read back from its own session.

    A refusal that files a tidy decision row but leaves the grant behind is the
    failure mode worth catching, so every sharing case below asserts the rows
    rather than only the status.
    """
    async with AsyncSessionLocal() as session:
        rows = await session.execute(
            select(FileShare)
            .where(FileShare.node_id == node.id, FileShare.revoked_at.is_(None))
            .order_by(FileShare.created_at)
        )
        return [(row.principal_id, row.role) for row in rows.scalars().all()]


async def test_an_org_admin_may_share_and_the_allow_is_on_record(
    files_client: AsyncClient,
    files_on: None,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
    idem: Any,
) -> None:
    """``SHARE`` sits on the manager rung, which is exactly where the org-admin
    descent floors an admin — so the grant lands, and the row that let it
    through names ``share`` rather than the ``write`` the mutation looks like."""
    node = await fx.node(SECRET_NAME.encode())
    drive = await _drive_id(files_client)
    response = await files_client.post(
        f"{BASE}/drives/{drive}/items/{node.id}/permissions",
        json=_share_body(files_org.member.id),
        headers={**idem(), "If-Match": str(node.etag)},
    )
    assert response.status_code == 201, response.text
    assert response.json()["principal"]["id"] == str(files_org.member.id)

    shares = _for_node(await _decisions(files_org.org.org_id), node)
    assert [p["effect"] for p in shares] == ["allow"]
    assert shares[-1]["action"] == "share"
    assert shares[-1]["policy"] == "files.access"
    _assert_ids_only(shares[-1], node)


async def test_a_reader_may_not_share_and_the_deny_names_the_action(
    client: AsyncClient,
    files_on: None,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
    idem: Any,
) -> None:
    """A reader can see the node, so the refusal is the visible one — and the
    row says the caller was refused ``share`` specifically, not the ``read``
    they were allowed a moment earlier when the route resolved the node."""
    node = await fx.node(SECRET_NAME.encode())
    reader = await _reader_client(client, fx, files_org, node)
    drive = await _drive_id(reader)
    response = await reader.post(
        f"{BASE}/drives/{drive}/items/{node.id}/permissions",
        json=_share_body(files_org.org.admin_id, ROLE_OWNER),
        headers={**idem(), "If-Match": str(node.etag)},
    )
    assert response.status_code == 403, response.text
    assert _code(response) == FORBIDDEN

    denies = [
        p for p in _for_node(await _decisions(files_org.org.org_id), node) if p["effect"] == "deny"
    ]
    assert denies[-1]["action"] == "share"
    assert denies[-1]["reason"] == "action_not_allowed"
    assert denies[-1]["as_not_found"] is False
    assert denies[-1]["error_code"] == FORBIDDEN
    _assert_ids_only(denies[-1], node)
    assert await _grants_on(node) == [(files_org.member.id, ROLE_READER)], (
        "the refused share still wrote a grant"
    )


async def test_a_cross_org_share_is_opaque_and_grants_nothing(
    client: AsyncClient,
    files_on: None,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
    idem: Any,
) -> None:
    """Sharing is the loudest thing a caller can try on someone else's node, so
    it is the one that must answer most quietly: the same opaque 404 an id that
    was never issued gets, no grant, and no row under the caller's own org."""
    node = await fx.node(SECRET_NAME.encode())
    outsider, outsider_org = await _other_org(client)
    other_drive = await _drive_id(outsider)
    body = _share_body(files_org.member.id)
    response = await outsider.post(
        f"{BASE}/drives/{other_drive}/items/{node.id}/permissions",
        json=body,
        headers={**idem(), "If-Match": str(node.etag)},
    )
    assert response.status_code == 404
    assert refusal(response) == OPAQUE

    nonexistent = await outsider.post(
        f"{BASE}/drives/{other_drive}/items/{uuid.uuid4()}/permissions",
        json=body,
        headers={**idem(), "If-Match": "1"},
    )
    assert nonexistent.status_code == 404
    assert refusal(nonexistent) == refusal(response)

    assert await _grants_on(node) == [], "a cross-org caller planted a grant"
    assert _for_node(await _decisions(files_org.org.org_id), node) == [], (
        "an outsider's attempt planted a row in the owning org's audit stream"
    )

    # Single-determined: the row the outsider's own stream holds says the node
    # was never there for them. Any other reason — ``not_in_org`` above all —
    # would mean the scoped repo had handed a foreign row to the policy, and
    # the 404 would then be a courtesy rather than a consequence.
    outside = _for_node(await _decisions(outsider_org), node)
    assert [row["reason"] for row in outside] == [NO_SUCH_NODE]
    assert outside[-1]["effect"] == "deny"
    assert outside[-1]["as_not_found"] is True
    assert outside[-1]["attrs"]["exists"] is False


async def test_an_agent_never_shares_however_much_its_user_may(
    files_client: AsyncClient,
    files_on: None,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
    idem: Any,
) -> None:
    """The agent confinement is not a role question: the very same request, from
    the very same admin session, is refused when the two assertion headers name
    an agent and allowed when they do not. The row proves the *agent* branch
    decided it — a policy that had merely run out of role would have said
    ``action_not_allowed`` instead.

    The refusal is the *visible* 403: an agent that may read the node is told
    which action it was refused. The opaque variant would mean the agent had no
    subtree at all and so could not read the node it just addressed."""
    node = await fx.node(SECRET_NAME.encode())
    drive = await _drive_id(files_client)
    url = f"{BASE}/drives/{drive}/items/{node.id}/permissions"
    body = _share_body(files_org.member.id)

    as_agent = await files_client.post(
        url,
        json=body,
        headers={**idem(), "If-Match": str(node.etag), **agent_headers("chat-01.session")},
    )
    assert as_agent.status_code == 403, as_agent.text
    assert _code(as_agent) == FORBIDDEN

    denies = [
        p for p in _for_node(await _decisions(files_org.org.org_id), node) if p["effect"] == "deny"
    ]
    assert denies[-1]["action"] == "share"
    assert denies[-1]["reason"] == "agent_may_not_share"
    assert denies[-1]["as_not_found"] is False
    assert denies[-1]["error_code"] == FORBIDDEN
    assert denies[-1]["attrs"]["is_agent"] is True
    # The agent could read: an unreadable node would have been the opaque row.
    assert "read" in denies[-1]["attrs"]["allowed_actions"]
    _assert_ids_only(denies[-1], node)
    assert await _grants_on(node) == [], "an agent's refused share still wrote a grant"

    as_user = await files_client.post(
        url, json=body, headers={**idem(), "If-Match": str(node.etag)}
    )
    assert as_user.status_code == 201, as_user.text
    assert await _grants_on(node) == [(files_org.member.id, ROLE_READER)]
