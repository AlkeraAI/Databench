"""How many opencode agent servers one box keeps, and for how long.

The opencode adapter spawns one agent server per chat by construction, and a
chat's mirror was never closed — so a box accumulated a server per chat it had
ever served (five of them on a two-vCPU demo box, none of them answering
anything). A mirror that has been silent for the idle window gives its server
back; a box serves a bounded number of chats at once, and at the cap the
least-recently-active idle chat is closed to make room. A chat mid-turn is never
the one closed — unless its turn has said nothing for the whole idle window: a
step wedged on something that never returns emits no event, trips no cap, and
without this would hold its slot and the chat's folder until the daemon was
restarted by hand. A chat whose mirror was closed is re-opened on its next
prompt (the catch-up runs the question that arrived while it was down).
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from _mirror_service import SERVICE_LOGGER, Clock, build_service
from alkera_cli.cloud import service as service_module
from alkera_cli.cloud import turn_inputs
from alkera_cli.cloud.service import (
    DAEMON_MEMORY_RESERVE_MB,
    DEFAULT_AGENT_MEMORY_MB,
    DEFAULT_MAX_MIRRORS,
    ENV_AGENT_MEMORY_MB,
    ENV_MAX_MIRRORS,
    ENV_MEMORY_LIMIT_MB,
    ENV_MIRROR_IDLE_MINUTES,
    MIB,
    MIN_MAX_MIRRORS,
    CloudMirrorService,
    default_max_mirrors,
    memory_cap_from_env,
    memory_max_mirrors,
    mirror_limits_from_env,
)
from alkera_cli.cloud.sleep_policy import (
    DEFAULT_MIRROR_IDLE_MINUTES,
    DEFAULT_PARKED_ASK_HOURS,
    ENV_PARKED_ASK_HOURS,
    parked_ask_hours_from_env,
)
from alkera_cli.files.live_sync import LiveEntry

# -- the limits are settings ---------------------------------------------------


def test_the_limits_are_settings_with_the_documented_defaults() -> None:
    """A day idle by default: a chat left at the end of a
    working day is still warm the next morning. The environment still wins, in
    either direction."""
    assert (DEFAULT_MIRROR_IDLE_MINUTES, DEFAULT_MAX_MIRRORS) == (1440.0, None)
    assert mirror_limits_from_env({}) == (DEFAULT_MIRROR_IDLE_MINUTES, DEFAULT_MAX_MIRRORS)
    assert mirror_limits_from_env({ENV_MIRROR_IDLE_MINUTES: "5", ENV_MAX_MIRRORS: "2"}) == (5.0, 2)
    assert mirror_limits_from_env({ENV_MIRROR_IDLE_MINUTES: "240"}) == (240.0, DEFAULT_MAX_MIRRORS)


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param("", id="empty"),
        pytest.param("   ", id="blank"),
        pytest.param("nope", id="not-a-number"),
        pytest.param("0", id="zero-is-not-unlimited"),
        pytest.param("-3", id="negative"),
        pytest.param("inf", id="infinity"),
        pytest.param("1e999", id="overflows-to-infinity"),
    ],
)
def test_an_unusable_limit_falls_back_to_the_default(raw: str) -> None:
    assert mirror_limits_from_env({ENV_MIRROR_IDLE_MINUTES: raw, ENV_MAX_MIRRORS: raw}) == (
        DEFAULT_MIRROR_IDLE_MINUTES,
        DEFAULT_MAX_MIRRORS,
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param(None, DEFAULT_PARKED_ASK_HOURS, id="unset"),
        pytest.param("", DEFAULT_PARKED_ASK_HOURS, id="blank"),
        pytest.param("nope", DEFAULT_PARKED_ASK_HOURS, id="not-a-number"),
        pytest.param("inf", DEFAULT_PARKED_ASK_HOURS, id="infinity"),
        pytest.param("-1", DEFAULT_PARKED_ASK_HOURS, id="negative"),
        pytest.param("0", 0.0, id="zero-means-never-sleep"),
        pytest.param("2.5", 2.5, id="a-window-an-operator-chose"),
    ],
)
def test_the_parked_ask_window_is_a_setting_with_a_day_as_its_default(
    raw: str | None, expected: float
) -> None:
    """A day by default: a card raised on a Friday afternoon is still live on
    Monday. ``0`` is the one way to say "never sleep a parked chat", so it is
    kept rather than read as junk."""
    assert DEFAULT_PARKED_ASK_HOURS == 24.0
    env = {} if raw is None else {ENV_PARKED_ASK_HOURS: raw}
    assert parked_ask_hours_from_env(env) == expected


# -- streaming the folder a chat is awake in -----------------------------------


class LiveFolders:
    """Custody whose live sync writes what the drive is holding onto the box.

    Not a recorder: the fake's ``pull_inbound`` really lands a file in the
    chat's working directory, so every assertion below is about what is on disk
    when the turn starts — never about a method having been called.
    """

    def __init__(self, *, inbound: dict[str, bytes] | None = None) -> None:
        self.enabled = True
        self.streaming: dict[str, Path] = {}
        self.stopped: list[str] = []
        self._held: dict[str, Any] = {}
        self._waiting = dict(inbound or {})
        self.drains = 0

    def bind_machine(self, machine_id: str) -> None:
        return None

    def take(self, chat_id: str, chat: Any, *, instance: str) -> Any:
        held = SimpleNamespace(record=SimpleNamespace(node_id=f"node-{chat_id}"), live=None)
        self._held[chat_id] = held
        return held

    def held(self, chat_id: str) -> Any:
        return self._held.get(chat_id)

    def live(self, chat_id: str, working_dir: Path) -> Any:
        held = self._held.get(chat_id)
        if held is None:
            return None
        self.streaming[chat_id] = working_dir
        held.live = SimpleNamespace(pull_inbound=lambda: self._land(working_dir))
        return held.live

    def stop_live(self, chat_id: str, deadline: float = 5.0) -> None:
        self.stopped.append(chat_id)

    def _land(self, working_dir: Path) -> list[LiveEntry]:
        self.drains += 1
        working_dir.mkdir(parents=True, exist_ok=True)
        applied: list[LiveEntry] = []
        for name, payload in self._waiting.items():
            (working_dir / name).write_bytes(payload)
            applied.append(LiveEntry(node_id=f"node-{name}", state="applied"))
        self._waiting.clear()
        return applied

    def owed(self) -> list[str]:
        return []

    def beat(self, chat_id: str) -> bool:
        return chat_id in self._held

    def push(self, chat_id: str) -> Any:
        return None

    def hand_back(
        self, chat_id: str, *, recover: bool = True, ending: str | None = None, gone: bool = False
    ) -> Any:
        self._held.pop(chat_id, None)
        return None

    def release(self, chat_id: str) -> bool:
        return self._held.pop(chat_id, None) is not None


async def test_taking_a_chats_folder_streams_the_directory_the_agent_runs_in(
    tmp_path: Path,
) -> None:
    """The chat's records sit beside its working directory, and streaming the
    folder would put the box's own bookkeeping on the drive as the chat's."""
    folders = LiveFolders()
    service, built = build_service(tmp_path, clock=Clock(), folders=folders)

    await service._ensure_mirror("chat-a", {"id": "chat-a"})

    assert folders.streaming == {"chat-a": built["chat-a"].working_dir}
    assert built["chat-a"].working_dir.name == "scratch"


async def test_a_lease_event_for_a_held_folder_takes_what_the_drive_is_holding(
    tmp_path: Path,
) -> None:
    """A file dropped into the folder on the web reaches the box at once, not
    a poll interval later — that latency is what makes a live folder feel like
    a batch job."""
    folders = LiveFolders(inbound={"dropped.csv": b"a,b\n1,2\n"})
    service, built = build_service(tmp_path, clock=Clock(), folders=folders)
    await service._ensure_mirror("chat-a", {"id": "chat-a"})
    working = built["chat-a"].working_dir
    assert not (working / "dropped.csv").exists()

    await service._pull_inbound_for("node-chat-a")

    assert (working / "dropped.csv").read_bytes() == b"a,b\n1,2\n"


async def test_a_lease_event_for_a_folder_this_box_does_not_hold_takes_nothing(
    tmp_path: Path,
) -> None:
    """Another box's folder is another box's business; reaching for it would
    download somebody else's chat onto this disk."""
    folders = LiveFolders(inbound={"dropped.csv": b"x"})
    service, built = build_service(tmp_path, clock=Clock(), folders=folders)
    await service._ensure_mirror("chat-a", {"id": "chat-a"})

    await service._pull_inbound_for("node-chat-zzz")

    assert folders.drains == 0
    assert not (built["chat-a"].working_dir / "dropped.csv").exists()


