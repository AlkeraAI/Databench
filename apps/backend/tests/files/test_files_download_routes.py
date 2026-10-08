"""The tree download, from the API host to the last byte on the content domain.

``POST …/items/{i}/download`` and ``GET /c/archive/{token}`` are one feature
split across two origins, and every interesting thing about it lives in the
seam: the operation is minted where the session cookie is, the bytes are served
where it is not. So the cases here always cross that boundary — the archive is
fetched from the content mount under its own Host, and what comes back is
unzipped and compared against the bytes that were uploaded through the API.

Three claims the SPEC makes that nothing else in the suite drives end to end:

* the archive is a real ZIP64 whose members are byte-identical to the tree,
  empty folders included, carrying the content-response header recipe;
* the token is the capability — it carries no session, so a cookie-less client
  and a *different* signed-in user redeem the same archive — but it is a claim
  about *whose* access is used, and redemption re-runs the authorization for
  the user it names, so a token re-signed for someone who may not export the
  subtree reaches zero bytes;
* an expired deadline, a forged signature and a node that is not the caller's
  are all the same opaque ``not_found``.
"""

from __future__ import annotations

import dataclasses
import io
import json
import struct
import uuid
import zipfile
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from _files_kit import NOT_FOUND, FilesFixtures, FilesOrgFixture, refusal
from _oracle import OPAQUE_NOT_FOUND_BODY, assert_no_oracle
from alkera_core.config import settings
from alkera_core.db.session import engine
from alkera_core.files.acl_intern import ace_body, body_hash
from alkera_core.files.authz.decider import NO_DOWNLOAD_BIT
from alkera_core.files.authz.grants import Grant, GrantOrigin, Principal
from alkera_core.models.files.acl import FileAcl
from alkera_core.models.files.tree import FileNode
from backend.content_app import CONTENT_HEADERS, CONTENT_MOUNT_PATH
from backend.services.files.archive import (
    SKIP_NO_EXPORT,
    SKIPPED_MEMBER,
    ArchiveClaim,
    sign_claim,
    verify_token,
)
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from test_files_no_oracle_items import ThreeClasses, three_classes  # noqa: F401  (a fixture)
from tests._suite_app import app as fastapi_app
from tests.conftest import login

pytestmark = pytest.mark.asyncio

BASE = "/api/v1/files"

CONTENT_HOST = "files.localhost:8000"
CONTENT_ORIGIN = f"http://{CONTENT_HOST}"

#: The ZIP64 extended-information extra field's header id, read off the local
#: file header — the streaming writer's own output, not the central directory
#: ``zipfile`` would rebuild on read.
ZIP64_EXTRA_ID = 0x0001

#: The engine ``_oracle`` counts statements on; the app and the fixtures share
#: one process-wide async engine, so its sync facade is what a listener binds.
SYNC_ENGINE = engine.sync_engine

README = b"the readme, with a trailing newline\n"
ROWS = b"id,name\n1,alpha\n2,beta\n"


@pytest_asyncio.fixture
async def anonymous(files_on: None) -> AsyncIterator[AsyncClient]:
    """A second client on the same app that has never authenticated.

    The suite's ``client`` fixture is the very object ``files_client`` logs in,
    so asking it to redeem a token would prove nothing about cookies; this one
    is built fresh against the same ASGI app and carries none.
    """
    async with AsyncClient(transport=ASGITransport(app=fastapi_app), base_url="http://test") as c:
        yield c


@pytest.fixture
def content_on(monkeypatch: pytest.MonkeyPatch, files_on: None) -> None:
    """Files enabled, a store behind it, and a content origin to mint onto."""
    monkeypatch.setattr(settings, "files_content_base_url", CONTENT_ORIGIN)


@dataclasses.dataclass(frozen=True)
class Tree:
    """The fixture tree, and the bytes each member is supposed to contain."""

    drive_id: uuid.UUID
    root: FileNode
    contents: dict[str, bytes]

    @property
    def expected_names(self) -> set[str]:
        """Every member the archive must hold: files, plus folders with the
        trailing slash a directory entry carries."""
        return set(self.contents) | {"data/", "empty/"}


async def _etag(session: AsyncSession, node_id: uuid.UUID) -> int:
    value = await session.execute(
        text("SELECT etag FROM file_nodes WHERE id = :id"), {"id": node_id}
    )
    return int(value.scalar_one())


