"""Bytes on demand with a REAL box against the REAL gateway.

Every other proof of a promotion drives one side with a double: the backend
suites answer the drive's request from a hand-held socket that acks whatever
the case scripts, and the holder suites hand ``MachineRequests`` a frame a test
built. Neither proves the seam between them — that the request the drive
publishes is one the box's own transport receives on the channel it
subscribed, that the box's answer is one the drive reads, and that the file the
box then pushes lands where the waiting reader is served from.

So here both ends are the production code. A uvicorn instance serves the real
app (real Postgres, real ``pg_notify`` lane, real listener and hub, the real
content route). The box is :class:`ChatFolders` holding the chat's folder with
its live sync watching the working directory, and a :class:`CloudSocket` minted
as the registered machine, holding ``machine:<id>`` and answering with
:class:`MachineRequests` — the pair ``CloudMirrorService`` wires at
registration.

The content queue is held back by the server's own knob: the lease is granted
with a bandwidth window smaller than any file a case writes, so a write's row
reaches the drive (the tree report is not windowed) and its bytes do not. That
is the state a reader meets while a clone's backlog trails its rows, and the
one a promotion exists for: the promoted file goes past the window on the
burst, and nothing else does.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import statistics
import time
import uuid
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from alkera_cli.cloud import CloudRestClient, CloudSocket
from alkera_cli.cloud.machine_requests import MachineRequests
from alkera_core.config import settings
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from test_cloud_live_folder import (  # noqa: F401  (fixtures registered by import)
    HTTP_TIMEOUT,
    SETUP_WAIT,
    LiveChat,
    _as_member,
    _until,
    content_server,
    live_chat,
)
from tests.chat_shares import files_on  # noqa: F401
from tests.files._promote_world import drained, requests_heard

pytestmark = [
    pytest.mark.usefixtures("files_on"),
    # Each case serves a real app, a real box and a real watcher; in series on
    # one worker so a wait measures the chain, not the other servers queued in
    # front of it.
    pytest.mark.xdist_group("cloud_live_promote"),
]

#: The bandwidth window the lease is granted with, in bytes a minute. The
#: fixture's readiness write (6 bytes) fits; every file a case writes is larger,
#: so its bytes wait on the window for the whole case unless it is promoted.
WINDOW_BYTES = 64

#: A file larger than the window: its row lands, its bytes do not.
BODY = b"id,total\n" + b"".join(b"%d,%d\n" % (i, i * 10) for i in range(40))
assert len(BODY) > WINDOW_BYTES

#: How long past a write a case waits before asking for it. The holder does
#: not read a file modified within its settle time (2 s by default), so a case
#: that asked sooner would be timing the settle, not the request path.
SETTLE_SECONDS = 2.2

#: A backstop on a frame or a landing the chain itself signals; never a claim.
BACKSTOP = 30.0


@pytest.fixture(autouse=True)
def _a_window_no_case_fits_in(monkeypatch: pytest.MonkeyPatch) -> None:
    """Served with the lease the box takes, so autouse: it must be in place
    before ``live_chat`` takes the folder."""
    monkeypatch.setattr(settings, "files_live_bandwidth_bytes_per_minute", WINDOW_BYTES)


# --------------------------------------------------------------------------- #
# the box's socket
# --------------------------------------------------------------------------- #


@dataclass
class BoxSocket:
    """The box's socket and every request its machine channel carried."""

    socket: CloudSocket
    rest: CloudRestClient
    channel: str
    requests: list[dict[str, Any]] = field(default_factory=list)
    acks: list[dict[str, Any]] = field(default_factory=list)

    async def close(self) -> None:
        await self.socket.stop()


class _Heard(logging.Handler):
    """The transport's own word that the gateway admitted the channel."""

    def __init__(self, channel: str) -> None:
        super().__init__(level=logging.INFO)
        self.channel = channel
        self.held = asyncio.Event()

    def emit(self, record: logging.LogRecord) -> None:
        if "holding the machine channel" in record.getMessage() and self.channel in (
            record.getMessage()
        ):
            self.held.set()


