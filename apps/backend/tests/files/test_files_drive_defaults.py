"""A drive born from ``ensure_org_drive`` has the room its settings allocate.

The drive row is where every quota answer starts, so a drive created with
``quota_bytes = 0`` refuses the org's very first upload with a ``507`` and no
product path ever raises it — only a test fixture does. These tests drive the
real routes against a drive built by the library, with the ceilings left
exactly as the org's first request would find them: no ``UPDATE file_drives``
anywhere below, on purpose.

The configured value is pinned by behaviour rather than by comparison: the
default is moved to a small number and the boundary is asserted *at* it and one
byte past it, so the test fails both if the seeding is removed and if it reads
the wrong setting.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from _files_kit import FilesFixtures
from alkera_core.config import FILES_STORE_DRIVERS, settings
from alkera_core.models.files.stores import STORE_DRIVERS, FileDrive, FileStore
from alkera_core.org_entitlements import org_entitlements
from backend.api.routes.files import PREFIX
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio

UPLOADS = f"{PREFIX}/uploads"

CONTENT_HOST = "files.localhost:8000"
CONTENT_ORIGIN = f"http://{CONTENT_HOST}"

HELLO = b"hello, a brand new drive\n"


async def _no_plan_figure(db: object, org_id: object) -> None:
    """The org's plan sets no storage figure, so the drive's own applies."""
    return None


@pytest.fixture
def content_on(monkeypatch: pytest.MonkeyPatch, files_on: None) -> None:
    """Files enabled, a real store behind it, and a content origin to mint onto."""
    monkeypatch.setattr(settings, "files_content_base_url", CONTENT_ORIGIN)


async def _open_upload(client: AsyncClient, parent_id: uuid.UUID, *, declared_size: int) -> Any:
    return await client.post(
        UPLOADS,
        json={
            "declaredSize": declared_size,
            "name": f"first-{uuid.uuid4().hex}.txt",
            "parentId": str(parent_id),
        },
        headers={"Idempotency-Key": uuid.uuid4().hex},
    )


async def _root_id(fx: FilesFixtures) -> uuid.UUID:
    """`/Shared` — the top-of-drive folder an upload may actually land in.

    The drive root is a traversal-only signpost and refuses every direct write,
    so an upload aimed there would answer `files.container_readonly` before the
    quota it is here to test was ever consulted.
    """
    return uuid.UUID(str((await fx.shared()).id))


async def test_a_brand_new_drive_accepts_the_org_first_upload(
    files_client: AsyncClient, fx: FilesFixtures, files_on: None
) -> None:
    """No fixture surgery: the drive is exactly as ``ensure_org_drive`` left it."""
    response = await _open_upload(files_client, await _root_id(fx), declared_size=len(HELLO))

    assert response.status_code != 507, response.text
    assert response.status_code == 201, response.text
    assert response.json()["uploadId"]


async def test_a_brand_new_drive_accepts_a_content_put(
    files_client: AsyncClient, fx: FilesFixtures, content_on: None
) -> None:
    """The single-call content PUT is the other quota gate, and it opens too."""
    node = await fx.node(b"note.txt")
    drive = await fx.drive()

    response = await files_client.put(
        f"{PREFIX}/drives/{drive.id}/items/{node.id}/content",
        content=HELLO,
        headers={
            "Idempotency-Key": uuid.uuid4().hex,
            "If-Match": f'"{node.etag}"',
            "Content-Type": "application/octet-stream",
            "Content-Length": str(len(HELLO)),
        },
    )

    assert response.status_code != 507, response.text
    assert response.status_code in (200, 201), response.text


async def test_the_drive_row_carries_the_configured_ceilings(
    monkeypatch: pytest.MonkeyPatch, fx: FilesFixtures, real_session: AsyncSession
) -> None:
    """The seeded row is the settings value, not a literal and not zero."""
    monkeypatch.setattr(settings, "files_quota_default_bytes", 4096)
    monkeypatch.setattr(settings, "files_quota_default_nodes", 17)

    drive = await fx.drive()
    reloaded = await real_session.get(FileDrive, drive.id)

    assert reloaded is not None
    assert (reloaded.quota_bytes, reloaded.quota_nodes) == (4096, 17)


@pytest.mark.parametrize(
    ("declared", "expected"),
    [
        pytest.param(64, 201, id="at-the-configured-ceiling"),
        pytest.param(65, 507, id="one-byte-past-it"),
    ],
)
async def test_the_byte_ceiling_is_the_configured_default(
    monkeypatch: pytest.MonkeyPatch,
    files_client: AsyncClient,
    fx: FilesFixtures,
    files_on: None,
    declared: int,
    expected: int,
) -> None:
    """Move the default and the refusal boundary moves with it, exactly.

    The default is what binds a tier the plan sets no storage figure for, so
    the org's plan is pinned to "no figure" here; a plan figure would replace
    the default (see ``test_files_storage_limits``).
    """
    monkeypatch.setattr(settings, "files_quota_default_bytes", 64)
    monkeypatch.setattr(org_entitlements(), "plan_storage_bytes", _no_plan_figure)

    response = await _open_upload(files_client, await _root_id(fx), declared_size=declared)

    assert response.status_code == expected, response.text


@pytest.mark.parametrize(
    ("ceiling", "expected_status", "expected_code"),
    [
        pytest.param(1, 201, None, id="room-for-exactly-this-node"),
        pytest.param(0, 507, "files.quota_nodes", id="no-room-for-any-node"),
    ],
)
async def test_the_node_ceiling_is_the_configured_default(
    monkeypatch: pytest.MonkeyPatch,
    files_client: AsyncClient,
    fx: FilesFixtures,
    files_on: None,
    ceiling: int,
    expected_status: int,
    expected_code: str | None,
) -> None:
    """The node column is seeded from its own setting, not from the byte one.

    The bytes ceiling stays at the product default throughout, so a refusal here
    can only be a *node* refusal — which is what distinguishes a seeded node
    column from a drive left at zero on both axes.
    """
    monkeypatch.setattr(settings, "files_quota_default_nodes", ceiling)

    response = await _open_upload(files_client, await _root_id(fx), declared_size=1)

    assert response.status_code == expected_status, response.text
    if expected_code is not None:
        assert response.json()["code"] == expected_code, response.text


@pytest.mark.parametrize(
    "provider",
    [
        pytest.param("filesystem", id="filesystem"),
        pytest.param("s3_compatible", id="s3_compatible"),
        pytest.param("aws", id="aws"),
    ],
)
async def test_the_first_drive_of_a_fresh_org_is_born_under_every_provider(
    monkeypatch: pytest.MonkeyPatch,
    files_client: AsyncClient,
    files_on: None,
    real_session: AsyncSession,
    provider: str,
) -> None:
    """Every provider the settings admit persists a driver the catalogue allows.

    The store handle stays the filesystem one the fixture installed, so the only
    thing changing here is the value the store-row writer derives from the
    provider — which is exactly what the CHECK on ``file_stores.driver``
    refused on the live stack when the two vocabularies disagreed.
    """
    monkeypatch.setattr(settings, "files_store_provider", provider)
    monkeypatch.setattr(settings, "files_store_bucket", "alkera-files")
    monkeypatch.setattr(settings, "files_store_endpoint", "http://store.invalid:9000")
    monkeypatch.setattr(settings, "files_store_region", "us-east-1")

    response = await files_client.get(f"{PREFIX}/drives")

    assert response.status_code == 200, response.text
    drive_id = uuid.UUID(response.json()["id"])
    drive = await real_session.get(FileDrive, drive_id)
    assert drive is not None
    store = await real_session.get(FileStore, drive.store_id)
    assert store is not None
    assert store.driver in STORE_DRIVERS, store.driver
    assert store.driver == FILES_STORE_DRIVERS[provider]