async def test_the_upkeep_pass_takes_what_arrived_while_nothing_was_streaming(
    tmp_path: Path,
) -> None:
    """The socket goes down; the drop that landed while it was down has to
    reach the box on the box's own next pass rather than waiting for the next
    drop."""
    folders = LiveFolders(inbound={"late.txt": b"landed"})
    service, built = build_service(tmp_path, clock=Clock(), folders=folders)
    await service._ensure_mirror("chat-a", {"id": "chat-a"})

    await service._upkeep_folders()

    assert (built["chat-a"].working_dir / "late.txt").read_bytes() == b"landed"


async def test_a_turn_starts_only_after_the_drive_has_handed_over_what_it_holds(
    tmp_path: Path,
) -> None:
    """The agent reaches a dropped file by name, so a turn that started before
    it landed would read a path the person can already see and find nothing."""
    folders = LiveFolders(inbound={"brief.md": b"# read me"})
    service, built = build_service(tmp_path, clock=Clock(), folders=folders)
    await service._ensure_mirror("chat-a", {"id": "chat-a"})
    working = built["chat-a"].working_dir

    await service.prepare_attachments("chat-a", {"parts": []})

    assert (working / "brief.md").read_bytes() == b"# read me"


async def test_a_drive_that_will_not_answer_does_not_hold_the_turn_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reader is waiting on the turn. A drive that never answers must cost
    the dropped file, not the conversation."""
    folders = LiveFolders()
    service, _built = build_service(tmp_path, clock=Clock(), folders=folders)
    await service._ensure_mirror("chat-a", {"id": "chat-a"})
    held = folders.held("chat-a")

    def _never() -> int:
        time.sleep(30.0)
        return 0

    held.live = SimpleNamespace(pull_inbound=_never)
    monkeypatch.setattr(turn_inputs, "INBOUND_STALL_FLOOR_SECONDS", 0.05)

    started = time.monotonic()
    await service.prepare_attachments("chat-a", {"parts": []})

    assert time.monotonic() - started < 5.0


# -- the idle close ------------------------------------------------------------


async def _serve(service: CloudMirrorService, chat_id: str) -> None:
    await service._ensure_mirror(chat_id, {"id": chat_id})


async def test_an_idle_chat_gives_its_agent_server_back(tmp_path: Path) -> None:
    clock = Clock()
    service, built = build_service(tmp_path, clock=clock, mirror_idle_minutes=20.0)
    await _serve(service, "chat-a")
    await service.sweep_idle_mirrors()
    assert not built["chat-a"].stopped  # just served

    clock.advance(19 * 60)
    await service.sweep_idle_mirrors()
    assert not built["chat-a"].stopped  # still inside the window

    clock.advance(2 * 60)
    await service.sweep_idle_mirrors()
    assert built["chat-a"].stopped  # 21 minutes silent — stop() reaps the server
    assert "chat-a" not in service.mirrors


async def test_a_chat_that_keeps_answering_keeps_its_mirror(tmp_path: Path) -> None:
    clock = Clock()
    service, built = build_service(tmp_path, clock=clock, mirror_idle_minutes=20.0)
    await _serve(service, "chat-a")
    for _ in range(5):
        clock.advance(15 * 60)
        built["chat-a"].published_count += 3  # it published something in that window
        await service.sweep_idle_mirrors()
    assert not built["chat-a"].stopped


async def test_a_turn_parked_on_a_person_is_never_swept(tmp_path: Path) -> None:
    """An ask nobody has answered is a turn in progress: the reader who comes
    back an afternoon later answers the card and the turn goes on, instead of
    finding the session it belonged to torn down under them."""
    clock = Clock()
    service, built = build_service(tmp_path, clock=clock, mirror_idle_minutes=20.0)
    await _serve(service, "chat-a")
    built["chat-a"].turn_running = True
    built["chat-a"].waiting_on_a_person = True
    built["chat-a"].quiescent = False  # the real mirror reads a pending ask as busy
    for _ in range(12):
        clock.advance(21 * 60)
        await service.sweep_idle_mirrors()
    assert not built["chat-a"].stopped, "four hours parked on a person is still mid-turn"

    built["chat-a"].waiting_on_a_person = False  # answered; the turn ran on and ended
    built["chat-a"].turn_running = False
    built["chat-a"].quiescent = True
    await service.sweep_idle_mirrors()
    clock.advance(21 * 60)
    await service.sweep_idle_mirrors()
    assert built["chat-a"].stopped, "the window runs from where the turn ended"


async def test_a_chat_parked_on_an_ask_sleeps_once_no_reader_has_come_back(
    tmp_path: Path,
) -> None:
    """An ask holds the chat's session for a reader who steps away — for a day,
    not for ever. Nobody may ever come back, and the ask is durable: it stays
    in the transcript and the next open re-offers it, so the answer still
    resumes the chat after the window, on a fresh session."""
    clock = Clock()
    service, built = build_service(
        tmp_path, clock=clock, mirror_idle_minutes=20.0, parked_ask_hours=24.0
    )
    await _serve(service, "chat-a")
    built["chat-a"].turn_running = True
    built["chat-a"].waiting_on_a_person = True
    built["chat-a"].parked_only = True
    built["chat-a"].quiescent = False  # the real mirror reads a pending ask as busy

    clock.advance(23 * 3600)
    await service.sweep_idle_mirrors()
    assert not built["chat-a"].stopped, "23 hours is inside the window a reader has"

    clock.advance(2 * 3600)
    await service.sweep_idle_mirrors()
    assert built["chat-a"].stopped, "a day with no reader and the chat sleeps like any other"
    assert "chat-a" not in service.mirrors


async def test_a_reader_who_comes_back_re_arms_the_parked_ask_window(
    tmp_path: Path,
) -> None:
    """The window measures silence FROM A READER, not from the ask. Someone
    looking in at hour 20 buys the ask another full day."""
    clock = Clock()
    service, built = build_service(
        tmp_path, clock=clock, mirror_idle_minutes=20.0, parked_ask_hours=24.0
    )
    await _serve(service, "chat-a")
    built["chat-a"].turn_running = True
    built["chat-a"].parked_only = True
    built["chat-a"].quiescent = False

    clock.advance(20 * 3600)
    # A reader opened the chat: the backend stamps the wake on the row, and the
    # row is the only place an open that sends nothing is visible to the box.
    await service._ensure_mirror(
        "chat-a", {"id": "chat-a", "wake_requested_at": "2026-09-18T00:00:00Z"}
    )
    assert built["chat-a"].readers == 1

    clock.advance(20 * 3600)
    await service.sweep_idle_mirrors()
    assert not built["chat-a"].stopped, "the window runs from the reader, not from the ask"

    clock.advance(5 * 3600)
    await service.sweep_idle_mirrors()
    assert built["chat-a"].stopped


async def test_the_same_wake_stamp_read_again_is_not_a_second_reader(tmp_path: Path) -> None:
    """The row carries the last wake for as long as the chat is served, and the
    box reads the row every pass. Re-arming on each pass would make the window
    unreachable for any chat a reader ever opened."""
    clock = Clock()
    service, built = build_service(
        tmp_path, clock=clock, mirror_idle_minutes=20.0, parked_ask_hours=24.0
    )
    await _serve(service, "chat-a")
    built["chat-a"].parked_only = True
    built["chat-a"].quiescent = False
    row = {"id": "chat-a", "wake_requested_at": "2026-09-18T00:00:00Z"}
    for _ in range(5):
        clock.advance(7 * 3600)
        await service._ensure_mirror("chat-a", dict(row))
    assert built["chat-a"].readers == 1
    await service.sweep_idle_mirrors()
    assert built["chat-a"].stopped, "the same stamp read again kept the chat awake for ever"


async def test_a_parked_ask_still_holds_the_chat_when_the_window_is_off(tmp_path: Path) -> None:
    """``0`` hours is the operator's way of saying an ask keeps its session for
    as long as the box lives."""
    clock = Clock()
    service, built = build_service(
        tmp_path, clock=clock, mirror_idle_minutes=20.0, parked_ask_hours=0.0
    )
    await _serve(service, "chat-a")
    built["chat-a"].parked_only = True
    built["chat-a"].quiescent = False
    clock.advance(40 * 24 * 3600)
    await service.sweep_idle_mirrors()
    assert not built["chat-a"].stopped


async def test_a_chat_busy_with_more_than_an_ask_is_not_swept_by_the_window(
    tmp_path: Path,
) -> None:
    """The window runs against a parked ask and nothing else. A background job
    still running, a message not yet handed over, a turn the agent is actually
    working — none of those go cold, however long the reader is away."""
    clock = Clock()
    service, built = build_service(
        tmp_path, clock=clock, mirror_idle_minutes=20.0, parked_ask_hours=24.0
    )
    await _serve(service, "chat-a")
    built["chat-a"].turn_running = True
    built["chat-a"].waiting_on_a_person = True
    built["chat-a"].parked_only = False  # a subagent is still running under the ask
    built["chat-a"].quiescent = False
    clock.advance(40 * 24 * 3600)
    await service.sweep_idle_mirrors()
    assert not built["chat-a"].stopped


async def test_a_running_turn_that_keeps_saying_something_is_never_swept(tmp_path: Path) -> None:
    """A long turn is not a wedged one: as long as it publishes within each
    idle window it keeps its mirror for as long as it takes."""
    clock = Clock()
    service, built = build_service(tmp_path, clock=clock, mirror_idle_minutes=20.0)
    await _serve(service, "chat-a")
    built["chat-a"].turn_running = True
    for _ in range(6):  # a two-hour turn, a tool result every 19 minutes
        clock.advance(19 * 60)
        built["chat-a"].published_count += 1
        await service.sweep_idle_mirrors()
    assert not built["chat-a"].stopped
    # And when the turn ends the window starts THERE, not at the last publish.
    clock.advance(19 * 60)
    built["chat-a"].turn_running = False
    await service.sweep_idle_mirrors()
    assert not built["chat-a"].stopped
    clock.advance(19 * 60)
    await service.sweep_idle_mirrors()
    assert not built["chat-a"].stopped, "19 minutes after the turn ended it is inside the window"
    clock.advance(2 * 60)
    await service.sweep_idle_mirrors()
    assert built["chat-a"].stopped


async def test_a_turn_that_only_streams_is_not_wedged(tmp_path: Path) -> None:
    """A model step that streams for longer than the window completes no
    durable event, but the reader is watching text arrive: that is a turn at
    work, not one to sweep."""
    clock = Clock()
    service, built = build_service(tmp_path, clock=clock, mirror_idle_minutes=20.0)
    await _serve(service, "chat-a")
    built["chat-a"].turn_running = True
    for _ in range(4):
        clock.advance(15 * 60)
        built["chat-a"].chunk_count += 40
        await service.sweep_idle_mirrors()
    assert not built["chat-a"].stopped


async def test_a_running_turn_is_never_swept_however_long_it_says_nothing(
    tmp_path: Path,
) -> None:
    """A turn may legitimately run for hours or days, and a long step says
    nothing while it runs — a warehouse query, a build, a subagent reading a
    repository. Silence is not evidence the turn is dead, and closing it would
    throw away work the reader is waiting on, so the timer never takes one."""
    clock = Clock()
    service, built = build_service(tmp_path, clock=clock, mirror_idle_minutes=20.0)
    await _serve(service, "chat-a")
    built["chat-a"].turn_running = True
    built["chat-a"].quiescent = False  # the real mirror reads a running turn as busy
    clock.advance(5 * 60)
    built["chat-a"].published_count += 1  # its last word
    for _ in range(24):  # eight hours of a silent step
        clock.advance(21 * 60)
        assert await service.sweep_idle_mirrors() == []
    assert not built["chat-a"].stopped
    assert "chat-a" in service.mirrors


async def test_a_chat_with_a_background_job_and_no_turn_is_never_swept(
    tmp_path: Path,
) -> None:
    """A backgrounded subagent or shell outlives the turn that started it, and
    the sleep destroys it. Nothing published while it runs, so only the chat's
    own account of what it owes keeps it here."""
    clock = Clock()
    service, built = build_service(tmp_path, clock=clock, mirror_idle_minutes=20.0)
    await _serve(service, "chat-a")
    built["chat-a"].quiescent = False  # a background job still running
    for _ in range(6):
        clock.advance(21 * 60)
        assert await service.sweep_idle_mirrors() == []

    built["chat-a"].quiescent = True  # it finished
    await service.sweep_idle_mirrors()
    clock.advance(21 * 60)
    assert await service.sweep_idle_mirrors() == ["chat-a"]


# -- the cap -------------------------------------------------------------------


async def test_the_cap_closes_the_least_recently_active_idle_mirror(tmp_path: Path) -> None:
    clock = Clock()
    service, built = build_service(tmp_path, clock=clock, max_mirrors=3, mirror_idle_minutes=60.0)
    for name in ("chat-a", "chat-b", "chat-c"):
        await _serve(service, name)
        clock.advance(60)
    # chat-a was opened first, but chat-b is the one that has been silent longest.
    built["chat-a"].published_count += 1
    await service.sweep_idle_mirrors()
    clock.advance(60)

    await _serve(service, "chat-d")
    assert built["chat-b"].stopped
    assert not built["chat-a"].stopped and not built["chat-c"].stopped
    assert set(service.mirrors) == {"chat-a", "chat-c", "chat-d"}


async def test_the_cap_never_evicts_a_chat_that_is_mid_turn(tmp_path: Path) -> None:
    clock = Clock()
    service, built = build_service(tmp_path, clock=clock, max_mirrors=2, mirror_idle_minutes=60.0)
    await _serve(service, "chat-a")
    clock.advance(60)
    await _serve(service, "chat-b")
    built["chat-a"].turn_running = True  # the oldest is the one that is working
    clock.advance(60)

    await _serve(service, "chat-c")
    assert not built["chat-a"].stopped
    assert built["chat-b"].stopped
    assert set(service.mirrors) == {"chat-a", "chat-c"}


async def test_a_new_chat_nobody_has_written_in_never_evicts_a_served_chat(
    tmp_path: Path,
) -> None:
    """Creating a chat is not work: a full box keeps the idle chat it serves
    (a reader may come back to it) and opens the new one when a slot frees or
    its first message arrives."""
    clock = Clock()
    service, built = build_service(tmp_path, clock=clock, max_mirrors=1, mirror_idle_minutes=60.0)
    await _serve(service, "chat-a")
    clock.advance(60)

    await service._ensure_mirror("chat-new", {"id": "chat-new", "last_seq": 0})
    assert not built["chat-a"].stopped
    assert "chat-new" not in built
    assert set(service.mirrors) == {"chat-a"}

    # Its first message is work, and work may take the idle chat's slot.
    await service._ensure_mirror(
        "chat-new", {"id": "chat-new", "last_seq": 1, "pending_turn": True}
    )
    assert built["chat-a"].stopped
    assert set(service.mirrors) == {"chat-new"}


async def test_a_new_chat_takes_a_slot_that_is_already_free(tmp_path: Path) -> None:
    clock = Clock()
    service, built = build_service(tmp_path, clock=clock, max_mirrors=2, mirror_idle_minutes=60.0)
    await _serve(service, "chat-a")
    await service._ensure_mirror("chat-new", {"id": "chat-new", "last_seq": 0})
    assert set(service.mirrors) == {"chat-a", "chat-new"}
    assert not built["chat-a"].stopped


async def test_a_chat_with_history_and_nothing_owed_still_makes_room(tmp_path: Path) -> None:
    """The asymmetric case: only an empty transcript holds a chat back. One
    with history that a reader opens is served as before, at the cost of the
    least recently used idle chat."""
    clock = Clock()
    service, built = build_service(tmp_path, clock=clock, max_mirrors=1, mirror_idle_minutes=60.0)
    await _serve(service, "chat-a")
    clock.advance(60)
    await service._ensure_mirror("chat-old", {"id": "chat-old", "last_seq": 7})
    assert built["chat-a"].stopped
    assert set(service.mirrors) == {"chat-old"}


async def test_a_configured_cap_never_evicts_a_silent_running_turn(tmp_path: Path) -> None:
    """The cap takes a slot from a chat that owes nothing, never from one that
    is working — and a turn that has gone quiet is still working. The new chat
    waits for a slot instead of a reader losing an eight-hour query."""
    clock = Clock()
    service, built = build_service(tmp_path, clock=clock, max_mirrors=2, mirror_idle_minutes=20.0)
    await _serve(service, "chat-a")
    await _serve(service, "chat-b")
    built["chat-a"].turn_running = True
    built["chat-a"].quiescent = False
    built["chat-b"].turn_running = True
    built["chat-b"].quiescent = False
    await service.sweep_idle_mirrors()  # a pass sees both turns start
    clock.advance(21 * 60)
    built["chat-b"].published_count += 1  # chat-b is slow but alive; chat-a has gone quiet

    await _serve(service, "chat-c")
    assert not built["chat-a"].stopped and not built["chat-b"].stopped
    assert set(service.mirrors) == {"chat-a", "chat-b"}
    assert "chat-c" not in built  # nothing was spawned for it; the next poll retries


async def test_a_box_whose_every_chat_is_busy_serves_no_new_one(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Nothing is taken from a reader who is waiting on an answer: the new chat
    waits for the next poll instead, and no agent server is spawned for it."""
    clock = Clock()
    service, built = build_service(tmp_path, clock=clock, max_mirrors=2, mirror_idle_minutes=60.0)
    await _serve(service, "chat-a")
    await _serve(service, "chat-b")
    built["chat-a"].turn_running = True
    built["chat-b"].turn_running = True

    with caplog.at_level(logging.INFO, logger=SERVICE_LOGGER):
        await _serve(service, "chat-c")
    assert not built["chat-a"].stopped and not built["chat-b"].stopped
    assert "chat-c" not in service.mirrors  # the next poll retries it
    assert "chat-c" not in built  # nothing was spawned for it
    assert any("chat-c" in r.getMessage() for r in caplog.records)


