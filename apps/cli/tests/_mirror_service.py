"""A CloudMirrorService whose mirrors are fakes, for the lifecycle tests.

The service's own bookkeeping — which chats it serves, when each was last
active, which mirror it closes — is what these tests are about, so the mirror
is reduced to what the SERVICE can see of one: a state, a published counter,
whether a turn is running, and a stop that reaps the agent server.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
from _adapter_factory import FakeAdapterFactory
from alkera_cli.cloud.activity import ChatActivity
from alkera_cli.cloud.rest import CloudRestClient
from alkera_cli.cloud.service import CloudMirrorService, MirrorSettings
from alkera_cli.cloud.transport import CloudSocket
from alkera_cli.harness import HarnessRuntime
from alkera_cli.harness._fake import FakeAdapter
from alkera_core.project.directory import ProjectDirectory

SERVICE_LOGGER = "alkera_cli.cloud.service"


def unreachable(_request: httpx.Request) -> httpx.Response:
    """The box's own backend routes with nothing at the other end.

    Refused in process rather than against a port nobody binds: how long a
    refused connect takes is the HOST's business — two whole seconds on the
    Windows runners against a closed loopback port, immediate on Linux — and a
    registration, a heartbeat, a chat poll and every event-stream reconnect each
    pay it. A test that waits on those is timing the runner, not the mirror."""
    raise httpx.ConnectError("no backend here")


def refused_rest() -> CloudRestClient:
    """A rest client whose every route is refused without touching the network."""
    return CloudRestClient(
        api_url="http://127.0.0.1:1",
        token="t",
        agent_id="machine:x",
        transport=httpx.MockTransport(unreachable),
    )


class NoSocket(CloudSocket):
    """A socket that never connects: the service's bookkeeping is the subject."""

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def rebind(self, rest: CloudRestClient) -> None:
        return None


class FakeMirror:
    """A mirror as the service sees it."""

    def __init__(
        self,
        chat_id: str,
        working_dir: Path | None = None,
        clock: Callable[[], float] | None = None,
        on_turn_end: Callable[[str], None] | None = None,
    ) -> None:
        self.chat_id = chat_id
        #: Who hears that a turn ended — the service, as the real mirror is
        #: built with it.
        self.on_turn_end = on_turn_end
        self._clock = clock or (lambda: 0.0)
        #: Where the agent runs. The service streams THIS to the drive, not the
        #: folder around it, so a fake that did not name one would let the
        #: wiring pass by never being asked.
        self.working_dir = working_dir or Path(chat_id)
        self.state = "running"
        self.published_count = 0
        self.chunk_count = 0
        self.turn_running = False
        # A turn parked on a person: still mid-turn, since the answer continues it.
        self.waiting_on_a_person = False
        #: Whether the ONLY thing in flight is an ask parked on a person. The
        #: real mirror works this out from its interrupts, its queue and its
        #: session; the service only asks, and decides the window itself.
        self.parked_only = False
        #: When a reader was last seen — stamped from the same clock the
        #: service reads, so the window the service applies is a real one.
        self.reader_at = self._clock()
        #: How many times the service said a reader was here.
        self.readers = 0
        self.stopped = False
        self.catch_ups = 0
        #: Something running that the counters cannot express — a background job,
        #: a subagent, a message not yet handed over. Set through ``quiescent``,
        #: which is the one question the service asks.
        self.busy = False
        #: A background job running in the chat's session (the real mirror
        #: asks its session); it makes the chat busy too.
        self.job_running = False
        #: What ``start`` raises instead of running, when set: the harness
        #: would not open the chat's session here (a pinned agent session
        #: the storage no longer has, a binary that is gone).
        self.fail_start: BaseException | None = None
        #: Shared with the test, when it wants the ORDER of what happened.
        self.log: list[tuple[str, str]] | None = None
        #: The notes the chat was given before a sleep for room, in order.
        self.notes: list[str] = []

    @property
    def activity(self) -> ChatActivity:
        """What the service reads, built from the fields a test sets exactly
        as the real mirror builds it from its own two questions."""
        if self.parked_only:
            return ChatActivity.AWAITING_USER
        if self.quiescent:
            return ChatActivity.IDLE
        return ChatActivity.RUNNING_JOB if self.job_running else ChatActivity.WORKING

    @property
    def quiescent(self) -> bool:
        """Whether this chat owes nothing, exactly as the real mirror reports it:
        a running turn — parked on a person or not — is something in flight, and
        so is anything a test has marked ``busy``."""
        return not self.turn_running and not self.busy and not self.job_running

    @quiescent.setter
    def quiescent(self, value: bool) -> None:
        self.busy = not value

    async def note_pressure_sleep(self, sentence: str) -> bool:
        """What the real mirror posts to the chat's transcript; kept here so a
        test reads what the chat was told, and only while it still runs."""
        assert not self.stopped, "a note after the release reaches nobody"
        self.notes.append(sentence)
        return True

    def end_turn(self, *, told: bool = True) -> None:
        """The running turn finishes having said something. ``told=False`` is
        an end nobody heard of — the notice lost, only the counters moved."""
        self.turn_running = False
        self.published_count += 1
        if told and self.on_turn_end is not None:
            self.on_turn_end(self.chat_id)

    async def start(self) -> None:
        if self.fail_start is not None:
            self.state = "failed"
            raise self.fail_start
        self.state = "running"

    async def stop(self) -> None:
        self.stopped = True
        self.state = "stopped"

    def request_catch_up(self, *, last_seq: int | None = None) -> None:
        self.catch_ups += 1

    async def wait_for_owed_turn(self, within: float) -> bool:
        """The owed turn is handed over at once; the log says when, relative to
        what else the service did."""
        if self.log is not None:
            self.log.append(("turn", self.chat_id))
        return True

    @property
    def reader_seen_at(self) -> float:
        return self.reader_at

    def note_reader(self) -> None:
        self.readers += 1
        self.reader_at = self._clock()