@contextlib.asynccontextmanager
async def box_socket(live: LiveChat) -> AsyncIterator[BoxSocket]:
    """The box's socket, minted as its registered machine and holding
    ``machine:<id>``, answering the drive with the production handler over the
    folders the box really holds."""
    rest = CloudRestClient(
        api_url=f"http://{live.addr}", token=live.box_token, agent_id=live.box_machine
    )
    socket = CloudSocket(rest)
    channel = f"machine:{live.box_machine}"
    box = BoxSocket(socket=socket, rest=rest, channel=channel)
    answers = MachineRequests(live.folders)

    def handle(frame: Mapping[str, Any]) -> Mapping[str, Any] | None:
        box.requests.append(dict(frame))
        ack = answers.handle(frame)
        if ack is not None:
            box.acks.append(dict(ack))
        return ack

    heard = _Heard(channel)
    transport_log = logging.getLogger("alkera_cli.cloud.transport")
    previous = transport_log.level
    transport_log.setLevel(logging.INFO)
    transport_log.addHandler(heard)
    try:
        socket.bind_machine(live.box_machine, handle)
        await socket.start()
        await asyncio.wait_for(heard.held.wait(), BACKSTOP)
        yield box
    finally:
        transport_log.removeHandler(heard)
        transport_log.setLevel(previous)
        with contextlib.suppress(Exception):
            await socket.stop()


# --------------------------------------------------------------------------- #
# rows, bytes and reads
# --------------------------------------------------------------------------- #


async def _row(db: AsyncSession, parent_id: str, name: bytes) -> Any:
    row = (
        await db.execute(
            text(
                "SELECT id, head_version_id, holder_size, holder_mtime_ns FROM file_nodes "
                "WHERE parent_id = :parent AND name = :name AND trashed_at IS NULL"
            ),
            {"parent": uuid.UUID(parent_id), "name": name},
        )
    ).one_or_none()
    await db.commit()
    return row


async def written_unlanded(
    live: LiveChat, db: AsyncSession, name: str, body: bytes = BODY
) -> uuid.UUID:
    """Write ``name`` on the box and wait until the drive lists it with the
    holder's stamp and no bytes; then until the holder would read it."""
    path = live.working_dir / name
    await asyncio.to_thread(path.write_bytes, body)
    written = time.monotonic()
    stamp = path.stat()

    async def listed() -> Any:
        row = await _row(db, live.scratch_node_id, name.encode())
        if row is None or row.holder_size != stamp.st_size:
            return None
        return row if row.holder_mtime_ns == stamp.st_mtime_ns else None

    row = await _until(listed, window=SETUP_WAIT, what=f"{name}'s row reaching the drive")
    assert row.head_version_id is None, f"{name}'s bytes landed through a {WINDOW_BYTES}B window"
    await asyncio.sleep(max(0.0, SETTLE_SECONDS - (time.monotonic() - written)))
    return uuid.UUID(str(row.id))


async def grant(client: httpx.AsyncClient, live: LiveChat, node_id: uuid.UUID) -> httpx.Response:
    return await client.post(
        live.url(f"{node_id}/content-grants"),
        json={"kind": "file"},
        headers={"Idempotency-Key": uuid.uuid4().hex},
    )


async def redeem(minted: httpx.Response) -> bytes:
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as anonymous:
        served = await anonymous.get(str(minted.json()["url"]))
    assert served.status_code == 200, served.text
    return served.content


def lease_epoch(live: LiveChat) -> int:
    held = live.folders.held(live.chat_id)
    assert held is not None
    return int(held.record.epoch)


# --------------------------------------------------------------------------- #
# the happy path
# --------------------------------------------------------------------------- #


async def test_a_reader_opening_an_unlanded_file_is_served_it_ahead_of_the_backlog(
    live_chat: LiveChat,  # noqa: F811  (the imported fixture)
    real_session: AsyncSession,
) -> None:
    """The whole seam, five times over: the box receives exactly one request
    naming the lease, the node, the path under the lease and the lease's
    epoch; the file it names goes past the window while a file queued before
    it does not; and the reader's grant answers from the store within the
    drive's own wait.

    The median of the five opens is printed: it is the figure the design
    budgets a click against."""
    backlog = await written_unlanded(live_chat, real_session, "backlog.csv")
    timings: list[float] = []
    async with box_socket(live_chat) as box, _as_member(live_chat) as member:
        for index in range(5):
            name = f"asked-{index}.csv"
            body = BODY + b"%d\n" % index
            node_id = await written_unlanded(live_chat, real_session, name, body)
            before = len(box.requests)
            started = time.monotonic()
            minted = await grant(member, live_chat, node_id)
            timings.append(time.monotonic() - started)
            assert minted.status_code == 201, minted.text
            assert minted.json()["contentState"] == "on_drive"
            assert await redeem(minted) == body

            asked = box.requests[before:]
            assert len(asked) == 1, f"the box was asked {len(asked)} times for one open"
            request = asked[0]
            assert (
                request["kind"],
                request["lease_node_id"],
                request["node_id"],
                request["path"],
                request["epoch"],
            ) == (
                "promote",
                live_chat.chat_node_id,
                str(node_id),
                f"scratch/{name}",
                lease_epoch(live_chat),
            )
            assert box.acks[-1]["outcome"] == "accepted"
            assert timings[-1] < settings.files_promote_wait_seconds

    still = await _row(real_session, live_chat.scratch_node_id, b"backlog.csv")
    assert str(still.id) == str(backlog)
    assert still.head_version_id is None, "the file queued first went past the window unasked"
    print(
        f"\npromote happy path: median {statistics.median(timings):.3f}s over five opens "
        f"({', '.join(f'{t:.3f}' for t in timings)})"
    )