async def test_a_chat_already_served_is_not_counted_against_the_cap(tmp_path: Path) -> None:
    """A pass over a full box must not evict anything to re-serve a chat it is
    already serving — that would close a mirror on every poll tick."""
    clock = Clock()
    service, built = build_service(tmp_path, clock=clock, max_mirrors=2, mirror_idle_minutes=60.0)
    await _serve(service, "chat-a")
    await _serve(service, "chat-b")
    for _ in range(3):
        await _serve(service, "chat-a")
        await _serve(service, "chat-b")
    assert not built["chat-a"].stopped and not built["chat-b"].stopped
    assert set(service.mirrors) == {"chat-a", "chat-b"}


async def test_a_chat_whose_mirror_was_closed_is_served_again(tmp_path: Path) -> None:
    clock = Clock()
    service, built = build_service(tmp_path, clock=clock, mirror_idle_minutes=20.0)
    await _serve(service, "chat-a")
    first = built["chat-a"]
    clock.advance(21 * 60)
    await service.sweep_idle_mirrors()
    assert first.stopped

    await _serve(service, "chat-a")  # the reader asks again
    assert service.mirrors["chat-a"] is built["chat-a"] is not first
    assert built["chat-a"].state == "running"
    assert built["chat-a"].catch_ups == 0  # a fresh mirror catches up in start(), not here


