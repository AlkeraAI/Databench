"""Whether a request's agent assertion is a registered machine speaking, for
the layers that must decide before any route runs.

The agent assertion is two headers any member can put on their own session.
The request throttles and the realtime connection caps give a machine a budget
of its own so a busy box never spends its owner's, and so they must not take
the header's word for it: a member writing a fresh id on every request would
otherwise be a fresh budget every time. They ask here instead, and get the
asserted id back only when :func:`machine_standing` says the machine SPEAKS —
a live workspace machine of the caller's org, registered by the caller, on the
very credential (``jti``) the request carries. Everything else — no assertion,
a malformed one, a name that is no machine, somebody else's machine, a box the
platform took away — is ``None``, and the caller is counted as themselves.

These layers run before the route opens its session, so the answer is cached
per (machine, user, org, credential): one primary-key read per box credential
every :data:`POSITIVE_TTL_SECONDS`, and a "not the machine" answer is kept for
:data:`NEGATIVE_TTL_SECONDS`. A caller rotating made-up ids misses the cache
every time, so the reads a principal can cause are metered too: past
:data:`LOOKUPS_PER_MINUTE` misses in a minute the assertion is simply not
verified (no read) until the window turns. The cache only ever decides which
budget a request is counted on; the routes still verify for themselves.
"""

from __future__ import annotations

import time
import uuid
from collections import OrderedDict
from collections.abc import Callable, Mapping
from typing import Final

import structlog
from alkera_core.authz.headers import AgentHeaderError, parse_agent_assertion
from alkera_core.compute.machines import MachineStanding, machine_standing
from alkera_core.db.session import AsyncSessionLocal
from sqlalchemy.ext.asyncio import AsyncSession

log = structlog.get_logger(__name__)

#: How long a verified machine is trusted before it is read again. A revoked
#: box keeps its own budget at most this long; its routes refuse it at once.
POSITIVE_TTL_SECONDS: Final = 30.0
#: How long "not the machine" is kept. Short, so a box that registers right
#: after its first call is on its own budget within seconds.
NEGATIVE_TTL_SECONDS: Final = 5.0
#: Cache misses one principal may turn into reads per minute. A box costs one
#: miss per credential per :data:`POSITIVE_TTL_SECONDS`, so this is room for
#: dozens of boxes behind one person, and no room for a rotation spray.
LOOKUPS_PER_MINUTE: Final = 120
_MAX_ENTRIES: Final = 8192

_Key = tuple[str, str, str, str]


class MachineAssertionCache:
    """Bounded, TTL'd answers to "is this assertion the machine speaking?".

    ``session_factory`` and ``clock`` are the seams a test pins.
    """

    def __init__(
        self,
        *,
        session_factory: Callable[[], AsyncSession] = AsyncSessionLocal,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock
        self._answers: OrderedDict[_Key, tuple[bool, float]] = OrderedDict()
        self._lookups: OrderedDict[str, tuple[float, int]] = OrderedDict()

    async def speaks(
        self, *, machine_id: str, user_id: str, org_id: str, credential_id: str
    ) -> bool:
        key: _Key = (machine_id, user_id, org_id, credential_id)
        now = self._clock()
        cached = self._answers.get(key)
        if cached is not None:
            verified, at = cached
            ttl = POSITIVE_TTL_SECONDS if verified else NEGATIVE_TTL_SECONDS
            if now - at < ttl:
                self._answers.move_to_end(key)
                return verified
        if not self._may_look_up(user_id, now):
            return False
        try:
            async with self._session_factory() as db:
                standing = await machine_standing(
                    db,
                    machine_id=machine_id,
                    org_id=uuid.UUID(org_id),
                    operator_user_id=uuid.UUID(user_id),
                    credential_id=credential_id,
                )
        except Exception:
            # Unreachable store: the caller is counted as themselves, which is
            # the budget they would have without the header. Nothing is cached.
            log.warning("ratelimit.machine_lookup_failed", exc_info=True)
            return False
        verified = standing is MachineStanding.SPEAKS
        self._answers[key] = (verified, now)
        self._answers.move_to_end(key)
        while len(self._answers) > _MAX_ENTRIES:
            self._answers.popitem(last=False)
        return verified

    def _may_look_up(self, principal: str, now: float) -> bool:
        started, count = self._lookups.get(principal, (now, 0))
        if now - started >= 60.0:
            started, count = now, 0
        if count >= LOOKUPS_PER_MINUTE:
            return False
        self._lookups[principal] = (started, count + 1)
        self._lookups.move_to_end(principal)
        while len(self._lookups) > _MAX_ENTRIES:
            self._lookups.popitem(last=False)
        return True

    def reset(self) -> None:
        self._answers.clear()
        self._lookups.clear()


CACHE: Final = MachineAssertionCache()


async def verified_machine_id(
    headers: Mapping[str, str],
    *,
    user_id: object,
    org_id: object,
    credential_id: str | None,
    cache: MachineAssertionCache | None = None,
) -> str | None:
    """The machine id the request asserts when that machine is the one
    speaking; ``None`` otherwise (no assertion, a malformed pair, no revocable
    credential, a name that is not a machine id, or a machine that does not
    verify for this user, org and credential)."""
    try:
        assertion = parse_agent_assertion(headers)
    except AgentHeaderError:
        return None
    if assertion is None or not credential_id:
        return None
    try:
        machine = str(uuid.UUID(assertion.session_id))
    except ValueError:
        return None
    speaks = await (cache or CACHE).speaks(
        machine_id=machine,
        user_id=str(user_id),
        org_id=str(org_id),
        credential_id=credential_id,
    )
    return assertion.session_id if speaks else None


__all__ = [
    "CACHE",
    "LOOKUPS_PER_MINUTE",
    "NEGATIVE_TTL_SECONDS",
    "POSITIVE_TTL_SECONDS",
    "MachineAssertionCache",
    "verified_machine_id",
]
