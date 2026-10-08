"""The trailing edge of the doc-sync announcement window.

Every accepted operation is rebroadcast as ``doc.op`` to the people with the
document open; the domain event is for everyone else, and it is coalesced to at
most one per document per window. The leading edge alone was not enough: a
typist who stopped INSIDE a window announced nothing at all, so a live title or
body edit never reached the knowledge catalogue (and a chat's last append never
reached the rail) until something else rang.

Pinned here: the last change of a burst is announced once the window elapses,
the leading edge still rings immediately and cancels the trailing one it
supersedes, an operation that changes nothing arms nothing, and a teardown
announces nothing at all.
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID, uuid4

import pytest
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.events import EventType, actor_for_user
from alkera_core.models import EventOutbox, User, WorkspaceObject
from alkera_core.schemas.realtime import DocEnvelope, OpPayload
from backend.services.chats import chat_service
from backend.services.realtime import channels as channel_service
from backend.services.realtime.channels import Channel, ChannelGrant
from backend.services.realtime.docsync import (
    DOMAIN_ANNOUNCE_INTERVAL_SECONDS,
    DocRegistry,
)
from backend.services.realtime.filters import EntitlementSnapshot, load_entitlements
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from tests.conftest import OrgWithAdmin


class _Gate:
    """A sleep the test decides the length of.

    The registry's trailing timer is a real task on a real loop, so the window
    cannot be faked with a frozen clock alone: the delay is recorded (that is
    the contract — it is the REMAINDER of the open window, not a fresh one) and
    the task is held until the test lets it run."""

    def __init__(self) -> None:
        self.delays: list[float] = []
        self._open = asyncio.Event()

    async def sleep(self, delay: float) -> None:
        self.delays.append(delay)
        await self._open.wait()

    def elapse(self) -> None:
        self._open.set()


def _op(intent: str, **kwargs: Any) -> OpPayload:
    return OpPayload(op_id=kwargs.pop("op_id", f"op-{uuid4().hex[:8]}"), intent=intent, **kwargs)  # type: ignore[arg-type]


def _event(index: int) -> dict[str, Any]:
    return {"event_id": f"e{index}", "event_type": "message.completed"}


def _envelope(channel: Channel, *, peer_id: str, op: OpPayload) -> DocEnvelope:
    return DocEnvelope(
        doc_id=channel.doc_id,
        doc_type=channel.doc_type,
        epoch=1,
        peer_id=peer_id,
        seq=0,
        kind="op",
        payload=op.model_dump(mode="json"),
    )


async def _admin(db: AsyncSession, org: OrgWithAdmin) -> User:
    user = await db.get(User, org.admin_id)
    assert user is not None
    return user


async def _ent(db: AsyncSession, user: User) -> EntitlementSnapshot:
    return await load_entitlements(db, user, org_id=user.home_org_team_id)


async def _grant_for(db: AsyncSession, user: User, channel: Channel) -> ChannelGrant:
    return await channel_service.authorize(
        db, user, channel, ent=await load_entitlements(db, user, org_id=user.home_org_team_id)
    )


async def _chat_object(db: AsyncSession, owner: User) -> WorkspaceObject:
    chat, _ = await chat_service.create_chat(
        db,
        owner=owner,
        title="Ops",
        client_id=None,
        machine_id=None,
        machine_status="none",
        org_id=owner.home_org_team_id,
    )
    await db.commit()
    return chat


async def _apply(
    db: AsyncSession,
    registry: DocRegistry,
    *,
    grant: ChannelGrant,
    user: User,
    op: OpPayload,
) -> Any:
    return await registry.apply_op(
        db,
        grant=grant,
        user=user,
        envelope=_envelope(grant.channel, peer_id="p:pub", op=op),
        actor=actor_for_user(user, org_id=user.home_org_team_id),
        ent=await _ent(db, user),
    )


async def _announcements(org_id: UUID) -> int:
    async with AsyncSessionLocal() as db:
        rows = await db.execute(
            select(EventOutbox).where(
                EventOutbox.org_id == org_id, EventOutbox.type == EventType.CHAT_UPDATED.value
            )
        )
        return len(list(rows.scalars().all()))


async def _settle(org_id: UUID, *, expected: int) -> int:
    """The announcement count once the trailing task has had its chance.

    It runs off the request, so the test waits for it rather than assuming an
    ordering — bounded, and returning whatever it found when the budget ran
    out so an assertion reports the real number."""
    deadline = 50
    count = await _announcements(org_id)
    while count != expected and deadline > 0:
        deadline -= 1
        await asyncio.sleep(0.02)
        count = await _announcements(org_id)
    return count


class _Rig:
    def __init__(self, gate: _Gate, registry: DocRegistry, clock: list[float]) -> None:
        self.gate = gate
        self.registry = registry
        self.clock = clock


def _rig() -> _Rig:
    gate = _Gate()
    clock = [1000.0]
    registry = DocRegistry(
        now=lambda: clock[0],
        sleep=gate.sleep,
        sessions=lambda: AsyncSessionLocal(),
    )
    return _Rig(gate, registry, clock)


async def test_the_last_change_of_a_burst_is_announced_when_the_window_elapses(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """A burst that stops inside the window still reaches everyone not watching.

    Two appends land a second apart: the first opens the window and rings, the
    second is swallowed by it. Nothing else happens — which is exactly the case
    that used to announce nothing — so the window's own expiry has to ring for
    it, once."""
    admin = await _admin(real_session, org_admin)
    chat = await _chat_object(real_session, admin)
    grant = await _grant_for(real_session, admin, Channel("chat", str(chat.id)))
    rig = _rig()
    before = await _announcements(org_admin.org_id)

    for index in (1, 2):
        rig.clock[0] += 1.0
        await _apply(
            real_session,
            rig.registry,
            grant=grant,
            user=admin,
            op=_op("append", events=[_event(index)]),
        )
    await real_session.commit()
    assert await _announcements(org_admin.org_id) == before + 1, "the second rode the first"

    # The timer waits out what is LEFT of the window the first append opened,
    # not a fresh one: the first rang at 1001, the second was swallowed at 1002.
    assert rig.gate.delays == [pytest.approx(DOMAIN_ANNOUNCE_INTERVAL_SECONDS - 1.0)]
    rig.clock[0] += DOMAIN_ANNOUNCE_INTERVAL_SECONDS
    rig.gate.elapse()
    assert await _settle(org_admin.org_id, expected=before + 2) == before + 2
    # And only once: the burst is one announcement however many ops it held.
    await asyncio.sleep(0.05)
    assert await _announcements(org_admin.org_id) == before + 2
    await rig.registry.aclose()


async def test_the_leading_edge_still_rings_at_once_and_arms_nothing(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """A change that opens a window is announced with its own transaction, and
    nothing is left waiting behind it."""
    admin = await _admin(real_session, org_admin)
    chat = await _chat_object(real_session, admin)
    grant = await _grant_for(real_session, admin, Channel("chat", str(chat.id)))
    rig = _rig()
    before = await _announcements(org_admin.org_id)

    await _apply(
        real_session, rig.registry, grant=grant, user=admin, op=_op("append", events=[_event(1)])
    )
    await real_session.commit()
    assert await _announcements(org_admin.org_id) == before + 1
    # No timer was armed at all, so nothing can ring a second time for it.
    assert rig.gate.delays == []
    rig.gate.elapse()
    await asyncio.sleep(0.05)
    assert await _announcements(org_admin.org_id) == before + 1
    await rig.registry.aclose()


async def test_an_announcement_made_here_cancels_the_one_that_was_waiting(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """The window closing on a real operation says everything the trailing edge
    was holding, so the org is not asked to refetch twice for one burst."""
    admin = await _admin(real_session, org_admin)
    chat = await _chat_object(real_session, admin)
    grant = await _grant_for(real_session, admin, Channel("chat", str(chat.id)))
    rig = _rig()
    before = await _announcements(org_admin.org_id)

    for index in (1, 2):
        rig.clock[0] += 1.0
        await _apply(
            real_session,
            rig.registry,
            grant=grant,
            user=admin,
            op=_op("append", events=[_event(index)]),
        )
    await asyncio.sleep(0)  # let the armed timer reach its first await
    assert rig.gate.delays, "the swallowed change armed the trailing edge"
    # The window elapses and a third append arrives on its heels: it rings the
    # leading edge itself, and must take the armed one with it.
    rig.clock[0] += DOMAIN_ANNOUNCE_INTERVAL_SECONDS
    await _apply(
        real_session, rig.registry, grant=grant, user=admin, op=_op("append", events=[_event(3)])
    )
    await real_session.commit()
    rig.gate.elapse()
    await asyncio.sleep(0.05)
    assert await _announcements(org_admin.org_id) == before + 2, "one per window, not three"
    await rig.registry.aclose()


async def test_a_teardown_announces_nothing_the_window_was_holding(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """The process is going away and with it the session the timer would write
    through: the armed announcement is dropped, not emitted late."""
    admin = await _admin(real_session, org_admin)
    chat = await _chat_object(real_session, admin)
    grant = await _grant_for(real_session, admin, Channel("chat", str(chat.id)))
    rig = _rig()
    before = await _announcements(org_admin.org_id)

    for index in (1, 2):
        rig.clock[0] += 1.0
        await _apply(
            real_session,
            rig.registry,
            grant=grant,
            user=admin,
            op=_op("append", events=[_event(index)]),
        )
    await real_session.commit()
    assert rig.gate.delays, "the swallowed change armed the trailing edge"

    await rig.registry.aclose()
    rig.gate.elapse()
    await asyncio.sleep(0.05)
    assert await _announcements(org_admin.org_id) == before + 1


async def test_an_append_that_records_nothing_arms_nothing(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """A republished event changes no state, so it must not ring the org later
    any more than it rings it now."""
    admin = await _admin(real_session, org_admin)
    chat = await _chat_object(real_session, admin)
    grant = await _grant_for(real_session, admin, Channel("chat", str(chat.id)))
    rig = _rig()
    before = await _announcements(org_admin.org_id)

    for _ in range(2):
        rig.clock[0] += 1.0
        await _apply(
            real_session,
            rig.registry,
            grant=grant,
            user=admin,
            op=_op("append", events=[_event(1)]),
        )
    await real_session.commit()
    assert rig.gate.delays == [], "the repeat recorded nothing, so it holds nothing"
    rig.gate.elapse()
    await asyncio.sleep(0.05)
    assert await _announcements(org_admin.org_id) == before + 1
    await rig.registry.aclose()
