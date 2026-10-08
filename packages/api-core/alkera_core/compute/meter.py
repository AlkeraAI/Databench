"""The compute meter's open half: the tick's vocabulary, and a port to the biller.

A worker runs the metering tick every minute (``worker.tasks.compute``). What a
tick does to a machine (verify it is still there, bill the minutes it ran, cut
it off when its funding runs dry, stop it when its grant lapses or its
heartbeat goes quiet) is one transaction per row, and every leg bills to its
own boundary before it stops anything. That work is the biller's: the product
registers it into :data:`COMPUTE_METERING` at composition, and this module only
defines what the tick reads and reports and hands each call to the registrant.

The stop reasons, the order a row records them in, the heartbeat watch and the
tick summary live here because the tick's callers, its machine history and the
platform's own readers name them whether or not anything bills.

With nothing registered, :data:`UNMETERED` answers: machines run unmetered. A
tick checks, bills and stops nothing; a release settles nothing; an org machine
has no funding account, an unknown runway (``ok``) and no spend, so a priced
machine start has nothing to carry its runway and is refused while a free one
starts. A deployment without billing therefore takes machines off the plane
only by an explicit release or power-off, not for silence, a lapsed grant or
exhausted credit.

The point admits one implementation: two meters would bill every minute twice.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Final, Literal, Protocol
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from alkera_core.compute.notices import NoticeSender
from alkera_core.compute.provider import ComputeProvider
from alkera_core.compute.runway import CreditState, credit_state
from alkera_core.config import settings
from alkera_core.extensions import ExtensionError, ExtensionPoint
from alkera_core.models.compute import ComputeAllocation
from alkera_core.models.org_machines import OrgMachine

METER_ACTOR: dict[str, str | None] = {"kind": "system", "email": None, "name": "meter"}
"""Who the machine history names for an edge the meter makes."""

DEFAULT_REASON = "compute"
"""The ledger ``reason`` a compute minute is debited under."""

GRANT_EXPIRED = "grant_expired"
"""Stopped because the grant that admitted the machine lapsed (or is gone)."""

HEARTBEAT_LOST = "heartbeat_lost"
"""Stopped because the daemon on a registered machine went silent past the
ready window — the only way such a machine can be found gone."""

HEARTBEAT_HELD = "heartbeat_held"
"""Skipped: a registered box is silent, but the platform cannot yet vouch that
it was in a position to hear it (see :class:`HeartbeatWatch`). Metered no
further until it beats again or the silence outlasts what the watch allows."""

NO_ELIGIBLE_OPERATOR = "no_eligible_operator"
"""Stopped because nobody with the authority to put the org's chats on this box
stands behind it any more — its operator was demoted, or it was registered
before that took an org admin. Placement, the banner and the machine assertion
all stop seeing such a box the moment the authority goes, so it serves nothing
while it keeps drawing on the org's grant. Rather than bill for compute nobody
can use, the meter bills it up to now, takes it off the plane and stops the
pod; a box that should come back is registered again by an admin.

