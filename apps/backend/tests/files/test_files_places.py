"""``GET /drives/{drive}/places`` — where the caller's own things are filed.

A client may never derive one of these folders from its name: the member can
rename it, move it, or have a folder of that name that is somebody else's. So
the server answers ids, and this module pins what the two shapes of the route
promise.

The bare read is a *read*: a place nobody has needed yet is ``null`` and stays
missing afterwards, because opening Files must not put folders in a drive the
member did not ask for. ``?ensure=`` is a *write*: it makes the named places,
is idempotent across two calls, and decides as the write it is — which is what
the decision rows below are for.

The refusals are the Files visibility rule: a principal with no home at all and
another org's drive id come back as the one opaque absence, with nothing about
org A on the wire and nothing about org A in the audit stream.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from _files_kit import NOT_FOUND, refusal
from alkera_core.authz.enums import CredentialKind
from alkera_core.authz.principal import ActingContext
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.files import drives
from alkera_core.models import EventOutbox
from backend.auth.dependencies import current_principal, require_verified_or_grace_or_machine
from backend.services.credentials import ci_tokens as ci_token_service
from backend.services.org import teams as team_service
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import select
from tests._suite_app import app as fastapi_app
from tests.conftest import app_client, login, make_member
from tests.files._files_kit import FilesFixtures, FilesOrgFixture

pytestmark = pytest.mark.asyncio

BASE = "/api/v1/files"
OPAQUE = NOT_FOUND
AUTHZ_TYPE = "authz.decision"
FILE_NODE = "file_node"


async def _drive_id(client: AsyncClient) -> str:
    response = await client.get(f"{BASE}/drives")
    assert response.status_code == 200, response.text
    return str(response.json()["id"])


def _places_url(drive_id: str, ensure: str | None = None) -> str:
    url = f"{BASE}/drives/{drive_id}/places"
    return f"{url}?ensure={ensure}" if ensure is not None else url


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


async def _for_node(org_id: uuid.UUID, node_id: str) -> list[dict[str, Any]]:
    return [row.payload for row in await _decisions(org_id) if row.entity_id == node_id]


async def _children(fx: FilesFixtures, parent_id: uuid.UUID) -> set[bytes]:
    """The live child names of ``parent_id``, as the library sees them."""
    async with fx.repo.transaction():
        return {bytes(child.name) for child in await fx.repo.siblings(parent_id)}


# --------------------------------------------------------------------------
# the bare read
# --------------------------------------------------------------------------


async def test_a_bare_read_answers_null_for_a_place_nobody_has_needed(
    files_client: AsyncClient, files_on: None, fx: FilesFixtures
) -> None:
    """Neither place exists for a member who has never made a chat or a
    template, and asking where they are does not make them: a page load that
    quietly created two folders would be a page load that changed the drive."""
    drive = await _drive_id(files_client)

    response = await files_client.get(_places_url(drive))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["homeId"] is not None
    assert body["chatsId"] is None
    assert body["chatTemplatesId"] is None
    assert await _children(fx, uuid.UUID(body["homeId"])) == set()


async def test_a_bare_read_names_a_place_that_already_exists(
    files_client: AsyncClient, files_on: None
) -> None:
    """Once the place is there the read answers its id — the same id the
    ensure minted, so a client that made it with one call finds it with the
    other."""
    drive = await _drive_id(files_client)
    made = await files_client.get(_places_url(drive, "chats"))
    assert made.status_code == 200, made.text

    read = await files_client.get(_places_url(drive))

    assert read.status_code == 200, read.text
    assert read.json()["chatsId"] == made.json()["chatsId"]
    assert read.json()["chatTemplatesId"] is None


# --------------------------------------------------------------------------
# ensure
# --------------------------------------------------------------------------


async def test_ensure_makes_the_named_places_under_the_callers_own_home(
    files_client: AsyncClient, files_on: None, fx: FilesFixtures
) -> None:
    """The folders are real children of the caller's home, carrying the names
    the library files a new chat and a new template under — so an object
    created later lands in the very folder the client was handed."""
    drive = await _drive_id(files_client)

    response = await files_client.get(_places_url(drive, "chats,chatTemplates"))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["chatsId"] is not None
    assert body["chatTemplatesId"] is not None
    assert await _children(fx, uuid.UUID(body["homeId"])) == {
        drives.CHATS_NAME,
        drives.CHAT_TEMPLATES_NAME,
    }


async def test_ensure_makes_only_what_it_names(
    files_client: AsyncClient, files_on: None, fx: FilesFixtures
) -> None:
    """A surface about to save a template asks for the templates place; it does
    not get a ``Chats`` folder it never mentioned as a side effect."""
    drive = await _drive_id(files_client)

    response = await files_client.get(_places_url(drive, "chatTemplates"))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["chatsId"] is None
    assert body["chatTemplatesId"] is not None
    assert await _children(fx, uuid.UUID(body["homeId"])) == {drives.CHAT_TEMPLATES_NAME}


async def test_a_second_ensure_is_the_same_two_ids_and_no_second_folder(
    files_client: AsyncClient, files_on: None, fx: FilesFixtures
) -> None:
    """Idempotent, which is what lets a client send ``ensure`` on every save
    rather than remembering whether it has sent one before."""
    drive = await _drive_id(files_client)
    first = await files_client.get(_places_url(drive, "chats,chatTemplates"))
    assert first.status_code == 200, first.text

    second = await files_client.get(_places_url(drive, "chats,chatTemplates"))

    assert second.status_code == 200, second.text
    assert second.json() == first.json()
    assert len(await _children(fx, uuid.UUID(first.json()["homeId"]))) == 2


async def test_a_place_that_is_not_one_is_refused_rather_than_ignored(
    files_client: AsyncClient, files_on: None
) -> None:
    """A client that misspells a place learns it now, instead of reading
    ``null`` forever and never knowing why its folder never appeared."""
    drive = await _drive_id(files_client)

    response = await files_client.get(_places_url(drive, "chats,chatTemplate"))

    assert response.status_code == 422, response.text
    assert response.json()["code"] == "files.unknown_place"


# --------------------------------------------------------------------------
# the decision rows
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("ensure", "action"),
    [
        pytest.param(None, "read", id="bare_read_decides_read"),
        pytest.param("chats", "write", id="ensure_decides_write"),
    ],
)
async def test_the_decision_is_on_record_naming_the_action_it_really_took(
    files_client: AsyncClient,
    files_on: None,
    files_org: FilesOrgFixture,
    ensure: str | None,
    action: str,
) -> None:
    """One row per request, on the caller's own home, naming ``read`` for the
    read and ``write`` for the call that puts a folder in that home — an
    ensure recorded as a read would be a write nobody could audit."""
    drive = await _drive_id(files_client)

    response = await files_client.get(_places_url(drive, ensure))

    assert response.status_code == 200, response.text
    payloads = await _for_node(files_org.org.org_id, response.json()["homeId"])
    assert [p["effect"] for p in payloads] == ["allow"]
    assert payloads[-1]["action"] == action
    assert payloads[-1]["policy"] == "files.access"


# --------------------------------------------------------------------------
# the refusals
# --------------------------------------------------------------------------


async def test_a_ci_token_never_reaches_the_files_surface_at_all(
    files_on: None, files_org: FilesOrgFixture
) -> None:
    """Today a machine credential is turned away one layer above this route.

    Every product router carries the email-verification gate, and that gate
    resolves a *user* — so a CI token is a 401 before any Files route reads a
    byte. Pinned here because the route below still has to answer correctly for
    a credential with no user, and the day the surface admits one this test is
    what says the two layers changed together.
    """
    async with AsyncSessionLocal() as session:
        _row, raw = await ci_token_service.mint(
            session,
            org_id=files_org.org.org_id,
            created_by_id=files_org.org.admin_id,
            label="places-ci",
        )
        await session.commit()
    async with AsyncClient(
        transport=ASGITransport(app=fastapi_app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {raw}"},
    ) as machine:
        assert (await machine.get(f"{BASE}/drives")).status_code == 401


async def test_a_principal_with_no_home_has_no_places(
    files_on: None, files_org: FilesOrgFixture
) -> None:
    """A service credential is not a member: it has no home for a place to be
    under. The answer is the same absence a stranger gets, not an empty
    document that would read as "you have none yet" and invite an ensure.

    The credential is the only thing substituted — the router, the drive, the
    decision engine and the database are the real ones — because the gate
    above refuses every real CI token before the route is reached (see the
    test above), and the branch is still the one that runs the day it does
    not.
    """
    context = ActingContext.for_service(
        token_id=uuid.uuid4(),
        org_id=files_org.org.org_id,
        label="places-ci",
        credential=CredentialKind.CI_TOKEN,
    )
    admin = files_org.org.admin_id
    fastapi_app.dependency_overrides[current_principal] = lambda: context
    fastapi_app.dependency_overrides[require_verified_or_grace_or_machine] = lambda: admin
    try:
        async with AsyncClient(
            transport=ASGITransport(app=fastapi_app), base_url="http://test"
        ) as machine:
            drive = await _drive_id(machine)
            assert machine.cookies.get("alkera_session") is None

            response = await machine.get(_places_url(drive))
    finally:
        fastapi_app.dependency_overrides.pop(current_principal, None)
        fastapi_app.dependency_overrides.pop(require_verified_or_grace_or_machine, None)

    assert response.status_code == 404, response.text
    assert refusal(response) == OPAQUE


async def test_org_b_aimed_at_org_as_drive_learns_nothing_on_the_wire_or_on_record(
    client: AsyncClient, files_client: AsyncClient, files_on: None, files_org: FilesOrgFixture
) -> None:
    """Another org's drive id is the opaque absence, byte-identical to a drive
    id that names nothing anywhere — and neither request leaves a row in org
    A's audit stream, which would be the same oracle one step sideways."""
    victim_drive = await _drive_id(files_client)
    before = len(await _decisions(files_org.org.org_id))

    async with AsyncSessionLocal() as session:
        org_b, _admin_b = await team_service.create_org_with_admin(
            session,
            org_name=f"Org B {uuid.uuid4().hex[:6]}",
            admin_email=f"b-admin-{uuid.uuid4().hex[:8]}@alkera.dev",
            admin_first_name="Bee",
            admin_last_name="Admin",
            admin_password="b-admin-pass-12345",
        )
        await session.commit()
        member_b, member_password = await make_member(session, org_id=org_b.id, verified=True)
        await session.commit()
    stranger = await login(
        app_client(),
        member_b.email,
        member_password or "",
    )
    try:
        # Org B's own drive first, so a later 404 cannot be this credential
        # being refused everywhere rather than this id being refused here.
        assert (await stranger.get(_places_url(await _drive_id(stranger)))).status_code == 200

        aimed = await stranger.get(_places_url(victim_drive))
        nowhere = await stranger.get(_places_url(str(uuid.uuid4())))
    finally:
        await stranger.aclose()

    assert (aimed.status_code, refusal(aimed)) == (404, OPAQUE)
    # Byte for byte but for the trace id, which every answer carries its own of.
    assert (aimed.status_code, _untraced(aimed)) == (nowhere.status_code, _untraced(nowhere))
    assert victim_drive not in aimed.text
    assert len(await _decisions(files_org.org.org_id)) == before


def _untraced(response: Response) -> str:
    trace_id = response.headers["x-trace-id"]
    assert trace_id in response.text
    return response.text.replace(trace_id, "<trace-id>")
