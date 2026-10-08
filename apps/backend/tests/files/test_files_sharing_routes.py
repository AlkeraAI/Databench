"""The permissions routes: what they grant, what they refuse, what they hide.

The isolation cases run through ``_oracle``, which asks the same route about a
member of the org, a user of *another* org and a uuid that was never issued and
asserts that the last two answers are the same bytes. That is the property the
route exists to preserve: a sharing dialog must not become a way to find out
who has an account.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from _files_kit import NOT_FOUND, node_etag, refusal
from _oracle import assert_byte_identical, assert_same_work, probe, quiesce_auth
from alkera_core.authz.headers import agent_headers
from alkera_core.authz.principal import ActingContext
from alkera_core.db.session import engine
from alkera_core.files import acl
from alkera_core.files.authz.grants import (
    Principal,
    register_principal_kind,
    unregister_principal_kind,
)
from alkera_core.files.ids import NodeId
from alkera_core.models.files.tree import FileNode
from backend.services.org import teams as team_service
from freezegun import freeze_time
from httpx import AsyncClient, Response
from sqlalchemy.ext.asyncio import AsyncSession
from tests.conftest import login, make_member
from tests.files.conftest import FilesFixtures, FilesOrgFixture

pytestmark = pytest.mark.asyncio

API = "/api/v1/files"

#: The engine the probe counts statements on: the app and the fixtures share
#: one process-wide async engine, so its sync facade is what the listener binds.
SYNC_ENGINE = engine.sync_engine


def _url(node: FileNode, suffix: str = "") -> str:
    return f"{API}/drives/{node.drive_id}/items/{node.id}/permissions{suffix}"


def _body(response: Response) -> Any:
    return response.json()


def _fingerprint(response: Response) -> tuple[int, Any]:
    """What two answers must share to be indistinguishable: status and body.

    ``Date`` and the request id are the only headers that legitimately differ,
    and neither is part of the tuple; the body's trace id is the same request
    id, so a refusal is compared without it.
    """
    if response.status_code >= 400:
        return (response.status_code, refusal(response))
    return (response.status_code, response.json())


async def _mutate(
    session: AsyncSession, node: FileNode, idem: Callable[[], dict[str, str]]
) -> dict[str, str]:
    """The headers every permissions mutation carries: a fresh idempotency key
    and the etag of the node the caller believes it is changing. A grant moves
    that counter, so the value is read back from the database each time rather
    than remembered.
    """
    return {**idem(), "If-Match": await node_etag(session, node.id)}


async def test_grant_then_list_shows_the_direct_grant(
    files_on: None,
    files_client: AsyncClient,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
    real_session: AsyncSession,
    idem: Callable[[], dict[str, str]],
) -> None:
    node = await fx.node(b"shared.txt")
    created = await files_client.post(
        _url(node),
        json={
            "principal": {"kind": "user", "id": str(files_org.member.id)},
            "role": "writer",
        },
        headers=await _mutate(real_session, node, idem),
    )
    assert created.status_code == 201, created.text
    assert created.json()["role"] == "writer"
    assert created.json()["origin"] == "direct"

    listed = await files_client.get(_url(node))
    assert listed.status_code == 200
    entries = listed.json()["value"]
    assert [(e["principal"]["id"], e["role"]) for e in entries] == [
        (str(files_org.member.id), "writer")
    ]


async def test_effective_list_names_the_granting_ancestor(
    files_on: None,
    files_client: AsyncClient,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
    real_session: AsyncSession,
    idem: Callable[[], dict[str, str]],
) -> None:
    """A grant made on a folder shows up on the child as ``inherited`` with the
    folder's id — the fact the revoke route needs to point the caller at."""
    folder = await fx.node(b"team", kind="folder")
    child = await fx.node(b"note.txt", parent=folder)
    granted = await files_client.post(
        _url(folder),
        json={"principal": {"kind": "user", "id": str(files_org.member.id)}, "role": "reader"},
        headers=await _mutate(real_session, folder, idem),
    )
    assert granted.status_code == 201, granted.text

    direct = await files_client.get(_url(child))
    assert direct.json()["value"] == []

    effective = await files_client.get(_url(child, "?effective=true"))
    assert effective.status_code == 200
    inherited = [e for e in effective.json()["value"] if e["origin"] == "inherited"]
    assert [(e["principal"]["id"], e["grantingNodeId"]) for e in inherited] == [
        (str(files_org.member.id), str(folder.id))
    ]


async def test_changing_a_rung_then_removing_the_person_takes_their_access(
    files_on: None,
    files_client: AsyncClient,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
    real_session: AsyncSession,
    idem: Callable[[], dict[str, str]],
) -> None:
    """Share, move the rung, Remove — and nothing of the access is left.

    The second grant moves the standing share rather than adding one, so the
    listing keeps one row and the id the panel is holding is still the id that
    withdraws the person. Two rows would leave the weaker one behind, and the
    effective body takes the strongest of whatever is live.
    """
    node = await fx.node(b"plans.txt")
    created = await files_client.post(
        _url(node),
        json={"principal": {"kind": "user", "id": str(files_org.member.id)}, "role": "reader"},
        headers=await _mutate(real_session, node, idem),
    )
    assert created.status_code == 201, created.text
    share_id = created.json()["id"]

    changed = await files_client.post(
        _url(node),
        json={"principal": {"kind": "user", "id": str(files_org.member.id)}, "role": "writer"},
        headers=await _mutate(real_session, node, idem),
    )
    assert changed.status_code == 201, changed.text
    assert changed.json()["id"] == share_id
    listed = await files_client.get(_url(node))
    assert [(e["id"], e["role"]) for e in listed.json()["value"]] == [(share_id, "writer")]

    removed = await files_client.request(
        "DELETE",
        _url(node, f"/{share_id}"),
        headers=await _mutate(real_session, node, idem),
    )
    assert removed.status_code == 204, removed.text
    after = await files_client.get(_url(node, "?effective=true"))
    assert str(files_org.member.id) not in {e["principal"]["id"] for e in after.json()["value"]}