# -- more bound chats than slots ----------------------------------------------


def _bind(service: CloudMirrorService, chats: dict[str, int]) -> None:
    """Point the service's chat list at these chats and their ``last_seq``."""
    service._machine_id = "machine:x"
    rows = [
        {"id": chat_id, "machine_id": "machine:x", "last_seq": seq}
        for chat_id, seq in chats.items()
    ]

    async def _list(*, cursor: str | None = None, limit: int = 50) -> dict[str, object]:
        return {"items": rows, "next_cursor": None}

    service._rest.list_chats = _list  # type: ignore[method-assign]


async def test_by_default_a_box_serves_every_chat_bound_to_it(tmp_path: Path) -> None:
    """No cap unless an operator sets one. A chat held back looks to its reader
    exactly like a chat nobody is serving, so the twentieth opens like the
    first and no mirror is closed to let it in."""
    clock = Clock()
    service, built = build_service(tmp_path, clock=clock, mirror_idle_minutes=60.0)
    _bind(service, {f"chat-{n}": 4 for n in range(20)})

    await service.sync_once()

    assert len(service.mirrors) == 20
    assert not any(m.stopped for m in built.values())


async def test_more_bound_chats_than_slots_does_not_churn_every_poll(tmp_path: Path) -> None:
    """Seven idle chats bound to a six-slot box. The first pass fills the box and
    leaves one unserved; every later pass must leave it exactly there. Evicting a
    served chat to open the seventh only makes the seventh the next victim — the
    box would close and re-spawn an agent server on every 15 s tick forever, and
    the chat taken from is whichever has been quiet longest, which includes the
    one being read between two questions."""
    clock = Clock()
    service, built = build_service(tmp_path, clock=clock, max_mirrors=6, mirror_idle_minutes=60.0)
    _bind(service, {f"chat-{n}": 4 for n in range(7)})

    await service.sync_once()
    served = set(service.mirrors)
    assert len(served) == 6
    spawned = set(built)  # the seventh took a slot from one of the first six

    for _ in range(2):
        clock.advance(60)
        await service.sync_once()
        assert set(service.mirrors) == served  # nothing evicted
        assert set(built) == spawned  # and no agent server re-spawned
        assert sum(1 for m in built.values() if m.stopped) == 1


