"""A content URL an agent mints is redeemable only under that agent's session.

The bytes never leave the API host: both content routes answer ``302`` to a
signed path, and that path is the whole credential on the origin that serves
it. What keeps it from being a bearer token anyone can forward is the session
recorded on the grant at mint time -- so a mint that recorded no session would
hand out a link that outlives the chat it belongs to, and would look identical
on the wire. These cases pin the recorded binding through redemption, which is
the only place the difference is observable: presented as the minting session
the grant is taken, presented as another it is refused, and the cookie-less
content domain (which presents none) still serves the bytes it was minted for.

Both routes are driven with the same body because they share one mint: the
version lane and the item lane must not drift into two bindings.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable

import pytest
import pytest_asyncio
from _files_kit import FilesFixtures, node_etag
from alkera_core.config import settings
from alkera_core.files import signed_urls
from alkera_core.files.clock import SystemClock
from alkera_core.files.ids import OrgScope, SessionId
from alkera_core.files.repo import FilesRepo
from alkera_core.models.files.tree import FileNode
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio

BASE = "/api/v1/files"

CONTENT_HOST = "files.localhost:8000"
CONTENT_ORIGIN = f"http://{CONTENT_HOST}"

PAYLOAD = b"the agent's own bytes\n"


@pytest.fixture
def content_on(monkeypatch: pytest.MonkeyPatch, files_on: None) -> None:
    """Files enabled, with a content origin for the item lane to mint onto."""
    monkeypatch.setattr(settings, "files_content_base_url", CONTENT_ORIGIN)


@pytest_asyncio.fixture
async def written(
    content_on: None,
    agent_files_client: AsyncClient,
    fx: FilesFixtures,
    real_session: AsyncSession,
    idem: Callable[[], dict[str, str]],
) -> FileNode:
    """A file with real bytes in the store, written by the agent itself."""
    drive = await fx.drive()
    node = await fx.node(b"notes.txt")
    response = await agent_files_client.put(
        f"{BASE}/drives/{drive.id}/items/{node.id}/content",
        content=PAYLOAD,
        headers={
            **idem(),
            "If-Match": await node_etag(real_session, node.id),
            "Content-Type": "application/octet-stream",
            "Content-Length": str(len(PAYLOAD)),
        },
    )
    assert response.status_code in {200, 201}, response.text
    await real_session.refresh(node)
    return node


async def _mint(client: AsyncClient, fx: FilesFixtures, node: FileNode, route: str) -> str:
    """The signed token the route hands out, taken from its ``302``.

    Single use, so every case mints its own rather than replaying one.
    """
    drive = await fx.drive()
    head = node.head_version_id
    path = (
        f"{BASE}/drives/{drive.id}/items/{node.id}/versions/{head}/content"
        if route == "versions"
        else f"{BASE}/drives/{drive.id}/items/{node.id}/content"
    )
    response = await client.get(path, follow_redirects=False)
    assert response.status_code == 302, response.text
    location = response.headers["location"].removeprefix(CONTENT_ORIGIN)
    assert location.startswith(signed_urls.CONTENT_PATH_PREFIX), location
    return location.removeprefix(signed_urls.CONTENT_PATH_PREFIX)


async def _redeem(
    session: AsyncSession, fx: FilesFixtures, token: str, *, presenting: SessionId | None
) -> signed_urls.ContentGrant | None:
    repo = FilesRepo(session, OrgScope(org_team_id=fx.org_team_id))
    return await signed_urls.redeem(
        repo,
        token=token,
        session_id=presenting,
        clock=SystemClock(),
        key=settings.effective_files_content_signing_key.encode(),
    )


ROUTES = [pytest.param("versions", id="versions-content"), pytest.param("items", id="item-content")]


@pytest.mark.parametrize("route", ROUTES)
async def test_the_minting_agent_session_redeems_the_url_it_was_handed(
    route: str,
    agent_files_client: AsyncClient,
    agent_session_id: uuid.UUID,
    fx: FilesFixtures,
    written: FileNode,
    real_session: AsyncSession,
) -> None:
    """The grant names the agent's session, so that session can take it.

    A mint that recorded no session fails here rather than in some later
    forwarding scenario: the presenting session is compared against the row, so
    an unbound row refuses even the agent that minted it.
    """
    token = await _mint(agent_files_client, fx, written, route)

    grant = await _redeem(real_session, fx, token, presenting=SessionId(agent_session_id))

    assert grant is not None
    assert grant.session_id == SessionId(agent_session_id)


@pytest.mark.parametrize("route", ROUTES)
async def test_another_session_cannot_redeem_the_url_the_agent_minted(
    route: str,
    agent_files_client: AsyncClient,
    fx: FilesFixtures,
    written: FileNode,
    real_session: AsyncSession,
) -> None:
    """Forwarded, pasted or lifted from a log, the link is refused elsewhere."""
    token = await _mint(agent_files_client, fx, written, route)

    grant = await _redeem(real_session, fx, token, presenting=SessionId(uuid.uuid4()))

    assert grant is None


@pytest.mark.parametrize("route", ROUTES)
async def test_the_content_domain_still_serves_the_bytes_it_minted(
    route: str,
    agent_files_client: AsyncClient,
    fx: FilesFixtures,
    written: FileNode,
) -> None:
    """The binding does not cost the agent its own download.

    The content origin carries no credential of its own, so it presents no
    session; what it checks is that the row and the token's MAC-covered claim
    name the same one. A URL an agent minted therefore still streams -- to the
    holder of that URL, for the one use it was minted for.
    """
    token = await _mint(agent_files_client, fx, written, route)

    served = await agent_files_client.get(
        f"{signed_urls.CONTENT_PATH_PREFIX}{token}", headers={"Host": CONTENT_HOST}
    )

    assert served.status_code == 200, served.text
    assert served.content == PAYLOAD


@pytest.mark.parametrize("route", ROUTES)
async def test_a_cookie_users_url_names_no_session_at_all(
    route: str,
    files_client: AsyncClient,
    fx: FilesFixtures,
    written: FileNode,
    real_session: AsyncSession,
) -> None:
    """The contrast case: a browser session has no id to bind to yet.

    Asserting it keeps the agent cases honest -- they would pass on a mint that
    bound every caller to a constant, and this one would not.
    """
    token = await _mint(files_client, fx, written, route)

    grant = await _redeem(real_session, fx, token, presenting=None)

    assert grant is not None
    assert grant.session_id is None