async def test_revoking_an_inherited_grant_is_a_409_with_the_ancestor(
    files_on: None,
    files_client: AsyncClient,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
    real_session: AsyncSession,
    idem: Callable[[], dict[str, str]],
) -> None:
    """The only honest way to remove an inherited grant is at its source,
    and the refusal says where that is."""
    folder = await fx.node(b"team", kind="folder")
    child = await fx.node(b"note.txt", parent=folder)
    created = await files_client.post(
        _url(folder),
        json={"principal": {"kind": "user", "id": str(files_org.member.id)}, "role": "reader"},
        headers=await _mutate(real_session, folder, idem),
    )
    share_id = created.json()["id"]

    refused = await files_client.request(
        "DELETE",
        _url(child, f"/{share_id}"),
        headers=await _mutate(real_session, child, idem),
    )
    assert refused.status_code == 409, refused.text
    assert refused.json()["code"] == "files.inherited_grant"
    assert refused.json()["detail"]["ancestor_id"] == str(folder.id)

    # And the grant is still standing: the refusal changed nothing.
    still = await files_client.get(_url(folder))
    assert [e["id"] for e in still.json()["value"]] == [share_id]


async def test_revoking_a_direct_grant_removes_it(
    files_on: None,
    files_client: AsyncClient,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
    real_session: AsyncSession,
    idem: Callable[[], dict[str, str]],
) -> None:
    node = await fx.node(b"doc.txt")
    created = await files_client.post(
        _url(node),
        json={"principal": {"kind": "user", "id": str(files_org.member.id)}, "role": "reader"},
        headers=await _mutate(real_session, node, idem),
    )
    share_id = created.json()["id"]
    gone = await files_client.request(
        "DELETE",
        _url(node, f"/{share_id}"),
        headers=await _mutate(real_session, node, idem),
    )
    assert gone.status_code == 204, gone.text
    assert (await files_client.get(_url(node))).json()["value"] == []


async def _oracle(
    client: AsyncClient,
    session: AsyncSession,
    node: FileNode,
    principal_id: uuid.UUID,
    idem: Callable[[], dict[str, str]],
) -> Response:
    return await client.post(
        _url(node),
        json={"principal": {"kind": "user", "id": str(principal_id)}, "role": "reader"},
        headers=await _mutate(session, node, idem),
    )


async def test_sharing_never_confirms_an_outsider(
    files_on: None,
    files_client: AsyncClient,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
    real_session: AsyncSession,
    idem: Callable[[], dict[str, str]],
) -> None:
    """An other-org user id and an id that was never issued answer the
    same bytes, and neither is the answer a member gets."""
    other_org, _ = await team_service.create_org_with_admin(
        real_session,
        org_name=f"other-{uuid.uuid4().hex[:8]}",
        admin_email=f"other-{uuid.uuid4().hex[:8]}@test.dev",
        admin_first_name="Other",
        admin_last_name="Admin",
        admin_password="other-pass-12345",
    )
    await real_session.commit()
    outsider, _ = await make_member(real_session, org_id=other_org.id, verified=True)
    await real_session.commit()
    node = await fx.node(b"target.txt")

    member = await _oracle(files_client, real_session, node, files_org.member.id, idem)
    foreign = await _oracle(files_client, real_session, node, outsider.id, idem)
    random = await _oracle(files_client, real_session, node, uuid.uuid4(), idem)

    assert member.status_code == 201
    assert _fingerprint(foreign) == _fingerprint(random)
    assert foreign.status_code == 404
    assert refusal(foreign) == NOT_FOUND


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        pytest.param({"kind": "wizard", "role": "reader"}, 422, id="unknown-principal-kind"),
        pytest.param({"kind": "user", "role": "sorcerer"}, 422, id="unknown-role"),
    ],
)
async def test_bad_grant_shapes_are_422(
    files_on: None,
    files_client: AsyncClient,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
    real_session: AsyncSession,
    idem: Callable[[], dict[str, str]],
    payload: dict[str, str],
    expected: int,
) -> None:
    node = await fx.node(b"target.txt")
    answer = await files_client.post(
        _url(node),
        json={
            "principal": {"kind": payload["kind"], "id": str(files_org.member.id)},
            "role": payload["role"],
        },
        headers=await _mutate(real_session, node, idem),
    )
    assert answer.status_code == expected, answer.text


async def test_a_grant_without_an_idempotency_key_is_428(
    files_on: None,
    files_client: AsyncClient,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
) -> None:
    node = await fx.node(b"target.txt")
    answer = await files_client.post(
        _url(node),
        json={"principal": {"kind": "user", "id": str(files_org.member.id)}, "role": "reader"},
    )
    assert answer.status_code == 428, answer.text


