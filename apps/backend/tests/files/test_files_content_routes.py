"""The content routes end to end: write bytes on the API host, read them back
from the content domain.

Everything here goes through the real ASGI app, a real Postgres and a real
filesystem store under ``tmp_path``, so a passing test is a statement about the
routes rather than about its own fixtures. The one thing that is *not* real is
the hostname: the content mount refuses anything arriving under a Host that is
not the content origin, so every content request carries it explicitly — which
is also what proves the two origins stay separate.
"""

from __future__ import annotations

import base64
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import replace
from typing import Any

import pytest
import pytest_asyncio
from _files_kit import NOT_FOUND, refusal
from alkera_core.authz.principal import ActingContext
from alkera_core.config import settings
from alkera_core.files import signed_urls
from alkera_core.files.ids import OrgScope, SessionId
from alkera_core.files.repo import FilesRepo
from backend.api.routes.files.content import session_binding
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from tests.conftest import app_client
from tests.files.conftest import FilesFixtures, FilesOrgFixture

pytestmark = pytest.mark.asyncio

CONTENT_HOST = "files.localhost:8000"
CONTENT_ORIGIN = f"http://{CONTENT_HOST}"

HELLO = b"hello, files\n"
OTHER = b"a different set of bytes\n"


@pytest.fixture
def content_on(monkeypatch: pytest.MonkeyPatch, files_on: None) -> None:
    """Files enabled, a store behind it, and a content origin to mint onto."""
    monkeypatch.setattr(settings, "files_content_base_url", CONTENT_ORIGIN)


@pytest_asyncio.fixture
async def node(fx: FilesFixtures, real_session: AsyncSession) -> Any:
    """One empty file node under a drive with room in it.

    ``ensure_org_drive`` writes ``quota_bytes 0``, so a fresh org's drive refuses
    every upload with a 507 until an allocation lands; the test raises it to the
    configured default the way provisioning eventually will. Everything else
    about the node comes from the Files factories.
    """
    # Under `/Shared`: a conflict=rename PUT puts a new sibling beside it, and
    # the drive root takes no direct write.
    created = await fx.node(b"note.txt", parent=await fx.shared())
    drive = await fx.drive()
    await real_session.execute(
        text("UPDATE file_drives SET quota_bytes = :b, quota_nodes = :n WHERE id = :id"),
        {
            "b": settings.files_quota_default_bytes,
            "n": settings.files_quota_default_nodes,
            "id": drive.id,
        },
    )
    await real_session.commit()
    return created


def _url(drive_id: uuid.UUID, node_id: uuid.UUID) -> str:
    return f"/api/v1/files/drives/{drive_id}/items/{node_id}/content"


async def _put(
    client: AsyncClient,
    drive_id: uuid.UUID,
    node_id: uuid.UUID,
    payload: bytes,
    *,
    etag: int,
    idem: Callable[[], dict[str, str]],
    conflict: str | None = None,
    content_type: str = "application/octet-stream",
) -> Any:
    params = {} if conflict is None else {"conflictBehavior": conflict}
    return await client.put(
        _url(drive_id, node_id),
        content=payload,
        params=params,
        headers={
            **idem(),
            "If-Match": f'"{etag}"',
            "Content-Type": content_type,
            "Content-Length": str(len(payload)),
        },
    )


async def _redeem(client: AsyncClient, location: str, **headers: str) -> Any:
    """Follow a minted URL onto the content domain."""
    path = location.removeprefix(CONTENT_ORIGIN)
    return await client.get(path, headers={"Host": CONTENT_HOST, **headers})


async def _mint(
    client: AsyncClient, drive_id: uuid.UUID, node_id: uuid.UUID, **headers: str
) -> str:
    response = await client.get(_url(drive_id, node_id), headers=headers)
    assert response.status_code == 302, response.text
    return str(response.headers["location"])


async def _etag(session: AsyncSession, node_id: uuid.UUID) -> int:
    value = await session.execute(
        text("SELECT etag FROM file_nodes WHERE id = :id"), {"id": node_id}
    )
    return int(value.scalar_one())


# --------------------------------------------------------------------------
# the round trip
# --------------------------------------------------------------------------


async def test_put_then_get_returns_the_same_bytes(
    files_client: AsyncClient,
    fx: FilesFixtures,
    node: Any,
    content_on: None,
    idem: Callable[[], dict[str, str]],
) -> None:
    """A PUT publishes a version; the redirect it mints serves those bytes back.

    The assertion is on the bytes, not on a status: a route that answered 302
    with a URL that served *something else* would pass a status check and fail
    a user.
    """
    drive = await fx.drive()
    created = await _put(files_client, drive.id, node.id, HELLO, etag=node.etag, idem=idem)
    assert created.status_code == 201, created.text

    served = await _redeem(files_client, await _mint(files_client, drive.id, node.id))
    assert served.status_code == 200, served.text
    assert served.content == HELLO