async def _put_bytes(
    client: AsyncClient,
    session: AsyncSession,
    drive_id: uuid.UUID,
    node: FileNode,
    payload: bytes,
    idem: Callable[[], dict[str, str]],
) -> None:
    response = await client.put(
        f"{BASE}/drives/{drive_id}/items/{node.id}/content",
        content=payload,
        headers={
            **idem(),
            "If-Match": f'"{await _etag(session, node.id)}"',
            "Content-Type": "application/octet-stream",
            "Content-Length": str(len(payload)),
        },
    )
    assert response.status_code in {200, 201}, response.text


@pytest_asyncio.fixture
async def tree(
    files_client: AsyncClient,
    fx: FilesFixtures,
    real_session: AsyncSession,
    content_on: None,
    idem: Callable[[], dict[str, str]],
) -> Tree:
    """``proj/`` with a file, a nested file and an empty folder.

    The empty folder is the member browsers lose and this archive must not; the
    nested file is what proves member paths are built from the walk rather than
    from a flattened listing.
    """
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

    # The tree hangs off the actor's home rather than the drive root: a chat
    # folder planted in it below is one the actor owns, as the product files
    # them, and not one reached only through the org-admin floor.
    proj = await fx.node(b"proj", kind="folder", parent=await fx.home())
    data = await fx.node(b"data", kind="folder", parent=proj)
    await fx.node(b"empty", kind="folder", parent=proj)
    readme = await fx.node(b"readme.txt", parent=proj)
    rows = await fx.node(b"rows.csv", parent=data)
    await _put_bytes(files_client, real_session, drive.id, readme, README, idem)
    await _put_bytes(files_client, real_session, drive.id, rows, ROWS, idem)
    return Tree(
        drive_id=drive.id,
        root=proj,
        contents={"readme.txt": README, "data/rows.csv": ROWS},
    )


async def _start_download(
    client: AsyncClient,
    session: AsyncSession,
    drive_id: uuid.UUID,
    node_id: uuid.UUID,
    idem: Callable[[], dict[str, str]],
    *,
    etag_of: uuid.UUID | None = None,
) -> Response:
    """Start an export. The resource is the folder, so its etag is the
    precondition every mutation must carry; ``etag_of`` names a different node
    to read it from when the path deliberately points at an id that has none."""
    return await client.post(
        f"{BASE}/drives/{drive_id}/items/{node_id}/download",
        headers={**idem(), "If-Match": str(await _etag(session, etag_of or node_id))},
    )


async def _redeem(client: AsyncClient, token: str) -> Response:
    """Redeem ``token`` on the content domain, at the path that is served.

    The mount lives at :data:`CONTENT_MOUNT_PATH` and the archive route
    declares ``/archive/{token}`` relative to it, so this is the same address
    ``operations._archive_link`` mints — which
    ``test_the_minted_result_url_is_where_the_archive_is_served`` pins by
    following ``resultUrl`` verbatim instead of composing it.
    """
    return await client.get(f"{CONTENT_MOUNT_PATH}/archive/{token}", headers={"Host": CONTENT_HOST})


def _token_of(result_url: str) -> str:
    return result_url.rsplit("/", 1)[-1]


def _signing_key() -> bytes:
    return settings.effective_files_content_signing_key.encode()


def _claim_behind(result_url: str) -> ArchiveClaim:
    """The claim the minted token carries, verified with the server's own key."""
    claim = verify_token(_token_of(result_url), _signing_key(), now=datetime.now(UTC))
    assert claim is not None, "the route minted a token its own verifier rejects"
    return claim


def _local_header_extra_ids(raw: bytes, member: str) -> set[int]:
    """The extra-field ids on ``member``'s local file header.

    The offset comes from the central directory (which is where a reader finds
    it) but the bytes read are the *local* header the streaming writer emitted,
    which is the half a non-ZIP64 writer would get wrong.
    """
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        start = archive.getinfo(member).header_offset
    name_len, extra_len = struct.unpack("<HH", raw[start + 26 : start + 30])
    extra = raw[start + 30 + name_len : start + 30 + name_len + extra_len]
    ids: set[int] = set()
    cursor = 0
    while cursor + 4 <= len(extra):
        header_id, size = struct.unpack("<HH", extra[cursor : cursor + 4])
        ids.add(header_id)
        cursor += 4 + size
    return ids