async def test_a_writer_may_not_share(
    files_on: None,
    client: AsyncClient,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
    real_session: AsyncSession,
    idem: Callable[[], dict[str, str]],
) -> None:
    """SHARE needs manager. A writer can read the node, so the refusal is a
    visible 403 rather than the opaque 404 an outsider gets."""
    node = await fx.node(b"target.txt")
    async with fx.repo.transaction():
        await acl.grant(
            fx.repo,
            _fixture_ctx(files_org),
            node,
            Principal(kind="user", id=files_org.member.id),
            "writer",
        )
    writer = await login(client, files_org.member.email, files_org.member_password)
    refused = await _oracle(writer, real_session, node, files_org.org.admin_id, idem)
    assert refused.status_code == 403, refused.text
    assert refused.json()["code"] == "files.forbidden"
    assert refused.json()["message"]


async def test_an_agent_may_never_share(
    files_on: None,
    files_client: AsyncClient,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
    real_session: AsyncSession,
    idem: Callable[[], dict[str, str]],
) -> None:
    """The org admin is manager everywhere by descent — and still cannot share
    while acting through an agent assertion.

    The refusal is the visible one the policy defines. An opaque 404 here would
    not be discretion, it would be the sign that the request never reached the
    agent branch at all: an agent with no subtree has no actions to be refused.
    """
    node = await fx.node(b"target.txt")
    headers = {
        **(await _mutate(real_session, node, idem)),
        **agent_headers(str(uuid.uuid4())),
    }
    refused = await files_client.post(
        _url(node),
        json={"principal": {"kind": "user", "id": str(files_org.member.id)}, "role": "reader"},
        headers=headers,
    )
    # 403 rather than 404: this agent may read the node, so hiding it would be
    # telling a caller who can already list it that it does not exist.
    assert refused.status_code == 403, refused.text
    assert refused.json()["code"] == "files.forbidden"
    assert (await files_client.get(_url(node))).json()["value"] == []


async def test_permissions_are_dark_when_files_are_off(
    files_client: AsyncClient,
    fx: FilesFixtures,
    files_off: None,
) -> None:
    """Kill switch off: the surface is mounted (the SDKs need it) and answers
    the same opaque 404 as a node that does not exist."""
    node = await fx.node(b"target.txt")
    answer = await files_client.get(_url(node))
    assert answer.status_code == 404
    assert refusal(answer) == NOT_FOUND


def _fixture_ctx(files_org: FilesOrgFixture) -> ActingContext:
    return ActingContext.for_user(
        user_id=files_org.org.admin_id, org_id=files_org.org.org_id, email="fixture@test"
    )


async def test_a_grant_at_a_stale_if_match_is_412_and_writes_nothing(
    files_on: None,
    files_client: AsyncClient,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
    real_session: AsyncSession,
    idem: Callable[[], dict[str, str]],
) -> None:
    """The precondition the 428 exists to make reachable: an etag from before
    the node last changed is refused, and the grant does not land."""
    node = await fx.node(b"target.txt")
    stale = str(int(await node_etag(real_session, node.id)) - 1)
    refused = await files_client.post(
        _url(node),
        json={"principal": {"kind": "user", "id": str(files_org.member.id)}, "role": "reader"},
        headers={**idem(), "If-Match": stale},
    )
    assert refused.status_code == 412, refused.text
    assert (await files_client.get(_url(node))).json()["value"] == []

    accepted = await files_client.post(
        _url(node),
        json={"principal": {"kind": "user", "id": str(files_org.member.id)}, "role": "reader"},
        headers={**idem(), "If-Match": await node_etag(real_session, node.id)},
    )
    assert accepted.status_code == 201, accepted.text


async def test_a_writer_may_not_grant_a_role_above_their_own(
    files_on: None,
    client: AsyncClient,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
    real_session: AsyncSession,
    idem: Callable[[], dict[str, str]],
) -> None:
    """The ladder's ceiling: sharing is "grant up to your own role", and a
    writer's own role is a rung below the ``manager`` they are trying to hand
    out — so the refusal stands at the rung immediately under the one that may
    share, which is where a ladder that had slipped by one would show it.
    """
    node = await fx.node(b"target.txt")
    async with fx.repo.transaction():
        await acl.grant(
            fx.repo,
            _fixture_ctx(files_org),
            node,
            Principal(kind="user", id=files_org.member.id),
            "writer",
        )
    writer = await login(client, files_org.member.email, files_org.member_password)
    refused = await writer.post(
        _url(node),
        json={
            "principal": {"kind": "user", "id": str(files_org.org.admin_id)},
            "role": "manager",
        },
        headers=await _mutate(real_session, node, idem),
    )
    assert refused.status_code == 403, refused.text
    assert refused.json()["code"] == "files.forbidden"
    listed = await files_client_grants(fx, node)
    assert listed == [(files_org.member.id, "writer")], f"the refused grant landed anyway: {listed}"


async def files_client_grants(fx: FilesFixtures, node: FileNode) -> list[tuple[uuid.UUID, str]]:
    """The live direct grants on ``node``, read straight from the rows.

    Read back through the repo rather than through the route, so a refusal that
    answered 403 and still wrote is caught even if the listing route were the
    thing that broke.
    """
    async with fx.repo.transaction():
        rows = await fx.repo.shares_of(NodeId(node.id))
    return [(row.principal_id, row.role) for row in rows if row.revoked_at is None]


# --------------------------------------------------------------------------
# the principal side of the no-oracle contract
# --------------------------------------------------------------------------


