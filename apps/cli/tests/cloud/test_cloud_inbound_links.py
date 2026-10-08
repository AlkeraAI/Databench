"""A composer-attached file is a LINK in the message, not a ``file`` part.

The web composer uploads a paste or a pick straight into the chat's working
folder and writes a chat-relative link — ``[File 1: rows.csv](file-1-ab12.csv)``
— so nothing about it goes through the attachment fetcher: the bytes reach the
box on the live plane, when the drive's inbound record is drained.

These drive the real service methods over a fake folder and a fake live sync:
what the turn is handed when the bytes land on the second ask, what it is handed
when they never land, and what the log says when the drain itself fails.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
from alkera_cli.cloud import turn_inputs
from alkera_cli.cloud.attachments import linked_files_in
from alkera_cli.cloud.service import CloudMirrorService
from alkera_cli.cloud.sleep_policy import SleepPolicy
from alkera_cli.cloud.workspace_host import WorkspaceHost
from alkera_cli.harness.sandbox_processes import SandboxProbes

CHAT = "chat-links"


class _LiveEntry:
    def __init__(self, node_id: str, state: str) -> None:
        self.node_id = node_id
        self.state = state


class _Spool:
    """The live sync's spool as the drain reads it: one partial per path."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        directory.mkdir(exist_ok=True)

    def landing_bytes(self, relative: str) -> int:
        partial = self.directory / relative
        return partial.stat().st_size if partial.exists() else 0


class _FakeLive:
    """The live plane, as the drain sees it: a root on disk and a pull that
    lands what the drive is holding."""

    def __init__(self, root: Path, *, lands: dict[str, bytes] | None = None) -> None:
        self.root = root
        self.pulls = 0
        # What the drive hands over, keyed by which pull it arrives on. The
        # real thing is a race: the upload committed, but its inbound record
        # reached the box a beat after the reader's words did.
        self.lands = lands or {}
        self.raises: Exception | None = None
        # Where a download still arriving streams, as the real sync's spool:
        # the daemon's own directory, beside the tree and never inside it.
        self.tree = None
        self.spool = _Spool(root.parent / f"{root.name}.spool")

    def pull_inbound(self) -> list[_LiveEntry]:
        self.pulls += 1
        if self.raises is not None:
            raise self.raises
        landed: list[_LiveEntry] = []
        for name, body in self.lands.pop(self.pulls, {}).items():  # type: ignore[union-attr]
            (self.root / name).write_bytes(body)
            landed.append(_LiveEntry(node_id=f"node-{name}", state="applied"))
        return landed


class _Held:
    def __init__(self, live: _FakeLive | None) -> None:
        self.live = live


class _Folders:
    def __init__(self, held: _Held | None) -> None:
        self._held = held

    def held(self, chat_id: str) -> _Held | None:
        return self._held


