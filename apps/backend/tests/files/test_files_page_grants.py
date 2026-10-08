"""The page grant end to end: mint one on the API host, serve a folder from it.

A rendered document is the one read that is not a download. It arrives as a
frame on the app origin and then fetches, with no credential of its own, the
images and media sitting beside it — so the grant covers the entry's FOLDER and
every request under it is decided again against the node it actually names.

Everything the two routes promise is asserted here against the real ASGI app,
the real content mount and a real store, because every one of those promises is
a hole if it is only true in a unit test:

* the mint decides ``EXPORT`` and refuses a node that is not a document;
* the walk descends by name BYTES and refuses every spelling that means
  "somewhere else" — including the ones a second percent-decode would create;
* a sibling this reader may not see is the same opaque 404 a missing one is,
  and the page beside it still serves;
* the headers a page gets are the page recipe and nothing wider;
* the grant dies on time, on revoke, on the wrong route and past its ceiling.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest
import pytest_asyncio
import structlog
from _files_kit import NOT_FOUND, refusal
from alkera_core.config import settings
from alkera_core.files.authz.decider import NO_DOWNLOAD_BIT
from alkera_core.files.ids import OrgScope
from alkera_core.files.page_grants import page_grant_max_requests
from alkera_core.files.repo import FilesRepo
from alkera_core.models.event_outbox import EventOutbox
from backend.api import rate_limit
from backend.content_app import NOT_FOUND_BODY
from httpx import AsyncClient, Response
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from structlog.testing import capture_logs
from tests.conftest import login, make_member
from tests.files._files_kit import FilesFixtures, FilesOrgFixture

pytestmark = [
    pytest.mark.asyncio,
    # Every case rebuilds the whole report folder, and the one process-wide
    # logger it depends on is rebound inside the case that reads it, so the cases
    # may land on different workers.
    pytest.mark.spread,
]

CONTENT_HOST = "files.localhost:8000"
CONTENT_ORIGIN = f"http://{CONTENT_HOST}"
APP_ORIGIN = "http://portal.localhost:5173"

PAGE = b"<!doctype html>\n<p>Q3</p>\n<img src='assets/chart.png'>\n"
CHART = b"\x89PNG\r\n\x1a\n" + b"0" * 64
SECRET = b"a sibling the reader may not see\n"


@pytest.fixture
def content_on(monkeypatch: pytest.MonkeyPatch, files_on: None, client: AsyncClient) -> None:
    """Files enabled, a content origin to mint onto, and one framing origin.

    Moving the portal origin moves the only origin a cookie-authenticated
    mutation may come from, so the browser standing in for it says the new one:
    the suite's client was built carrying the default, and every write below
    would otherwise be refused by the origin guard rather than by anything this
    module is about.
    """
    monkeypatch.setattr(settings, "files_content_base_url", CONTENT_ORIGIN)
    monkeypatch.setattr(settings, "frontend_base_url", APP_ORIGIN)
    monkeypatch.setattr(settings, "api_cors_origins", APP_ORIGIN)
    monkeypatch.setitem(client.headers, "Origin", APP_ORIGIN)


class Folder:
    """One report folder of org A: the page, an asset under it, and a secret."""

    def __init__(self, drive_id: uuid.UUID, ids: dict[str, uuid.UUID]) -> None:
        self.drive_id = drive_id
        self.ids = ids


def _content_url(drive_id: uuid.UUID, node_id: uuid.UUID) -> str:
    return f"/api/v1/files/drives/{drive_id}/items/{node_id}/content"


def _grants_url(drive_id: uuid.UUID, node_id: uuid.UUID) -> str:
    return f"/api/v1/files/drives/{drive_id}/items/{node_id}/content-grants"


async def _put(
    client: AsyncClient,
    drive_id: uuid.UUID,
    node: Any,
    payload: bytes,
    idem: Callable[[], dict[str, str]],
) -> Response:
    return await client.put(
        _content_url(drive_id, node.id),
        content=payload,
        headers={
            **idem(),
            "If-Match": f'"{node.etag}"',
            "Content-Type": "application/octet-stream",
            "Content-Length": str(len(payload)),
        },
    )


@pytest_asyncio.fixture
async def report(
    files_client: AsyncClient,
    fx: FilesFixtures,
    real_session: AsyncSession,
    content_on: None,
    idem: Callable[[], dict[str, str]],
) -> Folder:
    """One report folder holding ``index.html``, ``assets/chart.png`` and a secret.

    Built through the real content PUT so the bytes are really in the store:
    a fixture that only wrote version rows would let a serving bug pass.
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

    # Under ``home/`` rather than ``/Shared``: everything in Shared is readable by
    # every member of the org, and a test about who may read what needs a folder
    # whose readers are exactly the ones it was given to. ``home/`` is a signpost —
    # members may list it and nothing more — so a folder placed there starts out
    # reachable by its owner alone.
    home = (
        await real_session.execute(
            text(
                "SELECT id FROM file_nodes "
                "WHERE parent_id = :root AND name = :name AND drive_id = :drive"
            ),
            {"root": drive.root_node_id, "name": b"home", "drive": drive.id},
        )
    ).scalar_one()
    folder = await fx.node(b"report", kind="folder", parent=await fx.folder(home))
    assets = await fx.node(b"assets", kind="folder", parent=folder)
    page = await fx.node(b"index.html", parent=folder)
    chart = await fx.node(b"chart.png", parent=assets)
    secret = await fx.node(b"secret.txt", parent=folder)
    percent = await fx.node(b"50%.png", parent=assets)

    for node, payload in (
        (page, PAGE),
        (chart, CHART),
        (secret, SECRET),
        (percent, CHART),
    ):
        written = await _put(files_client, drive.id, node, payload, idem)
        assert written.status_code == 201, written.text

    return Folder(
        drive.id,
        {
            "folder": folder.id,
            "assets": assets.id,
            "page": page.id,
            "chart": chart.id,
            "secret": secret.id,
            "percent": percent.id,
        },
    )