async def test_a_grant_body_never_says_whether_a_principal_exists(
    files_on: None,
    files_client: AsyncClient,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
    real_session: AsyncSession,
    idem: Callable[[], dict[str, str]],
) -> None:
    """The no-oracle contract, principal side: the *url* is the caller's own node every time,
    so the only thing that varies is who the grant names. A member is granted; a
    real user of another org and a uuid that was never issued must come back as
    the same bytes, from the same amount of work, naming neither id.

    Byte-identity is checked through ``_oracle`` rather than by hand: headers
    count too, since a ``Content-Length`` that moved with the id would be the
    oracle the body refused to be.
    """
    other_org, _ = await team_service.create_org_with_admin(
        real_session,
        org_name=f"other-{uuid.uuid4().hex[:8]}",
        admin_email=f"other-{uuid.uuid4().hex[:8]}@test.dev",
        admin_first_name="Other",
        admin_last_name="Admin",
        admin_password="other-pass-12345",
    )
    await real_session.commit()
    outsider, _ = await make_member(real_session, org_id=other_org.id, verified=True)
    await real_session.commit()
    node = await fx.node(b"target.txt")

    member = await _oracle(files_client, real_session, node, files_org.member.id, idem)
    assert member.status_code == 201, member.text

    quiesce_auth()
    foreign = await probe(
        files_client,
        SYNC_ENGINE,
        "POST",
        _url(node),
        json={"principal": {"kind": "user", "id": str(outsider.id)}, "role": "reader"},
        headers=await _mutate(real_session, node, idem),
    )
    unissued = uuid.uuid4()
    quiesce_auth()
    never = await probe(
        files_client,
        SYNC_ENGINE,
        "POST",
        _url(node),
        json={"principal": {"kind": "user", "id": str(unissued)}, "role": "reader"},
        headers=await _mutate(real_session, node, idem),
    )

    assert foreign.status == 404
    assert_byte_identical([foreign, never], [str(outsider.id), str(unissued)])
    assert_same_work([foreign, never])
    assert await files_client_grants(fx, node) == [(files_org.member.id, "reader")], (
        "a refused grantee still got a row"
    )


async def test_a_grant_to_another_orgs_team_answers_the_same_as_a_random_team(
    files_on: None,
    files_client: AsyncClient,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
    real_session: AsyncSession,
    idem: Callable[[], dict[str, str]],
) -> None:
    """The negative twin of the user row, one principal kind over: a team is
    resolved by walking parents to the org root, so an existing foreign team is
    the case a user-only check would let through.
    """
    other_org, _ = await team_service.create_org_with_admin(
        real_session,
        org_name=f"other-{uuid.uuid4().hex[:8]}",
        admin_email=f"other-{uuid.uuid4().hex[:8]}@test.dev",
        admin_first_name="Other",
        admin_last_name="Admin",
        admin_password="other-pass-12345",
    )
    await real_session.commit()
    node = await fx.node(b"target.txt")

    mine = await files_client.post(
        _url(node),
        json={"principal": {"kind": "team", "id": str(files_org.org.org_id)}, "role": "reader"},
        headers=await _mutate(real_session, node, idem),
    )
    assert mine.status_code == 201, mine.text

    quiesce_auth()
    foreign = await probe(
        files_client,
        SYNC_ENGINE,
        "POST",
        _url(node),
        json={"principal": {"kind": "team", "id": str(other_org.id)}, "role": "reader"},
        headers=await _mutate(real_session, node, idem),
    )
    unissued = uuid.uuid4()
    quiesce_auth()
    never = await probe(
        files_client,
        SYNC_ENGINE,
        "POST",
        _url(node),
        json={"principal": {"kind": "team", "id": str(unissued)}, "role": "reader"},
        headers=await _mutate(real_session, node, idem),
    )

    assert foreign.status == 404
    assert_byte_identical([foreign, never], [str(other_org.id), str(unissued)])
    assert_same_work([foreign, never])
    assert await files_client_grants(fx, node) == [(files_org.org.org_id, "reader")], (
        "a foreign team was granted a role"
    )


async def test_a_grant_without_an_if_match_is_428(
    files_on: None,
    files_client: AsyncClient,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
    idem: Callable[[], dict[str, str]],
) -> None:
    """A grant is a mutation, so it must name the version of the node it
    believes it is changing. With the header absent the request is refused
    before anything is written, and the answer is the 428 that says adding the
    header makes the request acceptable."""
    node = await fx.node(b"target.txt")
    refused = await files_client.post(
        _url(node),
        json={"principal": {"kind": "user", "id": str(files_org.member.id)}, "role": "reader"},
        headers=idem(),
    )
    assert refused.status_code == 428, refused.text
    assert refused.json()["code"] == "files.if_match_required"
    assert (await files_client.get(_url(node))).json()["value"] == []


