"""A refused registration is re-asked on a backoff, not every heartbeat.

A refusal (no grant, no credit, the org's ceiling in use) is an ANSWER: only a
human changes it. The mirror used to re-ask every heartbeat interval — 20 s —
and each ask cost the org a ``refused`` frame in every open browser and a row on
its audit trail: 4,320 of each per day for one refused box. So the loop backs
off to a five-minute ceiling, and starts over the moment the reason CHANGES, so
a box refused for something new is not stuck behind the old refusal's delay.

The loop is driven through its injected ``sleep``: what it waits IS the
behaviour under test, and the seam is the one the service already takes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from _adapter_factory import FakeAdapterFactory
from alkera_cli.cloud.rest import CloudApiError, CloudRestClient
from alkera_cli.cloud.service import (
    REFUSAL_BACKOFF_CAP_SECONDS,
    CloudMirrorService,
    MirrorSettings,
)
from alkera_cli.cloud.transport import CloudSocket
from alkera_cli.harness import HarnessRuntime
from alkera_cli.harness._fake import FakeAdapter
from alkera_core.project.directory import ProjectDirectory

HEARTBEAT = 20.0
MISSING_ROUTE_RETRY = 60.0


class _LoopStoppedError(Exception):
    """Ends the loop from inside its own sleep seam."""


class _NoSocket(CloudSocket):
    """A socket that never connects: the machine loop is what is under test."""

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def rebind(self, rest: CloudRestClient) -> None:
        return None


class _ScriptedRest(CloudRestClient):
    """Answers each registration attempt from a script of ``(status, code)``
    (the last entry repeats); ``None`` means the registration succeeds."""

    def __init__(self, script: list[tuple[int, str] | None]) -> None:
        super().__init__(api_url="http://127.0.0.1:1", token="t", agent_id="machine:x")
        self.script = script
        self.attempts = 0
        self.heartbeats = 0

    async def register_machine(self, **_kwargs: Any) -> dict[str, Any]:
        self.attempts += 1
        answer = self.script[min(self.attempts - 1, len(self.script) - 1)]
        if answer is None:
            return {"id": "machine-1"}
        status, code = answer
        raise CloudApiError(
            status,
            {"error": {"code": code, "message": "refused"}},
            method="POST",
            path="/api/v1/machines/register",
        )

    async def heartbeat_machine(self, machine_id: str, **_kwargs: Any) -> dict[str, Any]:
        self.heartbeats += 1
        return {}

    def for_agent(self, agent_id: str) -> _ScriptedRest:
        """Still this script. A box that registers re-binds its client to the
        machine id it was granted, and a fresh real client here would send the
        beats to the address in the settings instead of to the script."""
        self._agent_id = agent_id
        return self


def _service(tmp_path: Path, rest: _ScriptedRest, waits: list[float]) -> CloudMirrorService:
    async def sleep(seconds: float) -> None:
        waits.append(seconds)
        if len(waits) >= 6:
            raise _LoopStoppedError

    settings = MirrorSettings(
        api_url="http://127.0.0.1:1",
        token="t",
        project_dir=tmp_path,
        machine_name="demo box",
        provider_pod_id="pod-demo",
        machine_type_code="cpu3c",
        heartbeat_interval=HEARTBEAT,
        missing_route_retry=MISSING_ROUTE_RETRY,
    )
    runtime = HarnessRuntime(
        ProjectDirectory(tmp_path / ".alkera"),
        adapter_factory=FakeAdapterFactory(FakeAdapter, available=True),
    )
    service = CloudMirrorService(settings, runtime, rest=rest, socket=_NoSocket(rest), sleep=sleep)
    return service


async def _run(service: CloudMirrorService) -> None:
    with pytest.raises(_LoopStoppedError):
        await service._machine_loop()


def _band(attempt: int) -> tuple[float, float]:
    """The jittered delay window for the nth refusal: the raw exponential step
    (capped) scaled by the backoff's ``[0.5, 1.5)`` factor."""
    raw = min(REFUSAL_BACKOFF_CAP_SECONDS, HEARTBEAT * (2**attempt))
    return 0.5 * raw, 1.5 * raw


async def test_a_repeated_refusal_backs_off_instead_of_asking_every_heartbeat(
    tmp_path: Path,
) -> None:
    rest = _ScriptedRest([(429, "compute_limit_reached")])
    waits: list[float] = []
    await _run(_service(tmp_path, rest, waits))

    assert rest.attempts == 6, "the box keeps trying — it just stops hammering"
    for attempt, wait in enumerate(waits):
        low, high = _band(attempt)
        assert low <= wait < high, f"attempt {attempt} waited {wait}, not in [{low}, {high})"
    assert waits[2] > HEARTBEAT, "by the third refusal the wait is well past a heartbeat"
    assert max(waits) < 1.5 * REFUSAL_BACKOFF_CAP_SECONDS, "the delay is capped"
    # An hour of a refused box is a handful of asks, not 180.
    assert sum(waits) > 20 * HEARTBEAT


