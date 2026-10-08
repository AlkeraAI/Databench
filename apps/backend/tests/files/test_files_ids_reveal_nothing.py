"""Every identifier a caller can see is either random or scoped to their drive.

The rule: "node, version, session, operation, lease and grant ids are random
UUIDs; ``ino`` is per drive, allocated in blocks of 1,000 …; etags are per-node
counters; delta tokens are opaque and signed". Each clause closes a different
counting oracle — a sequential id is a platform-wide activity meter, a shared
ino space leaks another tenant's creates, a global etag counter leaks every
write anywhere, and an unsigned token is a cursor a caller can rewind past
their own history.

The properties are proven through the routes rather than against the allocator,
because the thing under test is what a caller receives.
"""

from __future__ import annotations

import base64
import uuid
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import pytest
import pytest_asyncio
from alkera_core.files.ino import DEFAULT_BLOCK
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from tests.conftest import OrgWithAdmin, login
from tests.files.conftest import FilesFixtures

if TYPE_CHECKING:  # the fixtures live in a conftest, which is not an importable package
    pass

pytestmark = pytest.mark.asyncio

BASE = "/api/v1/files"


def _write_headers(idem: Callable[[], dict[str, str]], *, etag: int = 1) -> dict[str, str]:
    return {**idem(), "If-Match": f'"{etag}"'}


async def _create_child(
    client: AsyncClient,
    drive_id: Any,
    parent_id: Any,
    name: str,
    idem: Callable[[], dict[str, str]],
) -> Any:
    return await client.post(
        f"{BASE}/drives/{drive_id}/items/{parent_id}/children",
        json={"name": name, "kind": "folder"},
        headers=_write_headers(idem),
    )


async def _next_ino(session: AsyncSession, drive_id: Any) -> int:
    """What the drive row says the next unreserved ino is, read fresh."""
    await session.commit()
    value = await session.execute(
        text("SELECT next_ino FROM file_drives WHERE id = :id"), {"id": drive_id}
    )
    return int(value.scalar_one())


async def _shared_of(client: AsyncClient, drive: dict[str, Any]) -> Any:
    """``/Shared`` in a drive reached only over HTTP (another org's)."""
    listed = await client.get(f"{BASE}/drives/{drive['id']}/items/{drive['rootId']}/children")
    return str(next(row for row in listed.json()["value"] if row["name"] == "Shared")["id"])


@pytest_asyncio.fixture
async def root(fx: FilesFixtures) -> tuple[Any, Any]:
    """``(driveId, parentId)`` — and the parent is `/Shared`.

    The drive root is a traversal-only signpost and refuses every direct write,
    so a create aimed there never reaches the id allocation under test.
    """
    drive = await fx.drive()
    return drive.id, (await fx.shared()).id


async def test_created_node_ids_are_random_version_4_uuids(
    files_client: AsyncClient,
    files_on: None,
    root: tuple[Any, Any],
    idem: Callable[[], dict[str, str]],
) -> None:
    """Random, not sequential and not derived from the name.

    A v1 uuid would carry a timestamp and a MAC; a v5 would be a hash of the
    name, so a caller could confirm a sibling exists by computing its id.
    """
    drive_id, parent_id = root
    seen: set[uuid.UUID] = set()
    for index in range(6):
        created = await _create_child(files_client, drive_id, parent_id, f"f{index}", idem)
        assert created.status_code == 201, created.text
        node_id = uuid.UUID(created.json()["id"])
        assert node_id.version == 4, f"node id is not a random uuid: {node_id}"
        assert node_id.variant == uuid.RFC_4122
        seen.add(node_id)
    assert len(seen) == 6


async def test_ino_allocation_moves_in_whole_blocks_and_never_per_node(
    files_client: AsyncClient,
    files_on: None,
    real_session: AsyncSession,
    root: tuple[Any, Any],
    idem: Callable[[], dict[str, str]],
) -> None:
    """Creating three nodes never costs the drive counter more than creating one.

    That is the whole point of the block: ``next_ino`` moves in whole blocks,
    reserved apart and kept by the process, so it is never a count of the
    org's nodes. A read-then-write allocator would move it by one per node and
    make the gap between two of a caller's inos an exact create count.
    """
    drive_id, parent_id = root
    before = await _next_ino(real_session, drive_id)

    one = await _create_child(files_client, drive_id, parent_id, "one", idem)
    assert one.status_code == 201, one.text
    after_one = await _next_ino(real_session, drive_id)
    single = after_one - before

    three = await files_client.post(
        f"{BASE}/drives/{drive_id}/items/{parent_id}/tree",
        json={"paths": ["a/b/c"]},
        headers=_write_headers(idem),
    )
    assert three.status_code in (200, 201), three.text
    many = await _next_ino(real_session, drive_id) - after_one

    assert single % DEFAULT_BLOCK == 0, f"a create moved next_ino by {single}, not whole blocks"
    assert many % DEFAULT_BLOCK == 0, f"three creates moved next_ino by {many}, not whole blocks"
    assert many <= max(single, DEFAULT_BLOCK), (
        f"three nodes cost {many} where one node cost {single}"
    )