async def test_a_listing_names_the_user_and_the_team_it_is_shared_with(
    files_on: None,
    files_client: AsyncClient,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
    real_session: AsyncSession,
    idem: Callable[[], dict[str, str]],
) -> None:
    """A dialog cannot ask a person about ``0f3c…``: every grant the listing
    returns carries the name of whoever it is for, across both principal kinds
    a share can name."""
    team = await team_service.create_subteam(
        real_session, org_team_id=files_org.org.org_id, name="Archivists"
    )
    await real_session.commit()
    node = await fx.node(b"target.txt")

    for principal in (
        {"kind": "user", "id": str(files_org.member.id)},
        {"kind": "team", "id": str(team.id)},
    ):
        made = await files_client.post(
            _url(node),
            json={"principal": principal, "role": "reader"},
            headers=await _mutate(real_session, node, idem),
        )
        assert made.status_code == 201, made.text
        # The grant's own answer stays ids-only; the name is the listing's job.
        assert made.json()["principalName"] is None

    listed = await files_client.get(_url(node))
    assert listed.status_code == 200, listed.text
    named = {
        (row["principal"]["kind"], row["principal"]["id"]): row["principalName"]
        for row in listed.json()["value"]
    }
    expected_user = f"{files_org.member.first_name} {files_org.member.last_name}".strip()
    assert named[("user", str(files_org.member.id))] == expected_user
    assert named[("team", str(team.id))] == "Archivists"


async def test_a_grant_to_a_team_that_is_gone_keeps_the_name_null(
    files_on: None,
    files_client: AsyncClient,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
    real_session: AsyncSession,
    idem: Callable[[], dict[str, str]],
) -> None:
    """A principal with no row left is absent from the listing's name map, not
    invented and not an error: the row keeps its id and reports no name."""
    team = await team_service.create_subteam(
        real_session, org_team_id=files_org.org.org_id, name="Temporary"
    )
    await real_session.commit()
    node = await fx.node(b"target.txt")
    made = await files_client.post(
        _url(node),
        json={"principal": {"kind": "team", "id": str(team.id)}, "role": "reader"},
        headers=await _mutate(real_session, node, idem),
    )
    assert made.status_code == 201, made.text
    assert (await files_client.get(_url(node))).json()["value"][0]["principalName"] == "Temporary"

    await team_service.delete_team(real_session, team)
    await real_session.commit()

    rows = (await files_client.get(_url(node))).json()["value"]
    assert [(row["principal"]["id"], row["principalName"]) for row in rows] == [
        (str(team.id), None)
    ]


async def test_the_effective_listing_names_a_grant_inherited_from_a_folder(
    files_on: None,
    files_client: AsyncClient,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
    real_session: AsyncSession,
    idem: Callable[[], dict[str, str]],
) -> None:
    """The effective set is assembled from the ACL body rather than from share
    rows, so it is a second path to the name — and the one a share dialog
    actually reads, since it is what tells a direct grant from an inherited
    one."""
    folder = await fx.node(b"papers", kind="folder")
    inside = await fx.node(b"draft.txt", parent=folder)
    granted = await files_client.post(
        _url(folder),
        json={"principal": {"kind": "user", "id": str(files_org.member.id)}, "role": "reader"},
        headers=await _mutate(real_session, folder, idem),
    )
    assert granted.status_code == 201, granted.text

    listed = await files_client.get(f"{_url(inside)}?effective=true")
    assert listed.status_code == 200, listed.text
    mine = [
        row for row in listed.json()["value"] if row["principal"]["id"] == str(files_org.member.id)
    ]
    expected = f"{files_org.member.first_name} {files_org.member.last_name}".strip()
    assert [(row["principalName"], row["grantingNodeId"]) for row in mine] == [
        (expected, str(folder.id))
    ]


# --------------------------------------------------------------------------
# the principal kind is an open registry, and only what is registered is taken
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kind",
    [
        pytest.param("link", id="link"),
        pytest.param("service", id="a-kind-nobody-registered"),
        pytest.param("User", id="a-registered-kind-in-the-wrong-case"),
        pytest.param("", id="no-kind-at-all"),
    ],
)
async def test_a_grant_naming_an_unregistered_principal_kind_is_refused(
    files_on: None,
    files_client: AsyncClient,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
    real_session: AsyncSession,
    idem: Callable[[], dict[str, str]],
    kind: str,
) -> None:
    """A kind the decider never learned has no matcher, so a row carrying it
    would be a grant nothing can ever resolve — and one the database trigger
    blows up on rather than refuses. The route turns it into a typed 400
    before anything is written.
    """
    node = await fx.node(b"target.txt")
    refused = await files_client.post(
        _url(node),
        json={"principal": {"kind": kind, "id": str(files_org.member.id)}, "role": "reader"},
        headers=await _mutate(real_session, node, idem),
    )
    assert refused.status_code == 422, refused.text
    assert refused.json()["code"] == "files.unknown_principal_kind"
    assert await files_client_grants(fx, node) == [], "the refused grant landed anyway"


async def test_the_kind_gate_admits_whatever_the_decider_has_registered(
    files_on: None,
    files_client: AsyncClient,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
    real_session: AsyncSession,
    idem: Callable[[], dict[str, str]],
) -> None:
    """The allowed set is the decider's registry, not a list spelled at the
    route: a kind registered at runtime gets past the gate and is then judged
    on who it names, not on what it is.
    """
    register_principal_kind("scratch_kind", lambda principal, caller: False)
    node = await fx.node(b"target.txt")
    try:
        answered = await files_client.post(
            _url(node),
            json={
                "principal": {"kind": "scratch_kind", "id": str(files_org.member.id)},
                "role": "reader",
            },
            headers=await _mutate(real_session, node, idem),
        )
    finally:
        unregister_principal_kind("scratch_kind")
    assert answered.json()["code"] != "files.unknown_principal_kind", answered.text
    assert answered.status_code == 404, answered.text