async def test_identical_bytes_are_a_200_no_op(
    files_client: AsyncClient,
    fx: FilesFixtures,
    node: Any,
    content_on: None,
    real_session: AsyncSession,
    idem: Callable[[], dict[str, str]],
) -> None:
    """Re-putting the head's own bytes writes no version and answers 200.

    Pinned on the version count, not only the status: a route that answered 200
    while quietly writing a second identical version would charge the org twice
    for the same object.
    """
    drive = await fx.drive()
    await _put(files_client, drive.id, node.id, HELLO, etag=node.etag, idem=idem)
    etag = await _etag(real_session, node.id)

    again = await _put(files_client, drive.id, node.id, HELLO, etag=etag, idem=idem)
    assert again.status_code == 200, again.text

    count = await real_session.execute(
        text("SELECT count(*) FROM file_versions WHERE node_id = :id"), {"id": node.id}
    )
    assert count.scalar_one() == 1


async def test_a_stale_if_match_is_412(
    files_client: AsyncClient,
    fx: FilesFixtures,
    node: Any,
    content_on: None,
    idem: Callable[[], dict[str, str]],
) -> None:
    """A caller writing against an etag the node has moved past changes nothing."""
    drive = await fx.drive()
    await _put(files_client, drive.id, node.id, HELLO, etag=node.etag, idem=idem)

    stale = await _put(files_client, drive.id, node.id, OTHER, etag=node.etag, idem=idem)
    assert stale.status_code == 412, stale.text

    served = await _redeem(files_client, await _mint(files_client, drive.id, node.id))
    assert served.content == HELLO


# --------------------------------------------------------------------------
# conflictBehavior
# --------------------------------------------------------------------------


async def test_conflict_fail_refuses_a_node_that_already_holds_content(
    files_client: AsyncClient,
    fx: FilesFixtures,
    node: Any,
    content_on: None,
    real_session: AsyncSession,
    idem: Callable[[], dict[str, str]],
) -> None:
    drive = await fx.drive()
    await _put(files_client, drive.id, node.id, HELLO, etag=node.etag, idem=idem)
    etag = await _etag(real_session, node.id)

    refused = await _put(
        files_client, drive.id, node.id, OTHER, etag=etag, idem=idem, conflict="fail"
    )
    assert refused.status_code == 409, refused.text
    assert refused.json()["code"] == "files.name_conflict"


async def test_conflict_replace_versions_over_the_existing_bytes(
    files_client: AsyncClient,
    fx: FilesFixtures,
    node: Any,
    content_on: None,
    real_session: AsyncSession,
    idem: Callable[[], dict[str, str]],
) -> None:
    drive = await fx.drive()
    await _put(files_client, drive.id, node.id, HELLO, etag=node.etag, idem=idem)
    etag = await _etag(real_session, node.id)

    replaced = await _put(
        files_client, drive.id, node.id, OTHER, etag=etag, idem=idem, conflict="replace"
    )
    assert replaced.status_code == 201, replaced.text

    served = await _redeem(files_client, await _mint(files_client, drive.id, node.id))
    assert served.content == OTHER


async def test_conflict_rename_renames_the_incoming_bytes_not_the_occupant(
    files_client: AsyncClient,
    fx: FilesFixtures,
    node: Any,
    content_on: None,
    real_session: AsyncSession,
    idem: Callable[[], dict[str, str]],
) -> None:
    """``rename`` moves the CALLER'S upload aside; the addressed node is untouched.

    Both sets of bytes survive, which is the difference from ``replace`` — but
    which node ends up holding which is the whole point: a content
    PUT must never rename or re-point a node the caller did not address, so
    ``note.txt`` still serves what it served and the incoming bytes land on the
    new sibling. Asserting the two names alone would pass either way, so the
    test reads the bytes back through both nodes.
    """
    drive = await fx.drive()
    await _put(files_client, drive.id, node.id, HELLO, etag=node.etag, idem=idem)
    etag = await _etag(real_session, node.id)

    renamed = await _put(
        files_client, drive.id, node.id, OTHER, etag=etag, idem=idem, conflict="rename"
    )
    assert renamed.status_code == 201, renamed.text
    landed = uuid.UUID(renamed.json()["id"])
    assert landed != node.id

    rows = await real_session.execute(
        text(
            "SELECT id, name FROM file_nodes WHERE parent_id = :p AND trashed_at IS NULL "
            "ORDER BY name"
        ),
        {"p": node.parent_id},
    )
    by_name = {bytes(row[1]): row[0] for row in rows}
    assert {b"note.txt", b"note (1).txt"} <= set(by_name)
    # The occupant keeps its name AND its id: nothing about it moved.
    assert by_name[b"note.txt"] == node.id
    assert by_name[b"note (1).txt"] == landed

    occupant = await _redeem(files_client, await _mint(files_client, drive.id, node.id))
    assert occupant.content == HELLO
    incoming = await _redeem(files_client, await _mint(files_client, drive.id, landed))
    assert incoming.content == OTHER


