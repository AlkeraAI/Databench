"""One person, two orgs: a socket sees its own org's documents and events only.

The subject is the person of ``two_org_identity``: a member of org A (their
home) who also holds a membership in org B. A socket opened on their B
credential is in B, whatever their home org is. Every case drives the real
socket session, the real channel rules, the real CRDT registry and the real
per-event filter against real Postgres, with multi-org on (the only setting
under which a B credential exists at all).

The invariants:

* a B socket cannot subscribe to a chat, or a chat's CRDT workspace, of A, even
  one the person owns; the same socket in A can;
* an A event never reaches a B socket or a B event stream, even on a channel
  key both orgs use;
* deactivating the A membership ends the A socket on its next reauthorize and
  leaves the B socket standing.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from alkera_core.auth import SessionClaims, decode_session_token
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.events import HubEvent
from alkera_core.models import MembershipStatus, OrgMembership, RealtimeDoc, User
from backend.services.crdt.registry import ChatWorkspaceType, DocRef
from backend.services.realtime import channels
from backend.services.realtime.channels import Channel, ChannelError, ChannelGrant
from backend.services.realtime.docsync import DocRegistry
from backend.services.realtime.filters import (
    EntitlementRef,
    EntitlementSnapshot,
    load_entitlements,
    stream_predicate,
    visible_to,
)
from backend.services.realtime.runtime import RealtimeRuntime
from backend.services.realtime.session import SocketSession
from fastapi import WebSocket
from sqlalchemy import update
from tests.conftest import TwoOrg
from tests.crdt.crdt_world import crdt_docs
from tests.test_chat_doc_scope import declare_chat

pytestmark = [pytest.mark.spread, pytest.mark.usefixtures("multi_org")]


class _SocketEnd:
    """The socket's far end: frames go nowhere, a close is remembered."""

    def __init__(self) -> None:
        self.closed_with: int | None = None

    async def send_text(self, _text: str) -> None:
        return None

    async def close(self, code: int, reason: str = "") -> None:
        self.closed_with = code


def _claims(token: str) -> SessionClaims:
    return decode_session_token(token)


async def _user(user_id: UUID) -> User:
    async with AsyncSessionLocal() as db:
        user = await db.get(User, user_id)
        assert user is not None
        db.expunge(user)
        return user


async def _ent(user: User, org_id: UUID) -> EntitlementSnapshot:
    async with AsyncSessionLocal() as db:
        return await load_entitlements(db, user, org_id=org_id)


async def _socket(t: TwoOrg, token: str, *, crdt: Any = None) -> SocketSession:
    """A person's socket as ``run_socket`` builds it from an admission: the
    snapshot taken in the ticket's org, the CRDT lane when the process runs one."""
    claims = _claims(token)
    user = await _user(t.user.id)
    runtime = SimpleNamespace(crdt=crdt) if crdt is not None else None
    return SocketSession(
        websocket=cast(WebSocket, _SocketEnd()),
        user=user,
        claims=claims,
        peer_id=f"p:{uuid4().hex[:8]}",
        runtime=cast(RealtimeRuntime, runtime),
        registry=cast(DocRegistry, None),
        ref=EntitlementRef(await _ent(user, claims.org_team_id)),
    )


async def _chat_in(org_id: UUID, owner_id: UUID) -> Channel:
    async with AsyncSessionLocal() as db:
        return await declare_chat(db, org_id=org_id, owner_user_id=owner_id)


def _event(*, org_id: UUID, channel: str | None = None, team_id: UUID | None = None) -> HubEvent:
    payload: dict[str, Any] = {} if team_id is None else {"team_id": str(team_id)}
    return HubEvent(
        lane="durable",
        org_id=org_id,
        type="kb_item.changed",
        entity="kb_item",
        entity_id="item-1",
        version=1,
        visibility="org",
        payload=payload,
        id=1,
        channel=channel,
    )


# --- the socket is in its credential's org ------------------------------------