async def test_etags_count_per_node_and_not_across_the_drive(
    files_client: AsyncClient,
    files_on: None,
    root: tuple[Any, Any],
    idem: Callable[[], dict[str, str]],
) -> None:
    """A write to one node leaves every other node's etag alone.

    A drive-wide (or platform-wide) counter would make an untouched node's etag
    a read-out of how many writes happened anywhere the caller cannot see.
    """
    drive_id, parent_id = root
    first = await _create_child(files_client, drive_id, parent_id, "first", idem)
    second = await _create_child(files_client, drive_id, parent_id, "second", idem)
    assert first.status_code == 201 and second.status_code == 201, second.text
    first_id, second_id = first.json()["id"], second.json()["id"]
    first_etag = int(str(first.json()["etag"]).strip('"'))
    second_etag_before = int(str(second.json()["etag"]).strip('"'))

    renamed = await files_client.patch(
        f"{BASE}/drives/{drive_id}/items/{first_id}",
        json={"name": "first-renamed"},
        headers=_write_headers(idem, etag=first_etag),
    )
    assert renamed.status_code == 200, renamed.text
    assert int(str(renamed.json()["etag"]).strip('"')) == first_etag + 1

    untouched = await files_client.get(f"{BASE}/drives/{drive_id}/items/{second_id}")
    assert untouched.status_code == 200, untouched.text
    assert int(str(untouched.json()["etag"]).strip('"')) == second_etag_before


async def test_a_delta_token_is_opaque_and_a_modified_one_is_refused(
    files_client: AsyncClient, files_on: None, root: tuple[Any, Any]
) -> None:
    """The token names nothing in the clear and does not survive a single edit.

    Without the signature check a caller could decrement the outbox cursor and
    replay a drive's whole history, including the window before a grant that
    the tombstone rules exist to hide.
    """
    drive_id, _ = root
    issued = await files_client.get(f"{BASE}/drives/{drive_id}/delta?token=latest")
    assert issued.status_code == 200, issued.text
    token = str(issued.json()["deltaLink"])

    body, _, signature = token.partition(".")
    assert str(drive_id) not in token, "the token spells the drive id in the clear"
    decoded = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
    assert str(drive_id).encode() in decoded, "the body is not the signed payload we think it is"

    accepted = await files_client.get(f"{BASE}/drives/{drive_id}/delta?token={token}")
    assert accepted.status_code == 200, accepted.text

    # The edit is made to the signature BYTES, not to the base64 text. 32 bytes
    # encode to 43 unpadded characters, and the last of those carries only four
    # significant bits — so rewriting the final character leaves the decoded
    # signature unchanged whenever the new character shares that group, and the
    # server rightly accepts a token nothing about has actually changed.
    raw_signature = base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4))
    tampered = bytes([raw_signature[0] ^ 0x01]) + raw_signature[1:]
    assert tampered != raw_signature
    flipped = base64.urlsafe_b64encode(tampered).decode("ascii").rstrip("=")
    refused = await files_client.get(f"{BASE}/drives/{drive_id}/delta?token={body}.{flipped}")
    assert refused.status_code == 422, refused.text


async def test_two_drives_allocate_inos_from_independent_sequences(
    client: AsyncClient,
    files_on: None,
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    fx: FilesFixtures,
    idem: Callable[[], dict[str, str]],
) -> None:
    """Each drive numbers its nodes out of its own counter, and neither moves the other.

    One platform-wide sequence could never give two orgs' first created nodes
    the same ino, and it would let each org read the other's create volume out
    of the gaps in its own. Both halves are asserted: the neighbour's three
    creates leave this drive's counter where it was, and each drive's nodes
    come from blocks of its own counter, never past it.
    """
    from backend.services.org import teams as team_service

    other_org, other_admin = await team_service.create_org_with_admin(
        real_session,
        org_name=f"other-{uuid.uuid4().hex[:8]}",
        admin_email=f"other-{uuid.uuid4().hex[:8]}@test.dev",
        admin_first_name="Other",
        admin_last_name="Admin",
        admin_password="other-pass-12345",
    )
    await real_session.commit()
    assert other_org.id != org_admin.org_id

    drive = await fx.drive()
    mine_before = await _next_ino(real_session, drive.id)
    my_parent = (await fx.shared()).id

    # One transport, so the neighbour's login replaces ours: their work happens
    # first and we sign back in afterwards.
    neighbour = await login(client, other_admin.email, "other-pass-12345")
    their_drive = await neighbour.get(f"{BASE}/drives")
    assert their_drive.status_code == 200, their_drive.text
    their = their_drive.json()
    their_parent = await _shared_of(neighbour, their)
    their_inos = []
    for index in range(3):
        made = await _create_child(neighbour, their["id"], their_parent, f"n{index}", idem)
        assert made.status_code == 201, made.text
        their_inos.append(int(made.json()["ino"]))

    assert await _next_ino(real_session, drive.id) == mine_before, (
        "another org's creates moved this drive's ino counter"
    )

    # Each drive numbers out of its own counter. A shared sequence would put
    # one org's nodes past the other's counter, and the gaps between one
    # caller's inos would count the other org's creates.
    mine = await login(client, org_admin.admin_email, org_admin.admin_password)
    created = await _create_child(mine, drive.id, my_parent, "mine", idem)
    assert created.status_code == 201, created.text
    # Drawn from the neighbour's own blocks: consecutive, and below the
    # counter their drive now holds, which nothing of mine moved.
    their_after = await _next_ino(real_session, their["id"])
    assert their_inos == [their_inos[0] + index for index in range(3)], (
        f"the neighbour's nodes took inos {their_inos}, not successive inos of one block"
    )
    assert their_inos[-1] < their_after, (
        f"the neighbour's nodes took inos {their_inos} past their own drive's counter "
        f"({their_after})"
    )
    # Mine comes from my own drive: a block this process already holds for it,
    # or the one its counter pointed at, never past it.
    assert int(created.json()["ino"]) <= mine_before, (
        f"my node took ino {created.json()['ino']}, past my own drive's counter ({mine_before})"
    )
