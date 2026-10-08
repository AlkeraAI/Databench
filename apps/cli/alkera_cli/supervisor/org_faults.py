"""Why each org's worker is not serving, and whether that leaves the box unable
to serve anyone.

The supervisor names every failure with a
:class:`~alkera_core.compute.worker_faults.WorkerFault` and the failure's own
words, scrubbed (:func:`describe`). The code rides the shipped
``start_failed`` and ``crash_loop`` events, so a box nobody can log in to
(a RunPod pod has no SSH) still says why its worker will not start. When every
org the box was asked to serve is failing and none has a worker up, the box
itself is faulted (:meth:`FaultBook.box_fault`): the heartbeat says so, the
machine reads unhealthy in the product, and its running time stops being
billed until a worker serves.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass

from alkera_core.compute.box_logs import scrub_text
from alkera_core.compute.worker_faults import WorkerFault

from alkera_cli.supervisor.http import ApiError
from alkera_cli.supervisor.slots import SlotError


class WorkerStartError(RuntimeError):
    """A worker could not be started, for a named reason."""

    def __init__(self, fault: WorkerFault, message: str) -> None:
        super().__init__(message)
        self.fault = fault


@dataclass(frozen=True, slots=True)
class Fault:
    """One failure: its code, and its own words as they may leave the box."""

    code: WorkerFault
    summary: str

    def heartbeat(self) -> dict[str, str]:
        """As the heartbeat carries it (``MachineFaultReport``)."""
        return {"code": self.code.value, "summary": self.summary}


def describe(exc: BaseException) -> Fault:
    """The fault an exception from starting a worker names."""
    if isinstance(exc, WorkerStartError):
        code = exc.fault
    elif isinstance(exc, SlotError):
        code = WorkerFault.SLOTS_EXHAUSTED
    elif isinstance(exc, ApiError):
        code = WorkerFault.CREDENTIAL_REFUSED
    elif isinstance(exc, OSError):
        code = WorkerFault.SPAWN_FAILED
    else:
        code = WorkerFault.OTHER
    return Fault(code=code, summary=scrub_text(str(exc) or type(exc).__name__))


class FaultBook:
    """Why each org's worker last failed, until it serves. When an org counts
    as failing is :mod:`alkera_cli.supervisor.crash_loop`'s to say; this keeps
    only the code and words that go with it."""

    def __init__(self) -> None:
        self._last: dict[str, Fault] = {}

    def failed(self, org_id: str, fault: Fault) -> None:
        self._last[org_id] = fault

    def last(self, org_id: str) -> Fault | None:
        return self._last.get(org_id)

    def serving(self, org_id: str) -> None:
        """The org's worker is up (or was stopped on purpose): whatever failed
        before is over."""
        self._last.pop(org_id, None)

    def box_fault(
        self, *, wanted: Collection[str], serving: Collection[str], failing: Collection[str]
    ) -> Fault | None:
        """The box's fault: the last failure of the orgs it was asked to serve
        when every one of them is ``failing`` and none has a worker up; ``None``
        while any is served, or while nothing is wanted of it."""
        if not wanted or set(wanted) & set(serving) or not set(wanted) <= set(failing):
            return None
        return next((self._last[o] for o in sorted(wanted) if o in self._last), None)


__all__ = ["Fault", "FaultBook", "WorkerStartError", "describe"]