It is the LAST reason the pass considers — see :data:`STOP_PRECEDENCE` — with
one exception that is the same rule read the other way: a registered box whose
silence is being HELD is stopped for it rather than held, because the hold is
waiting on a beat that could not change the answer."""

PROVIDER_GONE = "provider_gone"
"""Stopped because the provider says the pod it created is not there."""

CREDITS_EXHAUSTED = "credits_exhausted"
"""Stopped because the funding account could not cover the next minute."""

MAX_MINUTES = "max_minutes"
"""Stopped because a session lease reached its ceiling."""

#: Which stop a row records when a tick satisfies several at once — a draining
#: box whose grant lapsed, whose account ran dry and whose operator was demoted.
#: Earlier entry wins.
#:
#: The rule that makes this order true is TEMPORAL, not a ranking of severity:
#: every leg bills to its OWN boundary and records the event at that boundary,
#: so the stop that happened FIRST is the one that stands. An account that ran
#: dry two minutes in outranks a grant that lapsed at four and a silence that
#: began at five, because the machine was already stopped when those arrived —
#: which is what the grant leg and :func:`_stop_silent` each say in their own
#: words ("a cutoff inside that window is the stop that stands"). The two ends
#: are the exceptions that prove it: ``provider_gone`` is first because a pod
#: confirmed gone has no knowable death time to bill up to, so nothing may be
#: billed after it at all; ``no_eligible_operator`` is last because it is not a
#: moment the machine passed through — it says only that nobody may use the box
#: any more, so it is asked after the money is settled.
#:
#: Two pairs in here can never coincide, and are ordered only so the tuple is
#: total: ``provider_gone`` and ``heartbeat_lost`` are mutually exclusive by
#: origin (the plane asks the provider about a pod it created, and the daemon's
#: heartbeat about one it did not), and ``max_minutes`` is a session-lease stop
#: from which a workspace box is exempt, so it never meets
#: ``no_eligible_operator``.
#:
#: Every pair that CAN coincide is pinned through a real meter pass in
#: the backend's draining meter tests — the winner and the
#: minutes — and those cases read this tuple, so reordering it reds them.
STOP_PRECEDENCE: tuple[str, ...] = (
    PROVIDER_GONE,
    CREDITS_EXHAUSTED,
    HEARTBEAT_LOST,
    GRANT_EXPIRED,
    MAX_MINUTES,
    NO_ELIGIBLE_OPERATOR,
)


@dataclass(frozen=True)
class SilenceFloor:
    """What the platform can vouch for at one tick, when judging a silent box.

    A box is judged silent against the ready window, measured NOT from its last
    heartbeat alone but from the latest of: its last heartbeat, the moment this
    meter has been running every tick since (``observing_since``), and the last
    tick at which no box at all had been heard within the window
    (``unheard_at`` — up to then, nothing proved the API was taking
    heartbeats). While the fleet is ``dark`` — nobody heard within the window
    right now — a dead fleet cannot be told from a deaf platform, so the box is
    held until the silence outlasts the reap ceiling instead.
    """

    observing_since: datetime
    unheard_at: datetime | None
    dark: bool

    def reaps(
        self, last_heard: datetime, now: datetime, *, window: timedelta, ceiling: timedelta
    ) -> bool:
        """Whether a box last heard at ``last_heard`` may be reaped at ``now``."""
        if self.dark:
            return now - max(last_heard, self.observing_since) >= ceiling
        anchors = [last_heard, self.observing_since]
        if self.unheard_at is not None:
            anchors.append(self.unheard_at)
        return now - max(anchors) >= window


class HeartbeatWatch:
    """The meter's own memory of when it was in a position to hear a box.

    A registered machine's only liveness is the heartbeat its daemon posts, and
    during a SERVER outage — the database down, the API down, a proxy in front
    of it answering every box 5xx — every box is silent at once. A meter that
    read that silence as the boxes' own reaped the whole fleet on its first
    tick back, and every chat bound to those boxes lost its machine for nothing
    the box did. So the watch records two things a tick can vouch for: that the
    meter itself has been running every tick (a failed tick or a gap longer
    than ``max_gap`` restarts ``observing_since``, which is what gives every
    box a fresh window after the worker or its database come back), and the
    last tick at which nobody in the fleet had been heard inside the window
    (nothing proved the API was ingesting heartbeats up to then). Process
    memory is the right store: the question is about THIS meter's run.
    """

    def __init__(self, *, max_gap: timedelta) -> None:
        self._max_gap = max_gap
        self._previous: datetime | None = None
        self._observing_since: datetime | None = None
        self._unheard_at: datetime | None = None
        self._broken = True

    def begin(
        self, now: datetime, *, fleet_heard_at: datetime | None, window: timedelta
    ) -> SilenceFloor:
        """Open a tick: the floor every silent box is judged against on it.
        ``fleet_heard_at`` is the newest heartbeat of any live registered box."""
        if (
            self._broken
            or self._observing_since is None
            or self._previous is None
            or now - self._previous > self._max_gap
        ):
            self._observing_since = now
        # Until ``finish`` says otherwise this tick did not complete.
        self._broken = True
        dark = fleet_heard_at is None or now - fleet_heard_at >= window
        floor = SilenceFloor(
            observing_since=self._observing_since, unheard_at=self._unheard_at, dark=dark
        )
        if dark:
            self._unheard_at = now
        return floor

    def finish(self, now: datetime) -> None:
        """The tick that began at ``now`` ran to the end."""
        self._previous = now
        self._broken = False

    def broke(self) -> None:
        """A tick did not run at all (the database refused it before it
        began): the next tick starts the observation over."""
        self._broken = True


#: A credit cutoff and a lapsed grant are refusals the UI must surface; every


@dataclass
class MeterSummary:
    """Per-tick outcome counts (for the task log / tests)."""

    checked: int = 0
    metered: int = 0
    terminated: int = 0
    skipped: int = 0
    #: Rows skipped because the PROVIDER could not be reached (an unset or
    #: rejected API key answers every poll this way). Counted apart from the
    #: ordinary skips so a tick that metered nothing because the money path is
    #: unconfigured is legible at a glance instead of only per row.
    provider_errors: int = 0
    reasons: list[str] = field(default_factory=list)


RunwayRefusal = Literal["team_budget", "insufficient"]
"""Why a start runway could not be carried: the owner team's budget is short,
or no funding class can hold the whole runway."""


class ComputeMetering(Protocol):
    """What the open platform asks of the compute biller."""

    async def meter_and_cutoff(
        self,
        db: AsyncSession,
        *,
        provider: ComputeProvider,
        now: datetime | None,
        max_minutes: int | None,
        reason: str,
        watch: HeartbeatWatch | None,
        send_notice: NoticeSender | None,
    ) -> MeterSummary:
        """Verify, bill and cut off every live allocation in one pass. Commits."""
        ...

    async def settle_final(
        self,
        db: AsyncSession,
        alloc: ComputeAllocation,
        *,
        now: datetime,
        max_minutes: int | None,
        reason: str,
    ) -> None:
        """Bill the interval a release between ticks would otherwise forfeit."""
        ...

    async def machine_funding_account(
        self, db: AsyncSession, *, org_id: UUID, owner_team_id: UUID
    ) -> UUID | None:
        """The account an org machine owned by ``owner_team_id`` debits."""
        ...

    async def machine_runway(
        self, db: AsyncSession, om: OrgMachine
    ) -> tuple[int | None, CreditState]:
        """How long the credit behind ``om`` lasts, and the state it reads as."""
        ...

    async def carry_start_runway(
        self,
        db: AsyncSession,
        *,
        account_id: UUID,
        owner_team_id: UUID,
        org_id: UUID,
        need_nanos: int,
        request_id: str,
        now: datetime,
    ) -> RunwayRefusal | None:
        """Whether ``account_id`` can carry ``need_nanos`` of start runway for
        a machine of ``owner_team_id``: ``None`` when it can, else why not. A
        check, not a hold: nothing stays reserved afterwards."""
        ...

    async def spend_this_cycle(
        self,
        db: AsyncSession,
        *,
        org_id: UUID,
        machine_ids: Sequence[UUID],
        now: datetime | None,
    ) -> dict[UUID, int]:
        """What each org machine has been billed since the org's cycle began."""
        ...

    def add_credit_url(self, org_id: UUID) -> str | None:
        """Where a person adds the credit a machine of ``org_id`` draws on."""
        ...

    def notice_sender(self) -> NoticeSender | None:
        """How the biller tells a machine's managers about its credit (the
        sender a tick is handed), or ``None`` when it has nothing to tell."""
        ...