async def _mint(
    client: AsyncClient, report: Folder, which: str = "page", kind: str = "page"
) -> Response:
    return await client.post(
        _grants_url(report.drive_id, report.ids[which]),
        json={"kind": kind},
    )


async def _page_token(client: AsyncClient, report: Folder) -> str:
    minted = await _mint(client, report)
    assert minted.status_code == 201, minted.text
    url = str(minted.json()["url"])
    assert url.startswith(f"{CONTENT_ORIGIN}/c/p/"), url
    return url.removeprefix(f"{CONTENT_ORIGIN}/c/p/").partition("/")[0]


async def _fetch(client: AsyncClient, token: str, path: str, method: str = "GET") -> Response:
    return await client.request(method, f"/c/p/{token}/{path}", headers={"Host": CONTENT_HOST})


async def _share(
    client: AsyncClient, session: AsyncSession, report: Folder, which: str, user_id: uuid.UUID
) -> None:
    """Give ``user_id`` the reader rung on one node of the report folder."""
    etag = (
        await session.execute(
            text("SELECT etag FROM file_nodes WHERE id = :id"), {"id": report.ids[which]}
        )
    ).scalar_one()
    granted = await client.post(
        f"/api/v1/files/drives/{report.drive_id}/items/{report.ids[which]}/permissions",
        json={"principal": {"kind": "user", "id": str(user_id)}, "role": "reader"},
        headers={"Idempotency-Key": uuid.uuid4().hex, "If-Match": f'"{etag}"'},
    )
    assert granted.status_code in (200, 201), granted.text


# --------------------------------------------------------------------------
# the mint
# --------------------------------------------------------------------------