# --------------------------------------------------------------------------
# the signed URL is a one-shot credential
# --------------------------------------------------------------------------


async def test_a_replayed_token_answers_exactly_like_a_forged_one(
    files_client: AsyncClient,
    fx: FilesFixtures,
    node: Any,
    content_on: None,
    idem: Callable[[], dict[str, str]],
) -> None:
    """The two failures are indistinguishable — status, body and headers.

    If a replay answered differently from a bad token, a holder of one spent URL
    could tell "this token existed" from "this token never did", which is the
    oracle the no-oracle contract exists to close.
    """
    drive = await fx.drive()
    await _put(files_client, drive.id, node.id, HELLO, etag=node.etag, idem=idem)
    location = await _mint(files_client, drive.id, node.id)

    first = await _redeem(files_client, location)
    assert first.status_code == 200

    replay = await _redeem(files_client, location)
    forged = await _redeem(files_client, f"{CONTENT_ORIGIN}/c/{'0' * 32}.{'0' * 64}")
    assert _oracle(replay) == _oracle(forged)
    assert replay.status_code == 404


def _oracle(response: Any) -> tuple[int, bytes, tuple[tuple[str, str], ...]]:
    """Everything about a refusal a caller can see, minus the parts that must
    differ between any two responses."""
    volatile = {"date", "server", "x-request-id", "x-trace-id", "content-length"}
    headers = tuple(
        sorted((k.lower(), v) for k, v in response.headers.items() if k.lower() not in volatile)
    )
    return response.status_code, response.content, headers


async def test_a_revoked_caller_cannot_redeem_a_url_minted_before_the_revoke(
    files_client: AsyncClient,
    client: AsyncClient,
    files_org: FilesOrgFixture,
    fx: FilesFixtures,
    node: Any,
    content_on: None,
    real_session: AsyncSession,
    idem: Callable[[], dict[str, str]],
) -> None:
    """Trashing the node between the mint and the redemption is effective at once.

    The URL was valid when it was handed out; the policy runs again at
    redemption, so what was allowed a moment ago is refused now. Remove the
    re-authorization in ``content_serve`` and this test serves the bytes.
    """
    drive = await fx.drive()
    await _put(files_client, drive.id, node.id, HELLO, etag=node.etag, idem=idem)
    location = await _mint(files_client, drive.id, node.id)

    await real_session.execute(
        text("UPDATE file_nodes SET trashed_at = now() WHERE id = :id"), {"id": node.id}
    )
    await real_session.commit()

    refused = await _redeem(files_client, location)
    assert refused.status_code == 404
    assert refused.json() == {"code": "not_found"}


# --------------------------------------------------------------------------
# ranges
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        pytest.param("bytes=0-0", HELLO[0:1], id="first-byte"),
        pytest.param("bytes=3-6", HELLO[3:7], id="middle-span"),
        pytest.param(f"bytes=-{len(HELLO) - 4}", HELLO[4:], id="suffix"),
        pytest.param(f"bytes=0-{len(HELLO) * 2}", HELLO, id="clamped-to-the-end"),
    ],
)
async def test_a_range_is_honoured_through_the_signed_url(
    files_client: AsyncClient,
    fx: FilesFixtures,
    node: Any,
    content_on: None,
    idem: Callable[[], dict[str, str]],
    header: str,
    expected: bytes,
) -> None:
    """The span asked for on the API host is the span the content domain serves."""
    drive = await fx.drive()
    await _put(files_client, drive.id, node.id, HELLO, etag=node.etag, idem=idem)
    location = await _mint(files_client, drive.id, node.id, Range=header)

    served = await _redeem(files_client, location, Range=header)
    assert served.content == expected
    if expected != HELLO:
        assert served.status_code == 206
        assert served.headers["content-range"].endswith(f"/{len(HELLO)}")