async def _archive_bytes(
    client: AsyncClient,
    session: AsyncSession,
    tree_fixture: Tree,
    idem: Callable[[], dict[str, str]],
) -> tuple[Response, bytes]:
    started = await _start_download(
        client, session, tree_fixture.drive_id, tree_fixture.root.id, idem
    )
    assert started.status_code == 202, started.text
    served = await _redeem(client, _token_of(started.json()["resultUrl"]))
    assert served.status_code == 200, served.text
    return started, served.content


# ---- the archive itself ---------------------------------------------------


async def test_download_yields_an_archive_whose_members_match_the_tree(
    files_client: AsyncClient,
    tree: Tree,
    real_session: AsyncSession,
    idem: Callable[[], dict[str, str]],
) -> None:
    """Every file's bytes survive the round trip and the empty folder does too.

    The expected bytes are the ones the test uploaded, so nothing here is
    recomputed from the code under test: a walk that dropped ``data/rows.csv``,
    flattened it to ``rows.csv``, or lost ``empty/`` fails on the name set, and
    a stream that truncated a member fails on the comparison.
    """
    _, raw = await _archive_bytes(files_client, real_session, tree, idem)
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        assert set(archive.namelist()) == tree.expected_names
        for name, expected in tree.contents.items():
            assert archive.read(name) == expected


async def test_the_archive_is_zip64_on_the_wire(
    files_client: AsyncClient,
    tree: Tree,
    real_session: AsyncSession,
    idem: Callable[[], dict[str, str]],
) -> None:
    """The streamed local headers carry the ZIP64 extra field.

    ``zipfile`` reads the central directory, which a non-ZIP64 writer would
    also produce; the claim is about what went out first, so the first local
    header is parsed by hand.
    """
    _, raw = await _archive_bytes(files_client, real_session, tree, idem)
    for member in tree.contents:
        assert ZIP64_EXTRA_ID in _local_header_extra_ids(raw, member), member


async def test_the_operation_reports_the_archive_it_will_serve(
    files_client: AsyncClient,
    tree: Tree,
    real_session: AsyncSession,
    idem: Callable[[], dict[str, str]],
) -> None:
    """202, settled, and a link on the content domain with a deadline."""
    started = await _start_download(files_client, real_session, tree.drive_id, tree.root.id, idem)
    assert started.status_code == 202, started.text
    body = started.json()
    assert body["kind"] == "download"
    assert body["state"] == "done"
    assert body["total"] == len(tree.expected_names)
    assert body["errors"] == []
    assert body["resultUrl"].startswith(f"{CONTENT_ORIGIN}/c/archive/")
    assert datetime.fromisoformat(body["resultUrlExpiresAt"]) > datetime.now(UTC)


async def test_the_minted_result_url_is_where_the_archive_is_served(
    files_client: AsyncClient,
    tree: Tree,
    real_session: AsyncSession,
    idem: Callable[[], dict[str, str]],
) -> None:
    """A client that follows ``resultUrl`` verbatim gets the archive.

    This is the only thing a browser ever does with a download operation, so it
    is asserted against the minted URL exactly as returned rather than against
    any path the test composes itself.
    """
    started = await _start_download(files_client, real_session, tree.drive_id, tree.root.id, idem)
    assert started.status_code == 202, started.text
    minted = started.json()["resultUrl"]
    assert minted.startswith(CONTENT_ORIGIN), minted
    served = await files_client.get(
        minted.removeprefix(CONTENT_ORIGIN), headers={"Host": CONTENT_HOST}
    )
    assert served.status_code == 200, served.text


# ---- the content-domain contract -----------------------------------------


async def test_the_served_archive_carries_the_content_domain_recipe(
    files_client: AsyncClient,
    tree: Tree,
    real_session: AsyncSession,
    idem: Callable[[], dict[str, str]],
) -> None:
    """A5's whole header block, ``Cache-Control: private, no-store`` included.

    A cached archive is a subtree readable by whoever gets the cache entry
    next, which is why the recipe is asserted on the bytes and not only on the
    refusals.
    """
    started = await _start_download(files_client, real_session, tree.drive_id, tree.root.id, idem)
    served = await _redeem(files_client, _token_of(started.json()["resultUrl"]))
    assert served.status_code == 200
    for name, value in CONTENT_HEADERS.items():
        assert served.headers[name] == value
    assert served.headers["Cache-Control"] == "private, no-store"
    assert served.headers["Content-Type"] == "application/zip"