async def test_a_new_message_on_an_evicted_chat_wins_a_slot_back(tmp_path: Path) -> None:
    """The cap holds a chat back only while it has nothing to answer. The moment
    its counter moves, the box makes room for it."""
    clock = Clock()
    service, built = build_service(tmp_path, clock=clock, max_mirrors=6, mirror_idle_minutes=60.0)
    seqs = {f"chat-{n}": 4 for n in range(7)}
    _bind(service, seqs)
    await service.sync_once()
    unserved = next(c for c in seqs if c not in service.mirrors)

    clock.advance(60)
    seqs[unserved] = 5  # someone typed into it
    _bind(service, seqs)
    await service.sync_once()

    assert unserved in service.mirrors
    assert len(service.mirrors) == 6
    assert sum(1 for m in built.values() if m.stopped) == 1  # exactly one slot taken


# -- the memory bounds the agent servers, whatever is configured ---------------


@pytest.mark.parametrize(
    ("limit_bytes", "expected"),
    [
        pytest.param(None, None, id="unknown-memory-is-no-cap"),
        pytest.param(8_000_000_000, 25, id="the-demo-box"),
        pytest.param(64 * 1024**3, 252, id="a-big-instance-is-not-clamped"),
        pytest.param(1024 * MIB, MIN_MAX_MIRRORS, id="a-box-below-its-reserve-still-holds-two"),
        pytest.param(2 * 1024 * MIB, 4, id="two-gigabytes-hold-four"),
    ],
)
def test_how_many_agent_servers_a_memory_holds(
    limit_bytes: int | None, expected: int | None
) -> None:
    assert (DEFAULT_AGENT_MEMORY_MB, DAEMON_MEMORY_RESERVE_MB) == (256, 1024)
    assert memory_max_mirrors(limit_bytes) == expected


def test_the_memory_cap_reads_the_environment_before_the_host() -> None:
    """An operator states the memory when the container cannot read its own
    limit, and re-budgets a server when a model of theirs needs more."""
    assert memory_cap_from_env({}, limit_bytes=8_000_000_000) == 25
    assert memory_cap_from_env({ENV_MEMORY_LIMIT_MB: "2048"}, limit_bytes=8_000_000_000) == 4
    assert memory_cap_from_env({ENV_AGENT_MEMORY_MB: "512"}, limit_bytes=8_000_000_000) == 12
    assert memory_cap_from_env({ENV_MEMORY_LIMIT_MB: "nope"}, limit_bytes=2 * 1024 * MIB) == 4
    assert memory_cap_from_env({ENV_AGENT_MEMORY_MB: "0"}, limit_bytes=8_000_000_000) == 25


async def test_a_box_serves_no_more_agent_servers_than_its_memory_holds(tmp_path: Path) -> None:
    """Nothing configured, the memory holds six: seven idle chats bound to the
    box leave one asleep, and the box does not churn it back in on later
    passes — the shape a configured cap has, reached without anyone setting
    one. The demo box ran forty-two servers into its 8 GB limit this way."""
    clock = Clock()
    service, built = build_service(
        tmp_path, clock=clock, memory_max_mirrors=6, mirror_idle_minutes=60.0
    )
    _bind(service, {f"chat-{n}": 4 for n in range(7)})

    await service.sync_once()
    served = set(service.mirrors)
    assert len(served) == 6
    assert sum(1 for m in built.values() if m.stopped) == 1

    for _ in range(2):
        clock.advance(60)
        await service.sync_once()
        assert set(service.mirrors) == served


@pytest.mark.parametrize(
    ("configured", "memory", "effective"),
    [
        pytest.param(None, None, None, id="nothing-known-serves-everything"),
        pytest.param(3, None, 3, id="a-configured-cap-alone"),
        pytest.param(None, 6, 6, id="the-memory-alone"),
        pytest.param(10, 6, 6, id="a-cap-above-the-memory-yields-to-it"),
        pytest.param(3, 6, 3, id="a-cap-below-the-memory-stands"),
    ],
)
def test_the_memory_bounds_whatever_cap_is_configured(
    tmp_path: Path, configured: int | None, memory: int | None, effective: int | None
) -> None:
    service, _ = build_service(
        tmp_path, clock=Clock(), max_mirrors=configured, memory_max_mirrors=memory
    )
    assert service._effective_cap() == effective


