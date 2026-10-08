"""The upload sweeper: idle sessions expire, abandoned commits get re-driven.

The deadline is crossed by moving the sweeper's clock past it, never by
waiting, and the effects are read where they live — the session row's state and
hold, and the staged objects on disk.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from alkera_core.files.ids import SessionId
from alkera_core.models.files.uploads import FileUploadSession
from sqlalchemy import select, text
from tests.files.uploads import test_files_upload_complete as _complete

#: The commit test module owns the rig; re-binding its fixture here keeps the
#: two suites on one definition of "a drive, a folder and an upload service".
Rig = _complete.Rig
rig = _complete.rig


async def _ops_for(rig: Rig, session_id: SessionId) -> list[tuple[uuid.UUID, str]]:
    rows = (
        await rig.session.execute(
            text(
                "SELECT id, state FROM file_ops WHERE result ->> 'session_id' = :session "
                "ORDER BY created_at"
            ),
            {"session": str(session_id)},
        )
    ).all()
    return [(row[0], str(row[1])) for row in rows]


async def _age(rig: Rig, session_id: SessionId, *, by: timedelta) -> None:
    await rig.session.execute(
        text(
            "UPDATE file_upload_sessions SET created_at = created_at - "
            "make_interval(secs => :seconds) WHERE id = :id"
        ),
        {"seconds": by.total_seconds(), "id": session_id},
    )
    await rig.session.commit()


async def _row(rig: Rig, session_id: SessionId) -> tuple[str, int]:
    async with rig.repo.transaction():
        statement = select(FileUploadSession.state, FileUploadSession.quota_hold_bytes).where(
            FileUploadSession.id == session_id
        )
        row = (await rig.repo.execute_scoped(statement)).one()
    return str(row[0]), int(row[1])


async def test_an_idle_session_expires_with_its_hold_and_its_parts(rig: Rig) -> None:
    """Past the TTL the room comes back and the staged bytes go."""
    session_id, refs = await rig.upload(b"idle.bin", b"abcdefghijklmnop")
    assert (await _row(rig, session_id))[1] > 0
    assert rig.part_path(session_id, refs[0]).exists()

    swept = await rig.completion.sweep_expired(datetime.now(UTC) + timedelta(days=8))

    assert swept == 1
    assert await _row(rig, session_id) == ("expired", 0)
    assert not rig.part_path(session_id, refs[0]).exists()
    assert not rig.part_path(session_id, refs[1]).exists()


async def test_a_session_inside_its_ttl_is_left_alone(rig: Rig) -> None:
    """The sweeper is a deadline, not a broom: a live upload is untouched."""
    session_id, refs = await rig.upload(b"live.bin", b"abcdefgh")
    state, hold = await _row(rig, session_id)

    swept = await rig.completion.sweep_expired(datetime.now(UTC) + timedelta(days=6))

    assert swept == 0
    assert await _row(rig, session_id) == (state, hold)
    assert rig.part_path(session_id, refs[0]).exists()


async def test_a_stuck_committing_session_is_re_driven(rig: Rig) -> None:
    """A commit whose worker died gets a new operation, not a stuck session."""
    session_id, refs = await rig.upload(b"stuck.bin", b"abcdefgh")
    first = await rig.completion.complete(session_id, refs)
    # The worker died: its operation is gone and the session is still committing.
    await rig.session.execute(
        text("UPDATE file_ops SET state = 'failed' WHERE id = :id"), {"id": first.id}
    )
    await rig.session.commit()
    await _age(rig, session_id, by=timedelta(hours=2))

    redriven = await rig.completion.sweep_expired(datetime.now(UTC))

    assert redriven == 1
    operations = await _ops_for(rig, session_id)
    assert [state for _, state in operations] == ["failed", "queued"]
    assert (await _row(rig, session_id))[0] == "committing"
    # And the re-driven operation actually finishes the upload.
    await rig.completion.promote(session_id, operations[-1][0])  # type: ignore[arg-type]
    assert (await _row(rig, session_id))[0] == "done"


async def test_a_committing_session_with_a_live_operation_is_not_re_driven(rig: Rig) -> None:
    """Twice-driving a commit would duplicate the version; the sweeper refuses."""
    session_id, refs = await rig.upload(b"busy.bin", b"abcdefgh")
    await rig.completion.complete(session_id, refs)
    await _age(rig, session_id, by=timedelta(hours=2))

    assert await rig.completion.sweep_expired(datetime.now(UTC)) == 0
    assert len(await _ops_for(rig, session_id)) == 1


@pytest.mark.parametrize(
    "age",
    [
        pytest.param(timedelta(minutes=59), id="just-inside-the-hour"),
        pytest.param(timedelta(minutes=61), id="just-past-the-hour"),
    ],
)
async def test_the_committing_deadline_is_one_hour(rig: Rig, age: timedelta) -> None:
    """The boundary is the point of the rule, so both sides of it are pinned."""
    session_id, refs = await rig.upload(b"boundary.bin", b"abcdefgh")
    first = await rig.completion.complete(session_id, refs)
    await rig.session.execute(
        text("UPDATE file_ops SET state = 'failed' WHERE id = :id"), {"id": first.id}
    )
    await rig.session.commit()
    await _age(rig, session_id, by=age)

    swept = await rig.completion.sweep_expired(datetime.now(UTC))

    assert swept == (1 if age > timedelta(hours=1) else 0)
