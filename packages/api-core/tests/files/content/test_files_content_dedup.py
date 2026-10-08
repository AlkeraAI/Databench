"""Dedup is invisible: the three paths cost the same store calls in the same order.

A caller must not be able to learn from their own upload whether someone else in
the org already holds those bytes — not from the response, not from the timing,
not from their bill. The proof is the driver's own call log: a fresh upload, a
re-put of the same bytes onto the same node, and the same bytes onto a *different*
node all make the identical sequence of store calls, and only the rows differ.
"""

from __future__ import annotations

import hashlib

import pytest
from alkera_core.config import settings
from alkera_core.files.store import keys
from alkera_core.models.files.tree import FileNode
from alkera_core.models.files.versions import FileVersion
from sqlalchemy import func, select
from tests.files.content.conftest import ContentRig, stream

#: Comfortably past the inline cap, so every path goes through the store.
OBJECT_SIZE = settings.files_inline_max_bytes + 4096

PAYLOAD = (hashlib.sha256(b"dedup").digest() * (OBJECT_SIZE // 32 + 1))[:OBJECT_SIZE]


def call_shape(rig: ContentRig) -> list[str]:
    """The driver's log as (method, key-kind) pairs — the observable work."""
    shape = []
    for call in rig.store.calls:
        key = str(call.key)
        kind = "incoming" if key.startswith("incoming/") else "content"
        shape.append(f"{call.method}:{kind}")
    return shape


async def versions_of(rig: ContentRig, node_id: object) -> list[FileVersion]:
    rows = await rig.session.execute(
        select(FileVersion).where(FileVersion.node_id == node_id).order_by(FileVersion.seq)
    )
    return list(rows.scalars())


async def test_the_three_dedup_paths_make_the_same_store_calls(content_rig: ContentRig) -> None:
    """Fresh, same-node-identical and other-node-identical are indistinguishable."""
    fresh = await content_rig.service.put_version(
        content_rig.node_id("a.bin"), stream(PAYLOAD), size_declared=OBJECT_SIZE, if_match=0
    )
    fresh_shape = call_shape(content_rig)
    content_rig.store.calls.clear()

    same_node = await content_rig.service.put_version(
        content_rig.node_id("a.bin"), stream(PAYLOAD), size_declared=OBJECT_SIZE, if_match=1
    )
    same_node_shape = call_shape(content_rig)
    content_rig.store.calls.clear()

    other_node = await content_rig.service.put_version(
        content_rig.node_id("b.bin"), stream(PAYLOAD), size_declared=OBJECT_SIZE, if_match=0
    )
    other_node_shape = call_shape(content_rig)

    # Same number of calls, same methods, same order — only the publish call's
    # name differs (move vs delete), which never leaves our process.
    assert len(fresh_shape) == len(same_node_shape) == len(other_node_shape)
    assert [step.split(":")[1] for step in fresh_shape] == [
        step.split(":")[1] for step in same_node_shape
    ]
    assert [step.split(":")[1] for step in fresh_shape] == [
        step.split(":")[1] for step in other_node_shape
    ]
    assert fresh_shape[0] == "put:incoming"
    assert fresh_shape[1] == "head:content"
    # The second head asks whether the sweep parked these bytes; every path
    # asks it, so it says nothing about which path this is.
    assert fresh_shape[2] == "head:content"
    assert fresh_shape[4] == "head:content"
    # The publish call is the only step that differs, and it differs in the
    # method name alone — the key it addresses is the temp on every path.
    assert fresh_shape[3] == "move:incoming"
    assert same_node_shape[3] == "delete:incoming"
    assert other_node_shape[3] == "delete:incoming"

    # The rows are where the three differ.
    assert fresh.unchanged is False
    assert same_node.unchanged is True
    assert same_node.id == fresh.id
    assert other_node.unchanged is False
    assert other_node.id != fresh.id


async def test_the_same_bytes_twice_on_one_node_write_no_second_version(
    content_rig: ContentRig,
) -> None:
    first = await content_rig.service.put_version(
        content_rig.node_id("a.bin"), stream(PAYLOAD), size_declared=OBJECT_SIZE, if_match=0
    )
    await content_rig.service.put_version(
        content_rig.node_id("a.bin"), stream(PAYLOAD), size_declared=OBJECT_SIZE, if_match=1
    )

    rows = await versions_of(content_rig, content_rig.node_id("a.bin"))
    assert [row.id for row in rows] == [first.id]
    node = await content_rig.session.get(FileNode, content_rig.node_id("a.bin"))
    assert node is not None
    await content_rig.session.refresh(node)
    # The no-op does not consume an etag: the caller's If-Match still holds.
    assert node.etag == 1
    assert node.head_version_id == first.id


async def test_the_same_bytes_on_another_node_share_one_object(
    content_rig: ContentRig,
) -> None:
    first = await content_rig.service.put_version(
        content_rig.node_id("a.bin"), stream(PAYLOAD), size_declared=OBJECT_SIZE, if_match=0
    )
    second = await content_rig.service.put_version(
        content_rig.node_id("b.bin"), stream(PAYLOAD), size_declared=OBJECT_SIZE, if_match=0
    )

    assert second.content_hash == first.content_hash
    rows = await versions_of(content_rig, content_rig.node_id("b.bin"))
    assert len(rows) == 1
    content_key = keys.object_key(bytes.fromhex(first.content_hash))
    assert rows[0].store_key == content_key
    # One object on disk under the content prefix, and no temp left behind.
    objects = sorted(p for p in content_rig.object_path("objects").rglob("*") if p.is_file())
    assert len(objects) == 1
    assert objects[0].read_bytes() == PAYLOAD
    incoming = content_rig.object_path("incoming")
    assert not incoming.exists() or not any(p.is_file() for p in incoming.rglob("*"))
    # And the shared object is counted once per version, never merged into one row.
    total = await content_rig.session.execute(
        select(func.count())
        .select_from(FileVersion)
        .where(
            FileVersion.content_hash == first.content_hash,
            FileVersion.node_id.in_([content_rig.node_id("a.bin"), content_rig.node_id("b.bin")]),
        )
    )
    assert total.scalar_one() == 2


@pytest.mark.parametrize(
    "size",
    [
        pytest.param(settings.files_inline_max_bytes, id="inline"),
        pytest.param(settings.files_inline_max_bytes + 1, id="object"),
    ],
)
async def test_the_no_op_is_decided_per_node_not_per_org(
    content_rig: ContentRig, size: int
) -> None:
    """b.bin holding the bytes does not make a's first put a no-op."""
    payload = PAYLOAD[:size]
    await content_rig.service.put_version(
        content_rig.node_id("b.bin"), stream(payload), size_declared=size, if_match=0
    )
    onto_a = await content_rig.service.put_version(
        content_rig.node_id("a.bin"), stream(payload), size_declared=size, if_match=0
    )

    assert onto_a.unchanged is False
    assert len(await versions_of(content_rig, content_rig.node_id("a.bin"))) == 1
