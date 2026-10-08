"""The box outlives the server's absence and resumes on its own.

The demo box once spent twenty-five minutes posting every heartbeat, chat list
and folder beat to a host that answered 530 (the tunnel in front of the API had
moved), and an hour before that the API itself answered 500 for half an hour (a
database outage). Both times the box kept its chats and its leases in memory
and kept asking — which is right — but it asked at the flat interval with a
WARNING per beat, and when the server did answer again it waited for the next
poll tick to notice. This pins the shape the machine loop must have instead:

* a failed beat is retried after a JITTERED wait no shorter than the interval
  and always inside the ready window, so a fleet coming back from one outage
  spreads out and no box is judged silent for backing off too far;
* the outage is said ONCE, at WARNING, and the recovery once, at INFO — not a
  warning per beat into a log nobody reads;
* the first beat that lands after an outage re-reads the org's chats at once,
  so a chat the reader spoke into while the box was away is picked up on the
  beat that found the server, not a poll tick later;
* a row the platform reaped meanwhile is re-registered, and the id the server
  hands back is the id the box serves under — the chats bound to it stay
  served without a hand-over.

Driven through the injected ``sleep`` and ``rng`` seams; nothing here opens a
socket.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest
from _adapter_factory import FakeAdapterFactory
from alkera_cli.cloud.rest import CloudApiError, CloudRestClient
from alkera_cli.cloud.service import CloudMirrorService, MirrorSettings
from alkera_cli.cloud.transport import CloudSocket
from alkera_cli.harness import HarnessRuntime
from alkera_cli.harness._fake import FakeAdapter
from alkera_core.compute.liveness import MISSED_HEARTBEATS_BEFORE_UNREACHABLE
from alkera_core.project.directory import ProjectDirectory

HEARTBEAT = 15.0
WINDOW = HEARTBEAT * MISSED_HEARTBEATS_BEFORE_UNREACHABLE
MACHINE_ID = "807bf89a-464c-4867-8b08-6e020a9bd8a3"


class _LoopStoppedError(Exception):
    """Ends the loop from inside its own sleep seam."""


class _NoSocket(CloudSocket):
    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def rebind(self, rest: CloudRestClient) -> None:
        return None


def _refused(status: int) -> CloudApiError:
    return CloudApiError(
        status,
        {"error": {"code": "", "message": ""}},
        method="POST",
        path=f"/api/v1/machines/{MACHINE_ID}/heartbeat",
    )


class _OutageRest(CloudRestClient):
    """Answers the first ``down`` heartbeats with ``status`` (the tunnel's 530,
    the API's 500), then ``gone`` of them with 404 (the row was reaped), then
    lands every beat. Registration always hands back the same id."""

    def __init__(self, *, down: int, status: int = 530, gone: int = 0) -> None:
        super().__init__(api_url="http://127.0.0.1:1", token="t", agent_id=f"machine:{MACHINE_ID}")
        self.down = down
        self.status = status
        self.gone = gone
        self.beats = 0
        self.landed = 0
        self.registrations = 0
        self.chat_lists: list[int] = []

    async def register_machine(self, **_kwargs: Any) -> dict[str, Any]:
        self.registrations += 1
        return {"id": MACHINE_ID}

    async def heartbeat_machine(self, machine_id: str, **_kwargs: Any) -> dict[str, Any]:
        self.beats += 1
        if self.beats <= self.down:
            raise _refused(self.status)
        if self.beats <= self.down + self.gone:
            raise _refused(404)
        self.landed += 1
        return {}

    async def list_chats(self, *, cursor: str | None = None) -> dict[str, Any]:
        self.chat_lists.append(self.beats)
        return {"items": [], "next_cursor": None}

    def for_agent(self, agent_id: str) -> _OutageRest:
        self._agent_id = agent_id
        return self


def _service(
    tmp_path: Path,
    rest: _OutageRest,
    waits: list[float],
    *,
    stop_after: int,
    rng_values: list[float],
) -> CloudMirrorService:
    async def sleep(seconds: float) -> None:
        waits.append(seconds)
        if len(waits) >= stop_after:
            raise _LoopStoppedError

    draws = iter(rng_values)

    def rng() -> float:
        return next(draws)

    settings = MirrorSettings(
        api_url="http://127.0.0.1:1",
        token="t",
        project_dir=tmp_path,
        machine_name="demo box",
        provider_pod_id="pod-demo",
        machine_type_code="cpu3c",
        heartbeat_interval=HEARTBEAT,
    )
    runtime = HarnessRuntime(
        ProjectDirectory(tmp_path / ".alkera"),
        adapter_factory=FakeAdapterFactory(FakeAdapter, available=True),
    )
    return CloudMirrorService(
        settings, runtime, rest=rest, socket=_NoSocket(rest), sleep=sleep, rng=rng
    )


async def _run(service: CloudMirrorService) -> None:
    with pytest.raises(_LoopStoppedError):
        await service._machine_loop()
    # Whatever the recovery started must have run before the assertions read it.
    await service.settle_background()


@pytest.mark.parametrize(
    "status", [pytest.param(530, id="tunnel-530"), pytest.param(500, id="api-500")]
)
async def test_failed_beats_back_off_with_jitter_inside_the_ready_window(
    tmp_path: Path, status: int, caplog: pytest.LogCaptureFixture
) -> None:
    rest = _OutageRest(down=6, status=status)
    waits: list[float] = []
    draws = [0.1, 0.9, 0.5, 0.0, 0.99, 0.3]
    with caplog.at_level(logging.DEBUG, logger="alkera_cli.cloud.service"):
        await _run(_service(tmp_path, rest, waits, stop_after=8, rng_values=draws))

    outage = waits[:6]
    assert all(HEARTBEAT <= wait < 2 * HEARTBEAT for wait in outage), outage
    assert max(outage) < WINDOW, "no wait may reach the window the platform reaps on"
    assert len(set(outage)) > 1, "the waits are jittered, not a fixed interval"
    assert waits[6:] == [HEARTBEAT, HEARTBEAT], "a landed beat returns to the plain cadence"
    assert rest.landed == 2

    warnings = [
        r for r in caplog.records if r.levelno == logging.WARNING and "answer" in r.getMessage()
    ]
    assert len(warnings) == 1, "the outage is said once, not once per beat"
    assert str(status) in warnings[0].getMessage()
    recovered = [
        r
        for r in caplog.records
        if r.levelno == logging.INFO and "answered again" in r.getMessage()
    ]
    assert len(recovered) == 1
    assert "6 missed" in recovered[0].getMessage()


async def test_the_first_beat_back_re_reads_the_chats_at_once(tmp_path: Path) -> None:
    """Nothing lists the chats while the server is away (each list would only
    fail too), and the beat that finds it back lists them before the next poll
    tick would have."""
    rest = _OutageRest(down=3)
    waits: list[float] = []
    await _run(_service(tmp_path, rest, waits, stop_after=6, rng_values=[0.2] * 6))

    # The re-read runs as its own task, so the loop may have beaten again by
    # the time it lists: what is pinned is ONE re-read, and none before the
    # server was back.
    assert len(rest.chat_lists) == 1, "one re-read, started by the beat that landed"
    assert rest.chat_lists[0] >= 4, "no list was attempted while the server was away"


async def test_a_row_reaped_during_the_outage_is_re_registered_under_the_same_id(
    tmp_path: Path,
) -> None:
    """The platform reaped the row while it could not hear the box; the next
    beat's 404 re-registers, the server hands the same id back (it revives the
    row by pod id), and the chats bound to that id go on being served."""
    rest = _OutageRest(down=2, gone=1)
    waits: list[float] = []
    service = _service(tmp_path, rest, waits, stop_after=6, rng_values=[0.4] * 6)
    await _run(service)

    assert rest.registrations == 2, "once at start, once after the 404"
    assert service.machine_id == MACHINE_ID
    assert service._serves({"machine_id": MACHINE_ID}) is True
    assert rest.landed >= 1