async def test_the_unregistered_kind_refusal_never_says_whether_the_principal_exists(
    files_on: None,
    files_client: AsyncClient,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
    real_session: AsyncSession,
    idem: Callable[[], dict[str, str]],
) -> None:
    """The kind is refused for what it is, so the id it carries must not move a
    byte of the answer: a member of this org, a real user of another one and a
    uuid that was never issued come back identical, from the same work.
    """
    other_org, _ = await team_service.create_org_with_admin(
        real_session,
        org_name=f"other-{uuid.uuid4().hex[:8]}",
        admin_email=f"other-{uuid.uuid4().hex[:8]}@test.dev",
        admin_first_name="Other",
        admin_last_name="Admin",
        admin_password="other-pass-12345",
    )
    await real_session.commit()
    outsider, _ = await make_member(real_session, org_id=other_org.id, verified=True)
    await real_session.commit()
    node = await fx.node(b"target.txt")

    unissued = uuid.uuid4()
    # One refusal before the measured three: the first request of a test warms
    # caches the comparison is not about, and the property under test is that
    # the *id* moves nothing, not that a cold request costs the same as a warm
    # one.
    await probe(
        files_client,
        SYNC_ENGINE,
        "POST",
        _url(node),
        json={"principal": {"kind": "link", "id": str(uuid.uuid4())}, "role": "reader"},
        headers=await _mutate(real_session, node, idem),
    )
    named = [files_org.member.id, outsider.id, unissued]
    probes = []
    for who in named:
        quiesce_auth()
        probes.append(
            await probe(
                files_client,
                SYNC_ENGINE,
                "POST",
                _url(node),
                json={"principal": {"kind": "link", "id": str(who)}, "role": "reader"},
                headers=await _mutate(real_session, node, idem),
            )
        )

    assert probes[0].status == 422
    assert_byte_identical(probes, [str(who) for who in named])
    assert_same_work(probes)


@pytest.mark.parametrize(
    "kind",
    [
        pytest.param("user", id="user"),
        pytest.param("team", id="team"),
        pytest.param("org", id="org"),
    ],
)
async def test_every_registered_principal_kind_is_still_granted(
    files_on: None,
    files_client: AsyncClient,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
    real_session: AsyncSession,
    idem: Callable[[], dict[str, str]],
    kind: str,
) -> None:
    """The negative twin of the refusal: tightening the gate must not cost the
    three kinds a sharing dialog actually offers."""
    node = await fx.node(b"target.txt")
    principal_id = files_org.member.id if kind == "user" else files_org.org.org_id
    made = await files_client.post(
        _url(node),
        json={"principal": {"kind": kind, "id": str(principal_id)}, "role": "reader"},
        headers=await _mutate(real_session, node, idem),
    )
    assert made.status_code == 201, made.text
    assert made.json()["principal"]["kind"] == kind


async def test_concurrent_grants_under_one_if_match_land_exactly_one(
    files_on: None,
    files_client: AsyncClient,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
    real_session: AsyncSession,
    idem: Callable[[], dict[str, str]],
) -> None:
    """Five grants fired at once under the same etag: one wins, four are 412.

    Every caller is told the truth. The one 201 echoes the rung that is
    stored, and nobody else is told they granted a rung that does not exist.
    """
    node = await fx.node(b"target.txt")
    etag = await node_etag(real_session, node.id)
    roles = ["reader", "writer", "manager", "commenter", "writer"]

    async def fire(role: str) -> Response:
        return await files_client.post(
            _url(node),
            json={"principal": {"kind": "user", "id": str(files_org.member.id)}, "role": role},
            headers={**idem(), "If-Match": etag},
        )

    answers = await asyncio.gather(*(fire(role) for role in roles))
    statuses = sorted(answer.status_code for answer in answers)
    assert statuses == [201, 412, 412, 412, 412], [a.text for a in answers]
    winner = next(answer for answer in answers if answer.status_code == 201)
    assert await files_client_grants(fx, node) == [(files_org.member.id, winner.json()["role"])]


@pytest.mark.parametrize(
    "expires_at",
    [
        pytest.param(lambda now: now - timedelta(minutes=5), id="five-minutes-ago"),
        pytest.param(lambda now: now, id="exactly-now"),
        pytest.param(
            lambda now: (now - timedelta(minutes=5)).replace(tzinfo=None), id="past-no-offset"
        ),
    ],
)
async def test_a_grant_that_has_already_expired_is_422(
    files_on: None,
    files_client: AsyncClient,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
    real_session: AsyncSession,
    idem: Callable[[], dict[str, str]],
    expires_at: Callable[[datetime], datetime],
) -> None:
    node = await fx.node(b"target.txt")
    headers = await _mutate(real_session, node, idem)
    # Frozen at the real present, so the session token minted a moment ago is
    # still valid while ``exactly-now`` is exactly the server's now.
    with freeze_time(datetime.now(UTC), real_asyncio=True):
        refused = await files_client.post(
            _url(node),
            json={
                "principal": {"kind": "user", "id": str(files_org.member.id)},
                "role": "reader",
                "expiresAt": expires_at(datetime.now(UTC)).isoformat(),
            },
            headers=headers,
        )
    assert refused.status_code == 422, refused.text
    assert refused.json()["code"] == "files.expiry_in_past"
    assert await files_client_grants(fx, node) == []


