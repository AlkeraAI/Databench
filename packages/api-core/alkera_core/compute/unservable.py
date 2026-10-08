"""A machine that cannot serve, because its worker cannot start or because its
box has gone silent: recorded from its heartbeat, shown as unhealthy (or not
responding), and not billed for its running time.

A box's supervisor says on its beat when no worker of it can serve any org it
was asked to (``MachineHeartbeatRequest.fault``: a
:class:`~alkera_core.compute.worker_faults.WorkerFault` and the failure's own
scrubbed words). :func:`record_fault` keeps that on the allocation: the code
and summary, ``fault_since`` from the first such beat, and ``fault_until``
from the first beat after it that says the box serves again.

The billing rule, the same one as "no storage before ready": an org pays for
the minutes its machine could run its chats, and a box serves only while it
is heard from and says its worker can serve. Two kinds of minute are ours:

- the running minutes in ``[fault_since, fault_until)`` (or up to now while
  the fault lasts);
- the running minutes of a workspace machine after its last heartbeat once
  the ready window has passed without one (a box that cannot reach the API,
  say), for as long as it stays silent.

The meter records their true cost, bills none of them and does not count them
toward a lease (:func:`unbilled_span`), then forgets an ended fault span once
it has metered past it (:func:`settle`). Storage is still billed: the disk
is held either way, as it is while a machine sleeps.

The product reads the fault only through :func:`fault_read`: the org sees the
machine as unhealthy with one sentence (``UNHEALTHY_MESSAGE``), platform staff
also see the code, the box's words and since when.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

from alkera_core.compute.box_isolation import known_mechanisms, profile_from_capabilities
from alkera_core.compute.box_logs import scrub_text
from alkera_core.compute.machines import WORKSPACE, ready_window, silent_since
from alkera_core.compute.worker_faults import UNHEALTHY_MESSAGE, fault_code
from alkera_core.models.compute import ComputeAllocation
from alkera_core.schemas.compute_machines import MachineIsolationRead
from alkera_core.schemas.org_machines import MachineFaultRead

#: The longest summary kept (the column's width).
SUMMARY_MAX_CHARS = 300


def record_fault(
    alloc: ComputeAllocation, report: Mapping[str, object] | None, *, now: datetime
) -> None:
    """Keep what a beat said about the box's fault. A beat naming one starts a
    span (or continues it; a span that ended but was not yet metered is
    reopened, so the gap is not billed either); a beat naming none ends an
    open span at ``now``."""
    if report is not None:
        alloc.fault_code = fault_code(report.get("code")).value
        alloc.fault_summary = scrub_text(str(report.get("summary") or ""))[:SUMMARY_MAX_CHARS]
        if alloc.fault_since is None:
            alloc.fault_since = now
        alloc.fault_until = None
        return
    if alloc.fault_since is not None and alloc.fault_until is None:
        alloc.fault_until = now
    alloc.fault_code = None
    alloc.fault_summary = ""


def record_silence(alloc: ComputeAllocation, *, now: datetime) -> None:
    """A beat lands at ``now``: if the one before it is older than the ready
    window, the box was silent in between, and those minutes are ours exactly
    (the meter leaves them unbilled up to ``now`` while it ticks; this closes
    the part after its last tick). Merged into an ended span not yet metered;
    an open fault span already covers it."""
    last = alloc.last_heartbeat_at
    if last is None or now - last < ready_window():
        return
    if alloc.fault_since is not None and alloc.fault_until is None:
        return
    alloc.fault_since = last if alloc.fault_since is None else min(alloc.fault_since, last)
    alloc.fault_until = now if alloc.fault_until is None else max(alloc.fault_until, now)


def is_unhealthy(alloc: ComputeAllocation) -> bool:
    """Whether the box's last beat said no worker of it can serve."""
    return alloc.fault_code is not None and alloc.fault_until is None


def unservable_since(alloc: ComputeAllocation, *, now: datetime) -> datetime | None:
    """Since when the box has been unable to serve without a break: its open
    fault, or its silence past the ready window, whichever began first.
    ``None`` while it serves."""
    starts = [alloc.fault_since] if is_unhealthy(alloc) and alloc.fault_since else []
    if alloc.lifecycle == WORKSPACE and (silent := silent_since(alloc, now=now)) is not None:
        starts.append(silent)
    return min(starts) if starts else None


def fault_read(alloc: ComputeAllocation, *, staff: bool) -> MachineFaultRead | None:
    """The fault as a reader sees it, or ``None`` while the box serves."""
    if not is_unhealthy(alloc) or alloc.fault_code is None:
        return None
    return MachineFaultRead(
        code=alloc.fault_code,
        message=UNHEALTHY_MESSAGE,
        summary=alloc.fault_summary if staff else "",
        since=alloc.fault_since if staff else None,
    )


def unbilled_span(
    alloc: ComputeAllocation, *, anchor: datetime, now: datetime
) -> tuple[datetime, datetime] | None:
    """The earliest part of ``[anchor, now)`` the box could not serve, or
    ``None``. The meter bills up to it, leaves it, and asks again."""
    spans: list[tuple[datetime, datetime]] = []
    if alloc.fault_since is not None:
        spans.append((alloc.fault_since, alloc.fault_until or now))
    if alloc.lifecycle == WORKSPACE and (silent := silent_since(alloc, now=now)) is not None:
        spans.append((silent, now))
    clipped = [(max(anchor, start), min(now, end)) for start, end in spans]
    open_spans = sorted(span for span in clipped if span[1] > span[0])
    return open_spans[0] if open_spans else None


def settle(alloc: ComputeAllocation) -> None:
    """Forget a span that has ended once the meter has metered past it."""
    metered = alloc.last_metered_at
    if alloc.fault_until is not None and metered is not None and metered >= alloc.fault_until:
        alloc.fault_since = None
        alloc.fault_until = None


def record_isolation(alloc: ComputeAllocation, report: Mapping[str, object] | None) -> None:
    """Keep the isolation a beat reported, restated whole each beat. The
    profile is the one the ``org_isolation`` capability states, never the
    report's own word, so the console cannot disagree with placement."""
    if report is None:
        alloc.isolation_json = None
        return
    raw = report.get("mechanisms")
    names = [str(item) for item in raw] if isinstance(raw, list) else []
    alloc.isolation_json = {"mechanisms": [m.value for m in known_mechanisms(names)]}


def isolation_read(alloc: ComputeAllocation) -> MachineIsolationRead | None:
    """The isolation the box last reported, under the profile placement holds
    it to (its capabilities); ``None`` for a box that reported none."""
    if alloc.isolation_json is None:
        return None
    raw = alloc.isolation_json.get("mechanisms")
    return MachineIsolationRead(
        profile=profile_from_capabilities(alloc.capabilities_json).value,
        mechanisms=[str(item) for item in raw] if isinstance(raw, list) else [],
    )


__all__ = [
    "fault_read",
    "is_unhealthy",
    "isolation_read",
    "record_fault",
    "record_isolation",
    "record_silence",
    "settle",
    "unbilled_span",
    "unservable_since",
]