class _Clock:
    """A monotonic clock the test moves by hand, so a wait measured in minutes
    of service time costs no wall-clock time here."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _service(
    folders: _Folders,
    clock: _Clock | None = None,
    sleep: Callable[[float], Awaitable[None]] | None = None,
) -> CloudMirrorService:
    """The real service, with only what this path reads wired up. ``sleep``
    replaces the one that spends the wait on the fake clock."""
    box = object.__new__(CloudMirrorService)
    box._folders = folders  # type: ignore[attr-defined]
    box._clock = clock or _Clock()  # type: ignore[attr-defined]
    # An applied inbound change marks the chat as used.
    box._policy = SleepPolicy(  # type: ignore[attr-defined]
        idle_minutes=1440.0,
        parked_ask_hours=24.0,
        clock=box._clock,  # type: ignore[attr-defined]
        memory=lambda: None,
        probes=SandboxProbes(),
    )

    async def _sleep(seconds: float) -> None:
        # The wait is spent on the fake clock, so a service that waits for
        # hours costs this test nothing.
        box._clock.now += seconds  # type: ignore[attr-defined]

    box._sleep = _sleep  # type: ignore[attr-defined]
    # No chat here is a member of a workspace: each is its own custody key.
    box._workspaces = WorkspaceHost(  # type: ignore[attr-defined]
        folders=folders,  # type: ignore[arg-type]
        instance_of=lambda key: key,
        refuse=_no_refusal,
        clock=box._clock,  # type: ignore[attr-defined]
    )
    box._mirrors = {}  # type: ignore[attr-defined]
    box._inputs = turn_inputs.TurnInputs(  # type: ignore[attr-defined]
        folders=folders,  # type: ignore[arg-type]
        live_key=box._workspaces.live_key,  # type: ignore[attr-defined]
        drive_named=lambda _chat_id: None,
        drain=box._drain_inbound,
        reader_for=_no_reader,
        project_path=Path("/nonexistent"),
        clock=box._clock,  # type: ignore[attr-defined]
        sleep=sleep or _sleep,
    )
    return box


async def _no_reader(_drive: str | None) -> Any:
    raise AssertionError("no message here carries an attachment")


async def _no_refusal(_chat_id: str, _reason: str) -> None:
    return None


def _message(text: str) -> dict[str, Any]:
    return {"kind": "prompt", "message_id": "m1", "seq": 1, "text": text, "user_id": "u1"}


# -- what the message links --------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param(
            "compare [File 1: apollo_leads_import.csv](file-1-rvng.csv) please",
            [("file-1-rvng.csv", "apollo_leads_import.csv")],
            id="file-pill-carries-the-readers-name",
        ),
        pytest.param(
            "look ![Image 1](paste-1-ab12.png)",
            [("paste-1-ab12.png", "paste-1-ab12.png")],
            id="image-pill-has-no-name-after-the-colon",
        ),
        pytest.param(
            "[File 1: a.csv](file-1-aa.csv) and [File 2: b.csv](file-2-bb.csv)",
            [("file-1-aa.csv", "a.csv"), ("file-2-bb.csv", "b.csv")],
            id="two-pills-in-order",
        ),
        pytest.param(
            "[File 1: a.csv](file-1-aa.csv) [again](file-1-aa.csv)",
            [("file-1-aa.csv", "a.csv")],
            id="the-same-path-twice-is-one-file",
        ),
        pytest.param("see [the docs](https://example.com/guide)", [], id="an-ordinary-link"),
        pytest.param("read [notes](./notes.md) first", [], id="a-path-the-reader-typed"),
        pytest.param("[File 1: a.csv](files/file-1-aa.csv)", [], id="not-at-the-chat-root"),
        pytest.param("nothing linked at all", [], id="plain-words"),
    ],
)
def test_only_the_composers_own_staged_names_are_read_as_attachments(
    text: str, expected: list[tuple[str, str]]
) -> None:
    """A link a person typed must never be reported as a missing attachment."""
    found = linked_files_in(_message(text))
    assert [(one.path, one.display_name) for one in found] == expected


def test_a_message_with_no_text_links_nothing() -> None:
    assert linked_files_in({"kind": "prompt"}) == []
    assert linked_files_in({"text": None}) == []


# -- the drain the link waits on ---------------------------------------------


@pytest.mark.asyncio
async def test_bytes_that_arrive_on_the_second_pull_are_read_not_reported(
    tmp_path: Path,
) -> None:
    """The upload and the message are two requests. A file whose inbound record
    lands a beat late is the ordinary case, and the turn must not call it
    missing."""
    live = _FakeLive(tmp_path, lands={2: {"file-1-rvng.csv": b"col_a\n1\n"}})
    box = _service(_Folders(_Held(live)))

    landed = await box.prepare_attachments(
        CHAT, _message("what is in [File 1: apollo_leads_import.csv](file-1-rvng.csv)?")
    )

    assert landed.notices == ()
    assert landed.files == ()
    assert (tmp_path / "file-1-rvng.csv").read_bytes() == b"col_a\n1\n"
    # Asked twice: once before the turn, once because the file was not there.
    assert live.pulls == 2


@pytest.mark.asyncio
async def test_bytes_already_on_disk_are_not_asked_for_a_second_time(tmp_path: Path) -> None:
    (tmp_path / "file-1-rvng.csv").write_bytes(b"already here")
    live = _FakeLive(tmp_path)
    box = _service(_Folders(_Held(live)))

    landed = await box.prepare_attachments(
        CHAT, _message("read [File 1: rows.csv](file-1-rvng.csv)")
    )

    assert landed.notices == ()
    assert live.pulls == 1


@pytest.mark.asyncio
async def test_bytes_that_never_arrive_become_a_notice_naming_the_readers_file(
    tmp_path: Path,
) -> None:
    """The live failure: the chat renders a link and no bytes ever land. The
    turn must say so, by the name the reader sees, and must not invent a path
    the agent would then go looking for."""
    live = _FakeLive(tmp_path)
    box = _service(_Folders(_Held(live)))

    landed = await box.prepare_attachments(
        CHAT, _message("what is in [File 1: apollo_leads_import.csv](file-1-rvng.csv)?")
    )

    assert landed.files == ()
    (notice,) = landed.notices
    assert notice == "apollo_leads_import.csv: never arrived on this machine"
    # Nothing on disk is claimed for it, and nothing was written.
    assert "file-1-rvng.csv" not in notice
    assert sorted(os.listdir(tmp_path)) == []
    # Asked more than the once-and-once-more the old fixed window allowed: a
    # file that is not there yet is asked for again until it stops moving.
    assert live.pulls > 2


@pytest.mark.asyncio
async def test_a_chat_with_no_folder_held_is_not_answered_for() -> None:
    """The box never promised this chat a working directory, so a link in its
    message is not a file it owes — and reporting one would put a failure on a
    chat that is running exactly as designed."""
    nowhere = _service(_Folders(None))

    landed = await nowhere.prepare_attachments(CHAT, _message("[File 1: x.csv](file-1-aa.csv)"))

    assert landed.notices == ()
    assert landed.files == ()


@pytest.mark.asyncio
async def test_a_drain_that_fails_is_logged_with_the_chat_and_the_reason(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A failed drain is the reason a reader's file is not on disk. At INFO it
    was the one fact missing from the record when an agent reported a chat whose
    links pointed at nothing."""
    live = _FakeLive(tmp_path)
    live.raises = RuntimeError("the drive refused the fence")
    box = _service(_Folders(_Held(live)))

    with caplog.at_level(logging.WARNING, logger="alkera_cli.cloud.service"):
        landed = await box.prepare_attachments(
            CHAT, _message("read [File 1: rows.csv](file-1-rvng.csv)")
        )

    assert landed.notices == ("rows.csv: never arrived on this machine",)
    messages = [record.getMessage() for record in caplog.records]
    assert any("the drive refused the fence" in line and CHAT in line for line in messages), (
        messages
    )
    assert any("RuntimeError" in line for line in messages), messages
    assert any("file-1-rvng.csv" in line for line in messages), messages


