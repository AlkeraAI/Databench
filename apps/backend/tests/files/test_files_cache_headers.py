"""Two cache policies, one per origin, and neither may be the other's.

The no-oracle contract asks that content responses be ``Cache-Control: private,
no-store``: a shared cache that kept one signed URL's object could hand it to
the next holder of a different token, and ``private`` is what stops a CDN or a
corporate proxy from storing it at all. The API host has its own policy —
``no-store`` plus the compliance ``Pragma`` — applied by the security-headers
middleware, which is deliberately *not* mounted on the content app.

Both halves are asserted here rather than only the content one: if the API's
policy ever leaked onto the content mount (or the middleware started running
there), the content responses would lose ``private`` and nothing else in the
suite would notice.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import pytest
import pytest_asyncio
from alkera_core.config import settings
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

if TYPE_CHECKING:  # the fixtures live in a conftest, which is not an importable package
    from .conftest import FilesFixtures

pytestmark = pytest.mark.asyncio

CONTENT_HOST = "files.localhost:8000"
CONTENT_ORIGIN = f"http://{CONTENT_HOST}"

#: What every response leaving the content mount must carry.
CONTENT_POLICY = "private, no-store"

#: What the API host applies to a response that did not choose its own policy.
API_POLICY = "no-store"

HELLO = b"hello, cache\n"


@pytest.fixture
def content_on(monkeypatch: pytest.MonkeyPatch, files_on: None) -> None:
    monkeypatch.setattr(settings, "files_content_base_url", CONTENT_ORIGIN)


@pytest_asyncio.fixture
async def node(fx: FilesFixtures, real_session: AsyncSession) -> Any:
    """One file node under a drive with room for its bytes."""
    created = await fx.node(b"note.txt")
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


async def _put(
    client: AsyncClient,
    drive_id: uuid.UUID,
    node_id: uuid.UUID,
    payload: bytes,
    *,
    etag: int,
    idem: Callable[[], dict[str, str]],
) -> Any:
    return await client.put(
        f"/api/v1/files/drives/{drive_id}/items/{node_id}/content",
        content=payload,
        headers={
            **idem(),
            "If-Match": f'"{etag}"',
            "Content-Type": "application/octet-stream",
            "Content-Length": str(len(payload)),
        },
    )


async def _on_content_mount(client: AsyncClient, path: str) -> Any:
    return await client.get(path, headers={"Host": CONTENT_HOST})


async def test_a_served_version_is_private_and_never_stored(
    files_client: AsyncClient,
    fx: FilesFixtures,
    node: Any,
    content_on: None,
    idem: Callable[[], dict[str, str]],
) -> None:
    """The 200 that carries user bytes is the response that matters most."""
    drive = await fx.drive()
    written = await _put(files_client, drive.id, node.id, HELLO, etag=node.etag, idem=idem)
    assert written.status_code in (200, 201), written.text

    minted = await files_client.get(f"/api/v1/files/drives/{drive.id}/items/{node.id}/content")
    assert minted.status_code == 302, minted.text
    served = await _on_content_mount(
        files_client, str(minted.headers["location"]).removeprefix(CONTENT_ORIGIN)
    )

    assert served.status_code == 200
    assert served.content == HELLO
    assert served.headers["cache-control"] == CONTENT_POLICY


async def test_a_refusal_on_the_content_mount_is_private_and_never_stored(
    files_client: AsyncClient, content_on: None
) -> None:
    """A forged token's 404 carries the same policy the 200 does.

    A refusal that a proxy was allowed to store would let the proxy answer the
    next holder of that URL from its own cache, which is the one thing the
    single-use token exists to prevent.
    """
    refused = await _on_content_mount(files_client, f"/c/{'0' * 32}.{'0' * 64}")

    assert refused.status_code == 404
    assert refused.headers["cache-control"] == CONTENT_POLICY


@pytest.mark.parametrize(
    "route",
    [
        pytest.param("drives", id="get-drive"),
        pytest.param("children", id="list-children"),
        pytest.param("delta", id="delta"),
    ],
)
async def test_the_api_list_responses_carry_the_api_policy_not_the_content_one(
    files_client: AsyncClient, fx: FilesFixtures, files_on: None, route: str
) -> None:
    """The API host's own policy, and specifically not ``private``.

    ``private`` on an API response would be a weaker statement than the API
    host intends (it permits a browser-local store), so the two policies must
    stay distinct rather than one being copied over the other.
    """
    drive = await fx.drive()
    urls = {
        "drives": "/api/v1/files/drives",
        "children": f"/api/v1/files/drives/{drive.id}/items/{drive.root_node_id}/children",
        "delta": f"/api/v1/files/drives/{drive.id}/delta?token=latest",
    }
    response = await files_client.get(urls[route])

    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == API_POLICY
    assert response.headers["pragma"] == "no-cache"