async def test_a_document_mints_a_page_url_and_a_png_is_refused(
    files_client: AsyncClient, report: Folder
) -> None:
    """``kind: "page"`` is for documents; anything else is told so, not served.

    The refusal is the whole point of the type list: a page grant relaxes what
    the response may load and who may frame it, so it may not be minted for an
    arbitrary file by asking nicely.
    """
    minted = await _mint(files_client, report)
    assert minted.status_code == 201, minted.text
    body = minted.json()
    assert body["kind"] == "page"
    assert body["url"].startswith(f"{CONTENT_ORIGIN}/c/p/")
    assert body["url"].endswith("/index.html")
    assert body["etag"]
    assert datetime.fromisoformat(body["expiresAt"]) > datetime.now(UTC)

    refused = await _mint(files_client, report, which="chart")
    assert refused.status_code == 422, refused.text
    assert refused.json()["code"] == "files.not_a_page"


async def test_a_drawing_mints_a_page_and_is_framed_under_the_svg_policy(
    files_client: AsyncClient,
    report: Folder,
    fx: FilesFixtures,
    idem: Callable[[], dict[str, str]],
) -> None:
    """An SVG is rendered in a frame on the content origin, never in the app.

    The page grant is what lets the app frame it; what keeps that safe is the
    policy it is served under on the page path (sandboxed with no allowances,
    no script and no network), which is the drawing's own recipe, not the
    HTML page's looser one.
    """
    drawing = await fx.node(b"chart.svg", parent=await fx.folder(report.ids["folder"]))
    svg = (
        b'<svg xmlns="http://www.w3.org/2000/svg" width="4" height="4">'
        b'<rect width="4" height="4"/></svg>'
    )
    written = await _put(files_client, report.drive_id, drawing, svg, idem)
    assert written.status_code == 201, written.text

    minted = await files_client.post(
        _grants_url(report.drive_id, drawing.id), json={"kind": "page"}
    )
    assert minted.status_code == 201, minted.text
    url = str(minted.json()["url"])
    assert url.startswith(f"{CONTENT_ORIGIN}/c/p/") and url.endswith("/chart.svg")

    token = url.removeprefix(f"{CONTENT_ORIGIN}/c/p/").partition("/")[0]
    served = await _fetch(files_client, token, "chart.svg")
    assert served.status_code == 200, served.text
    assert served.content == svg
    assert served.headers["Content-Type"].startswith("image/svg+xml")
    assert served.headers["Content-Security-Policy"] == (
        "default-src 'none'; style-src 'unsafe-inline'; img-src data:; font-src data:; "
        f"frame-ancestors {APP_ORIGIN}; base-uri 'none'; form-action 'none'; sandbox"
    )
    assert served.headers["Cross-Origin-Resource-Policy"] == "cross-origin"


async def test_a_file_grant_answers_the_single_use_url(
    files_client: AsyncClient, report: Folder
) -> None:
    """``kind: "file"`` mints exactly what the redirect mints, as data.

    The route exists because a preview cannot follow a cross-origin redirect
    with credentials; it must not become a second, looser mint.
    """
    minted = await _mint(files_client, report, which="chart", kind="file")
    assert minted.status_code == 201, minted.text
    body = minted.json()
    assert body["kind"] == "file"
    assert body["url"].startswith(f"{CONTENT_ORIGIN}/c/")
    assert not body["url"].startswith(f"{CONTENT_ORIGIN}/c/p/")

    token = body["url"].removeprefix(f"{CONTENT_ORIGIN}/c/")
    served = await files_client.get(f"/c/{token}", headers={"Host": CONTENT_HOST})
    assert served.status_code == 200, served.text
    assert served.content == CHART
    replayed = await files_client.get(f"/c/{token}", headers={"Host": CONTENT_HOST})
    assert replayed.status_code == 404


async def test_a_mint_on_a_stranger_id_is_the_opaque_404(
    files_client: AsyncClient, report: Folder
) -> None:
    """A node that is not there and one in another org answer identically."""
    nowhere = await files_client.post(
        _grants_url(report.drive_id, uuid.uuid4()), json={"kind": "page"}
    )
    assert nowhere.status_code == 404
    assert refusal(nowhere) == NOT_FOUND