@pytest.mark.asyncio
async def test_an_entry_the_box_could_not_apply_is_named_in_the_log(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """``superseded`` is a real answer, not a silent one: the reader's file is
    not the one on disk, and the node it concerns belongs in the record."""

    class _Superseding(_FakeLive):
        def pull_inbound(self) -> list[_LiveEntry]:
            self.pulls += 1
            return [_LiveEntry(node_id="node-7", state="superseded")]

    box = _service(_Folders(_Held(_Superseding(tmp_path))))

    with caplog.at_level(logging.WARNING, logger="alkera_cli.cloud.service"):
        await box.prepare_attachments(CHAT, _message("read [File 1: rows.csv](file-1-rvng.csv)"))

    messages = [record.getMessage() for record in caplog.records]
    assert any("node-7" in line and "superseded" in line for line in messages), messages


# -- a transfer that is still coming is not a transfer that failed ------------


class _Trickling(_FakeLive):
    """A hand-over that writes into the partial the real inbound download uses,
    and renames it into place only after ``pulls_to_land`` asks."""

    def __init__(self, root: Path, *, name: str, chunk: int, pulls_to_land: int) -> None:
        super().__init__(root)
        self.partial = self.spool.directory / name
        self.final = root / name
        self.chunk = chunk
        self.pulls_to_land = pulls_to_land

    def pull_inbound(self) -> list[_LiveEntry]:
        self.pulls += 1
        with self.partial.open("ab") as writing:
            writing.write(b"\0" * self.chunk)
        if self.pulls >= self.pulls_to_land:
            os.replace(self.partial, self.final)
        return []


@pytest.mark.asyncio
async def test_a_file_still_arriving_is_waited_for_however_long_it_takes(
    tmp_path: Path,
) -> None:
    """The whole point: a turn whose reader linked a large file must wait for
    the transfer, not for a clock. Bytes landing keep the wait open past any
    fixed window — the old one gave up after ten seconds."""
    live = _Trickling(tmp_path, name="file-1-rvng.csv", chunk=4 << 20, pulls_to_land=40)
    clock = _Clock()
    box = _service(_Folders(_Held(live)), clock)

    landed = await box.prepare_attachments(
        CHAT, _message("summarise [File 1: export.csv](file-1-rvng.csv)")
    )

    assert landed.notices == ()
    assert (tmp_path / "file-1-rvng.csv").stat().st_size == 40 * (4 << 20)
    # Far past the ten seconds the drain used to allow, and past the stall
    # floor too: what held the wait open was the bytes, not a constant.
    assert clock.now > turn_inputs.INBOUND_STALL_FLOOR_SECONDS


@pytest.mark.asyncio
async def test_a_transfer_that_stalls_is_given_up_on_by_its_own_size(
    tmp_path: Path,
) -> None:
    """A stall still ends the wait — but the window is the file's own size over
    a floor bandwidth, so a 2 GiB file that stops moving is waited on far
    longer than a note is, and the turn is told what did arrive."""
    live = _FakeLive(tmp_path)
    partial = live.spool.directory / "file-1-rvng.csv"
    partial.touch()
    # Sparse: two gibibytes of declared size, no bytes on the disk.
    os.truncate(partial, 2 << 30)
    clock = _Clock()
    box = _service(_Folders(_Held(live)), clock)

    landed = await box.prepare_attachments(
        CHAT, _message("read [File 1: rows.csv](file-1-rvng.csv)")
    )

    (notice,) = landed.notices
    assert notice == f"rows.csv: never arrived on this machine ({2 << 30} of its bytes did)"
    # The wait was derived from the size, not from the floor: 2 GiB at the
    # floor bandwidth is hours, and the turn waited them before giving up.
    assert clock.now >= (2 << 30) / 131_072
    assert clock.now > turn_inputs.INBOUND_STALL_FLOOR_SECONDS


@pytest.mark.asyncio
async def test_a_small_file_that_never_starts_is_given_up_on_at_the_floor(
    tmp_path: Path,
) -> None:
    """The negative: nothing on disk means nothing is moving, so the wait is
    the floor and not a number derived from a size nobody knows."""
    live = _FakeLive(tmp_path)
    clock = _Clock()
    box = _service(_Folders(_Held(live)), clock)

    await box.prepare_attachments(CHAT, _message("read [File 1: rows.csv](file-1-rvng.csv)"))

    assert (
        turn_inputs.INBOUND_STALL_FLOOR_SECONDS
        <= clock.now
        < 2 * (turn_inputs.INBOUND_STALL_FLOOR_SECONDS + turn_inputs.INBOUND_POLL_MAX_SECONDS)
    )


@pytest.mark.asyncio
async def test_a_linked_file_is_polled_for_on_a_doubling_wait_to_its_ceiling(
    tmp_path: Path,
) -> None:
    """Between two asks of the drive the wait starts at a second and doubles
    to five, however long the file takes."""
    live = _FakeLive(tmp_path)
    clock = _Clock()
    waits: list[float] = []

    async def sleep(seconds: float) -> None:
        waits.append(seconds)
        clock.now += seconds

    box = _service(_Folders(_Held(live)), clock, sleep)
    await box.prepare_attachments(CHAT, _message("read [File 1: rows.csv](file-1-rvng.csv)"))

    assert waits[:6] == [1.0, 2.0, 4.0, 5.0, 5.0, 5.0]
    assert set(waits[3:]) == {turn_inputs.INBOUND_POLL_MAX_SECONDS}
