"""``POST …/children`` makes what the body asked for, or says why it cannot.

The route's namespace kinds are folder, symlink and special: a file exists once
its first version does, so this route has never been able to make one. What it
used to do with a body that asked for one anyway was answer ``201`` describing a
FOLDER of that name — ``kind: "file"`` fell outside the literal and a Drive-style
``file: {}`` facet was simply an unknown key — and the caller found out only when
the content ``PUT`` that followed answered ``404``, which reads as "the file you
just made has vanished" rather than "you were handed a folder".
"""

from __future__ import annotations

from typing import Any

import pytest
from _files_kit import FilesFixtures
from httpx import AsyncClient

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("files_on")]

BASE = "/api/v1/files"


async def _drive_and_home(client: AsyncClient) -> tuple[str, str]:
    response = await client.get(f"{BASE}/drives")
    assert response.status_code == 200, response.text
    body = response.json()
    return str(body["id"]), str(body["homeId"])


async def test_a_folder_is_made_and_is_a_folder(
    files_client: AsyncClient, fx: FilesFixtures, idem: Any
) -> None:
    """The kind this route does serve, so the refusal below is about the body
    and not about the route being shut."""
    await fx.drive()
    drive_id, home_id = await _drive_and_home(files_client)
    made = await files_client.post(
        f"{BASE}/drives/{drive_id}/items/{home_id}/children",
        json={"name": "reports", "kind": "folder"},
        headers=idem(),
    )
    assert made.status_code == 201, made.text
    assert made.json()["kind"] == "folder"


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"name": "secret.txt", "kind": "file"}, id="kind-names-a-file"),
        pytest.param({"name": "secret.txt", "file": {}}, id="a-file-facet-with-no-kind"),
        pytest.param(
            {"name": "secret.txt", "kind": "folder", "file": {}},
            id="a-file-facet-contradicting-the-kind",
        ),
    ],
)
async def test_a_body_asking_for_a_file_is_refused_by_name(
    files_client: AsyncClient, fx: FilesFixtures, idem: Any, body: dict[str, Any]
) -> None:
    """422 with a code that names the next call, and — the part that matters —
    NOTHING is created: the old behaviour's whole cost was the folder it left
    behind under the name the caller wanted for a file."""
    await fx.drive()
    drive_id, home_id = await _drive_and_home(files_client)
    refused = await files_client.post(
        f"{BASE}/drives/{drive_id}/items/{home_id}/children",
        json=body,
        headers=idem(),
    )
    assert refused.status_code == 422, refused.text
    assert refused.json()["code"] == "files.file_needs_content"

    listed = await files_client.get(f"{BASE}/drives/{drive_id}/items/{home_id}/children")
    assert listed.status_code == 200
    assert [child["name"] for child in listed.json()["value"]] == []