async def test_the_socket_and_its_snapshot_are_in_the_tickets_org(
    two_org_identity: TwoOrg,
) -> None:
    t = two_org_identity
    socket_a = await _socket(t, t.token_a)
    socket_b = await _socket(t, t.token_b)
    assert (socket_a.org_id, socket_a.ent.org_id) == (t.org_a, t.org_a)
    assert (socket_b.org_id, socket_b.ent.org_id) == (t.org_b, t.org_b)
    # The person sits on each org's root team; each snapshot holds only its own.
    assert socket_a.ent.team_ids == frozenset({t.org_a})
    assert socket_b.ent.team_ids == frozenset({t.org_b})


async def test_an_a_chat_the_person_owns_is_not_found_from_their_b_socket(
    two_org_identity: TwoOrg,
) -> None:
    t = two_org_identity
    channel = await _chat_in(t.org_a, t.user.id)
    socket_a = await _socket(t, t.token_a)
    socket_b = await _socket(t, t.token_b)

    grant, _peers = await socket_a._grant_for(channel)
    assert grant.org_id == t.org_a
    assert grant.can_write is True

    with pytest.raises(ChannelError) as info:
        await socket_b._grant_for(channel)
    assert info.value.code == "not_found"


async def test_the_same_chat_id_in_b_is_granted_in_b_and_never_reads_a(
    two_org_identity: TwoOrg,
) -> None:
    """Two orgs may hold a document under one id. The B socket's grant on its
    own chat is addressed to B, so nothing it writes or hears is A's."""
    t = two_org_identity
    channel = await _chat_in(t.org_b, t.user.id)
    socket_b = await _socket(t, t.token_b)
    grant, _peers = await socket_b._grant_for(channel)
    assert grant.org_id == t.org_b


async def test_a_snapshot_with_no_org_finds_no_persons_document(
    two_org_identity: TwoOrg,
) -> None:
    """A box's snapshot is in no org: the person's channel rules refuse it
    rather than pick an org for it."""
    t = two_org_identity
    channel = await _chat_in(t.org_a, t.user.id)
    user = await _user(t.user.id)
    boxless = EntitlementSnapshot(
        org_id=None, team_ids=frozenset(), org_admin=False, platform=False
    )
    async with AsyncSessionLocal() as db:
        with pytest.raises(ChannelError) as info:
            await channels.authorize(db, user, channel, ent=boxless)
    assert info.value.code == "not_found"


async def test_a_crdt_workspace_of_a_is_not_found_from_the_b_socket(
    two_org_identity: TwoOrg,
) -> None:
    """The chat's shared composer, through the socket's CRDT lane: the lane opens
    the document in the socket's org, and the registry refuses a snapshot taken
    in any other."""
    t = two_org_identity
    chat = await _chat_in(t.org_a, t.user.id)
    workspace = Channel("chat_workspace", chat.doc_id)
    async with crdt_docs() as docs:
        socket_a = await _socket(t, t.token_a, crdt=docs)
        socket_b = await _socket(t, t.token_b, crdt=docs)
        grant, _peers = await socket_a._grant_for(workspace)
        assert grant.org_id == t.org_a
        with pytest.raises(ChannelError) as info:
            await socket_b._grant_for(workspace)
        assert info.value.code == "not_found"


@pytest.mark.parametrize(
    ("ref_org", "ent_org", "readable"),
    [
        pytest.param("a", "a", True, id="same-org"),
        pytest.param("a", "b", False, id="snapshot-in-another-org"),
        pytest.param("b", "a", False, id="document-in-another-org"),
    ],
)
async def test_the_workspace_rule_reads_the_snapshots_org(
    two_org_identity: TwoOrg, ref_org: str, ent_org: str, readable: bool
) -> None:
    t = two_org_identity
    orgs = {"a": t.org_a, "b": t.org_b}
    chat = await _chat_in(orgs[ref_org], t.user.id)
    user = await _user(t.user.id)
    ent = await _ent(user, orgs[ent_org])
    async with AsyncSessionLocal() as db:
        access = await ChatWorkspaceType().authorize(
            db,
            ref=DocRef(org_id=orgs[ref_org], doc_type="chat_workspace", doc_id=chat.doc_id),
            user=user,
            ent=ent,
            agent_id=None,
        )
    assert access.can_read is readable