async def test_the_archive_filename_names_the_operation_and_not_the_folder(
    files_client: AsyncClient,
    tree: Tree,
    real_session: AsyncSession,
    idem: Callable[[], dict[str, str]],
) -> None:
    """``Content-Disposition`` is an attachment named for the operation.

    The folder's own name never reaches the header: the content domain serves
    strangers holding tokens, and a filename is the one part of the response a
    browser writes to disk unread.
    """
    started = await _start_download(files_client, real_session, tree.drive_id, tree.root.id, idem)
    served = await _redeem(files_client, _token_of(started.json()["resultUrl"]))
    disposition = served.headers["Content-Disposition"]
    name = f"{started.json()['id']}.zip"
    # The RFC 6266 pair the one content-domain helper emits, spelled out here
    # rather than borrowed from it: a route that hand-rolls the header gets the
    # quoted half right and drops the extended half, which is exactly the
    # difference between "this happens to work for ASCII" and "every name
    # survives".
    assert disposition == f"attachment; filename=\"{name}\"; filename*=UTF-8''{name}"
    assert "proj" not in disposition


@pytest.mark.parametrize(
    "target",
    [
        pytest.param("/", id="the-mount-root"),
        pytest.param("/c/archive/anything", id="the-address-the-doubled-mount-used-to-serve"),
        pytest.param("/archive/one/two", id="a-token-that-is-two-segments"),
        pytest.param("/nothing/here", id="a-path-no-family-declares"),
    ],
)
async def test_an_unrouted_content_path_is_the_same_opaque_refusal(
    files_client: AsyncClient, content_on: None, target: str
) -> None:
    """A path the content mount does not route answers ``not_found`` like a
    token that was never real.

    The default framework 404 is ``{"detail": "Not Found"}`` — a different body
    from every refusal the content domain makes on purpose, so a probe could
    sort "this address exists and refused me" from "this address does not
    exist" without holding a single valid token. It also has to carry the A5
    recipe: a 404 served with a relaxed CSP is a hole exactly where a probe
    looks.
    """
    refused = await files_client.get(
        f"{CONTENT_MOUNT_PATH}{target}", headers={"Host": CONTENT_HOST}
    )
    assert refused.status_code == 404
    assert refused.json() == {"code": "not_found"}
    for name, value in CONTENT_HEADERS.items():
        assert refused.headers[name] == value


# ---- the token: what it binds to, and what it does not --------------------


async def test_the_token_carries_the_capability_and_not_a_session(
    files_client: AsyncClient,
    real_session: AsyncSession,
    anonymous: AsyncClient,
    tree: Tree,
    idem: Callable[[], dict[str, str]],
) -> None:
    """A client with no cookie at all redeems the same archive.

    The content domain holds no session, so the token has to stand alone; if it
    were bound to the minting client's cookie this would be a 404 and the
    feature could not work from a browser at all.
    """
    started, mine = await _archive_bytes(files_client, real_session, tree, idem)
    assert not anonymous.cookies
    theirs = await _redeem(anonymous, _token_of(started.json()["resultUrl"]))
    assert theirs.status_code == 200, theirs.text
    with zipfile.ZipFile(io.BytesIO(theirs.content)) as archive:
        assert set(archive.namelist()) == tree.expected_names
        assert archive.read("readme.txt") == README
    assert len(theirs.content) == len(mine)


async def test_another_signed_in_user_redeeming_the_token_gets_the_same_archive(
    files_client: AsyncClient,
    real_session: AsyncSession,
    client: AsyncClient,
    files_org: FilesOrgFixture,
    tree: Tree,
    idem: Callable[[], dict[str, str]],
) -> None:
    """A cookie for user B does not change what user A's token serves.

    The claim names the user whose access is used; the request's own credential
    is never consulted, so B holding the link is exactly as powerful as B
    holding nothing but the link.
    """
    started, mine = await _archive_bytes(files_client, real_session, tree, idem)
    as_member = await login(client, files_org.member.email, files_org.member_password)
    served = await _redeem(as_member, _token_of(started.json()["resultUrl"]))
    assert served.status_code == 200, served.text
    assert served.content == mine


