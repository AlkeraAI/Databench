"""A bidi control in a name: refused in a new one, served escaped in an old one.

A right-to-left override (``report<U+202E>gnp.exe`` reads ``reportexe.png``)
makes a name lie about what it is, so the drive refuses one in a name a caller
proposes, whichever control it is. A name stored before that rule still has to
be drawn safely: ``nameDisplay`` is the name every client draws, so it is where
the control is neutralised. The server's list once stopped short of the Arabic
letter mark, U+061C, which reached every client raw.

The server's list and the web client's own copy
(``apps/web/src/lib/files/shownName.ts``) are also pinned to the
same literal set, so neither side can grow or lose a control alone; the web
suite pins its copy to Unicode's Bidi_Control property.
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path

import pytest
import pytest_asyncio
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.files import names
from httpx import AsyncClient
from sqlalchemy import text
from tests.conftest import login
from tests.files._files_kit import FilesOrgFixture

pytestmark = pytest.mark.asyncio

BASE = "/api/v1/files"

#: Unicode's Bidi_Control property, spelled out. The one literal list both sides
#: are held to.
BIDI_CONTROLS: tuple[int, ...] = (
    0x061C,
    0x200E,
    0x200F,
    0x202A,
    0x202B,
    0x202C,
    0x202D,
    0x202E,
    0x2066,
    0x2067,
    0x2068,
    0x2069,
)

WEB_HELPER = Path(__file__).resolve().parents[4] / "apps/web/src/lib/files/shownName.ts"


def _web_bidi_controls() -> set[int]:
    """The code points the web helper's ``BIDI_CONTROLS`` array lists."""
    source = WEB_HELPER.read_text(encoding="utf-8")
    block = re.search(r"BIDI_CONTROLS[^=]*=\s*\[(.*?)\];", source, re.S)
    assert block is not None, f"no BIDI_CONTROLS array in {WEB_HELPER}"
    return {int(value, 16) for value in re.findall(r"0x([0-9a-fA-F]+)", block.group(1))}


def test_the_server_escapes_exactly_the_bidi_controls() -> None:
    assert names.BIDI_CONTROLS == frozenset(BIDI_CONTROLS)


def test_the_web_helper_escapes_exactly_the_same_bidi_controls() -> None:
    assert _web_bidi_controls() == set(BIDI_CONTROLS)


@pytest.mark.parametrize("cp", BIDI_CONTROLS, ids=lambda cp: f"U+{cp:04X}")
def test_display_escapes_each_control_and_parses_back(cp: int) -> None:
    raw = f"a{chr(cp)}b.txt".encode()
    shown = names.display(raw)
    assert chr(cp) not in shown
    assert shown == f"a\\u{cp:04x}b.txt"
    assert names.parse_display(shown) == raw
    assert names.flags(raw).display_warning is True


@pytest_asyncio.fixture
async def member_client(
    client: AsyncClient, files_org: FilesOrgFixture, files_on: None
) -> AsyncClient:
    return await login(client, files_org.member.email, files_org.member_password)


@pytest.mark.parametrize("cp", BIDI_CONTROLS, ids=lambda cp: f"U+{cp:04X}")
async def test_a_name_carrying_a_bidi_control_is_refused_and_nothing_is_made(
    member_client: AsyncClient, cp: int
) -> None:
    drive = await member_client.get(f"{BASE}/drives")
    assert drive.status_code == 200, drive.text
    drive_id, home_id = drive.json()["id"], drive.json()["homeId"]
    tag = uuid.uuid4().hex[:8]

    refused = await member_client.post(
        f"{BASE}/drives/{drive_id}/items/{home_id}/children",
        json={"name": f"qa-{tag}-{chr(cp)}gnp.exe", "kind": "folder"},
        headers={"Idempotency-Key": uuid.uuid4().hex},
    )

    assert refused.status_code == 422, refused.text
    assert refused.json()["code"] == "files.invalid_name.bidi_control"
    assert "bidirectional control" in refused.json()["message"]
    listing = await member_client.get(f"{BASE}/drives/{drive_id}/items/{home_id}/children")
    assert [row for row in listing.json()["value"] if tag in row["nameDisplay"]] == []


@pytest.mark.parametrize("cp", BIDI_CONTROLS, ids=lambda cp: f"U+{cp:04X}")
async def test_a_stored_name_is_served_escaped_on_the_item_and_the_listing(
    member_client: AsyncClient, cp: int
) -> None:
    """A row written before the rule: made under a plain name, then given the
    control directly in the table, the way an older server stored it."""
    drive = await member_client.get(f"{BASE}/drives")
    assert drive.status_code == 200, drive.text
    drive_id, home_id = drive.json()["id"], drive.json()["homeId"]
    tag = uuid.uuid4().hex[:8]
    expected = f"qa-{tag}-\\u{cp:04x}gnp.exe"

    created = await member_client.post(
        f"{BASE}/drives/{drive_id}/items/{home_id}/children",
        json={"name": f"qa-{tag}-gnp.exe", "kind": "folder"},
        headers={"Idempotency-Key": uuid.uuid4().hex},
    )
    assert created.status_code in (200, 201), created.text
    node_id = created.json()["id"]
    stored = f"qa-{tag}-{chr(cp)}gnp.exe".encode()
    async with AsyncSessionLocal() as db:
        await db.execute(text("SET LOCAL row_security = off"))
        changed = await db.execute(
            text(
                "UPDATE file_nodes SET name = :name, name_display = :display, "
                "name_key = :key WHERE id = :id"
            ),
            {
                "name": stored,
                "display": names.display(stored),
                "key": names.name_key(stored),
                "id": uuid.UUID(node_id),
            },
        )
        assert changed.rowcount == 1
        await db.commit()

    item = await member_client.get(f"{BASE}/drives/{drive_id}/items/{node_id}")
    assert item.status_code == 200, item.text
    assert item.json()["nameDisplay"] == expected
    assert chr(cp) not in item.json()["nameDisplay"]

    listing = await member_client.get(f"{BASE}/drives/{drive_id}/items/{home_id}/children")
    assert listing.status_code == 200, listing.text
    shown = [row["nameDisplay"] for row in listing.json()["value"] if row["id"] == node_id]
    assert shown == [expected]