async def test_a_version_less_node_under_a_lease_says_the_bytes_are_coming(
    files_client: AsyncClient,
    fx: FilesFixtures,
    real_session: AsyncSession,
    files_org: FilesOrgFixture,
    report: Folder,
) -> None:
    """A file a machine has created but not synced is 409, not 404.

    Without the lease the same node is the opaque 404: "the bytes are on their
    way" is only sayable when a machine is actually holding the folder.
    """
    shared = await fx.shared()
    mounted = await fx.node(b"mounted", kind="folder", parent=shared)
    pending = await fx.node(b"pending.html", parent=mounted)

    bare = await files_client.post(_grants_url(report.drive_id, pending.id), json={"kind": "page"})
    assert bare.status_code == 404, bare.text

    await real_session.execute(
        text(
            "INSERT INTO file_leases (node_id, org_team_id, epoch, holder_principal_kind, "
            "holder_principal_id, holder_instance_id, machine_id, purpose, acquired_at, "
            "heartbeat_at, expires_at, grantable_after) "
            "VALUES (:node, :org, 1, 'user', :user, 'inst-1', 'machine-1', 'mount', now(), "
            "now(), now() + interval '10 minutes', now())"
        ),
        {"node": mounted.id, "org": files_org.org.org_id, "user": files_org.org.admin_id},
    )
    await real_session.commit()

    held = await files_client.post(_grants_url(report.drive_id, pending.id), json={"kind": "page"})
    assert held.status_code == 409, held.text
    assert held.json()["code"] == "files.live_pending"


# --------------------------------------------------------------------------
# the walk
# --------------------------------------------------------------------------


async def test_a_page_loads_the_asset_beside_it(files_client: AsyncClient, report: Folder) -> None:
    """The acceptance case: the entry serves, and so does the file it references."""
    token = await _page_token(files_client, report)

    served = await _fetch(files_client, token, "index.html")
    assert served.status_code == 200, served.text
    assert served.content == PAGE
    assert served.headers["Content-Type"].startswith("text/html")

    asset = await _fetch(files_client, token, "assets/chart.png")
    assert asset.status_code == 200, asset.text
    assert asset.content == CHART
    assert asset.headers["Content-Type"].startswith("image/png")


async def test_a_stored_percent_in_a_name_resolves_and_a_second_decode_does_not(
    files_client: AsyncClient, report: Folder
) -> None:
    """One percent-decode, never two.

    ``50%.png`` is a real name and has to resolve, which means the request
    spells it ``50%25.png`` and the route reads what Starlette already decoded.
    A route that decoded again would resolve that same request to ``50%.png``
    *and* turn ``%252e%252e`` into ``..`` — the two are the same bug, so they
    are pinned together.
    """
    token = await _page_token(files_client, report)

    stored = await _fetch(files_client, token, "assets/50%25.png")
    assert stored.status_code == 200, stored.text
    assert stored.content == CHART

    escaped = await _fetch(files_client, token, "%252e%252e/secret.txt")
    assert escaped.status_code == 404
    assert escaped.json() == NOT_FOUND_BODY


async def test_an_nfd_spelling_is_a_different_name(
    files_client: AsyncClient,
    fx: FilesFixtures,
    report: Folder,
    idem: Callable[[], dict[str, str]],
) -> None:
    """Names are bytes: two Unicode spellings of one word are two names.

    Normalizing here would make the route disagree with the tree it is walking
    — a folder can hold both spellings at once — so the request has to carry the
    bytes that were stored, and the other spelling of the same word finds
    nothing even though the file is plainly there.
    """
    composed = "caf\u00e9.txt"
    decomposed = "cafe\u0301.txt"
    folder = await fx.folder(report.ids["folder"])
    node = await fx.node(composed.encode(), parent=folder)
    written = await _put(files_client, report.drive_id, node, CHART, idem)
    assert written.status_code == 201, written.text

    token = await _page_token(files_client, report)
    assert (await _fetch(files_client, token, composed)).status_code == 200
    assert (await _fetch(files_client, token, decomposed)).status_code == 404