# --------------------------------------------------------------------------- #
# what asks nothing
# --------------------------------------------------------------------------- #


async def test_a_file_the_drive_already_holds_asks_the_box_nothing(
    live_chat: LiveChat,  # noqa: F811  (the imported fixture)
    real_session: AsyncSession,
) -> None:
    """``ready.txt`` landed in the fixture: its bytes are the machine's own, so
    the read is the store's and no request is published or delivered."""
    ready = await _row(real_session, live_chat.scratch_node_id, b"ready.txt")
    assert ready is not None and ready.head_version_id is not None
    async with box_socket(live_chat) as box, _as_member(live_chat) as member:
        with requests_heard() as heard:
            minted = await grant(member, live_chat, uuid.UUID(str(ready.id)))
            assert minted.status_code == 201, minted.text
            assert await redeem(minted) == b"ready\n"
            await asyncio.sleep(0.5)
            assert drained(heard) == [], "a promote was published for a file on the drive"
        assert box.requests == []
    # Not ``behind``, and nothing marks it as older. (A landed file with no
    # report pending reads ``none`` here, while the read a landing answered
    # reads ``on_drive``: both mean the store's copy is the machine's.)
    assert minted.json()["contentState"] in ("none", "on_drive")
    assert "x-alkera-content-state" not in minted.headers


# --------------------------------------------------------------------------- #
# a box that cannot answer
# --------------------------------------------------------------------------- #


async def test_with_the_boxs_socket_closed_a_reader_is_told_timed_out_at_the_ack_window(
    live_chat: LiveChat,  # noqa: F811  (the imported fixture)
    real_session: AsyncSession,
) -> None:
    """The box still beats its lease over HTTP, so the drive still serves the
    folder as ``live`` and asks — ``offline`` is only for a lease that stopped
    beating. Nobody holds the machine's channel, so nobody answers, and the
    reader is told ``timed_out`` when the ack window closes: two seconds, not
    the eight a download waits for bytes."""
    node_id = await written_unlanded(live_chat, real_session, "unanswered.csv")
    async with _as_member(live_chat) as member:
        listed = await member.get(live_chat.url(f"{node_id}"))
        assert listed.status_code == 200, listed.text
        assert listed.json()["lease"]["served"] == "live"
        started = time.monotonic()
        answer = await grant(member, live_chat, node_id)
        elapsed = time.monotonic() - started
    assert answer.status_code == 409, answer.text
    assert answer.headers["retry-after"] == "2"
    assert answer.json()["detail"]["outcome"] == "timed_out"
    assert settings.files_promote_ack_seconds * 0.9 <= elapsed
    assert elapsed < settings.files_promote_wait_seconds


async def test_a_file_the_box_rewrote_is_served_from_the_older_copy_marked_behind(
    live_chat: LiveChat,  # noqa: F811  (the imported fixture)
    real_session: AsyncSession,
) -> None:
    """The drive holds a copy the box has since rewritten; the box's socket is
    closed, so the newer bytes cannot be fetched in time. The read serves the
    store's older copy, and says so on the response and in the grant."""
    small = b"v1\n"
    path = live_chat.working_dir / "notes.txt"
    await asyncio.to_thread(path.write_bytes, small)
    landed = await _until(
        _landed(real_session, live_chat.scratch_node_id, b"notes.txt"),
        window=SETUP_WAIT,
        what="the first copy of notes.txt landing",
    )
    node_id = uuid.UUID(str(landed.id))
    await asyncio.to_thread(path.write_bytes, BODY)
    stamp = path.stat()

    async def reported_newer() -> Any:
        row = await _row(real_session, live_chat.scratch_node_id, b"notes.txt")
        return row if row is not None and row.holder_size == stamp.st_size else None

    await _until(reported_newer, window=SETUP_WAIT, what="the rewrite reaching the drive's row")
    async with _as_member(live_chat) as member:
        minted = await grant(member, live_chat, node_id)
        assert minted.status_code == 201, minted.text
        assert minted.headers["x-alkera-content-state"] == "behind"
        assert minted.headers.get("x-alkera-content-as-of")
        assert minted.json()["contentState"] == "behind"
        assert minted.json()["asOf"] is not None
        assert await redeem(minted) == small