async def test_a_token_re_signed_for_a_user_who_may_not_export_reaches_no_bytes(
    files_client: AsyncClient,
    anonymous: AsyncClient,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
    real_session: AsyncSession,
    content_on: None,
    idem: Callable[[], dict[str, str]],
) -> None:
    """Authorization is re-run for the user the token names.

    The token is signed with the server's own key — a forgery this test could
    not mount, and precisely the one a leaked key would — so what refuses it is
    the export check on redemption, not the signature. Without that check the
    archive would stream, because everything else about the token is valid.
    """
    body = ace_body(
        [
            Grant(
                principal=Principal(kind="user", id=files_org.org.admin_id),
                role="manager",
                origin=GrantOrigin.direct(),
            )
        ]
    )
    acl = FileAcl(
        id=uuid.uuid4(),
        org_team_id=files_org.org.org_id,
        body=body,
        body_hash=body_hash(body, org_team_id=files_org.org.org_id),
    )
    real_session.add(acl)
    await real_session.flush()
    private = await fx.node(b"private", kind="folder", acl_id=acl.id)
    await real_session.commit()
    drive = await fx.drive()

    started = await _start_download(files_client, real_session, drive.id, private.id, idem)
    assert started.status_code == 202, started.text
    admins_url = started.json()["resultUrl"]
    assert (await _redeem(anonymous, _token_of(admins_url))).status_code == 200

    members = dataclasses.replace(_claim_behind(admins_url), user_id=files_org.member.id)
    refused = await _redeem(anonymous, sign_claim(members, _signing_key()))
    assert refused.status_code == 404
    assert refused.json() == {"code": "not_found"}


# ---- the refusals ---------------------------------------------------------


@pytest.mark.parametrize(
    ("offset", "expected"),
    [
        pytest.param(timedelta(seconds=-1), 404, id="one-second-past-the-deadline"),
        pytest.param(timedelta(seconds=0), 404, id="exactly-at-the-deadline"),
        pytest.param(timedelta(minutes=5), 200, id="inside-the-window"),
    ],
)
async def test_the_deadline_is_the_boundary_the_archive_stops_at(
    files_client: AsyncClient,
    tree: Tree,
    real_session: AsyncSession,
    idem: Callable[[], dict[str, str]],
    offset: timedelta,
    expected: int,
) -> None:
    """A token is redeemable up to its deadline and not at it.

    The three cases are one signed claim each, differing only in ``expires_at``,
    so a redemption that ignored the deadline would answer 200 three times and
    one that mis-ordered the comparison would fail on the boundary alone.
    """
    started = await _start_download(files_client, real_session, tree.drive_id, tree.root.id, idem)
    claim = dataclasses.replace(
        _claim_behind(started.json()["resultUrl"]), expires_at=datetime.now(UTC) + offset
    )
    served = await _redeem(files_client, sign_claim(claim, _signing_key()))
    assert served.status_code == expected, served.text
    if expected == 404:
        assert served.json() == {"code": "not_found"}
        for name, value in CONTENT_HEADERS.items():
            assert served.headers[name] == value


