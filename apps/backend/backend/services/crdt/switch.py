"""Whether live editing is on for an org: the deployment's switch, and the org's own.

Live editing is a file opened as a co-edited document whose edits are saved as
they are typed, and a box merging its agent's edits into that document as a
text peer. It covers the document types that rest in a source (a file); a
type that rests nowhere (the chat's shared draft) is never switched here.

The deployment's ``LIVE_EDITING_ENABLED`` is the default; an org's
``org_settings.live_editing_enabled`` (set by platform admins) wins over it in
either direction. While it is off for an org:

* a subscribe or a ``hello`` on one of its files is refused ``crdt_unsupported``
  with :data:`LIVE_EDITING_OFF`, and a tab already holding one is refused on its
  socket's next tick (every tick decides each held document again), which
  drops its hold and writes the session back as any writer leaving does;
* a box's text-peer read or submit answers ``live: false``, so the box writes
  the file the ordinary way;
* nothing that saves is stopped: an update a tab already holding the document
  sends before its tick is still taken (its typing is the person's), write
  backs run, and the unsaved sweep keeps writing back what is left.

Turning it off also writes back, at once, every session of the org holding
edits not yet on the drive (:func:`write_back_org`). Turning it back on needs
nothing: the next subscribe opens the session where it stands.

Each replica caches an org's answer for :data:`SWITCH_CACHE_SECONDS`, so a
flip reaches every replica's sockets within that and one tick.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final

from alkera_core.config import settings
from alkera_core.logging import get_logger
from alkera_core.models import OrgSettings
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services.crdt import sweeper
from backend.services.crdt.errors import safe_error
from backend.services.crdt.registry import CrdtDocType
from backend.services.infra import now as _now

if TYPE_CHECKING:
    from backend.services.crdt.docs import CrdtDocs

log = get_logger(__name__)

#: How long a replica keeps an org's answer before reading it again.
SWITCH_CACHE_SECONDS: Final = 5.0
#: The reason a refusal names while live editing is off.
LIVE_EDITING_OFF: Final = "live_editing_off"
#: The message a refused subscribe or hello carries.
OFF_MESSAGE: Final = "live editing is off"
#: How long turning live editing off waits for the org's sessions to be
#: written back before answering; what is left is the unsaved sweep's.
WRITE_BACK_SECONDS: Final = 10.0
#: The most sessions one switch-off writes back itself.
WRITE_BACK_LIMIT: Final = 200


def resolve_live_editing(deployment: bool, override: bool | None) -> bool:
    """The answer for an org: its own setting when it has one, else the
    deployment's."""
    return deployment if override is None else override


def covers(doc_type: CrdtDocType) -> bool:
    """Whether the switch decides ``doc_type``: the types that rest in a
    source (a file). Others are always served."""
    return doc_type.source is not None


async def live_editing_override(db: AsyncSession, org_id: uuid.UUID) -> bool | None:
    """The org's own setting (``None``: it follows the deployment)."""
    value: bool | None = await db.scalar(
        select(OrgSettings.live_editing_enabled).where(OrgSettings.org_team_id == org_id)
    )
    return value


def _deployment_default() -> bool:
    return bool(settings.live_editing_enabled)


@dataclass
class LiveSwitch:
    """One replica's reading of the switch, per org. Every collaborator is
    injected: the session factory, the deployment's default, the clock."""

    session_factory: Callable[[], AsyncSession]
    deployment: Callable[[], bool] = _deployment_default
    clock: Callable[[], float] = time.monotonic
    ttl: float = SWITCH_CACHE_SECONDS
    _cache: dict[uuid.UUID, tuple[float, bool | None]] = field(default_factory=dict, init=False)

    async def on(self, org_id: uuid.UUID) -> bool:
        """Whether live editing is on for ``org_id`` now (cached briefly)."""
        now = self.clock()
        cached = self._cache.get(org_id)
        if cached is not None and now - cached[0] < self.ttl:
            override = cached[1]
        else:
            async with self.session_factory() as db:
                override = await live_editing_override(db, org_id)
                await db.commit()
            self._cache[org_id] = (now, override)
        return resolve_live_editing(self.deployment(), override)

    async def serves(self, org_id: uuid.UUID, doc_type: CrdtDocType) -> bool:
        """Whether a document of ``doc_type`` in ``org_id`` may be opened live."""
        return not covers(doc_type) or await self.on(org_id)

    def forget(self, org_id: uuid.UUID) -> None:
        """Read ``org_id`` afresh next time (this replica just changed it)."""
        self._cache.pop(org_id, None)


@dataclass(frozen=True, slots=True)
class OrgWriteBack:
    """What writing back an org's sessions did: how many landed (or were the
    drive's already), and how many the unsaved sweep is left to save."""

    written: int
    left: int


async def write_back_org(docs: CrdtDocs, org_id: uuid.UUID) -> OrgWriteBack:
    """Write back now every session of ``org_id`` holding edits not on the
    drive: what live editing being turned off owes the people who typed them.

    The sessions are claimed as the unsaved sweep claims them (a replica's
    sweep skips what this takes), each is written back in a transaction of
    its own, and the whole pass is bounded by :data:`WRITE_BACK_SECONDS`. A
    write back that is refused parks its session (saying so to its readers);
    one not reached in time is retried by the sweep once its claim lapses.
    Nothing is closed or restarted here, so no edit can be lost by it. Never
    raises."""
    written = 0
    try:
        async with asyncio.timeout(WRITE_BACK_SECONDS):
            found = await sweeper.unsaved(
                docs.session_factory,
                docs.registry,
                limit=WRITE_BACK_LIMIT,
                now=_now(),
                orgs=frozenset({org_id}),
            )
            for ref in found:
                async with docs.session_factory() as db:
                    settled = await docs.sessions.write_back(db, ref)
                    await db.commit()
                if settled.outcome in ("written", "unchanged"):
                    written += 1
    except Exception as exc:  # the sweep writes back whatever this did not
        log.warning("crdt.live_editing.write_back_failed", org=str(org_id), error=safe_error(exc))
    async with docs.session_factory() as db:
        left = await sweeper.count_unsaved(db, now=_now(), orgs=frozenset({org_id}))
        await db.commit()
    if left:
        log.warning("crdt.live_editing.write_back_left_unsaved", org=str(org_id), docs=left)
    return OrgWriteBack(written=written, left=left)


__all__ = [
    "LIVE_EDITING_OFF",
    "OFF_MESSAGE",
    "SWITCH_CACHE_SECONDS",
    "WRITE_BACK_LIMIT",
    "WRITE_BACK_SECONDS",
    "LiveSwitch",
    "OrgWriteBack",
    "covers",
    "live_editing_override",
    "resolve_live_editing",
    "write_back_org",
]