def _landed(db: AsyncSession, parent_id: str, name: bytes) -> Any:
    async def landed() -> Any:
        row = await _row(db, parent_id, name)
        return row if row is not None and row.head_version_id is not None else None

    return landed


# --------------------------------------------------------------------------- #
# a box that refuses
# --------------------------------------------------------------------------- #


async def test_a_request_under_an_epoch_the_box_does_not_hold_is_refused_not_holder(
    live_chat: LiveChat,  # noqa: F811  (the imported fixture)
    real_session: AsyncSession,
) -> None:
    """The lease is re-acquired on the drive (its epoch moves) while the box
    still holds the folder at the old one. The request names the new epoch;
    the box is not the holder the drive means, says so, promotes nothing, and
    the reader falls back to ``live_pending`` at once."""
    node_id = await written_unlanded(live_chat, real_session, "handed-over.csv")
    held_at = lease_epoch(live_chat)
    live_chat.stop_beating()
    await real_session.execute(
        text("UPDATE file_leases SET epoch = epoch + 1 WHERE node_id = :node"),
        {"node": uuid.UUID(live_chat.chat_node_id)},
    )
    await real_session.commit()
    async with box_socket(live_chat) as box, _as_member(live_chat) as member:
        answer = await grant(member, live_chat, node_id)
        assert answer.status_code == 409, answer.text
        assert answer.json()["detail"]["outcome"] == "not_holder"
        assert [r["epoch"] for r in box.requests] == [held_at + 1]
        assert [a["outcome"] for a in box.acks] == ["not_holder"]
    held = live_chat.folders.held(live_chat.chat_id)
    assert held is not None and held.live is not None
    assert "handed-over.csv" not in held.live.promotions


async def test_a_request_for_a_path_the_box_no_longer_has_is_missing(
    live_chat: LiveChat,  # noqa: F811  (the imported fixture)
    real_session: AsyncSession,
) -> None:
    """The drive names the file by a path the box's disk no longer carries (the
    row moved on the drive; the box has not heard). The box answers
    ``missing`` and reads nothing; the reader falls back at once."""
    node_id = await written_unlanded(live_chat, real_session, "moved.csv")
    await real_session.execute(
        text("UPDATE file_nodes SET name = :name WHERE id = :node"),
        {"name": b"elsewhere.csv", "node": node_id},
    )
    await real_session.commit()
    async with box_socket(live_chat) as box, _as_member(live_chat) as member:
        answer = await grant(member, live_chat, node_id)
        assert answer.status_code == 409, answer.text
        assert answer.json()["detail"]["outcome"] == "missing"
        assert [r["path"] for r in box.requests] == ["scratch/elsewhere.csv"]
        assert [a["outcome"] for a in box.acks] == ["missing"]


async def test_a_path_naming_another_file_is_changed_and_never_serves_that_file(
    live_chat: LiveChat,  # noqa: F811  (the imported fixture)
    real_session: AsyncSession,
) -> None:
    """The drive names node A by the path of file B (its row was renamed on
    the drive onto a name the box's disk gives another file). The box compares
    what B is with what the drive expects of A, answers ``changed`` with what
    it sees, and promotes nothing: A is never served B's bytes."""
    node_a = await written_unlanded(live_chat, real_session, "a.csv", BODY + b"a\n")
    other = BODY + b"a much longer tail than a's\n"
    await written_unlanded(live_chat, real_session, "b.csv", other)
    b_row = await _row(real_session, live_chat.scratch_node_id, b"b.csv")
    await real_session.execute(
        text("UPDATE file_nodes SET name = :name WHERE id = :node"),
        {"name": b"b-elsewhere.csv", "node": b_row.id},
    )
    await real_session.execute(
        text("UPDATE file_nodes SET name = :name WHERE id = :node"),
        {"name": b"b.csv", "node": node_a},
    )
    await real_session.commit()
    b_stat = (live_chat.working_dir / "b.csv").stat()
    async with box_socket(live_chat) as box, _as_member(live_chat) as member:
        answer = await grant(member, live_chat, node_a)
        assert answer.status_code == 409, answer.text
        assert answer.json()["detail"]["outcome"] == "changed"
        assert [r["path"] for r in box.requests] == ["scratch/b.csv"]
        assert box.acks == [
            {
                "t": "machine.ack",
                "request_id": box.requests[0]["request_id"],
                "outcome": "changed",
                "observed": {"size": b_stat.st_size, "mtime_ns": b_stat.st_mtime_ns},
            }
        ]
    held = live_chat.folders.held(live_chat.chat_id)
    assert held is not None and held.live is not None
    assert held.live.promotions == frozenset()