class _Reader:
    """A sandbox reader over a plain function of the chat id."""

    def __init__(self, work: Callable[[str], Any]) -> None:
        self._work = work

    def work(self, session_id: str) -> Any:
        return self._work(session_id)

    def drop(self, session_id: str) -> None:
        return None


class Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def build_service(
    tmp_path: Path,
    *,
    clock: Clock,
    folders: Any | None = None,
    rest: CloudRestClient | None = None,
    failing_starts: dict[str, BaseException] | None = None,
    sleep: Any | None = None,
    hard_stop: Any | None = None,
    wall_clock: Any | None = None,
    memory: Any | None = None,
    sandbox_work: Any | None = None,
    notebooks: Any | None = None,
    sleep_settings: Any | None = None,
    **overrides: Any,
) -> tuple[CloudMirrorService, dict[str, FakeMirror]]:
    """``failing_starts`` maps a chat id to what its mirror's ``start`` raises;
    the mapping is read when the mirror is BUILT, so a test that clears an
    entry lets the next attempt for that chat succeed. ``sleep`` replaces the
    service's own wait, which is what lets a test drive a timed loop (the
    drain's ceiling) off the injected clock instead of the wall;
    ``sleep_settings`` is the box's sleep knobs (``SleepSettings``)."""
    settings = MirrorSettings(
        api_url="http://127.0.0.1:1",
        token=overrides.pop("token", "t"),
        project_dir=tmp_path,
        machine_name="demo box",
        provider_pod_id="pod-demo",
        machine_type_code="cpu3c",
        **({} if sleep_settings is None else {"sleep": sleep_settings}),
        **overrides,
    )
    runtime = HarnessRuntime(
        ProjectDirectory(tmp_path / ".alkera"),
        adapter_factory=FakeAdapterFactory(FakeAdapter, available=True),
    )
    built: dict[str, FakeMirror] = {}

    def factory(chat_id: str, _chat: dict[str, Any]) -> Any:
        built[chat_id] = FakeMirror(
            chat_id,
            tmp_path / ".alkera" / "chats" / chat_id / "scratch",
            clock=clock,
            # What ``_default_mirror`` hands the real one.
            on_turn_end=lambda ended: service._turn_ended(ended),
        )
        if failing_starts is not None:
            built[chat_id].fail_start = failing_starts.get(chat_id)
        return built[chat_id]

    def _rest() -> CloudRestClient:
        return refused_rest()

    service = CloudMirrorService(
        settings,
        runtime,
        rest=rest or _rest(),
        socket=NoSocket(_rest()),
        mirror_factory=factory,
        folders=folders,
        clock=clock,
        **({} if sleep is None else {"sleep": sleep}),
        hard_stop=no_hard_stop if hard_stop is None else hard_stop,
        **({} if wall_clock is None else {"wall_clock": wall_clock}),
        # A box with no chat sandbox and memory nobody can read, unless the
        # test gives it one: what this host's probes would otherwise answer.
        memory=memory or (lambda: None),
        notebooks=notebooks,
        sandbox_probes=sandbox_work
        if hasattr(sandbox_work, "drop")
        else _Reader(sandbox_work or (lambda _chat_id: None)),
    )
    return service, built


def no_hard_stop(seconds: float, action: Callable[[], None]) -> None:
    """The hard-stop seam a lifecycle test gets unless it asks for one.

    The production seam arms a daemon timer that ends the PROCESS ninety
    seconds after a supervised restart begins. Inside pytest that process is
    the xdist worker, and the timer fires in whatever test happens to be
    running by then — a crash charged to a test that had nothing to do with it.
    A test that asserts on the arming passes its own recorder.
    """
    del seconds, action