@pytest.mark.parametrize(
    "path",
    [
        pytest.param("%2e%2e/secret.txt", id="dot-dot"),
        pytest.param("%2e/index.html", id="dot"),
        pytest.param("assets//chart.png", id="empty-segment"),
        pytest.param("assets\\chart.png", id="backslash"),
        pytest.param("index.html%00.png", id="nul"),
        pytest.param("assets/", id="trailing-slash"),
        pytest.param("/".join(["a"] * 33), id="too-many-segments"),
        pytest.param("x" * 256, id="over-long-segment"),
        pytest.param("assets", id="folder-leaf"),
        pytest.param("assets/chart.png/more", id="descend-through-a-file"),
    ],
)
async def test_the_lexical_and_shape_refusals_are_the_opaque_404(
    files_client: AsyncClient, report: Folder, path: str
) -> None:
    """Every spelling that means something other than "a file under this root"."""
    token = await _page_token(files_client, report)
    refused = await _fetch(files_client, token, path)
    assert refused.status_code == 404, refused.text
    assert refused.json() == NOT_FOUND_BODY


@pytest.mark.parametrize("kind", ["symlink", "special", "object"])
async def test_a_non_file_node_is_never_followed(
    files_client: AsyncClient, fx: FilesFixtures, report: Folder, kind: str
) -> None:
    """A pointer is refused rather than resolved.

    Following one is how a grant over a folder becomes a read of whatever the
    pointer names, which may be outside the folder entirely.
    """
    folder = await fx.folder(report.ids["folder"])
    await fx.node(b"elsewhere", kind=kind, parent=folder)
    token = await _page_token(files_client, report)

    refused = await _fetch(files_client, token, "elsewhere")
    assert refused.status_code == 404
    assert refused.json() == NOT_FOUND_BODY


async def test_a_trashed_asset_stops_serving(
    files_client: AsyncClient, real_session: AsyncSession, report: Folder
) -> None:
    """A grant is not a way to keep reading something that has been thrown away."""
    token = await _page_token(files_client, report)
    assert (await _fetch(files_client, token, "assets/chart.png")).status_code == 200

    await real_session.execute(
        text("UPDATE file_nodes SET trashed_at = now() WHERE id = :id"),
        {"id": report.ids["chart"]},
    )
    await real_session.commit()

    assert (await _fetch(files_client, token, "assets/chart.png")).status_code == 404
    assert (await _fetch(files_client, token, "index.html")).status_code == 200


# --------------------------------------------------------------------------
# the fan-out decision
# --------------------------------------------------------------------------


@pytest_asyncio.fixture
async def reader(
    client: AsyncClient,
    files_client: AsyncClient,
    real_session: AsyncSession,
    files_org: FilesOrgFixture,
    report: Folder,
) -> AsyncClient:
    """A member of the same org, given the report folder but not what it hides.

    The secret beside the page carries the mark that says "this one does not
    leave as a file", so the reader can list the folder and open the page while
    that sibling's bytes stay out of reach — which is the exact arrangement a
    page grant must not undo.
    """
    await _share(files_client, real_session, report, "folder", files_org.member.id)
    await real_session.execute(
        text("UPDATE file_nodes SET flags = flags | :bit WHERE id = :id"),
        {"bit": NO_DOWNLOAD_BIT, "id": report.ids["secret"]},
    )
    await real_session.commit()
    return await login(client, files_org.member.email, files_org.member_password)