# --------------------------------------------------------------------------- #
# robustness
# --------------------------------------------------------------------------- #


async def test_a_box_whose_socket_drops_before_its_ack_still_lands_and_is_not_asked_twice(
    live_chat: LiveChat,  # noqa: F811  (the imported fixture)
    real_session: AsyncSession,
) -> None:
    """The box takes the request onto its queue and its socket drops before
    the ack leaves. The request dies with the old socket — nothing re-sends it
    on the new one — but the promotion is the box's own now: the bytes land,
    and the reader's next open is served from the store without asking again.
    """
    node_id = await written_unlanded(live_chat, real_session, "dropped.csv")
    async with box_socket(live_chat) as box, _as_member(live_chat) as member:
        answers = box.socket._machine_handler
        assert answers is not None
        dropped: list[dict[str, Any]] = []

        def ack_lost(frame: Mapping[str, Any]) -> Mapping[str, Any] | None:
            answers(frame)
            dropped.append(dict(frame))
            # The connection goes away before the answer is sent: the box
            # reconnects on its own, as it does for any cut.
            ws = box.socket._ws
            if ws is not None:
                asyncio.get_running_loop().create_task(ws.close(code=1012, reason="cut"))
            return None

        box.socket._machine_handler = ack_lost
        first = await grant(member, live_chat, node_id)
        assert len(dropped) == 1
        if first.status_code == 409:
            # Landed after the ack window: the reader was told the machine did
            # not answer, and the Retry-After brings it back.
            assert first.json()["detail"]["outcome"] == "timed_out"
        else:
            assert first.status_code == 201, first.text
        await _until(
            _landed(real_session, live_chat.scratch_node_id, b"dropped.csv"),
            window=SETUP_WAIT,
            what="the promoted file landing after its socket dropped",
        )
        again = await grant(member, live_chat, node_id)
        assert again.status_code == 201, again.text
        assert await redeem(again) == BODY
        assert len(box.requests) == 1, "the box was asked again for a file it had landed"


async def test_a_promote_landing_after_the_readers_deadline_is_served_next_time_unasked(
    live_chat: LiveChat,  # noqa: F811  (the imported fixture)
    real_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reader gives up before the bytes arrive (a short wait, and a file
    still settling on the box); the box lands them anyway. The next open finds
    the file on the drive and asks nothing."""
    monkeypatch.setattr(settings, "files_promote_wait_seconds", 0.5)
    path = live_chat.working_dir / "late.csv"
    async with box_socket(live_chat) as box, _as_member(live_chat) as member:
        await asyncio.to_thread(path.write_bytes, BODY)
        stamp = path.stat()

        async def listed() -> Any:
            row = await _row(real_session, live_chat.scratch_node_id, b"late.csv")
            return row if row is not None and row.holder_mtime_ns == stamp.st_mtime_ns else None

        row = await _until(listed, window=SETUP_WAIT, what="late.csv's row reaching the drive")
        node_id = uuid.UUID(str(row.id))
        # Asked while the file is still inside the holder's settle time: it is
        # accepted and queued, and read only once it has settled.
        first = await grant(member, live_chat, node_id)
        assert first.status_code == 409, first.text
        assert first.json()["detail"]["outcome"] == "accepted"
        await _until(
            _landed(real_session, live_chat.scratch_node_id, b"late.csv"),
            window=SETUP_WAIT,
            what="the promoted file landing after the reader left",
        )
        with requests_heard() as heard:
            again = await grant(member, live_chat, node_id)
            assert again.status_code == 201, again.text
            assert drained(heard) == []
        assert await redeem(again) == BODY
        assert len(box.requests) == 1
