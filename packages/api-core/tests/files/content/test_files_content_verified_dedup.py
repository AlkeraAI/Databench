"""Dedup adopts an object only after the object has proved it is the right one.

Deduplication makes an object that is already there *the customer's file*, and
the staged upload is the only other copy of those bytes. If what was under the
content key had rotted — truncated by a half-finished write, clipped by a bad
restore — then dropping the temp first hands the customer a corrupt file and
destroys the good bytes with it. So the dedup target is checked against what was
just streamed before anything is deleted; a target that disagrees is not adopted
at all: the staged bytes take the key, and the bad object goes on record the way
``fsck`` records one.
"""

from __future__ import annotations

import hashlib

from alkera_core.config import settings
from alkera_core.files.ids import VersionId
from alkera_core.files.store import keys
from sqlalchemy import text
from tests.files.content.conftest import ContentRig, stream
from tests.files.content.test_files_content_dedup import call_shape

#: Comfortably past the inline cap, so both uploads go through the store.
OBJECT_SIZE = settings.files_inline_max_bytes + 4096

PAYLOAD = (hashlib.sha256(b"verified-dedup").digest() * (OBJECT_SIZE // 32 + 1))[:OBJECT_SIZE]


async def _quarantine(rig: ContentRig) -> list[tuple[str, str, str]]:
    rows = await rig.session.execute(
        text(
            "SELECT kind, ref_id, reason FROM file_quarantine "
            "WHERE org_team_id = :org ORDER BY ref_id"
        ),
        {"org": str(rig.org.org_team_id)},
    )
    return [(row[0], row[1], row[2]) for row in rows.all()]


async def _read(rig: ContentRig, version_id: VersionId) -> bytes:
    chunks = [chunk async for chunk in await rig.service.open(version_id)]
    return b"".join(chunks)


async def test_a_truncated_dedup_target_is_not_adopted_and_the_staged_bytes_win(
    content_rig: ContentRig,
) -> None:
    """The second uploader's bytes are served whole, and the rot is quarantined."""
    first = await content_rig.service.put_version(
        content_rig.node_id("a.bin"), stream(PAYLOAD), size_declared=OBJECT_SIZE, if_match=0
    )
    fresh_shape = [step.split(":")[1] for step in call_shape(content_rig)]
    content_key = keys.object_key(bytes.fromhex(first.content_hash))
    object_path = content_rig.object_path(content_key)
    # The object rots between the two uploads: a half-written restore left it
    # short. Nothing in the metadata knows yet.
    object_path.write_bytes(PAYLOAD[: OBJECT_SIZE // 2])
    content_rig.store.calls.clear()

    second = await content_rig.service.put_version(
        content_rig.node_id("b.bin"), stream(PAYLOAD), size_declared=OBJECT_SIZE, if_match=0
    )
    publish_shape = call_shape(content_rig)

    # The customer's bytes, byte-identical — not the truncated ones that were
    # sitting under the key.
    assert await _read(content_rig, second.id) == PAYLOAD
    assert object_path.read_bytes() == PAYLOAD
    # And the first node, which addresses the same object, is whole again too.
    assert await _read(content_rig, first.id) == PAYLOAD

    # The bad object is on record for an operator, named by the key it occupied.
    assert await _quarantine(content_rig) == [("object", content_key, "fsck.object_size_mismatch")]

    # The observable store work keeps the shape every other publish makes, so
    # the repair is not a timing oracle either.
    assert [step.split(":")[1] for step in publish_shape] == fresh_shape


async def test_an_intact_dedup_target_is_adopted_with_no_quarantine(
    content_rig: ContentRig,
) -> None:
    """The ordinary dedup path is untouched: the temp goes, nothing is recorded."""
    first = await content_rig.service.put_version(
        content_rig.node_id("a.bin"), stream(PAYLOAD), size_declared=OBJECT_SIZE, if_match=0
    )
    fresh_shape = [step.split(":")[1] for step in call_shape(content_rig)]
    content_rig.store.calls.clear()

    second = await content_rig.service.put_version(
        content_rig.node_id("b.bin"), stream(PAYLOAD), size_declared=OBJECT_SIZE, if_match=0
    )
    shape = call_shape(content_rig)

    assert second.content_hash == first.content_hash
    assert await _read(content_rig, second.id) == PAYLOAD
    assert await _quarantine(content_rig) == []
    assert "delete:incoming" in shape
    assert [step.split(":")[1] for step in shape] == fresh_shape