async def test_a_document_row_of_a_is_unreadable_under_a_b_snapshot(
    two_org_identity: TwoOrg,
) -> None:
    """The registry's per-frame re-check of the row it locks."""
    t = two_org_identity
    user = await _user(t.user.id)
    row = RealtimeDoc(org_id=t.org_a, doc_type="chat", doc_id="shared-id", team_id=None)
    assert channels.doc_readable(row, ent=await _ent(user, t.org_a)) is True
    assert channels.doc_readable(row, ent=await _ent(user, t.org_b)) is False


# --- events -------------------------------------------------------------------


async def test_an_a_event_never_reaches_a_b_socket_on_a_shared_channel_key(
    two_org_identity: TwoOrg,
) -> None:
    t = two_org_identity
    key = f"doc:chat:{uuid4()}"
    socket_a = await _socket(t, t.token_a)
    socket_b = await _socket(t, t.token_b)
    for socket, org in ((socket_a, t.org_a), (socket_b, t.org_b)):
        socket.channels[key] = ChannelGrant(
            channel=Channel("chat", key.removeprefix("doc:chat:")),
            org_id=org,
            can_write=False,
            owner_user_id=t.user.id,
            team_id=None,
        )
    from_a = _event(org_id=t.org_a, channel=key, team_id=t.org_a)
    assert socket_a._accept_event(from_a) is True
    assert socket_b._accept_event(from_a) is False
    assert socket_b._accept_event(_event(org_id=t.org_b, channel=key)) is True


async def test_an_a_event_never_reaches_a_b_event_stream(two_org_identity: TwoOrg) -> None:
    t = two_org_identity
    user = await _user(t.user.id)
    accept_b = stream_predicate(
        user_id=t.user.id, org_id=t.org_b, ref=EntitlementRef(await _ent(user, t.org_b))
    )
    assert accept_b(_event(org_id=t.org_a)) is False
    assert accept_b(_event(org_id=t.org_a, team_id=t.org_a)) is False
    assert accept_b(_event(org_id=t.org_b, team_id=t.org_b)) is True


async def test_a_snapshot_from_another_org_never_vouches_for_a_frame(
    two_org_identity: TwoOrg,
) -> None:
    """A stream in B handed an A snapshot (A's teams, A admin rights) still
    refuses B frames: team ids of one org mean nothing in another."""
    t = two_org_identity
    admin = await _user(t.admin_a.id)
    ent_a = await _ent(admin, t.org_a)
    assert ent_a.org_admin is True
    assert visible_to(_event(org_id=t.org_b), user_id=admin.id, org_id=t.org_b, ent=ent_a) is False
    assert visible_to(_event(org_id=t.org_a), user_id=admin.id, org_id=t.org_a, ent=ent_a) is True


# --- reauthorize --------------------------------------------------------------


async def _deactivate(membership_id: UUID) -> None:
    async with AsyncSessionLocal() as db:
        await db.execute(
            update(OrgMembership)
            .where(OrgMembership.id == membership_id)
            .values(status=MembershipStatus.DEACTIVATED)
        )
        await db.commit()


async def test_deactivating_a_ends_the_a_socket_on_reauthorize_and_spares_b(
    two_org_identity: TwoOrg,
) -> None:
    """Only the membership row moves: the person stays active, nobody revokes a
    token. The reauthorize every access change triggers re-reads the socket's
    own membership, in the socket's own org."""
    t = two_org_identity
    socket_a = await _socket(t, t.token_a)
    socket_b = await _socket(t, t.token_b)
    assert await socket_a._account_still_stands() is True
    assert await socket_b._account_still_stands() is True

    await _deactivate(t.membership_a.id)

    assert await socket_a._account_still_stands() is False
    assert socket_a.closed is True
    assert await socket_b._account_still_stands() is True
    assert socket_b.closed is False