async def test_a_changed_reason_starts_the_backoff_over(tmp_path: Path) -> None:
    """The refusal that matters is the CURRENT one: a box whose org just ran out
    of credit must not wait five minutes because it had also hit the ceiling."""
    rest = _ScriptedRest(
        [
            (429, "compute_limit_reached"),
            (429, "compute_limit_reached"),
            (429, "compute_limit_reached"),
            (402, "insufficient_credit"),
        ]
    )
    waits: list[float] = []
    await _run(_service(tmp_path, rest, waits))

    low, high = _band(0)
    assert low <= waits[3] < high, "the new reason is asked again at the floor delay"
    assert waits[3] < waits[2], "and that is shorter than the wait the old reason had earned"
    # ...and then it climbs again under the new reason.
    low, high = _band(1)
    assert low <= waits[4] < high


async def test_a_backend_without_the_machine_routes_keeps_its_own_flat_retry(
    tmp_path: Path,
) -> None:
    """A 404 is not a refusal — it is an older backend. That path already has its
    own interval and must not be turned into an exponential backoff."""
    rest = _ScriptedRest([(404, "")])
    waits: list[float] = []
    await _run(_service(tmp_path, rest, waits))

    assert waits == [MISSING_ROUTE_RETRY] * 6


async def test_registering_clears_the_backoff_a_later_refusal_starts_fresh(
    tmp_path: Path,
) -> None:
    """Once the box is admitted, the delay it earned while refused is gone: if
    the row is later dropped and it is refused again, it asks at the floor."""
    rest = _ScriptedRest(
        [
            (429, "compute_limit_reached"),
            (429, "compute_limit_reached"),
            None,  # admitted
            (429, "compute_limit_reached"),
        ]
    )
    waits: list[float] = []
    service = _service(tmp_path, rest, waits)

    async def _gone(machine_id: str, **_kwargs: Any) -> dict[str, Any]:
        raise CloudApiError(404, {}, method="POST", path="/api/v1/machines/x/heartbeat")

    rest.heartbeat_machine = _gone  # type: ignore[method-assign]

    await _run(service)

    assert waits[2] == HEARTBEAT, (
        "the tick that registered beats straight away and waits the plain heartbeat interval"
    )
    low, high = _band(0)
    assert low <= waits[3] < high and waits[3] != HEARTBEAT, (
        "the very next tick is the refusal after re-registering, at the floor delay — finding "
        "the row gone did not cost a tick of its own"
    )
    low, high = _band(1)
    assert low <= waits[4] < high, "and climbs from there"


async def test_the_first_beat_after_registering_does_not_wait_for_the_chat_pass(
    tmp_path: Path,
) -> None:
    """A box that has just registered beats before it opens anything.

    The platform measures a box's silence from the moment its row is ready and
    terminates it when nothing beats inside the ready window. Opening the org's
    chats is the box's slowest work — a mirror, a folder take and a session
    start apiece, one chat at a time, with no budget over the pass — so a box
    that did that before its first beat was terminated for silence while it was
    busy doing the very thing it was started for, and its next beat found the
    row gone and paid for the whole pass again.

    Time here is the injected sleep plus whatever the pass would have spent, so
    the assertion is what the box would have burned, not what this test waits.
    """
    rest = _ScriptedRest([None])
    waits: list[float] = []
    service = _service(tmp_path, rest, waits)

    now = {"t": 0.0}
    ready_at: list[float] = []
    beats: list[float] = []

    async def _sleep(seconds: float) -> None:
        now["t"] += seconds
        waits.append(seconds)
        if len(waits) >= 3:
            raise _LoopStoppedError

    async def _register_at(**kwargs: Any) -> dict[str, Any]:
        body = await _ScriptedRest.register_machine(rest, **kwargs)
        ready_at.append(now["t"])
        return body

    async def _beat_at(machine_id: str, **_kwargs: Any) -> dict[str, Any]:
        beats.append(now["t"])
        return {}

    async def _slow_pass() -> None:
        # A cold discovery pass over an org's chats: each one taken, started
        # and awaited in turn, and nothing bounding the whole.
        now["t"] += 300.0

    service._sleep = _sleep  # type: ignore[method-assign]
    rest.register_machine = _register_at  # type: ignore[method-assign]
    rest.heartbeat_machine = _beat_at  # type: ignore[method-assign]
    service.sync_once = _slow_pass  # type: ignore[method-assign]

    await _run(service)

    assert ready_at, "the box registered"
    assert beats, "and beat against the row it registered"
    assert beats[0] - ready_at[0] <= HEARTBEAT, (
        f"the first beat landed {beats[0] - ready_at[0]}s after the row was ready — past the "
        "window a silent box is reaped on, while the box was busy opening chats"
    )