async def test_a_grant_expiring_in_the_future_is_accepted(
    files_on: None,
    files_client: AsyncClient,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
    real_session: AsyncSession,
    idem: Callable[[], dict[str, str]],
) -> None:
    """The negative twin: the expiry check refuses the past, not every expiry."""
    node = await fx.node(b"target.txt")
    future = datetime.now(UTC) + timedelta(minutes=5)
    made = await files_client.post(
        _url(node),
        json={
            "principal": {"kind": "user", "id": str(files_org.member.id)},
            "role": "reader",
            "expiresAt": future.isoformat(),
        },
        headers=await _mutate(real_session, node, idem),
    )
    assert made.status_code == 201, made.text
    assert made.json()["expiresAt"] is not None


async def test_an_org_grant_naming_another_org_is_a_422(
    files_on: None,
    files_client: AsyncClient,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
    real_session: AsyncSession,
    idem: Callable[[], dict[str, str]],
) -> None:
    """An org grant has one valid id, the caller's own org. Any other id is
    the caller's input error, answered the same for every id, so it confirms
    nothing about which orgs exist."""
    node = await fx.node(b"target.txt")
    answered = await files_client.post(
        _url(node),
        json={"principal": {"kind": "org", "id": str(uuid.uuid4())}, "role": "reader"},
        headers=await _mutate(real_session, node, idem),
    )
    assert answered.status_code == 422, answered.text
    assert answered.json()["code"] == "files.org_principal_not_own"
    assert await files_client_grants(fx, node) == []


# --------------------------------------------------------------------------
# the ceiling holds the rung a change takes away, not only the one it gives
# --------------------------------------------------------------------------


async def _manager_and_owner_grantee(
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
    real_session: AsyncSession,
    node: FileNode,
    *,
    grantee_role: str,
    expires_at: datetime | None = None,
) -> tuple[uuid.UUID, uuid.UUID]:
    """The org member on the manager rung of ``node``, and a second member
    holding ``grantee_role`` there; returns the second member's id and share."""
    ben, _ = await make_member(real_session, org_id=files_org.org.org_id, verified=True)
    async with fx.repo.transaction():
        await acl.grant(
            fx.repo,
            _fixture_ctx(files_org),
            node,
            Principal(kind="user", id=files_org.member.id),
            "manager",
        )
        share = await acl.grant(
            fx.repo,
            _fixture_ctx(files_org),
            node,
            Principal(kind="user", id=ben.id),
            grantee_role,
            expires_at=expires_at,
        )
    await fx.repo.session.commit()
    return ben.id, share.id


async def test_a_manager_may_not_move_an_expiring_owner_grant_down(
    files_on: None,
    client: AsyncClient,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
    real_session: AsyncSession,
    idem: Callable[[], dict[str, str]],
) -> None:
    """A share moves a person's standing grant to the new rung, up or down. The
    ceiling was checked on the new rung only, so a manager could turn an
    owner's grant (one with an expiry, which the owner check reads past) into
    a view grant they could never have withdrawn."""
    node = await fx.node(b"target.txt")
    ben, _share = await _manager_and_owner_grantee(
        fx,
        files_org,
        real_session,
        node,
        grantee_role="owner",
        expires_at=datetime.now(UTC) + timedelta(days=3),
    )
    manager = await login(client, files_org.member.email, files_org.member_password)

    refused = await manager.post(
        _url(node),
        json={"principal": {"kind": "user", "id": str(ben)}, "role": "reader"},
        headers=await _mutate(real_session, node, idem),
    )

    assert refused.status_code == 403, refused.text
    assert refusal(refused)["code"] == "files.grant_exceeds_rung"
    assert (ben, "owner") in await files_client_grants(fx, node)