async def test_a_sibling_the_reader_cannot_open_is_404_and_the_page_still_serves(
    reader: AsyncClient, report: Folder, real_session: AsyncSession
) -> None:
    """The grant buys the right to ask, never the right to read.

    Its holder can reach the folder, so the marked file beside the page is
    exactly the request the grant would leak if the walk trusted it. It does
    not: the per-request decision refuses the sibling while the page it was
    minted for keeps serving, and it refuses it with the same opaque answer a
    file that is not there gets — this origin has no session to explain a
    visible refusal to, and a distinguishable one would be the probe.
    """
    token = await _page_token(reader, report)

    leaked = await _fetch(reader, token, "secret.txt")
    assert leaked.status_code == 404
    assert leaked.json() == NOT_FOUND_BODY

    served = await _fetch(reader, token, "index.html")
    assert served.status_code == 200
    assert served.content == PAGE

    rows = (
        (
            await real_session.execute(
                select(EventOutbox)
                .where(EventOutbox.type == "authz.decision")
                .order_by(EventOutbox.id.desc())
                .limit(60)
            )
        )
        .scalars()
        .all()
    )
    decided = {(str(row.entity_id), row.payload.get("effect")) for row in rows}
    assert (str(report.ids["secret"]), "deny") in decided, decided
    assert (str(report.ids["page"]), "allow") in decided, decided


async def test_an_unreadable_intermediate_folder_refuses_the_leaf_under_it(
    client: AsyncClient,
    files_client: AsyncClient,
    real_session: AsyncSession,
    files_org: FilesOrgFixture,
    report: Folder,
) -> None:
    """A path descends through folders, so every one of them is decided.

    This reader was handed the page and the asset folder but never the report
    folder the grant is rooted on. A route that decided only the leaf would let
    that grant serve its whole root; deciding the chain means the page they were
    given is unreachable through it — which is the safe direction, and the one
    a shallower check would silently reverse.
    """
    deep_user, password = await make_member(
        real_session, org_id=files_org.org.org_id, verified=True
    )
    await real_session.commit()
    assert password is not None
    for node in ("page", "assets", "chart"):
        await _share(files_client, real_session, report, node, deep_user.id)
    deep = await login(client, deep_user.email, password)

    token = await _page_token(deep, report)
    for path in ("index.html", "assets/chart.png"):
        refused = await deep.get(f"/c/p/{token}/{path}", headers={"Host": CONTENT_HOST})
        assert refused.status_code == 404, refused.text
        assert refused.json() == NOT_FOUND_BODY


# --------------------------------------------------------------------------
# the headers
# --------------------------------------------------------------------------


async def test_the_page_carries_the_page_recipe_and_a_sibling_route_does_not(
    files_client: AsyncClient, report: Folder
) -> None:
    """The page path is the only place the two relaxations apply.

    ``'self'`` in the asset directives is what lets the document load the chart
    beside it; ``cross-origin`` is what lets the app origin frame it at all.
    Both are scoped to this path, and the single-use route a few characters away
    keeps the narrow recipe.
    """
    token = await _page_token(files_client, report)
    served = await _fetch(files_client, token, "index.html")

    assert served.headers["Content-Security-Policy"] == (
        "default-src 'none'; script-src 'self' 'unsafe-inline'; object-src 'none'; "
        "style-src 'unsafe-inline'; img-src 'self' data:; font-src 'self' data:; "
        f"media-src 'self' data:; frame-ancestors {APP_ORIGIN}; "
        "base-uri 'none'; form-action 'none'; sandbox allow-scripts"
    )
    assert served.headers["Cross-Origin-Resource-Policy"] == "cross-origin"
    assert served.headers["X-Content-Type-Options"] == "nosniff"
    assert served.headers["Cache-Control"] == "private, no-store"
    assert served.headers["Referrer-Policy"] == "no-referrer"
    assert served.headers["Accept-Ranges"] == "bytes"
    assert served.headers["ETag"]

    single = await _mint(files_client, report, which="chart", kind="file")
    token_single = str(single.json()["url"]).removeprefix(f"{CONTENT_ORIGIN}/c/")
    plain = await files_client.get(f"/c/{token_single}", headers={"Host": CONTENT_HOST})
    assert plain.headers["Cross-Origin-Resource-Policy"] == "same-origin"


