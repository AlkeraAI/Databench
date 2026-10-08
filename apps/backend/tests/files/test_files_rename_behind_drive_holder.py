"""A rename answers through the real route while the org's drive row is held.

A box syncing its chat folders keeps the org drive's row locked for most of
every tree report. A rename in a person's Home used to take that row first, so
it answered 503 ``db_lock_timeout`` -- and a rename onto a taken name answered
the same 503 instead of the conflict it only had to look up. The holder here is
a second connection with an uncommitted update on the drive row, which is what
a running tree report looks like to every other request.
"""

from __future__ import annotations

import secrets

import pytest
from alkera_core.db.session import AsyncSessionLocal
from httpx import AsyncClient
from sqlalchemy import text

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("files_on")]

BASE = "/api/v1/files"


def _idem() -> dict[str, str]:
    return {"Idempotency-Key": secrets.token_hex(8)}


async def _folder(client: AsyncClient, drive: str, parent: str, name: str) -> dict[str, str]:
    made = await client.post(
        f"{BASE}/drives/{drive}/items/{parent}/children",
        json={"name": name, "kind": "folder"},
        headers=_idem(),
    )
    assert made.status_code == 201, made.text
    return {"id": str(made.json()["id"]), "etag": str(made.json()["etag"])}


@pytest.mark.parametrize(
    ("new_name", "status", "code"),
    [
        pytest.param("lvl1", 409, "files.exists", id="taken-name"),
        pytest.param("hidden-renamed", 200, None, id="free-name"),
        pytest.param("données-renamed", 200, None, id="accented"),
        pytest.param("c:d-renamed", 200, None, id="colon"),
    ],
)
async def test_a_rename_is_answered_while_the_drive_row_is_held(
    files_client: AsyncClient, new_name: str, status: int, code: str | None
) -> None:
    drives = await files_client.get(f"{BASE}/drives")
    assert drives.status_code == 200, drives.text
    drive, home = str(drives.json()["id"]), str(drives.json()["homeId"])
    work = await _folder(files_client, drive, home, f"work-{secrets.token_hex(4)}")
    await _folder(files_client, drive, work["id"], "lvl1")
    hidden = await _folder(files_client, drive, work["id"], ".hidden")

    async with AsyncSessionLocal() as holder:
        await holder.execute(
            text("UPDATE file_drives SET next_ino = next_ino + 1 WHERE id = :d"), {"d": drive}
        )
        try:
            renamed = await files_client.patch(
                f"{BASE}/drives/{drive}/items/{hidden['id']}",
                json={"name": new_name},
                headers={**_idem(), "If-Match": hidden["etag"]},
            )
        finally:
            await holder.rollback()

    assert renamed.status_code == status, renamed.text
    if code is not None:
        assert renamed.json()["code"] == code
    listed = await files_client.get(f"{BASE}/drives/{drive}/items/{work['id']}/children")
    assert listed.status_code == 200, listed.text
    names = {item["name"] for item in listed.json()["value"]}
    expected = {"lvl1", ".hidden"} if code is not None else {"lvl1", new_name}
    assert names == expected
