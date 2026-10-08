"""The workflow-side nudge of a singleton — start it, or signal the run already
going — exhausted with fakes. The real wiring (a child start on the email
queue, an external signal) is proven on the dev server by the email dispatch
tests; this module pins the decision every round of it makes.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from temporalio.exceptions import ApplicationError, WorkflowAlreadyStartedError
from worker.workflows._handoff import EXTERNAL_WORKFLOW_NOT_FOUND, start_or_signal


def _already_running() -> WorkflowAlreadyStartedError:
    return WorkflowAlreadyStartedError("billing.dispatch_emails", "billing.dispatch_emails")


def _not_found() -> ApplicationError:
    return ApplicationError("not found", type=EXTERNAL_WORKFLOW_NOT_FOUND)


@dataclass
class _Ops:
    """Scripted ``start`` / ``signal``: each call pops the next scripted outcome
    (``None`` succeeds, an exception is raised) and is logged in order."""

    starts: list[Exception | None]
    signals: list[Exception | None]
    log: list[str] = field(default_factory=list)

    async def start(self) -> object:
        self.log.append("start")
        exc = self.starts.pop(0)
        if exc is not None:
            raise exc
        return object()

    async def signal(self) -> None:
        self.log.append("signal")
        exc = self.signals.pop(0)
        if exc is not None:
            raise exc


async def test_a_singleton_that_is_not_running_is_started() -> None:
    ops = _Ops(starts=[None], signals=[])
    assert await start_or_signal(start=ops.start, signal=ops.signal) == "started"
    assert ops.log == ["start"]


async def test_a_singleton_already_running_is_signalled_instead() -> None:
    ops = _Ops(starts=[_already_running()], signals=[None])
    assert await start_or_signal(start=ops.start, signal=ops.signal) == "signalled"
    assert ops.log == ["start", "signal"]


async def test_a_run_that_finishes_between_the_refused_start_and_the_signal_is_restarted() -> None:
    """The one race: the start is refused, the run completes, the signal finds
    nothing — so the next round starts a fresh run."""
    ops = _Ops(starts=[_already_running(), None], signals=[_not_found()])
    assert await start_or_signal(start=ops.start, signal=ops.signal) == "started"
    assert ops.log == ["start", "signal", "start"]


@pytest.mark.parametrize("attempts", [1, 2, 3])
async def test_a_race_lost_every_round_is_reported_not_raised(attempts: int) -> None:
    ops = _Ops(starts=[_already_running()] * attempts, signals=[_not_found()] * attempts)
    outcome = await start_or_signal(start=ops.start, signal=ops.signal, attempts=attempts)
    assert outcome == "lost_race"
    assert ops.log == ["start", "signal"] * attempts


async def test_a_signal_refused_for_any_other_reason_propagates() -> None:
    """Only the not-found race is retried; anything else is the caller's."""
    other = ApplicationError("namespace gone", type="SomethingElse")
    ops = _Ops(starts=[_already_running(), None], signals=[other])
    with pytest.raises(ApplicationError) as excinfo:
        await start_or_signal(start=ops.start, signal=ops.signal)
    assert excinfo.value is other
    assert ops.log == ["start", "signal"], "no further round"


async def test_a_start_that_fails_for_any_other_reason_propagates() -> None:
    boom = RuntimeError("unknown child start fail cause")
    ops = _Ops(starts=[boom], signals=[None])
    with pytest.raises(RuntimeError) as excinfo:
        await start_or_signal(start=ops.start, signal=ops.signal)
    assert excinfo.value is boom
    assert ops.log == ["start"], "nothing was signalled"


def test_the_not_found_type_is_the_servers_spelling() -> None:
    assert EXTERNAL_WORKFLOW_NOT_FOUND == "ExternalWorkflowExecutionNotFound"