async def test_head_answers_the_same_headers_with_no_body(
    files_client: AsyncClient, report: Folder
) -> None:
    """A preview that probes with HEAD must learn the type without the bytes."""
    token = await _page_token(files_client, report)
    probed = await _fetch(files_client, token, "index.html", method="HEAD")
    assert probed.status_code == 200
    assert probed.content == b""
    assert probed.headers["Cross-Origin-Resource-Policy"] == "cross-origin"


# --------------------------------------------------------------------------
# the lifetime
# --------------------------------------------------------------------------


async def test_a_page_grant_serves_twice(files_client: AsyncClient, report: Folder) -> None:
    """Multi-use by design: a document is many requests, not one."""
    token = await _page_token(files_client, report)
    assert (await _fetch(files_client, token, "index.html")).status_code == 200
    assert (await _fetch(files_client, token, "index.html")).status_code == 200


async def test_a_grant_is_dead_once_its_quarter_hour_is_up(
    files_client: AsyncClient, real_session: AsyncSession, report: Folder
) -> None:
    """There is no refresh: a page still open past the deadline mints again.

    The clock moved here is Postgres's, by pushing the deadline into the past,
    because that is the clock the predicate reads — every API instance shares
    it, and a grant whose life depended on the process's own clock would outlive
    itself on a host whose time had drifted.
    """
    token = await _page_token(files_client, report)
    assert (await _fetch(files_client, token, "index.html")).status_code == 200

    stamped = await real_session.execute(
        text(
            "UPDATE file_page_grants SET expires_at = now() - interval '1 minute' "
            "WHERE nonce = :nonce"
        ),
        {"nonce": token.partition(".")[0]},
    )
    assert stamped.rowcount == 1
    await real_session.commit()
    assert (await _fetch(files_client, token, "index.html")).status_code == 404


async def test_a_revoked_grant_stops_serving(
    files_client: AsyncClient, real_session: AsyncSession, report: Folder
) -> None:
    """Closing the grant closes every page it opened."""
    token = await _page_token(files_client, report)
    assert (await _fetch(files_client, token, "index.html")).status_code == 200
    await real_session.execute(
        text("UPDATE file_page_grants SET revoked_at = now() WHERE nonce = :nonce"),
        {"nonce": token.partition(".")[0]},
    )
    await real_session.commit()
    assert (await _fetch(files_client, token, "index.html")).status_code == 404


async def test_each_kind_of_token_is_dead_on_the_other_route(
    files_client: AsyncClient, report: Folder
) -> None:
    """The claim's ``kind`` is decided before anything is read, on both routes.

    Without it a page grant would be redeemable as a single-use download of its
    entry, and a download token would be redeemable as a folder.
    """
    page_token = await _page_token(files_client, report)
    wrong_route = await files_client.get(f"/c/{page_token}", headers={"Host": CONTENT_HOST})
    assert wrong_route.status_code == 404
    assert wrong_route.json() == NOT_FOUND_BODY

    single = await _mint(files_client, report, which="chart", kind="file")
    file_token = str(single.json()["url"]).removeprefix(f"{CONTENT_ORIGIN}/c/")
    as_page = await _fetch(files_client, file_token, "chart.png")
    assert as_page.status_code == 404
    assert as_page.json() == NOT_FOUND_BODY


async def test_a_forged_signature_is_byte_identical_to_a_bogus_token(
    files_client: AsyncClient, report: Folder
) -> None:
    """A real nonce with a rewritten signature buys nothing, and says nothing."""
    token = await _page_token(files_client, report)
    nonce, _, rest = token.partition(".")
    claim, _, signature = rest.rpartition(".")
    forged = f"{nonce}.{claim}.{'0' * len(signature)}"

    refused = await _fetch(files_client, forged, "index.html")
    bogus = await _fetch(files_client, f"{'0' * 32}.{claim}.{signature}", "index.html")
    assert refused.status_code == bogus.status_code == 404
    assert refused.json() == bogus.json() == NOT_FOUND_BODY