class UnmeteredCompute:
    """The answers of a deployment with no compute biller registered."""

    async def meter_and_cutoff(
        self,
        db: AsyncSession,
        *,
        provider: ComputeProvider,
        now: datetime | None,
        max_minutes: int | None,
        reason: str,
        watch: HeartbeatWatch | None,
        send_notice: NoticeSender | None,
    ) -> MeterSummary:
        return MeterSummary()

    async def settle_final(
        self,
        db: AsyncSession,
        alloc: ComputeAllocation,
        *,
        now: datetime,
        max_minutes: int | None,
        reason: str,
    ) -> None:
        return None

    async def machine_funding_account(
        self, db: AsyncSession, *, org_id: UUID, owner_team_id: UUID
    ) -> UUID | None:
        return None

    async def machine_runway(
        self, db: AsyncSession, om: OrgMachine
    ) -> tuple[int | None, CreditState]:
        return None, credit_state(None, settings)

    async def carry_start_runway(
        self,
        db: AsyncSession,
        *,
        account_id: UUID,
        owner_team_id: UUID,
        org_id: UUID,
        need_nanos: int,
        request_id: str,
        now: datetime,
    ) -> RunwayRefusal | None:
        return "insufficient"

    async def spend_this_cycle(
        self,
        db: AsyncSession,
        *,
        org_id: UUID,
        machine_ids: Sequence[UUID],
        now: datetime | None,
    ) -> dict[UUID, int]:
        return dict.fromkeys(machine_ids, 0)

    def add_credit_url(self, org_id: UUID) -> str | None:
        return None

    def notice_sender(self) -> NoticeSender | None:
        return None