@pytest.mark.parametrize(
    "seconds",
    [
        pytest.param(60, id="a-minute-for-a-strict-deployment"),
        pytest.param(7200, id="two-hours-for-a-slow-link"),
    ],
)
async def test_the_archive_link_lives_as_long_as_the_deployment_says(
    files_client: AsyncClient,
    tree: Tree,
    real_session: AsyncSession,
    idem: Callable[[], dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
    seconds: int,
) -> None:
    """The window a download link stays redeemable for is a deployment setting.

    Asserted twice over, because the two can disagree: the deadline the
    operation advertises, bracketed by the instants either side of the request
    so a mint that ignored the setting lands outside it, and the deadline
    signed into the token itself — which is the one that actually stops the
    bytes.
    """
    monkeypatch.setattr(settings, "files_archive_url_ttl_seconds", seconds)
    window = timedelta(seconds=seconds)

    before = datetime.now(UTC)
    started = await _start_download(files_client, real_session, tree.drive_id, tree.root.id, idem)
    after = datetime.now(UTC)

    assert started.status_code == 202, started.text
    body = started.json()
    advertised = datetime.fromisoformat(body["resultUrlExpiresAt"])
    assert before + window <= advertised <= after + window
    assert _claim_behind(body["resultUrl"]).expires_at == advertised


async def test_a_tampered_token_is_the_same_refusal_as_an_expired_one(
    files_client: AsyncClient,
    tree: Tree,
    real_session: AsyncSession,
    idem: Callable[[], dict[str, str]],
) -> None:
    """A forged signature and a lapsed deadline are indistinguishable.

    Byte-identical, so someone holding a stale link cannot tell whether the key
    changed, the deadline passed, or their claim was never real.
    """
    started = await _start_download(files_client, real_session, tree.drive_id, tree.root.id, idem)
    minted = started.json()["resultUrl"]
    expired = dataclasses.replace(
        _claim_behind(minted), expires_at=datetime.now(UTC) - timedelta(seconds=1)
    )
    lapsed = await _redeem(files_client, sign_claim(expired, _signing_key()))

    payload, _, signature = _token_of(minted).partition(".")
    flipped = signature[:-1] + ("0" if signature[-1] != "0" else "1")
    forged = await _redeem(files_client, f"{payload}.{flipped}")

    assert forged.status_code == lapsed.status_code == 404
    assert forged.content == lapsed.content
    assert forged.json() == {"code": "not_found"}


async def test_a_chat_folder_is_neither_downloaded_nor_zipped_out_of_its_parent(
    files_client: AsyncClient,
    real_session: AsyncSession,
    fx: FilesFixtures,
    tree: Tree,
    idem: Callable[[], dict[str, str]],
) -> None:
    """ "You cannot download a chat" has to survive being inside a folder.

    Refusing the chat's own download is the easy half. The half that matters is
    the archive of the folder *above* it: a walk that authorized members for
    READ would happily put the chat's working files in someone's zip, so the
    walk asks for EXPORT and prunes the subtree when the answer is no. The
    chat's id shows up in the archive's own skipped manifest, which is how a
    caller learns something was left out without being told what.
    """
    chat = await fx.node(
        b"Kickoff.alkerachat",
        kind="folder",
        parent=tree.root,
        subtype="chat",
        target_object_id=uuid.uuid4(),
        flags=NO_DOWNLOAD_BIT,
    )
    inside = await fx.node(b"notes.md", parent=chat)
    await _put_bytes(files_client, real_session, tree.drive_id, inside, b"private\n", idem)

    # A visible refusal, not an opaque one: the caller can see the chat in
    # their own drive, so hiding it would only be confusing.
    refused = await _start_download(files_client, real_session, tree.drive_id, chat.id, idem)
    assert refused.status_code == 403
    assert refused.json()["code"] == "files.forbidden"

    _, raw = await _archive_bytes(files_client, real_session, tree, idem)
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        names = set(archive.namelist())
        manifest = archive.read(SKIPPED_MEMBER).decode("utf-8")
    assert not [name for name in names if name.startswith("Kickoff.alkerachat")]
    assert tree.expected_names <= names, "the rest of the tree still comes out whole"
    assert f"{chat.id}\t{SKIP_NO_EXPORT}" in manifest


async def test_a_node_that_is_not_yours_never_mints_an_archive(
    files_client: AsyncClient,
    real_session: AsyncSession,
    fx: FilesFixtures,
    tree: Tree,
    idem: Callable[[], dict[str, str]],
) -> None:
    """A download of an id the caller cannot export is an opaque 404."""
    drive = await fx.drive()
    started = await _start_download(
        files_client, real_session, drive.id, uuid.uuid4(), idem, etag_of=tree.root.id
    )
    assert started.status_code == 404
    assert refusal(started) == NOT_FOUND


async def test_download_answers_the_three_not_yours_classes_identically(
    three_classes: ThreeClasses,  # noqa: F811  (the imported fixture)
    real_session: AsyncSession,
    fx: FilesFixtures,
) -> None:
    """Nonexistent, other-org and same-org-unreadable are one answer.

    The item routes fail this today — a same-org unreadable node escapes through
    the platform enforcer's own envelope — so it is worth saying why download
    does not: its EXPORT check runs inside ``files.repo.transaction()`` and the
    ``FilesError`` it raises reaches the Files exception handler, which renders
    the same opaque body the other two classes get.
    """
    drive = await fx.drive()
    assert drive.root_node_id is not None
    root_id = drive.root_node_id
    probes = await assert_no_oracle(
        three_classes.client,
        SYNC_ENGINE,
        "POST",
        [
            f"{BASE}/drives/{three_classes.drive_id}/items/{identifier}/download"
            for identifier in three_classes.ids
        ],
        three_classes.ids,
        # One precondition for all three probes — the caller's own drive root,
        # the only node in the picture whose etag every class may name — so the
        # three answers differ in nothing but the id in the path.
        headers={
            "Idempotency-Key": uuid.uuid4().hex,
            "If-Match": str(await _etag(real_session, root_id)),
        },
        labels=("nonexistent", "other-org", "unreadable"),
        # Work equality is a separate claim with a separate failure mode; the
        # bodies are what this case is about.
        same_work=False,
    )
    assert probes[0].status == 404
    assert json.loads(probes[0].body) == OPAQUE_NOT_FOUND_BODY