async def test_a_grant_stops_at_its_ceiling(
    files_client: AsyncClient, real_session: AsyncSession, report: Folder
) -> None:
    """The counter is the bound, and it cannot be walked past.

    Driven by moving the row to one short of the ceiling rather than by firing
    five thousand requests: the property is the predicate, and the last request
    plus the one after it are what prove it.
    """
    token = await _page_token(files_client, report)
    stamped = await real_session.execute(
        text("UPDATE file_page_grants SET requests_served = :n WHERE nonce = :nonce"),
        {"n": page_grant_max_requests() - 1, "nonce": token.partition(".")[0]},
    )
    assert stamped.rowcount == 1
    await real_session.commit()

    assert (await _fetch(files_client, token, "index.html")).status_code == 200
    assert (await _fetch(files_client, token, "index.html")).status_code == 404

    served = (
        await real_session.execute(
            text("SELECT requests_served FROM file_page_grants WHERE nonce = :nonce"),
            {"nonce": token.partition(".")[0]},
        )
    ).scalar_one()
    assert served == page_grant_max_requests()


async def test_a_throttled_burst_is_the_opaque_404_not_a_429(
    files_client: AsyncClient, report: Folder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bucket is keyed on the grant and the address, and it refuses quietly.

    A ``429`` here would tell a prober that the nonce they are holding is real,
    which is exactly the bit every other refusal on this mount hides.
    """
    token = await _page_token(files_client, report)
    limiter = rate_limit.REGISTRY.limiter("page_grant")
    monkeypatch.setattr(limiter, "_per_minute", lambda: 1)
    monkeypatch.setattr(limiter, "_burst", lambda: 1)
    limiter.reset()

    assert (await _fetch(files_client, token, "index.html")).status_code == 200
    throttled = await _fetch(files_client, token, "index.html")
    assert throttled.status_code == 404
    assert throttled.json() == NOT_FOUND_BODY

    other = await _page_token(files_client, report)
    assert (await _fetch(files_client, other, "index.html")).status_code == 200


# --------------------------------------------------------------------------
# the log
# --------------------------------------------------------------------------


async def test_the_access_log_keeps_the_relative_path_and_drops_the_token(
    files_client: AsyncClient, report: Folder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An operator sees which file was read; nobody reading the log can replay it.

    The path is the useful half — it is the only place the asset's name appears
    — and the token is a live bearer credential, so exactly one of the two
    survives.
    """
    token = await _page_token(files_client, report)
    # structlog caches a bound logger on first use once the app's logging is
    # configured, and an earlier test on this worker may have used the content
    # app's module logger already; capture_logs only sees a logger that binds
    # after it installs its processors, so the content app gets a fresh one.
    from backend import content_app as content_app_module

    monkeypatch.setattr(
        content_app_module, "log", structlog.get_logger(content_app_module.__name__)
    )
    with capture_logs() as captured:
        served = await _fetch(files_client, token, "assets/chart.png")
    assert served.status_code == 200

    lines = [entry for entry in captured if entry.get("event") == "files.content.access"]
    assert lines, captured
    written = repr(lines)
    assert "assets/chart.png" in written, written
    assert token not in written, "the access log wrote a live grant token"


async def test_a_page_grant_of_another_org_is_the_404_a_bogus_token_gets(
    files_client: AsyncClient, real_session: AsyncSession, report: Folder
) -> None:
    """A grant row of A's, looked up in B's scope, matches nothing.

    The org rides in the signed claim and in the row's own predicate, so
    re-pointing one at another tenant is not a rewrite a holder can make.
    """
    token = await _page_token(files_client, report)
    repo = FilesRepo(real_session, OrgScope(org_team_id=uuid.uuid4()))
    from alkera_core.files.clock import SystemClock
    from alkera_core.files.page_grants import redeem_page_grant

    elsewhere = await redeem_page_grant(
        repo,
        token=token,
        clock=SystemClock(),
        key=settings.effective_files_content_signing_key.encode(),
    )
    assert elsewhere is None