UNMETERED: Final[ComputeMetering] = UnmeteredCompute()

#: The compute biller the platform hands the tick to. At most one registers.
COMPUTE_METERING: ExtensionPoint[ComputeMetering] = ExtensionPoint("compute_metering")


def compute_metering(
    point: ExtensionPoint[ComputeMetering] = COMPUTE_METERING,
) -> ComputeMetering:
    """The biller registered on ``point``, or :data:`UNMETERED`. Freezes the
    point. Callers pass nothing; a test passes a point of its own so it never
    freezes the process-wide one before a composition root installs into it."""
    registered = point.items()
    if len(registered) > 1:
        raise ExtensionError(
            f"{len(registered)} compute meters registered on {point.name!r}; "
            "the tick takes exactly one"
        )
    return registered[0] if registered else UNMETERED


def machines_are_priced(point: ExtensionPoint[ComputeMetering] | None = None) -> bool:
    """Whether a biller charges for machines here, so a buyer is shown what
    one costs. With none registered machines run unmetered and a price would
    name a charge nobody makes. Reads the process-wide point unless a test
    passes its own."""
    return bool((point if point is not None else COMPUTE_METERING).items())


async def meter_and_cutoff(
    db: AsyncSession,
    *,
    provider: ComputeProvider,
    now: datetime | None = None,
    max_minutes: int | None = None,
    reason: str = DEFAULT_REASON,
    watch: HeartbeatWatch | None = None,
    send_notice: NoticeSender | None = None,
) -> MeterSummary:
    """Run one metering tick through the registered biller. Commits.

    ``provider`` is injected (a real client in the task, a fake in tests);
    ``now`` lets a test drive accrual across minute boundaries. ``max_minutes``
    is an optional fleet-wide session lease fallback; ``reason`` names the
    ledger rows the pass writes. ``watch`` is the meter's memory of when it
    could hear a box (:class:`HeartbeatWatch`). ``send_notice`` delivers what
    the pass has to tell machine managers; without one the notices are
    dropped."""
    return await compute_metering().meter_and_cutoff(
        db,
        provider=provider,
        now=now,
        max_minutes=max_minutes,
        reason=reason,
        watch=watch,
        send_notice=send_notice,
    )


async def settle_final(
    db: AsyncSession,
    alloc: ComputeAllocation,
    *,
    now: datetime,
    max_minutes: int | None = None,
    reason: str = DEFAULT_REASON,
) -> None:
    """Bill the running interval a user-initiated release would otherwise
    forfeit, through the registered biller. Called by the release path before
    the row leaves the meterable set."""
    await compute_metering().settle_final(
        db, alloc, now=now, max_minutes=max_minutes, reason=reason
    )


async def machine_runway(db: AsyncSession, om: OrgMachine) -> tuple[int | None, CreditState]:
    """The runway of the account funding ``om`` and the credit state ``om``
    reads as: what the machine page shows, and what the meter acts on."""
    return await compute_metering().machine_runway(db, om)


__all__ = [
    "COMPUTE_METERING",
    "CREDITS_EXHAUSTED",
    "DEFAULT_REASON",
    "GRANT_EXPIRED",
    "HEARTBEAT_HELD",
    "HEARTBEAT_LOST",
    "MAX_MINUTES",
    "METER_ACTOR",
    "NO_ELIGIBLE_OPERATOR",
    "PROVIDER_GONE",
    "STOP_PRECEDENCE",
    "UNMETERED",
    "ComputeMetering",
    "HeartbeatWatch",
    "MeterSummary",
    "RunwayRefusal",
    "SilenceFloor",
    "UnmeteredCompute",
    "compute_metering",
    "machine_runway",
    "machines_are_priced",
    "meter_and_cutoff",
    "settle_final",
]
