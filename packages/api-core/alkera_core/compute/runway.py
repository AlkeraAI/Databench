"""How long an org's machines can keep running on the credit they have.

Pure: no database, no clock. The meter feeds it the two figures it reads once
per tick for a funding account (the credit left in the classes that may fund a
machine, and the per-minute burn of every machine running on that account,
compute plus storage) and acts on the answer; the read model computes the same
answer for a person looking at the machine, so the warning the meter emails
and the state the page shows can never disagree.

A credit state is a coarse reading of the runway against three thresholds
(settings): ``low`` once the runway is inside ``machine_credit_low_hours``,
``urgent`` inside ``machine_credit_urgent_minutes``, and the drain itself,
which starts at ``machine_credit_drain_minutes``. ``draining`` and ``stopped``
are not readings of the runway: they say the meter already acted (a credit
drain is under way, or the machine sleeps for lack of credit), and they
outrank whatever the runway reads, because a stopped machine burns nothing
and would otherwise read ``ok``.
"""

from __future__ import annotations

from typing import Protocol

from alkera_core.schemas.org_machines import CreditState

CREDIT_STATES: tuple[CreditState, ...] = ("ok", "low", "urgent", "draining", "stopped")

#: The states in which a funding account's machines are in a low-credit
#: episode: the one that starts when the state leaves ``ok``.
WARNING_STATES: frozenset[CreditState] = frozenset({"low", "urgent", "draining", "stopped"})


class RunwaySettings(Protocol):
    """The thresholds a credit state is read against."""

    @property
    def machine_credit_low_hours(self) -> int: ...

    @property
    def machine_credit_urgent_minutes(self) -> int: ...

    @property
    def machine_credit_drain_minutes(self) -> int: ...


def runway_minutes(available_nanos: int, burn_per_minute_nanos: int) -> int | None:
    """Whole minutes ``available_nanos`` pays for at ``burn_per_minute_nanos``.

    ``None`` when nothing burns (no running machine, or only free ones): an
    account that spends nothing has no runway to run out of. A balance already
    below zero (storage may overdraw it) has no runway left at all."""
    if burn_per_minute_nanos <= 0:
        return None
    if available_nanos <= 0:
        return 0
    return available_nanos // burn_per_minute_nanos


def credit_state(
    runway: int | None,
    settings: RunwaySettings,
    *,
    draining: bool = False,
    stopped: bool = False,
) -> CreditState:
    """What ``runway`` minutes read as, unless the meter already acted.

    Each threshold is inclusive: a runway of exactly
    ``machine_credit_urgent_minutes`` is urgent, one minute more is low."""
    if stopped:
        return "stopped"
    if draining:
        return "draining"
    if runway is None:
        return "ok"
    if runway <= settings.machine_credit_urgent_minutes:
        return "urgent"
    if runway <= settings.machine_credit_low_hours * 60:
        return "low"
    return "ok"


def drain_due(runway: int | None, settings: RunwaySettings) -> bool:
    """Whether a runway is inside the drain window: the moment the meter sets
    the window's cost aside and starts stopping the machines."""
    return runway is not None and runway <= settings.machine_credit_drain_minutes


__all__ = [
    "CREDIT_STATES",
    "WARNING_STATES",
    "CreditState",
    "RunwaySettings",
    "credit_state",
    "drain_due",
    "runway_minutes",
]