def test_the_capacity_the_box_reports_never_exceeds_what_its_memory_holds(tmp_path: Path) -> None:
    """Placement spreads a pool by the reported number, so a box must not
    advertise servers it cannot hold — whether the higher figure came from
    its cores or from an operator's cap."""
    cores = default_max_mirrors()
    service, _ = build_service(tmp_path, clock=Clock(), memory_max_mirrors=cores + 5)
    assert service._capacity() == cores
    service, _ = build_service(tmp_path, clock=Clock(), memory_max_mirrors=MIN_MAX_MIRRORS)
    assert service._capacity() == MIN_MAX_MIRRORS
    service, _ = build_service(tmp_path, clock=Clock(), max_mirrors=12, memory_max_mirrors=6)
    assert service._capacity() == 6
    service, _ = build_service(tmp_path, clock=Clock(), max_mirrors=12)
    assert service._capacity() == 12


# -- a restart leaves a sleeping chat asleep -----------------------------------


def _bind_rows(service: CloudMirrorService, rows: list[dict[str, Any]]) -> None:
    """Point the service's chat list at these rows as the backend would list
    them, every one bound to this box."""
    service._machine_id = "machine:x"
    listed = [{"machine_id": "machine:x", "last_seq": 4, **row} for row in rows]

    async def _list(*, cursor: str | None = None, limit: int = 50) -> dict[str, object]:
        return {"items": listed, "next_cursor": None}

    service._rest.list_chats = _list  # type: ignore[method-assign]


@pytest.mark.parametrize(
    ("row", "served"),
    [
        pytest.param({"machine_status": "ready"}, True, id="an-awake-chat-is-taken"),
        pytest.param({"machine_status": "asleep"}, False, id="a-sleeping-chat-stays-asleep"),
        pytest.param(
            {"machine_status": "asleep", "wake_requested_at": "2026-09-25T08:00:00Z"},
            True,
            id="a-reader-who-opened-it-wakes-it",
        ),
        pytest.param(
            {"machine_status": "asleep", "pending_turn": True},
            True,
            id="a-message-waiting-in-it-wakes-it",
        ),
        pytest.param({}, True, id="a-row-that-says-nothing-is-taken"),
    ],
)
async def test_a_fresh_process_leaves_a_chat_the_row_says_is_asleep(
    tmp_path: Path, row: dict[str, Any], served: bool
) -> None:
    """A daemon that restarts holds no memory of the chats it put to sleep;
    the row does. Re-opening every one of them spawned an agent server per
    chat the org has — forty-eight on the demo box, straight back into the
    memory limit that had just killed it — and took every folder again."""
    service, built = build_service(tmp_path, clock=Clock())
    _bind_rows(service, [{"id": "chat-a", **row}])

    await service.sync_once()

    assert ("chat-a" in service.mirrors) is served
    assert ("chat-a" in built) is served


async def test_a_chat_this_process_slept_is_judged_by_its_own_memory_first(
    tmp_path: Path,
) -> None:
    """While the process runs, the counter it recorded at the sleep is the
    word: a row still spelled ``asleep`` whose counter moved is a chat
    somebody typed into, and it is taken back even before the backend's
    activity row catches up."""
    clock = Clock()
    service, built = build_service(tmp_path, clock=clock, mirror_idle_minutes=20.0)
    _bind_rows(service, [{"id": "chat-a", "machine_status": "ready"}])
    await service.sync_once()
    clock.advance(21 * 60)
    assert await service.sweep_idle_mirrors() == ["chat-a"]
    assert built["chat-a"].stopped

    _bind_rows(service, [{"id": "chat-a", "machine_status": "asleep"}])
    await service.sync_once()
    assert "chat-a" not in service.mirrors

    _bind_rows(service, [{"id": "chat-a", "machine_status": "asleep", "last_seq": 5}])
    await service.sync_once()
    assert "chat-a" in service.mirrors


# -- a box going away hands every chat back ------------------------------------


class BlockingFolders(LiveFolders):
    """Custody whose hand-back really blocks the thread it runs on.

    A tree push that will not return is what a large folder, an unroutable
    content origin or a drive mid-deploy looks like from here, so the wait is a
    real one on a real worker thread rather than a recorded call.
    """

    def __init__(self, *, blocked: set[str]) -> None:
        super().__init__()
        self._blocked = set(blocked)
        self.gate = threading.Event()
        self._lock = threading.Lock()
        self.in_flight = 0
        #: The most hand-backs that were ever running at the same moment.
        self.peak = 0

    def hand_back(
        self, chat_id: str, *, recover: bool = True, ending: str | None = None, gone: bool = False
    ) -> Any:
        with self._lock:
            self.in_flight += 1
            self.peak = max(self.peak, self.in_flight)
        try:
            if chat_id in self._blocked:
                assert self.gate.wait(timeout=30.0), "the test never opened the gate"
            return super().hand_back(chat_id)
        finally:
            with self._lock:
                self.in_flight -= 1


async def _until(predicate: Callable[[], bool], *, within: float = 10.0) -> bool:
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return False


async def test_one_chat_whose_hand_back_blocks_does_not_strand_the_others(
    tmp_path: Path,
) -> None:
    """A slow folder costs its own chat's lease, never the whole box's.

    Released one at a time, a chat whose push does not come back holds every
    chat behind it: their leases are given up only when the TTL runs out, so
    for minutes no other box may serve them, and their readers see the last
    turn's files missing.
    """
    folders = BlockingFolders(blocked={"chat-slow"})
    service, built = build_service(tmp_path, clock=Clock(), folders=folders)
    for chat_id in ("chat-slow", "chat-a", "chat-b"):
        await service._ensure_mirror(chat_id, {"id": chat_id})

    stopping = asyncio.ensure_future(service.stop())

    handed_back = await _until(
        lambda: folders.held("chat-a") is None and folders.held("chat-b") is None
    )
    assert handed_back, "the chats behind the slow one still hold their leases while it pushes"
    assert folders.held("chat-slow") is not None, "the slow chat should still be in its hand-back"
    assert built["chat-a"].stopped and built["chat-b"].stopped
    assert not stopping.done(), "the stop cannot be over while a folder is still going up"

    folders.gate.set()
    await asyncio.wait_for(stopping, timeout=30.0)
    assert folders.held("chat-slow") is None, "the slow chat never gave its lease back"


