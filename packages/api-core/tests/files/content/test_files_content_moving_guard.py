"""A commit onto a node a batched move is rewriting is refused, not published.

The small verbs (`create`, `rename`, `move`) already refuse a write into a
subtree stamped `moving`. A content commit is the same kind of write — it swaps
the node's head and bumps its etag — so it has to give the same answer, and it
has to give it in the commit transaction rather than before the upload: a move
can start while the caller's bytes are still streaming.

The subtree is stamped the way `run_large_move` stamps it — one `UPDATE` over
`path_ids <@ root`, committed — so the guard is read off the row a real move
would have written and not off a value this test handed the service.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import pytest
from alkera_core.files.errors import Conflict
from alkera_core.files.ids import NodeId
from sqlalchemy import text
from tests.files.content.conftest import ContentRig, stream

PAYLOAD = b"a version that must not land while the tree is being rewritten"


@dataclass(frozen=True, slots=True)
class Row:
    """The three columns a commit changes, read as plain values.

    The rig shares one session with the service, and a refused put rolls that
    session back — which expires every mapped instance — so the assertions hold
    values rather than ORM rows.
    """

    etag: int
    head_version_id: uuid.UUID | None
    state: str


async def _stamp(rig: ContentRig, path: str, state: str) -> None:
    """Mark a subtree, the way the move runner's one statement does."""
    await rig.session.execute(
        text(
            "UPDATE file_nodes SET state = :state WHERE org_team_id = :org "
            "AND path_ids <@ CAST(:path AS ltree)"
        ),
        {"state": state, "org": rig.org.org_team_id, "path": path},
    )
    await rig.session.commit()


async def _read(rig: ContentRig, node_id: uuid.UUID) -> Row:
    row = (
        await rig.session.execute(
            text(
                "SELECT etag, head_version_id, state FROM file_nodes "
                "WHERE id = :id AND org_team_id = :org"
            ),
            {"id": node_id, "org": rig.org.org_team_id},
        )
    ).one()
    return Row(etag=int(row[0]), head_version_id=row[1], state=str(row[2]))


async def test_a_commit_into_a_moving_subtree_is_refused_and_lands_nothing(
    content_rig: ContentRig,
) -> None:
    node_id = content_rig.files["a.bin"].id
    path = content_rig.files["a.bin"].path_ids
    before = await _read(content_rig, node_id)
    await _stamp(content_rig, path, "moving")

    with pytest.raises(Conflict) as raised:
        await content_rig.service.put_version(
            content_rig.node_id("a.bin"),
            stream(PAYLOAD),
            size_declared=len(PAYLOAD),
            if_match=before.etag,
        )
    assert raised.value.code == "files.moving"

    after = await _read(content_rig, node_id)
    assert after.head_version_id == before.head_version_id
    assert after.etag == before.etag


async def test_the_same_commit_is_accepted_once_the_move_has_finished(
    content_rig: ContentRig,
) -> None:
    """The negative twin: the refusal is the flag, not the payload or the node."""
    # Captured as plain values: a refused put rolls the shared session back,
    # which expires every mapped instance the rig is holding.
    node_id = content_rig.files["a.bin"].id
    path = content_rig.files["a.bin"].path_ids
    before = await _read(content_rig, node_id)

    await _stamp(content_rig, path, "moving")
    with pytest.raises(Conflict):
        await content_rig.service.put_version(
            NodeId(node_id),
            stream(PAYLOAD),
            size_declared=len(PAYLOAD),
            if_match=before.etag,
        )

    await _stamp(content_rig, path, "live")
    info = await content_rig.service.put_version(
        NodeId(node_id),
        stream(PAYLOAD),
        size_declared=len(PAYLOAD),
        if_match=before.etag,
    )

    assert info.size == len(PAYLOAD)
    after = await _read(content_rig, node_id)
    assert after.head_version_id == uuid.UUID(str(info.id))
    assert after.etag == before.etag + 1


async def test_a_sibling_outside_the_moving_subtree_still_commits(
    content_rig: ContentRig,
) -> None:
    """Only the marked subtree is refused — the flag is not a folder-wide freeze."""
    await _stamp(content_rig, content_rig.files["a.bin"].path_ids, "moving")

    node_id = content_rig.files["b.bin"].id
    other = await _read(content_rig, node_id)
    info = await content_rig.service.put_version(
        content_rig.node_id("b.bin"),
        stream(PAYLOAD),
        size_declared=len(PAYLOAD),
        if_match=other.etag,
    )
    assert info.size == len(PAYLOAD)
    assert (await _read(content_rig, node_id)).etag == other.etag + 1
