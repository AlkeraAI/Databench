"""Handing an org worker its credential.

The supervisor mints each worker a credential bound to (this machine, that
org) that lives minutes, and hands it a fresh one before the last runs out.
A hand-off is sent again until the worker's status names it as taken: a
frame the worker never read would otherwise strand it on an expired bearer
until the next mint, refused on every call meanwhile. A worker whose status
says its credential was refused as expired is minted a fresh one at once
rather than at that mint.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Final, Protocol

from alkera_cli.org_worker_protocol import Credential, Status
from alkera_cli.supervisor.http import ApiError
from alkera_cli.supervisor.slots import Slot

logger = logging.getLogger(__name__)

#: A worker's credential is replaced once this share of its life has passed.
CREDENTIAL_REFRESH_AT: Final = 2 / 3
#: How long a hand-off waits for its acknowledgement before it is sent again.
RESEND_SECONDS: Final = 5.0
#: How long a mint the backend refused waits before it is tried again.
MINT_RETRY_SECONDS: Final = 30.0


@dataclass(slots=True)
class CredentialHandoff:
    """One worker's credential: when it is next minted, and the hand-off
    still waiting for the worker to say it has it."""

    #: When a fresh credential is next minted.
    refresh_at: float = float("inf")
    #: The number of the last hand-off.
    seq: int = 0
    #: The hand-off the worker has not named as taken yet.
    pending: Credential | None = None
    resend_at: float = float("inf")
    #: A mint is on the wire: a status asking for one now waits for it.
    minting: bool = False

    def started(self, lives: float, now: float) -> None:
        """The worker started on a credential that lives ``lives`` seconds."""
        self.refresh_at = now + lives * CREDENTIAL_REFRESH_AT

    def minted(self, credential: str, lives: float, now: float) -> Credential:
        """The frame that hands over ``credential``, pending until taken."""
        self.seq += 1
        self.pending = Credential(credential=credential, seq=self.seq)
        self.resend_at = now + RESEND_SECONDS
        self.started(lives, now)
        return self.pending

    def mint_failed(self, now: float) -> None:
        self.refresh_at = now + MINT_RETRY_SECONDS

    def resend(self, now: float) -> Credential | None:
        """The pending hand-off when its acknowledgement is overdue."""
        if self.pending is None or now < self.resend_at:
            return None
        self.resend_at = now + RESEND_SECONDS
        return self.pending

    def reported(self, status: Status, *, now: float) -> bool:
        """Read what the worker says it holds. A hand-off it names is taken;
        ``True`` when the newest one it holds was refused as expired and a
        fresh one is to be minted now."""
        if self.pending is not None and status.credential_seq >= self.pending.seq:
            self.pending = None
            self.resend_at = float("inf")
        refused = status.credential_refused and status.credential_seq == self.seq
        if refused and self.pending is None and not self.minting:
            self.refresh_at = min(self.refresh_at, now)
            return True
        return False


class _Worker(Protocol):
    """What a hand-off needs of a running worker."""

    slot: Slot
    stopping: bool
    credential: CredentialHandoff

    @property
    def send(self) -> Callable[[Credential], Awaitable[None]]: ...


class _Minter(Protocol):
    async def worker_credential(self, org_id: str) -> tuple[str, float]: ...


async def hand_out(
    workers: Mapping[str, _Worker],
    api: _Minter,
    *,
    now: float,
    drain: bool = False,
) -> None:
    """Hand each worker a fresh credential before its own runs out, and send a
    hand-off again that its status has not named as taken in time. A stopping
    worker is skipped unless the box is draining (``drain``)."""
    for org_id, worker in workers.items():
        handoff = worker.credential
        if (worker.stopping and not drain) or handoff.minting:
            continue
        if now < handoff.refresh_at:
            if (again := handoff.resend(now)) is not None:
                await worker.send(again)
            continue
        # Claimed before the mint is awaited: a worker asking at the moment
        # its refresh falls due is minted one credential, not two.
        handoff.minting = True
        try:
            credential, lives = await api.worker_credential(org_id)
        except (ApiError, OSError, ValueError) as exc:
            logger.warning("could not refresh org slot %d's credential: %s", worker.slot.index, exc)
            handoff.mint_failed(now)
            continue
        finally:
            handoff.minting = False
        await worker.send(handoff.minted(credential, lives, now))


__all__ = [
    "CREDENTIAL_REFRESH_AT",
    "MINT_RETRY_SECONDS",
    "RESEND_SECONDS",
    "CredentialHandoff",
    "hand_out",
]