async def test_a_grant_raised_between_the_check_and_the_revoke_is_never_withdrawn(
    files_on: None,
    client: AsyncClient,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
    real_session: AsyncSession,
    idem: Callable[[], dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Revoke reads the share's rung in one transaction and withdraws it in
    another, and a role change keeps the share's id. A grant raised to owner in
    between must not be withdrawn by the manager whose check saw a writer: the
    raise moves the node's etag, so the revoke's precondition refuses it (and
    under the node lock the revoke would also judge the rung the row now
    holds)."""
    node = await fx.node(b"target.txt")
    ben, share_id = await _manager_and_owner_grantee(
        fx, files_org, real_session, node, grantee_role="writer"
    )
    sharing = __import__("backend.api.routes.files.sharing", fromlist=["authorize_node"])
    checked = sharing.authorize_node

    async def then_the_owner_raises_it(*args: Any, **kwargs: Any) -> Any:
        authorized = await checked(*args, **kwargs)
        async with fx.repo.transaction():
            await acl.grant(
                fx.repo,
                _fixture_ctx(files_org),
                node,
                Principal(kind="user", id=ben),
                "owner",
            )
        await fx.repo.session.commit()
        return authorized

    monkeypatch.setattr(sharing, "authorize_node", then_the_owner_raises_it)
    manager = await login(client, files_org.member.email, files_org.member_password)

    refused = await manager.delete(
        _url(node, f"/{share_id}"), headers=await _mutate(real_session, node, idem)
    )

    assert refused.status_code == 412, refused.text
    assert (ben, "owner") in await files_client_grants(fx, node)


class _AboveTheCeilingError(Exception):
    pass


def _ceiling(top: str) -> Callable[[str], Any]:
    order = ["reader", "commenter", "writer", "manager", "owner"]

    async def check(role: str) -> None:
        if order.index(role) > order.index(top):
            raise _AboveTheCeilingError(role)

    return check


@pytest.mark.parametrize("change", ["move-down", "revoke"])
async def test_the_rung_taken_away_is_judged_as_the_row_holds_it_under_the_lock(
    files_on: None,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
    real_session: AsyncSession,
    change: str,
) -> None:
    """With no precondition to fence a race, the library still never takes away
    a rung above the caller's: the check runs under the node lock on the rung
    the share holds at that moment, after the owner has raised it."""
    node = await fx.node(b"target.txt")
    ben, share_id = await _manager_and_owner_grantee(
        fx, files_org, real_session, node, grantee_role="writer"
    )
    async with fx.repo.transaction():
        await acl.grant(
            fx.repo, _fixture_ctx(files_org), node, Principal(kind="user", id=ben), "owner"
        )
    await fx.repo.session.commit()

    with pytest.raises(_AboveTheCeilingError):
        async with fx.repo.transaction():
            if change == "revoke":
                await acl.revoke(
                    fx.repo,
                    _fixture_ctx(files_org),
                    node,
                    share_id,
                    withdrawing=_ceiling("manager"),
                )
            else:
                await acl.grant(
                    fx.repo,
                    _fixture_ctx(files_org),
                    node,
                    Principal(kind="user", id=ben),
                    "reader",
                    replacing=_ceiling("manager"),
                )
    await fx.repo.session.rollback()
    await fx.repo.session.refresh(node)
    assert (ben, "owner") in await files_client_grants(fx, node)


async def test_the_listing_says_what_the_caller_may_do_to_each_row(
    files_on: None,
    client: AsyncClient,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
    real_session: AsyncSession,
    idem: Callable[[], dict[str, str]],
) -> None:
    """A share dialog renders the server's answer: which rungs the caller may
    hand out, and per row whether it may be moved or withdrawn. A manager may
    work a reader's grant but not an owner's, and the controls the listing
    offers are exactly the writes the route then admits."""
    node = await fx.node(b"plan.md")
    reader_holder, _ = await make_member(real_session, org_id=files_org.org.org_id, verified=True)
    owner_holder, _ = await make_member(real_session, org_id=files_org.org.org_id, verified=True)
    await real_session.commit()
    async with fx.repo.transaction():
        for who, rung in (
            (files_org.member.id, "manager"),
            (reader_holder.id, "commenter"),
            (owner_holder.id, "owner"),
        ):
            await acl.grant(
                fx.repo, _fixture_ctx(files_org), node, Principal(kind="user", id=who), rung
            )
    manager = await login(client, files_org.member.email, files_org.member_password)

    listed = await manager.get(_url(node))
    assert listed.status_code == 200, listed.text
    body = listed.json()
    assert body["assignableRoles"] == [
        {"role": "reader", "label": "Can view"},
        {"role": "writer", "label": "Can edit"},
        {"role": "manager", "label": "Full access"},
    ]
    rows = {row["principal"]["id"]: row for row in body["value"]}
    verdicts = {
        who: tuple(
            rows[str(who)][key] for key in ("shownRole", "roleLabel", "canChange", "canRemove")
        )
        for who in (reader_holder.id, owner_holder.id)
    }
    # A grant on the withdrawn commenting rung reads and is selected as Can view.
    assert verdicts == {
        reader_holder.id: ("reader", "Can view", True, True),
        owner_holder.id: ("owner", "Owner", False, False),
    }

    # The verdicts are the write's: the owner's grant is refused, the reader's taken.
    refused = await manager.delete(
        _url(node, f"/{rows[str(owner_holder.id)]['id']}"),
        headers=await _mutate(real_session, node, idem),
    )
    assert refused.status_code == 403, refused.text
    taken = await manager.delete(
        _url(node, f"/{rows[str(reader_holder.id)]['id']}"),
        headers=await _mutate(real_session, node, idem),
    )
    assert taken.status_code == 204, taken.text


async def test_a_reader_who_may_not_share_is_offered_nothing(
    files_on: None,
    client: AsyncClient,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
) -> None:
    """A writer may read the roster but hand out no rung, and no row on it is
    theirs to change, including their own."""
    node = await fx.node(b"plan.md")
    async with fx.repo.transaction():
        await acl.grant(
            fx.repo,
            _fixture_ctx(files_org),
            node,
            Principal(kind="user", id=files_org.member.id),
            "writer",
        )
    writer = await login(client, files_org.member.email, files_org.member_password)

    for listing in ("", "?effective=true"):
        body = (await writer.get(_url(node, listing))).json()
        assert body["assignableRoles"] == []
        assert {(row["canChange"], row["canRemove"]) for row in body["value"]} == {(False, False)}


async def test_the_owner_row_is_marked_and_never_offered_a_control(
    files_on: None,
    files_client: AsyncClient,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
) -> None:
    """The person who owns the item is named as such by the server, so the
    dialog never compares ids to find them: a grant they also hold reads as
    Owner and carries no control, while a colleague's grant beside it does."""
    node = await fx.node(b"plan.md", created_by=files_org.org.admin_id)
    async with fx.repo.transaction():
        for who in (files_org.org.admin_id, files_org.member.id):
            await acl.grant(
                fx.repo, _fixture_ctx(files_org), node, Principal(kind="user", id=who), "reader"
            )

    rows = (await files_client.get(_url(node))).json()["value"]
    seen = {
        row["principal"]["id"]: (
            row["isOwner"],
            row["roleLabel"],
            row["canChange"],
            row["canRemove"],
        )
        for row in rows
    }
    assert seen == {
        str(files_org.org.admin_id): (True, "Owner", False, False),
        str(files_org.member.id): (False, "Can view", True, True),
    }