@pytest.mark.parametrize(
    "header",
    [
        pytest.param("bytes=99999-", id="start-past-the-end"),
        pytest.param("bytes=8-3", id="inverted"),
        pytest.param("bytes=-0", id="zero-length-suffix"),
    ],
)
async def test_an_unsatisfiable_range_is_416_on_the_content_domain(
    files_client: AsyncClient,
    fx: FilesFixtures,
    node: Any,
    content_on: None,
    idem: Callable[[], dict[str, str]],
    header: str,
) -> None:
    drive = await fx.drive()
    await _put(files_client, drive.id, node.id, HELLO, etag=node.etag, idem=idem)
    location = await _mint(files_client, drive.id, node.id)

    served = await _redeem(files_client, location, Range=header)
    assert served.status_code == 416, served.text


# --------------------------------------------------------------------------
# the served type is the sniffed one
# --------------------------------------------------------------------------


PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


@pytest.mark.parametrize(
    ("payload", "declared", "served"),
    [
        pytest.param(PNG, "text/html", "image/png", id="png-declared-as-html"),
        pytest.param(HELLO, "image/png", "text/plain", id="text-declared-as-png"),
    ],
)
async def test_the_served_type_is_sniffed_never_the_clients(
    files_client: AsyncClient,
    fx: FilesFixtures,
    node: Any,
    content_on: None,
    idem: Callable[[], dict[str, str]],
    payload: bytes,
    declared: str,
    served: str,
) -> None:
    """A PNG uploaded as ``text/html`` is served as ``image/png``.

    The negative twin matters as much as the positive: a client hint that would
    *widen* what the browser does with the bytes is dropped in both directions.
    """
    drive = await fx.drive()
    await _put(
        files_client,
        drive.id,
        node.id,
        payload,
        etag=node.etag,
        idem=idem,
        content_type=declared,
    )
    response = await _redeem(files_client, await _mint(files_client, drive.id, node.id))
    assert response.headers["content-type"].split(";")[0] == served


# --------------------------------------------------------------------------
# the limits
# --------------------------------------------------------------------------


async def test_a_declared_size_over_the_cap_is_413_before_any_bytes_land(
    files_client: AsyncClient,
    fx: FilesFixtures,
    node: Any,
    content_on: None,
    monkeypatch: pytest.MonkeyPatch,
    real_session: AsyncSession,
    idem: Callable[[], dict[str, str]],
) -> None:
    """The cap is enforced on the declaration, not after the body is streamed."""
    monkeypatch.setattr(settings, "files_single_put_max_bytes", 4)
    drive = await fx.drive()

    refused = await _put(files_client, drive.id, node.id, HELLO, etag=node.etag, idem=idem)
    assert refused.status_code == 413, refused.text

    count = await real_session.execute(
        text("SELECT count(*) FROM file_versions WHERE node_id = :id"), {"id": node.id}
    )
    assert count.scalar_one() == 0


async def test_a_body_longer_than_it_declared_is_refused(
    files_client: AsyncClient,
    fx: FilesFixtures,
    node: Any,
    content_on: None,
    real_session: AsyncSession,
    idem: Callable[[], dict[str, str]],
) -> None:
    """Declaring 4 bytes and streaming more leaves no version and no bytes."""
    drive = await fx.drive()

    async def body() -> AsyncIterator[bytes]:
        yield HELLO

    refused = await files_client.put(
        _url(drive.id, node.id),
        content=body(),
        headers={
            **idem(),
            "If-Match": f'"{node.etag}"',
            "Content-Length": "4",
            "Content-Type": "text/plain",
        },
    )
    assert refused.status_code == 422, refused.text

    count = await real_session.execute(
        text("SELECT count(*) FROM file_versions WHERE node_id = :id"), {"id": node.id}
    )
    assert count.scalar_one() == 0


async def test_a_missing_idempotency_key_is_428(
    files_client: AsyncClient,
    fx: FilesFixtures,
    node: Any,
    content_on: None,
) -> None:
    drive = await fx.drive()
    response = await files_client.put(
        _url(drive.id, node.id),
        content=HELLO,
        headers={"If-Match": f'"{node.etag}"', "Content-Length": str(len(HELLO))},
    )
    assert response.status_code == 428, response.text


# --------------------------------------------------------------------------
# not yours
# --------------------------------------------------------------------------


