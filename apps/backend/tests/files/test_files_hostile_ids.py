"""A Files id the caller mangled is a 422, never the 500 the audit row earned.

Files is left out of the request-wide NUL scan because it names its own
refusals, so a drive or item id reached the route as typed. The route resolved
the node, the decision was recorded, and the row carried the raw path into the
outbox, where Postgres refused the NUL and the request ended as a 500.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
import pytest_asyncio
from alkera_core.config import settings
from alkera_core.models.files.leases import FileLease
from backend.api.routes.files.leases import HOLDER_ID_MAX
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def node(fx: Any, real_session: AsyncSession) -> Any:
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


@pytest.mark.parametrize(
    "drive_segment",
    [
        pytest.param("%00", id="nul"),
        pytest.param("a%01b", id="control-character"),
        pytest.param("d" * 10_000, id="ten-kilobytes"),
    ],
)
async def test_a_mangled_drive_id_is_refused_before_the_route_runs(
    files_client: AsyncClient, files_on: None, node: Any, drive_segment: str
) -> None:
    response = await files_client.get(
        f"/api/v1/files/drives/{drive_segment}/items/{node.id}/content"
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "validation_error"


async def test_a_null_byte_in_a_files_query_string_is_refused_at_the_boundary(
    files_client: AsyncClient, files_on: None, fx: Any, node: Any
) -> None:
    """Files answers for its ids and its names, not for the rest of a request:
    a query string is checked there as it is on every other route."""
    drive = await fx.drive()
    response = await files_client.get(
        f"/api/v1/files/drives/{drive.id}/items/{node.id}?probe=a%00b"
    )
    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "unstorable_text"
    assert error["details"]["field"] == "query.probe"


async def test_a_null_byte_in_a_field_files_does_not_validate_is_refused_at_the_boundary(
    files_client: AsyncClient, files_on: None, fx: Any
) -> None:
    drive = await fx.drive()
    shared = await fx.shared()
    response = await files_client.post(
        f"/api/v1/files/drives/{drive.id}/items/{shared.id}/lease",
        json={"instanceId": "i\x00d", "machineId": "m"},
        headers={"Idempotency-Key": uuid.uuid4().hex, "If-Match": f'"{shared.etag}"'},
    )
    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "unstorable_text"
    assert error["details"]["field"] == "body.instanceId"


async def test_a_null_byte_in_a_name_is_still_files_own_refusal(
    files_client: AsyncClient, files_on: None, fx: Any
) -> None:
    """The asymmetric half: a name is Files' to refuse, in the code its
    clients' copy is keyed off, even beside a query the boundary would pass."""
    drive = await fx.drive()
    shared = await fx.shared()
    response = await files_client.post(
        f"/api/v1/files/drives/{drive.id}/items/{shared.id}/children?probe=plain",
        json={"name": "a\x00b", "kind": "folder"},
        headers={"Idempotency-Key": uuid.uuid4().hex},
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "files.invalid_name.nul"


async def test_the_real_ids_still_resolve(
    files_client: AsyncClient, files_on: None, fx: Any, node: Any
) -> None:
    """The asymmetric half: the shape refuses mangled ids, not ordinary ones."""
    drive = await fx.drive()
    response = await files_client.get(f"/api/v1/files/drives/{drive.id}/items/{node.id}")
    assert response.status_code == 200, response.text


async def test_a_lease_holder_id_wider_than_its_column_is_a_422(
    files_client: AsyncClient, files_on: None, fx: Any
) -> None:
    """A megabyte instance id reached the INSERT and was a 500."""
    drive = await fx.drive()
    shared = await fx.shared()
    response = await files_client.post(
        f"/api/v1/files/drives/{drive.id}/items/{shared.id}/lease",
        json={"instanceId": "i" * (HOLDER_ID_MAX + 1), "machineId": "m"},
        headers={"Idempotency-Key": uuid.uuid4().hex, "If-Match": f'"{shared.etag}"'},
    )
    assert response.status_code == 422, response.text


def test_the_holder_id_bound_is_the_columns_width() -> None:
    columns = FileLease.__table__.c
    assert HOLDER_ID_MAX == columns.holder_instance_id.type.length == columns.machine_id.type.length
