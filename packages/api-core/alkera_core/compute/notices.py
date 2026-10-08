"""What the compute meter has to tell a machine's managers, as plain data.

The meter decides that a notice is due; it does not send it. Sending mail pulls
in the email stack (Jinja templates read as package data), which a box binary
does not carry, so the meter stays importable on a box and the process that
runs it (the worker) hands it a :data:`NoticeSender` that delivers each notice.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from alkera_core.extensions import ExtensionError, ExtensionPoint


@dataclass(frozen=True)
class _Notice:
    #: Claimed once in ``billing_email_log``: a redelivered notice never re-sends.
    dedupe_key: str
    recipients: tuple[str, ...]
    org_team_id: UUID | None


@dataclass(frozen=True)
class MachineStoppedForCredits(_Notice):
    machine_name: str
    deletion_at: datetime
    email_type = "machine_stopped_credits"


@dataclass(frozen=True)
class MachineDeletionScheduled(_Notice):
    machine_name: str
    deletion_at: datetime
    email_type = "machine_deletion_scheduled"


@dataclass(frozen=True)
class MachineCreditLow(_Notice):
    machine_names: tuple[str, ...]
    runway_minutes: int
    urgent: bool
    email_type = "machine_credit_low"


MachineNotice = MachineStoppedForCredits | MachineDeletionScheduled | MachineCreditLow

#: Delivers one notice at most once. Called after the rows the notice describes
#: have committed, with a session that holds no pending effect.
NoticeSender = Callable[[AsyncSession, MachineNotice], Awaitable[None]]


#: How notices are delivered, registered by the process that sends mail (the
#: worker installs the email sender). At most one is registered.
NOTICE_SENDERS: ExtensionPoint[NoticeSender] = ExtensionPoint("machine_notice_senders")


def notice_sender(point: ExtensionPoint[NoticeSender] = NOTICE_SENDERS) -> NoticeSender | None:
    """The registered sender, or ``None``: a process with none drops notices."""
    registered = point.items()
    if len(registered) > 1:
        raise ExtensionError("more than one extension delivers machine notices")
    return registered[0] if registered else None


__all__ = [
    "NOTICE_SENDERS",
    "MachineCreditLow",
    "MachineDeletionScheduled",
    "MachineNotice",
    "MachineStoppedForCredits",
    "NoticeSender",
    "notice_sender",
]