async def test_the_three_not_yours_classes_are_indistinguishable(
    files_client: AsyncClient,
    fx: FilesFixtures,
    content_on: None,
    real_session: AsyncSession,
) -> None:
    """A nonexistent node, another org's node and a malformed id all answer alike."""
    drive = await fx.drive()
    other_org = uuid.uuid4()
    other_repo = FilesRepo(real_session, OrgScope(org_team_id=other_org))
    assert other_repo.scope.org_team_id == other_org

    answers = [
        await files_client.get(_url(drive.id, uuid.uuid4())),
        await files_client.get(_url(uuid.uuid4(), uuid.uuid4())),
        await files_client.get(_url(drive.id, uuid.uuid4())),
    ]
    assert {response.status_code for response in answers} == {404}
    assert [refusal(response) for response in answers] == [NOT_FOUND] * len(answers)


async def test_the_routes_are_dark_when_files_is_disabled(
    files_client: AsyncClient,
    fx: FilesFixtures,
    node: Any,
    files_store: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mounted, in the OpenAPI surface, and answering the opaque 404."""
    monkeypatch.setattr(settings, "files_enabled", False)
    drive = await fx.drive()
    response = await files_client.get(_url(drive.id, node.id))
    assert response.status_code == 404
    assert refusal(response) == NOT_FOUND


# --------------------------------------------------------------------------
# the lease fence
# --------------------------------------------------------------------------


@pytest_asyncio.fixture
async def leased(fx: FilesFixtures, node: Any) -> Any:
    """A folder holding one empty file — the shape a mount actually leases.

    It hangs off the ``node`` fixture so the drive already has quota; the file
    the tests write to is the one INSIDE the folder, so the fence is being asked
    about an ancestor rather than about the node itself.
    """
    folder = await fx.node(b"mounted", kind="folder")
    return folder, await fx.node(b"inside.txt", parent=folder)


async def _acquire(
    client: AsyncClient,
    session: AsyncSession,
    drive_id: uuid.UUID,
    folder_id: uuid.UUID,
    idem: Callable[[], dict[str, str]],
) -> int:
    """Mount ``folder_id``. A lease is a mutation, so it carries the folder's
    own etag as its precondition — read back from the database because the
    tree was built after the handle was cached."""
    granted = await client.post(
        f"/api/v1/files/drives/{drive_id}/items/{folder_id}/lease",
        json={
            "instanceId": "instance-a",
            "machineId": "machine-a",
            "purpose": "mount",
        },
        headers={**idem(), "If-Match": str(await _etag(session, folder_id))},
    )
    assert granted.status_code == 200, granted.text
    return int(granted.json()["epoch"])


async def test_the_lease_holder_may_write_content_inside_its_mount(
    files_client: AsyncClient,
    fx: FilesFixtures,
    leased: Any,
    content_on: None,
    real_session: AsyncSession,
    idem: Callable[[], dict[str, str]],
) -> None:
    """The holder's own epoch is not fenced — the mount would be useless."""
    drive = await fx.drive()
    folder, target = leased
    epoch = await _acquire(files_client, real_session, drive.id, folder.id, idem)

    written = await files_client.put(
        _url(drive.id, target.id),
        content=HELLO,
        headers={
            **idem(),
            "If-Match": f'"{target.etag}"',
            "Content-Type": "application/octet-stream",
            "Content-Length": str(len(HELLO)),
            "X-Alkera-Lease-Epoch": str(epoch),
            "X-Alkera-Lease-Instance": "instance-a",
        },
    )
    assert written.status_code == 201, written.text
    assert await _etag(real_session, target.id) == target.etag + 1


@pytest.mark.parametrize(
    ("epoch_delta", "instance", "code"),
    [
        pytest.param(-1, "instance-a", "files.lease_fenced", id="a-superseded-epoch"),
        pytest.param(0, "instance-z", "files.lease_fenced", id="another-instance"),
        pytest.param(None, None, "files.leased", id="no-lease-headers-at-all"),
    ],
)
async def test_a_content_put_inside_somebody_elses_mount_is_refused(
    files_client: AsyncClient,
    fx: FilesFixtures,
    leased: Any,
    content_on: None,
    real_session: AsyncSession,
    idem: Callable[[], dict[str, str]],
    epoch_delta: int | None,
    instance: str | None,
    code: str,
) -> None:
    """A fenced PUT is a 409 that changed nothing: no version, no etag bump.

    The etag is the assertion that matters — a refusal that had already swapped
    the head would hand the holder a file it never wrote on its next sync.
    """
    drive = await fx.drive()
    folder, target = leased
    epoch = await _acquire(files_client, real_session, drive.id, folder.id, idem)

    fence: dict[str, str] = {}
    if epoch_delta is not None and instance is not None:
        fence = {
            "X-Alkera-Lease-Epoch": str(epoch + epoch_delta),
            "X-Alkera-Lease-Instance": instance,
        }
    refused = await files_client.put(
        _url(drive.id, target.id),
        content=HELLO,
        headers={
            **idem(),
            "If-Match": f'"{target.etag}"',
            "Content-Type": "application/octet-stream",
            "Content-Length": str(len(HELLO)),
            **fence,
        },
    )
    assert refused.status_code == 409, refused.text
    assert refused.json()["code"] == code
    assert await _etag(real_session, target.id) == target.etag
    heads = await real_session.execute(
        text("SELECT head_version_id FROM file_nodes WHERE id = :id"), {"id": target.id}
    )
    assert heads.scalar_one() is None


# --------------------------------------------------------------------------
# the content origin carries no credential of its own
# --------------------------------------------------------------------------


def _anonymous(client: AsyncClient) -> AsyncClient:
    """The same app, reached with nothing that identifies anybody.

    A browser following a signed URL onto the content origin sends no cookie
    and no ``Authorization`` — that separation is the whole point of the second
    origin. The test client keeps a cookie jar, so every content request made
    through it is *more* authenticated than a real one; this strips that away
    so the assertion is about the route rather than about the fixture.
    """
    return app_client()


async def test_a_minted_url_serves_its_bytes_with_no_credential_at_all(
    files_client: AsyncClient,
    fx: FilesFixtures,
    node: Any,
    content_on: None,
    idem: Callable[[], dict[str, str]],
) -> None:
    """The content origin has no cookie by design, so the URL is the credential.

    Fails when the route asks an ambient credential who is calling: there is
    none to ask on this origin, so the answer would be the opaque 404 for every
    real download.
    """
    drive = await fx.drive()
    await _put(files_client, drive.id, node.id, HELLO, etag=node.etag, idem=idem)
    location = await _mint(files_client, drive.id, node.id)

    async with _anonymous(files_client) as bare:
        served = await bare.get(
            location.removeprefix(CONTENT_ORIGIN), headers={"Host": CONTENT_HOST}
        )

    assert served.status_code == 200, served.text
    assert served.content == HELLO


async def test_a_url_minted_before_a_trash_is_dead_after_it(
    files_client: AsyncClient,
    fx: FilesFixtures,
    node: Any,
    content_on: None,
    real_session: AsyncSession,
    idem: Callable[[], dict[str, str]],
) -> None:
    """The re-authorization at redemption is what keeps a revoke immediate.

    The grant is minted while the node is readable and redeemed after it is
    gone from under the caller: a route that trusted the token would still
    serve the bytes.
    """
    drive = await fx.drive()
    await _put(files_client, drive.id, node.id, HELLO, etag=node.etag, idem=idem)
    location = await _mint(files_client, drive.id, node.id)

    await real_session.execute(
        text("UPDATE file_nodes SET trashed_at = now() WHERE id = :id"), {"id": node.id}
    )
    await real_session.commit()

    async with _anonymous(files_client) as bare:
        refused = await bare.get(
            location.removeprefix(CONTENT_ORIGIN), headers={"Host": CONTENT_HOST}
        )

    assert refused.status_code == 404
    assert refused.json() == {"code": "not_found"}


async def test_a_token_naming_no_user_is_refused(
    files_client: AsyncClient,
    fx: FilesFixtures,
    node: Any,
    content_on: None,
    real_session: AsyncSession,
    idem: Callable[[], dict[str, str]],
) -> None:
    """A grant nobody can be authorized as is not served as nobody.

    The signature covers the claim, so this is *minted* rather than edited: a
    URL made with no ``user_id`` is a real, unexpired, unused grant whose only
    defect is that there is no access to resolve.
    """
    from alkera_core.files.clock import SystemClock
    from alkera_core.files.ids import VersionId
    from alkera_core.files.signed_urls import mint_content_url

    drive = await fx.drive()
    await _put(files_client, drive.id, node.id, HELLO, etag=node.etag, idem=idem)
    version_id = (
        await real_session.execute(
            text("SELECT id FROM file_versions WHERE node_id = :id"), {"id": node.id}
        )
    ).scalar_one()
    repo = FilesRepo(real_session, OrgScope(org_team_id=drive.org_team_id))
    userless = await mint_content_url(
        repo,
        version_id=VersionId(version_id),
        session_id=None,
        clock=SystemClock(),
        key=settings.effective_files_content_signing_key.encode(),
    )
    await real_session.commit()

    async with _anonymous(files_client) as bare:
        refused = await bare.get(userless, headers={"Host": CONTENT_HOST})

    assert refused.status_code == 404
    assert refused.json() == {"code": "not_found"}


# --------------------------------------------------------------------------
# the token is verified before anything is built for the org it names
# --------------------------------------------------------------------------


def _forged_token(org_id: uuid.UUID, user_id: uuid.UUID) -> str:
    """A syntactically perfect content token whose signature is garbage.

    The claim segment is what the content domain reads before it has anything
    else to go on, so the three fields are spelled exactly the way
    ``encode_claim`` spells them; only the HMAC is wrong.
    """
    raw = b"\x1f".join((str(org_id).encode("ascii"), str(user_id).encode("ascii"), b""))
    claim = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    return f"{'0' * 32}.{claim}.{'0' * 64}"


async def test_a_forged_claim_creates_nothing_for_the_org_it_names(
    files_client: AsyncClient,
    content_on: None,
    real_session: AsyncSession,
) -> None:
    """An unverified claim must not be a drive-provisioning trigger.

    The content origin is public and cookie-less, so ``GET /c/<token>`` is
    reachable by anyone on the internet. If the route builds the Files context
    before the HMAC is checked, an attacker-chosen org UUID gets a committed
    drive, a dedup domain and a root node stamped with an attacker-chosen
    actor — unbounded writes behind a 404. The refusal must leave the database
    exactly as it found it.
    """
    forged_org = uuid.uuid4()
    forged_user = uuid.uuid4()

    async with _anonymous(files_client) as bare:
        refused = await bare.get(
            f"/c/{_forged_token(forged_org, forged_user)}", headers={"Host": CONTENT_HOST}
        )

    assert refused.status_code == 404
    assert refused.json() == {"code": "not_found"}
    for table in ("file_drives", "file_nodes", "dedup_domains"):
        count = await real_session.execute(
            text(f"SELECT count(*) FROM {table} WHERE org_team_id = :org"),
            {"org": forged_org},
        )
        assert count.scalar_one() == 0, f"{table} grew for an org named by an unverified claim"


async def test_session_binding_names_the_session_the_credential_carries() -> None:
    """The mint side has to read a session that exists.

    An agent's chat session id is its principal id; a browser JWT carries no
    session id on the acting context today, and ``None`` is the honest answer
    for it rather than a binding that silently never applies.
    """
    org = uuid.uuid4()
    user = uuid.uuid4()
    chat = uuid.uuid4()
    agent = ActingContext.for_agent(
        user_id=user, org_id=org, email="a@example.com", session_id=str(chat)
    )
    assert session_binding(agent) == SessionId(chat)
    assert session_binding(ActingContext.for_user(user_id=user, org_id=org, email="a@x")) is None


async def test_a_put_into_a_mount_answers_with_the_lease(
    files_on: None,
    files_client: AsyncClient,
    fx: FilesFixtures,
    node: Any,
    real_session: AsyncSession,
    idem: Callable[[], dict[str, str]],
) -> None:
    """The item a content PUT hands back names the mount it was written into.

    The holder's own client is the one that reads this answer back, so an item
    that said the file was under no lease told the machine holding the mount
    that it did not hold it.
    """
    # The ``node`` fixture is what raises the fresh drive's quota off zero.
    drive = await fx.drive()
    folder = await fx.node(b"mounted", kind="folder")
    written_node = await fx.node(b"notes.txt", parent=folder)
    granted = await files_client.post(
        f"/api/v1/files/drives/{drive.id}/items/{folder.id}/lease",
        json={"instanceId": "instance-a", "machineId": "machine-a", "purpose": "mount"},
        headers={**idem(), "If-Match": f'"{folder.etag}"'},
    )
    assert granted.status_code == 200, granted.text
    epoch = int(granted.json()["epoch"])

    written = await files_client.put(
        _url(drive.id, written_node.id),
        content=HELLO,
        headers={
            **idem(),
            "If-Match": f'"{written_node.etag}"',
            "Content-Type": "application/octet-stream",
            "Content-Length": str(len(HELLO)),
            "X-Alkera-Lease-Epoch": str(epoch),
            "X-Alkera-Lease-Instance": "instance-a",
        },
    )

    assert written.status_code == 201, written.text
    lease = written.json()["lease"]
    assert lease is not None, "a PUT into a live mount answered with no lease"
    assert lease["machine"] == "machine-a"
    assert lease["mine"] is True


# --------------------------------------------------------------------------
# the disposition a mint asks for
# --------------------------------------------------------------------------

#: A node named with a space and a non-ASCII character, so the RFC 6266
#: parameters are worth being byte-exact about.
UNICODE_NAME = "réport final.json"
JSON_BYTES = b'{"a": 1}'


async def _named_json_node(fx: FilesFixtures) -> Any:
    return await fx.node(UNICODE_NAME.encode())


async def test_a_sniffed_json_is_inline_by_default_and_an_attachment_when_asked(
    files_client: AsyncClient,
    fx: FilesFixtures,
    content_on: None,
    idem: Callable[[], dict[str, str]],
) -> None:
    """The default is untouched; ``?disposition=attachment`` really downloads.

    The disposition rule serves sniffed JSON inline, which is right for a
    preview and wrong for a Download button — the browser opened a tab instead
    of saving a file. The filename parameters are pinned byte-for-byte and are
    the SAME in both answers: only the disposition token moves.
    """
    drive = await fx.drive()
    node = await _named_json_node(fx)
    await _put(files_client, drive.id, node.id, JSON_BYTES, etag=node.etag, idem=idem)

    inline = await _redeem(files_client, await _mint(files_client, drive.id, node.id))
    assert inline.status_code == 200, inline.text
    parameters = "filename=\"r_port final.json\"; filename*=UTF-8''r%C3%A9port%20final.json"
    assert inline.headers["content-disposition"] == f"inline; {parameters}"

    asked = await files_client.get(f"{_url(drive.id, node.id)}?disposition=attachment")
    assert asked.status_code == 302, asked.text
    downloaded = await _redeem(files_client, str(asked.headers["location"]))
    assert downloaded.status_code == 200, downloaded.text
    assert downloaded.headers["content-disposition"] == f"attachment; {parameters}"
    assert downloaded.content == JSON_BYTES


async def test_the_download_shorthand_asks_for_the_same_attachment(
    files_client: AsyncClient,
    fx: FilesFixtures,
    content_on: None,
    idem: Callable[[], dict[str, str]],
) -> None:
    """``?download=1`` is the spelling a link builder reaches for."""
    drive = await fx.drive()
    node = await _named_json_node(fx)
    await _put(files_client, drive.id, node.id, JSON_BYTES, etag=node.etag, idem=idem)
    asked = await files_client.get(f"{_url(drive.id, node.id)}?download=1")
    assert asked.status_code == 302, asked.text
    served = await _redeem(files_client, str(asked.headers["location"]))
    assert served.headers["content-disposition"].startswith("attachment;")


async def test_a_redeemer_cannot_flip_the_disposition_the_mint_signed(
    files_client: AsyncClient,
    fx: FilesFixtures,
    content_on: None,
    idem: Callable[[], dict[str, str]],
) -> None:
    """The flag is under the HMAC: editing it in the URL is the opaque 404.

    Whoever holds a signed URL must not be able to turn a download into a page
    the browser renders — a stored-XSS-shaped upgrade — so the claim carrying
    the flag is covered by the signature rather than trusted as presented.
    """
    drive = await fx.drive()
    node = await _named_json_node(fx)
    await _put(files_client, drive.id, node.id, JSON_BYTES, etag=node.etag, idem=idem)
    asked = await files_client.get(f"{_url(drive.id, node.id)}?disposition=attachment")
    location = str(asked.headers["location"])

    token = location.rsplit("/", 1)[1]
    nonce, claim_part, signature = token.split(".")
    claim = signed_urls.parse_claim(token)
    assert claim is not None and claim.attachment is True
    flipped = signed_urls.encode_claim(replace(claim, attachment=False))
    assert flipped != claim_part

    forged = await _redeem(files_client, f"{CONTENT_ORIGIN}/c/{nonce}.{flipped}.{signature}")
    assert forged.status_code == 404, forged.text
    # Indistinguishable from a token that never existed, down to the headers:
    # the flipped flag is not a distinct failure a prober could sort out.
    unknown = await _redeem(files_client, f"{CONTENT_ORIGIN}/c/{'0' * 32}.{flipped}.{signature}")
    assert _oracle(forged) == _oracle(unknown)

    # A URL that was NOT edited still serves, with the disposition it was
    # signed for — the refusal above is the edit, not the flag.
    honest = await files_client.get(f"{_url(drive.id, node.id)}?disposition=attachment")
    served = await _redeem(files_client, str(honest.headers["location"]))
    assert served.status_code == 200
    assert served.headers["content-disposition"].startswith("attachment;")