async def test_the_hand_backs_a_box_opens_at_once_are_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A box with no configured cap serves every chat bound to it, and a
    shutdown that opened one tree push per chat would put the box's whole
    bandwidth behind whichever of them is slowest."""
    monkeypatch.setattr(service_module, "default_max_mirrors", lambda: 2)
    folders = BlockingFolders(blocked={"chat-0", "chat-1", "chat-2"})
    service, _built = build_service(tmp_path, clock=Clock(), folders=folders)
    for n in range(3):
        await service._ensure_mirror(f"chat-{n}", {"id": f"chat-{n}"})
    assert len(service.mirrors) == 3, "an uncapped box serves every chat bound to it"

    stopping = asyncio.ensure_future(service.stop())
    assert await _until(lambda: folders.peak >= 2), (
        f"the releases did not run together (peak={folders.peak})"
    )
    # Long enough for a third to start if nothing were holding it back.
    await asyncio.sleep(0.25)
    assert folders.peak == 2, (
        f"a shutdown opened more folder pushes at once than the box is sized for "
        f"(peak={folders.peak})"
    )

    folders.gate.set()
    await asyncio.wait_for(stopping, timeout=30.0)
    assert folders.peak == 2, f"the bound did not hold once the pushes returned ({folders.peak})"


# -- the take pass serves need first --------------------------------------------


class _PagedChats:
    """``GET /chats`` as the backend pages it: fifty at a time, in listing
    order, with the need facts the listing carries."""

    def __init__(self, chats: list[dict[str, Any]]) -> None:
        self.chats = chats

    async def list_chats(self, *, cursor: str | None = None, limit: int = 50) -> dict[str, Any]:
        start = int(cursor or 0)
        page = self.chats[start : start + 50]
        nxt = start + 50
        return {"items": page, "next_cursor": str(nxt) if nxt < len(self.chats) else None}


async def test_the_chats_that_owe_a_turn_are_taken_first_and_started_before_the_pass_moves_on(
    tmp_path: Path,
) -> None:
    """Three hundred chats on the box, two of them with somebody waiting —
    listed on the third and sixth pages. They are the first two taken, each
    one's turn started before the next chat is taken; the chat active a few
    minutes ago comes next, and the ones whose rows say nothing after. The one
    untouched for hours, with nothing owed, is not taken at all: it waits for
    the reader or the message that comes for it."""
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    chats: list[dict[str, Any]] = [
        {"id": f"chat-{n:03d}", "machine_id": "machine:x", "last_seq": 1} for n in range(300)
    ]
    chats[140]["pending_turn"] = True
    chats[290]["pending_turn"] = True
    chats[200]["last_activity_at"] = (now - timedelta(minutes=5)).isoformat()
    chats[10]["last_activity_at"] = (now - timedelta(hours=3)).isoformat()
    log: list[tuple[str, str]] = []
    service, _built = build_service(tmp_path, clock=Clock())
    service._machine_id = "machine:x"
    listing = _PagedChats(chats)
    service._rest.list_chats = listing.list_chats  # type: ignore[method-assign]
    real_factory = service._mirror_factory

    def factory(chat_id: str, chat: dict[str, Any]) -> Any:
        mirror = real_factory(chat_id, chat)
        mirror.log = log
        log.append(("take", chat_id))
        return mirror

    service._mirror_factory = factory

    await service.sync_once()

    assert log[:5] == [
        ("take", "chat-140"),
        ("turn", "chat-140"),
        ("take", "chat-290"),
        ("turn", "chat-290"),
        ("take", "chat-200"),
    ]
    assert ("turn", "chat-010") not in log
    taken = [chat_id for kind, chat_id in log if kind == "take"]
    assert "chat-010" not in taken, (
        "a chat untouched for hours that owes nothing was taken at claim"
    )
    assert len(taken) == 299
    assert taken[3:] == [
        c["id"] for c in chats if c["id"] not in taken[:3] and c["id"] != "chat-010"
    ]


# -- a chat bound while the start-up pass is still running -------------------------


class _SlowPass:
    """A box coming up over three hundred chats, one take at a time, held at
    the fiftieth take until the test has done what happens mid-pass.

    The listing is newest first, like the backend's; a chat the test creates
    mid-pass goes on its front, where the pass's own first read never saw it.
    """

    def __init__(self, tmp_path: Path, *, held_at: str = "chat-049") -> None:
        self.chats: list[dict[str, Any]] = [
            {"id": f"chat-{n:03d}", "machine_id": "machine:x", "last_seq": 1} for n in range(300)
        ]
        self.log: list[tuple[str, str]] = []
        self.reached = asyncio.Event()
        self.release = asyncio.Event()
        self.clock = Clock()
        self.service, self.built = build_service(tmp_path, clock=self.clock)
        self.service._machine_id = "machine:x"
        self.service._rest.list_chats = self.list_chats  # type: ignore[method-assign]
        self.service._rest.get_chat = self.get_chat  # type: ignore[method-assign]
        real_factory = self.service._mirror_factory
        held = held_at

        def factory(chat_id: str, chat: dict[str, Any]) -> Any:
            mirror = real_factory(chat_id, chat)
            mirror.log = self.log
            self.log.append(("take", chat_id))
            plain_start = mirror.start

            async def start() -> None:
                if chat_id == held:
                    self.reached.set()
                    await self.release.wait()
                await plain_start()

            mirror.start = start
            return mirror

        self.service._mirror_factory = factory

    async def list_chats(self, *, cursor: str | None = None, limit: int = 50) -> dict[str, Any]:
        start = int(cursor or 0)
        page = self.chats[start : start + 50]
        nxt = start + 50
        return {"items": page, "next_cursor": str(nxt) if nxt < len(self.chats) else None}

    async def get_chat(self, chat_id: str) -> dict[str, Any]:
        for chat in self.chats:
            if chat["id"] == chat_id:
                return chat
        raise AssertionError(f"no such chat {chat_id}")

    def bind_new_chat(self) -> dict[str, Any]:
        chat = {"id": "chat-new", "machine_id": "machine:x", "last_seq": 1, "pending_turn": True}
        self.chats.insert(0, chat)
        return chat

    def taken(self) -> list[str]:
        return [chat_id for kind, chat_id in self.log if kind == "take"]


async def test_a_chat_a_frame_names_mid_pass_is_taken_next_not_after_the_pass(
    tmp_path: Path,
) -> None:
    """The walkthrough: the box restarted and its start-up pass was working
    through ~270 chats when a person created a chat and sent its first
    message. The frame naming that chat must not wait behind the pass — the
    event stream that delivered it is not held, and the chat is the next one
    taken, its turn started before the pass goes on — and the pass's end must
    not stop the mirror as "no longer listed" because its own first read
    predates the chat."""
    rig = _SlowPass(tmp_path)
    passing = asyncio.get_running_loop().create_task(rig.service.sync_once())
    await asyncio.wait_for(rig.reached.wait(), 10.0)

    rig.bind_new_chat()
    # What the event stream does on a ``chat.updated`` frame. It returns at
    # once, with the pass still held: the stream carries every other chat's
    # frames and lease changes, and none of them waits for this take.
    await asyncio.wait_for(rig.service._chat_named("chat-new"), 2.0)
    assert not rig.release.is_set()

    rig.release.set()
    await asyncio.wait_for(passing, 30.0)

    taken = rig.taken()
    assert taken.index("chat-new") == 50, f"taken at {taken.index('chat-new')} of {len(taken)}"
    at = rig.log.index(("take", "chat-new"))
    assert rig.log[at + 1] == ("turn", "chat-new"), "its owed turn starts before the next take"
    assert len(taken) == 301
    assert not rig.built["chat-new"].stopped, "the pass that never listed it stopped it"
    assert "chat-new" in rig.service.mirrors


async def test_a_chat_owing_a_turn_is_found_mid_pass_without_any_frame(tmp_path: Path) -> None:
    """The frame is best-effort. A pass that has been running for longer than
    its re-read interval looks at the newest page again and takes a chat
    bound meanwhile that owes a turn before the long tail continues."""
    rig = _SlowPass(tmp_path)
    passing = asyncio.get_running_loop().create_task(rig.service.sync_once())
    await asyncio.wait_for(rig.reached.wait(), 10.0)

    rig.bind_new_chat()
    rig.clock.advance(service_module.PASS_RELIST_SECONDS + 1)
    rig.release.set()
    await asyncio.wait_for(passing, 30.0)

    taken = rig.taken()
    assert "chat-new" in taken, "the pass finished without taking the chat bound during it"
    assert taken.index("chat-new") == 50, f"taken at {taken.index('chat-new')} of {len(taken)}"
    assert not rig.built["chat-new"].stopped


async def test_a_chat_bound_mid_pass_without_a_message_waits_for_its_turn_in_the_order(
    tmp_path: Path,
) -> None:
    """The re-read serves need, not novelty: a chat bound mid-pass that owes
    nothing is not pulled ahead of the pass (it is taken on the next one, or
    the moment a frame names it)."""
    rig = _SlowPass(tmp_path)
    passing = asyncio.get_running_loop().create_task(rig.service.sync_once())
    await asyncio.wait_for(rig.reached.wait(), 10.0)

    rig.bind_new_chat()["pending_turn"] = False
    rig.clock.advance(service_module.PASS_RELIST_SECONDS + 1)
    rig.release.set()
    await asyncio.wait_for(passing, 30.0)

    assert "chat-new" not in rig.taken()


async def test_a_frame_while_the_box_is_idle_takes_the_chat_at_once(tmp_path: Path) -> None:
    """No pass running: the frame's own re-read takes the chat, no poll tick
    in between."""
    rig = _SlowPass(tmp_path, held_at="never")
    rig.chats = []
    rig.bind_new_chat()
    await asyncio.wait_for(rig.service.reconcile_chat("chat-new"), 5.0)
    assert rig.taken() == ["chat-new"]
    assert rig.service.mirrors["chat-new"].state == "running"


async def test_a_frame_mid_pass_returns_once_its_chat_is_settled_and_the_sweep_spares_only_it(
    tmp_path: Path,
) -> None:
    """Both sides of the end-of-pass sweep, in one pass. A chat a frame names
    mid-pass that this pass's listing never held is kept; a chat this pass's
    listing no longer carries (deleted: its re-read is 404) is stopped. And a
    caller that awaits ``reconcile_chat`` while the pass holds the lock gets
    control back only once that chat has been dealt with — the re-read is
    what it asked for, not a note that somebody will do it later."""
    from alkera_cli.cloud.rest import CloudApiError

    rig = _SlowPass(tmp_path, held_at="chat-held")
    rig.chats = [
        {"id": f"chat-{n}", "machine_id": "machine:x", "last_seq": 1} for n in range(3)
    ] + [{"id": "chat-gone", "machine_id": "machine:x", "last_seq": 1}]
    await rig.service.sync_once()
    assert set(rig.service.mirrors) == {"chat-0", "chat-1", "chat-2", "chat-gone"}

    rig.chats = [c for c in rig.chats if c["id"] != "chat-gone"]
    rig.chats.append({"id": "chat-held", "machine_id": "machine:x", "last_seq": 1})
    plain_get = rig.get_chat

    async def get_chat(chat_id: str) -> dict[str, Any]:
        if chat_id == "chat-gone":
            raise CloudApiError(404, {}, method="GET", path=f"/api/v1/chats/{chat_id}")
        return await plain_get(chat_id)

    rig.service._rest.get_chat = get_chat  # type: ignore[method-assign]
    passing = asyncio.get_running_loop().create_task(rig.service.sync_once())
    await asyncio.wait_for(rig.reached.wait(), 10.0)

    rig.bind_new_chat()  # chat-new: on no page this pass read

    async def frame(chat_id: str) -> bool:
        await rig.service.reconcile_chat(chat_id)
        return chat_id in rig.service.mirrors

    gone = asyncio.get_running_loop().create_task(frame("chat-gone"))
    new = asyncio.get_running_loop().create_task(frame("chat-new"))
    await asyncio.sleep(0.05)
    rig.release.set()
    assert await asyncio.wait_for(gone, 10.0) is False, "returned before the deleted chat stopped"
    assert await asyncio.wait_for(new, 10.0) is True, "returned before the named chat was taken"
    await asyncio.wait_for(passing, 10.0)

    assert rig.built["chat-gone"].stopped
    assert "chat-new" in rig.service.mirrors and not rig.built["chat-new"].stopped
    assert set(rig.service.mirrors) == {"chat-0", "chat-1", "chat-2", "chat-held", "chat-new"}


@pytest.mark.parametrize("woken_at", [0, 3], ids=["listed-first", "listed-after-the-awake"])
async def test_a_restart_over_a_full_box_wakes_what_a_reader_opened_and_leaves_the_sleepers(
    tmp_path: Path, woken_at: int
) -> None:
    """The daemon comes back on a box whose memory holds two agent servers,
    over a listing that has two chats the row says are asleep, three awake
    ones, and one a reader opened while the box was down. The sleepers stay
    asleep — no agent server, no folder taken — the box serves no more than
    its memory holds, and the chat the reader is looking at is served on the
    very first pass rather than evicted for an awake chat nobody is reading."""
    clock = Clock()
    folders = LiveFolders()
    service, built = build_service(
        tmp_path, clock=clock, folders=folders, memory_max_mirrors=2, mirror_idle_minutes=60.0
    )
    rows: list[dict[str, Any]] = [
        {"id": "awake-a", "machine_status": "ready"},
        {"id": "awake-b", "machine_status": "ready"},
        {"id": "awake-c", "machine_status": "ready"},
        {"id": "sleeper-1", "machine_status": "asleep"},
        {"id": "sleeper-2", "machine_status": "asleep"},
    ]
    rows.insert(
        woken_at,
        {"id": "woken", "machine_status": "asleep", "wake_requested_at": "2026-09-25T08:00:00Z"},
    )
    _bind_rows(service, rows)

    await service.sync_once()

    # The reader's chat is the first agent server the box spawns, wherever the
    # listing put it, and it is still being served when the pass ends.
    assert next(iter(built)) == "woken"
    assert "woken" in service.mirrors
    assert len(service.mirrors) <= 2
    for sleeper in ("sleeper-1", "sleeper-2"):
        assert sleeper not in built
        assert folders.held(sleeper) is None

    clock.advance(60)
    await service.sync_once()
    assert "woken" in service.mirrors
    assert len(service.mirrors) <= 2
