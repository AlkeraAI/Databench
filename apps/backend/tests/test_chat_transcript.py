"""The durable half of a chat document: the transcript it writes, the window
it keeps, and the relay it enriches.

``chat_messages`` is the system of record and ``realtime_docs.state`` is a
bounded live window over it. The tests that matter here are the ones where
those two disagree on purpose: an event dropped from the window is still in the
table and still pages over REST, and an event republished after a reconnect
lands once in the table however many times it arrives.

The relay half pins the contract the daemon reads: a ``prompt`` from a
peer becomes a transcript entry and is rebroadcast carrying the id and sequence
the server gave it; a server-minted kind arriving from a peer is refused.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from alkera_core.config import settings
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.events import EventType, actor_for_user
from alkera_core.events.outbox import MAX_PAYLOAD_BYTES
from alkera_core.files.authz.ladder import ROLE_READER, ROLE_WRITER
from alkera_core.models import ChatMessage, EventOutbox, RealtimeDoc, User, WorkspaceObject
from alkera_core.schemas.objects import ChatPromptRecord, ChatTranscriptEntry
from alkera_core.schemas.realtime import DocEnvelope, OpPayload
from backend.services.chats import chat_service
from backend.services.realtime import channels as channel_service
from backend.services.realtime.channels import Channel, ChannelGrant
from backend.services.realtime.docsync import (
    DOMAIN_ANNOUNCE_INTERVAL_SECONDS,
    STATE_SCHEMA_VERSION_KEY,
    ChatAppendById,
    DocOpRejectedError,
    DocRegistry,
    compact_window,
    state_size,
    window_threshold,
)
from backend.services.realtime.filters import EntitlementSnapshot, load_entitlements
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from tests.chat_declarations import declare_chat
from tests.chat_shares import files_on, share_chat  # noqa: F401
from tests.conftest import OrgWithAdmin, make_member

# ---------------------------------------------------------------------------
# Helpers (mirroring test_docsync.py so the two read the same way)
# ---------------------------------------------------------------------------


def _op(intent: str, **kwargs: Any) -> OpPayload:
    return OpPayload(op_id=kwargs.pop("op_id", f"op-{uuid4().hex[:8]}"), intent=intent, **kwargs)  # type: ignore[arg-type]


def _envelope(channel: Channel, *, epoch: int, peer_id: str, op: OpPayload) -> DocEnvelope:
    return DocEnvelope(
        doc_id=channel.doc_id,
        doc_type=channel.doc_type,
        epoch=epoch,
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


async def _messages(chat_id: UUID) -> list[ChatMessage]:
    async with AsyncSessionLocal() as db:
        rows = await db.execute(
            select(ChatMessage).where(ChatMessage.chat_id == chat_id).order_by(ChatMessage.seq)
        )
        return list(rows.scalars().all())


def _event(index: int, *, size: int = 0, event_type: str = "message.completed") -> dict[str, Any]:
    event: dict[str, Any] = {"event_id": f"e{index}", "event_type": event_type}
    if size:
        event["text"] = "y" * size
    return event


async def _apply(
    db: AsyncSession,
    registry: DocRegistry,
    *,
    grant: ChannelGrant,
    user: User,
    op: OpPayload,
    epoch: int = 1,
    peer_id: str = "p:pub",
) -> Any:
    return await registry.apply_op(
        db,
        grant=grant,
        user=user,
        envelope=_envelope(grant.channel, epoch=epoch, peer_id=peer_id, op=op),
        actor=actor_for_user(user, org_id=user.home_org_team_id),
        ent=await _ent(db, user),
    )


# ---------------------------------------------------------------------------
# compact_window (pure)
# ---------------------------------------------------------------------------


def _window(count: int, *, size: int) -> dict[str, Any]:
    events = [_event(index, size=size) for index in range(count)]
    return {
        "schema_version": "1.0.0",
        "meta": {},
        "events": events,
        "ids": {event["event_id"]: index for index, event in enumerate(events)},
    }


def test_a_small_window_is_left_alone() -> None:
    state = _window(3, size=10)
    assert compact_window(state) is state


def test_compaction_drops_the_oldest_events_and_rebuilds_the_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "realtime_doc_max_bytes", 4000)
    state = _window(40, size=100)
    assert state_size(state) > window_threshold()
    trimmed = compact_window(state)
    assert state_size(trimmed) <= window_threshold()
    kept = [event["event_id"] for event in trimmed["events"]]
    assert kept == [f"e{index}" for index in range(40 - len(kept), 40)], "the OLDEST go first"
    assert trimmed["ids"] == {event_id: index for index, event_id in enumerate(kept)}


def test_compaction_never_empties_the_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """One event larger than the whole threshold still leaves a window a client
    can render, rather than a document that reads as an empty chat."""
    monkeypatch.setattr(settings, "realtime_doc_max_bytes", 200)
    trimmed = compact_window(_window(3, size=500))
    assert len(trimmed["events"]) == 1
    assert trimmed["ids"] == {"e2": 0}


# ---------------------------------------------------------------------------
# The transcript
# ---------------------------------------------------------------------------


async def test_an_append_writes_the_transcript_with_server_assigned_sequences(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    admin = await _admin(real_session, org_admin)
    chat = await _chat_object(real_session, admin)
    channel = Channel("chat", str(chat.id))
    grant = await _grant_for(real_session, admin, channel)
    registry = DocRegistry()
    applied = await _apply(
        real_session,
        registry,
        grant=grant,
        user=admin,
        op=_op("append", events=[_event(1), _event(2)]),
    )
    await real_session.commit()
    assert applied.changed is True
    rows = await _messages(chat.id)
    assert [(row.seq, row.event_id, row.role) for row in rows] == [
        (1, "e1", "assistant"),
        (2, "e2", "assistant"),
    ]
    assert rows[0].payload["event_type"] == "message.completed"
    assert rows[0].org_team_id == org_admin.org_id


async def test_a_republished_event_lands_once(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """A publisher republishes on reconnect; the transcript must be able to
    absorb that without saying the same thing twice."""
    admin = await _admin(real_session, org_admin)
    chat = await _chat_object(real_session, admin)
    channel = Channel("chat", str(chat.id))
    grant = await _grant_for(real_session, admin, channel)
    registry = DocRegistry()
    await _apply(
        real_session, registry, grant=grant, user=admin, op=_op("append", events=[_event(1)])
    )
    await real_session.commit()
    await _apply(
        real_session,
        registry,
        grant=grant,
        user=admin,
        op=_op("append", events=[_event(1), _event(2)]),
    )
    await real_session.commit()
    assert [row.event_id for row in await _messages(chat.id)] == ["e1", "e2"]
    assert [row.seq for row in await _messages(chat.id)] == [1, 2]


@pytest.mark.parametrize(
    ("event", "expected"),
    [
        pytest.param(
            {"event_id": "x", "event_type": "message.completed"}, "assistant", id="message"
        ),
        pytest.param({"event_id": "x", "event_type": "tool.completed"}, "tool", id="tool_event"),
        pytest.param(
            {"event_id": "x", "event_type": "x", "role": "system"}, "system", id="declared"
        ),
        pytest.param({"event_id": "x"}, "assistant", id="nothing_to_go_on"),
        pytest.param({"event_id": "x", "role": "nonsense"}, "assistant", id="unknown_role"),
    ],
)
def test_the_role_of_a_published_event(event: dict[str, Any], expected: str) -> None:
    """A tool result is not the assistant speaking, and the distinction is what
    lets a reader render a tool card rather than a paragraph."""
    assert chat_service.role_for_event(event) == expected


#: What the machine actually publishes: an ENVELOPE around the harness event
#: (``alkera_cli.cloud.publish.append_entry``), not the event flat. Built from
#: the shared model rather than transcribed by hand — a transcription is how a
#: reader test goes on passing after the writer's shape has moved.
def _published_entry(event_id: str, harness_event: dict[str, Any]) -> dict[str, Any]:
    return ChatTranscriptEntry(
        event_id=event_id,
        role="assistant",
        kind=harness_event["event_type"],
        payload=harness_event,
    ).model_dump(mode="json")


@pytest.mark.parametrize(
    ("event", "expected"),
    [
        pytest.param(
            _published_entry("x", {"event_type": "message.completed", "tokens": {}}),
            "message.completed",
            id="the-envelope-the-machine-publishes",
        ),
        pytest.param(
            {"event_id": "x", "event_type": "message.completed"},
            "message.completed",
            id="a-flat-event",
        ),
        pytest.param(
            {"event_id": "x", "payload": {"event_type": "tool.call"}},
            "tool.call",
            id="an-envelope-without-its-kind",
        ),
        pytest.param({"event_id": "x"}, "", id="nothing-to-go-on"),
    ],
)
def test_the_kind_of_a_published_event(event: dict[str, Any], expected: str) -> None:
    """The transcript row's ``kind`` is how a receipt or a cost roll-up selects
    the rows it cares about, so it has to survive the envelope the publisher
    wraps every harness event in."""
    assert chat_service.kind_for_event(event) == expected


async def test_a_published_message_completion_keeps_its_kind_and_its_usage(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """A completed message is what every cost surface reads: the row must be
    findable by kind, and the usage it carries must be the usage the harness
    reported — the same figures the turn meter counted."""
    admin = await _admin(real_session, org_admin)
    chat = await _chat_object(real_session, admin)
    channel = Channel("chat", str(chat.id))
    grant = await _grant_for(real_session, admin, channel)
    tokens = {"input": 4997, "output": 148, "cache_read": 28032, "cache_write": 0, "total": 33177}
    harness_event = {
        "event_id": "mc1",
        "event_type": "message.completed",
        "message_id": "msg-1",
        "finish_reason": "tool-calls",
        "tokens": tokens,
        "cost": 0.0,
    }
    await _apply(
        real_session,
        DocRegistry(),
        grant=grant,
        user=admin,
        op=_op("append", events=[_published_entry("mc1", harness_event)]),
    )
    await real_session.commit()
    (row,) = await _messages(chat.id)
    assert row.kind == "message.completed"
    assert row.payload["payload"]["tokens"] == tokens


async def test_the_window_compacts_while_the_transcript_keeps_everything(
    real_session: AsyncSession, org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole point of E5: the live document stays small, and nothing is
    lost — the dropped events still page over REST."""
    monkeypatch.setattr(settings, "realtime_doc_max_bytes", 8000)
    admin = await _admin(real_session, org_admin)
    chat = await _chat_object(real_session, admin)
    channel = Channel("chat", str(chat.id))
    grant = await _grant_for(real_session, admin, channel)
    registry = DocRegistry()
    for index in range(30):
        await _apply(
            real_session,
            registry,
            grant=grant,
            user=admin,
            op=_op("append", events=[_event(index, size=300)]),
        )
        await real_session.commit()
    doc = await registry.ensure(
        real_session,
        grant=grant,
        user=admin,
        actor=actor_for_user(admin, org_id=admin.home_org_team_id),
        ent=await _ent(real_session, admin),
    )
    await real_session.commit()
    window = [event["event_id"] for event in doc.state["events"]]
    assert len(window) < 30, "the live window was trimmed"
    assert state_size(doc.state) <= window_threshold()

    rows = await _messages(chat.id)
    assert [row.event_id for row in rows] == [f"e{index}" for index in range(30)]
    assert [row.seq for row in rows] == list(range(1, 31))

    dropped, next_after, resync = await chat_service.list_messages(
        real_session, chat_id=chat.id, after_seq=0, limit=100
    )
    assert [row.event_id for row in dropped] == [f"e{index}" for index in range(30)]
    assert next_after == 30
    assert resync is None


async def _doc_op_events(org_id: UUID, entity_id: str) -> list[list[dict[str, Any]]]:
    """The ``events`` array of every ``doc.op`` row this document left, oldest
    first — what a replica actually rebroadcasts to its subscribers."""
    async with AsyncSessionLocal() as db:
        rows = await db.execute(
            select(EventOutbox)
            .where(
                EventOutbox.org_id == org_id,
                EventOutbox.type == EventType.DOC_OP.value,
                EventOutbox.entity_id == entity_id,
            )
            .order_by(EventOutbox.id)
        )
        return [row.payload["envelope"]["payload"]["events"] for row in rows.scalars().all()]


async def test_a_live_append_is_rebroadcast_carrying_the_sequences_it_recorded(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """A viewer folding a live append can name every row it just took.

    The transcript sequence is assigned by the durable write, so the publisher
    cannot put it on the frame. Without the server putting it there a reader's
    loaded window can only ever let go of the page it opened on: every row that
    arrived since is unnameable, so asking for it again is impossible and
    dropping it would lose transcript. A multi-day chat then grows for the life
    of the tab.
    """
    admin = await _admin(real_session, org_admin)
    chat = await _chat_object(real_session, admin)
    channel = Channel("chat", str(chat.id))
    grant = await _grant_for(real_session, admin, channel)
    registry = DocRegistry()
    applied = await _apply(
        real_session,
        registry,
        grant=grant,
        user=admin,
        op=_op("append", events=[_event(1), _event(2)]),
    )
    await real_session.commit()
    rows = await _messages(chat.id)
    recorded = {row.event_id: row.seq for row in rows}
    assert recorded == {"e1": 1, "e2": 2}
    assert [(entry["event_id"], entry["seq"]) for entry in applied.envelope.payload["events"]] == [
        ("e1", 1),
        ("e2", 2),
    ]
    # And the same numbers travel on the row every replica rebroadcasts from,
    # not only on the envelope this caller happens to hold.
    broadcast = await _doc_op_events(org_admin.org_id, channel.key)
    assert [(entry["event_id"], entry["seq"]) for entry in broadcast[-1]] == [("e1", 1), ("e2", 2)]
    # The retained window says the same thing, so the snapshot a reader opens
    # on and the frames it takes afterwards cannot disagree about a row.
    doc = await registry.ensure(
        real_session,
        grant=grant,
        user=admin,
        actor=actor_for_user(admin, org_id=admin.home_org_team_id),
        ent=await _ent(real_session, admin),
    )
    assert [(entry["event_id"], entry["seq"]) for entry in doc.state["events"]] == [
        ("e1", 1),
        ("e2", 2),
    ]


async def test_a_republished_entry_is_rebroadcast_with_the_sequence_it_already_has(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """A publisher reconnecting republishes what it already sent. That write
    records nothing, so it returns no row to read a sequence off — but the
    frame still carries the entry, and a reader that has since let the row go
    needs to be able to ask for it again. One unnameable row in the window is
    enough to stop the reader ever releasing anything above it."""
    admin = await _admin(real_session, org_admin)
    chat = await _chat_object(real_session, admin)
    channel = Channel("chat", str(chat.id))
    grant = await _grant_for(real_session, admin, channel)
    registry = DocRegistry()
    await _apply(
        real_session, registry, grant=grant, user=admin, op=_op("append", events=[_event(1)])
    )
    await real_session.commit()
    again = await _apply(
        real_session,
        registry,
        grant=grant,
        user=admin,
        op=_op("append", events=[_event(1), _event(2)]),
    )
    await real_session.commit()
    assert [(row.event_id, row.seq) for row in await _messages(chat.id)] == [("e1", 1), ("e2", 2)]
    assert [(entry["event_id"], entry["seq"]) for entry in again.envelope.payload["events"]] == [
        ("e1", 1),
        ("e2", 2),
    ]


async def test_an_append_that_records_nothing_at_all_reaches_no_subscriber(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """The asymmetric case: a republish of ONLY known ids moves no state, so
    there is no frame to stamp and none is sent. A reader hears nothing rather
    than an unstamped echo of rows it already holds."""
    admin = await _admin(real_session, org_admin)
    chat = await _chat_object(real_session, admin)
    channel = Channel("chat", str(chat.id))
    grant = await _grant_for(real_session, admin, channel)
    registry = DocRegistry()
    await _apply(
        real_session, registry, grant=grant, user=admin, op=_op("append", events=[_event(1)])
    )
    await real_session.commit()
    sent = len(await _doc_op_events(org_admin.org_id, channel.key))
    again = await _apply(
        real_session, registry, grant=grant, user=admin, op=_op("append", events=[_event(1)])
    )
    await real_session.commit()
    assert again.changed is False
    assert len(await _doc_op_events(org_admin.org_id, channel.key)) == sent


async def test_an_append_whose_write_is_refused_reaches_no_subscriber_and_records_nothing(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """An entry the transcript cannot hold refuses the whole operation in band:
    the caller's transaction rolls back, so there is no row, no sequence and no
    frame — a viewer is never told about an append with no sequence on it."""
    admin = await _admin(real_session, org_admin)
    chat = await _chat_object(real_session, admin)
    chat_id = chat.id
    channel = Channel("chat", str(chat_id))
    grant = await _grant_for(real_session, admin, channel)
    registry = DocRegistry()
    with pytest.raises(DocOpRejectedError) as excinfo:
        await _apply(
            real_session,
            registry,
            grant=grant,
            user=admin,
            op=_op("append", events=[_event(1, size=MAX_PAYLOAD_BYTES + 1)]),
        )
    assert excinfo.value.code == "op_too_large"
    await real_session.rollback()
    assert await _messages(chat_id) == []
    assert await _doc_op_events(org_admin.org_id, channel.key) == []


async def test_a_prompt_relay_is_not_stamped_by_the_append_path(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """A person's message rides ``user_message`` and is rebroadcast as the
    relay the record minted — ``PromptRelay``, which names its own sequence.
    The append stamp must not reach it and rewrite a body the mirror parses
    field by field."""
    admin = await _admin(real_session, org_admin)
    chat = await _chat_object(real_session, admin)
    channel = Channel("chat", str(chat.id))
    grant = await _grant_for(real_session, admin, channel)
    registry = DocRegistry()
    relayed = await _apply(
        real_session,
        registry,
        grant=grant,
        user=admin,
        op=_op("user_message", events=[{"kind": "prompt", "text": "hi", "client_id": "c1"}]),
    )
    await real_session.commit()
    (event,) = relayed.envelope.payload["events"]
    assert event["kind"] == "prompt" and event["seq"] == 1
    assert "event_id" not in event, "a relay body is the record's, not the appended envelope"


async def test_an_event_larger_than_a_transcript_entry_is_refused_in_band(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    admin = await _admin(real_session, org_admin)
    chat = await _chat_object(real_session, admin)
    # Read the id before the rollback: the rollback expires the instance, and
    # touching it afterwards would be lazy IO outside the session's greenlet.
    chat_id = chat.id
    channel = Channel("chat", str(chat_id))
    grant = await _grant_for(real_session, admin, channel)
    registry = DocRegistry()
    with pytest.raises(DocOpRejectedError) as excinfo:
        await _apply(
            real_session,
            registry,
            grant=grant,
            user=admin,
            op=_op("append", events=[_event(1, size=MAX_PAYLOAD_BYTES + 1)]),
        )
    assert excinfo.value.code == "op_too_large"
    await real_session.rollback()
    assert await _messages(chat_id) == []


async def test_a_rolled_back_operation_leaves_no_transcript(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    admin = await _admin(real_session, org_admin)
    chat = await _chat_object(real_session, admin)
    chat_id = chat.id
    channel = Channel("chat", str(chat_id))
    grant = await _grant_for(real_session, admin, channel)
    registry = DocRegistry()
    await _apply(
        real_session, registry, grant=grant, user=admin, op=_op("append", events=[_event(1)])
    )
    await real_session.rollback()
    assert await _messages(chat_id) == []


async def test_a_chat_document_with_no_workspace_object_still_works(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """A declared chat whose id is not a workspace object's (an older session
    id, a test peer) has no object to write a transcript into. The window keeps
    working, nothing is written, and nothing fails."""
    admin = await _admin(real_session, org_admin)
    channel = channel_service.parse_channel(declare_chat(org_admin.org_id, owner_user_id=admin.id))
    grant = await _grant_for(real_session, admin, channel)
    registry = DocRegistry()
    orphan_id = f"orphan-{uuid4().hex}"
    applied = await _apply(
        real_session,
        registry,
        grant=grant,
        user=admin,
        op=_op("append", events=[{"event_id": orphan_id, "event_type": "message.completed"}]),
    )
    await real_session.commit()
    assert applied.changed is True
    async with AsyncSessionLocal() as db:
        rows = await db.execute(select(ChatMessage).where(ChatMessage.event_id == orphan_id))
        assert rows.scalars().all() == []


# ---------------------------------------------------------------------------
# The relay
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("files_on")
async def test_a_prompt_relay_is_recorded_and_rebroadcast_with_its_identity(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """C2/C3: one service function records a person's message whether it
    arrived over HTTP or over the socket, and the rebroadcast carries what the
    record gave it rather than what the peer guessed."""
    admin = await _admin(real_session, org_admin)
    viewer, _password = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    chat = await _chat_object(real_session, admin)
    # Driving the agent takes the ladder's WRITE rung, so the second person is
    # a colleague the chat was actually shared with rather than a bystander
    # the audience merely admits.
    await share_chat(real_session, chat=chat, owner=admin, user=viewer, role=ROLE_WRITER)
    channel = Channel("chat", str(chat.id))
    owner_grant = await _grant_for(real_session, admin, channel)
    registry = DocRegistry()
    await registry.ensure(
        real_session,
        grant=owner_grant,
        user=admin,
        actor=actor_for_user(admin, org_id=admin.home_org_team_id),
        ent=await _ent(real_session, admin),
    )
    await real_session.commit()
    viewer_grant = await _grant_for(real_session, viewer, channel)
    assert viewer_grant.owner_user_id == admin.id, "the rung does not make them the publisher"
    assert viewer_grant.can_write is True, "a Can-edit rung drives the chat"
    relayed = await _apply(
        real_session,
        registry,
        grant=viewer_grant,
        user=viewer,
        peer_id="p:viewer",
        op=_op(
            "user_message",
            events=[{"kind": "prompt", "text": "and by region?", "client_id": "vc1"}],
        ),
    )
    await real_session.commit()
    assert relayed.changed is False
    event = relayed.envelope.payload["events"][0]
    assert event["kind"] == "prompt"
    assert event["seq"] == 1
    assert event["client_id"] == "vc1"
    assert event["user_id"] == str(viewer.id)
    rows = await _messages(chat.id)
    assert [(row.seq, row.role, row.kind, row.event_id) for row in rows] == [
        (1, "user", "prompt", "usr:vc1")
    ]
    assert str(rows[0].id) == event["message_id"]
    # The relay carries the very stamp the row is read back with, so a reader
    # who hears the send and one who reads the row show the same time.
    assert event["at"] is not None
    assert datetime.fromisoformat(event["at"]) == rows[0].created_at


async def test_a_prompt_relay_repeated_with_one_client_id_is_recorded_once(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    admin = await _admin(real_session, org_admin)
    chat = await _chat_object(real_session, admin)
    channel = Channel("chat", str(chat.id))
    grant = await _grant_for(real_session, admin, channel)
    registry = DocRegistry()
    prompt = {"kind": "prompt", "text": "hello", "client_id": "same"}
    for _ in range(2):
        await _apply(
            real_session,
            registry,
            grant=grant,
            user=admin,
            op=_op("user_message", events=[dict(prompt)]),
        )
        await real_session.commit()
    assert len(await _messages(chat.id)) == 1


@pytest.mark.parametrize("kind", ["run_query", "promote"], ids=["run_query", "promote"])
async def test_a_server_minted_relay_from_a_peer_is_refused(
    real_session: AsyncSession, org_admin: OrgWithAdmin, kind: str
) -> None:
    """A ``run_query`` or ``promote`` is minted by a route that already decided;
    a peer sending one would be asking the machine to act on a decision nobody
    made."""
    admin = await _admin(real_session, org_admin)
    chat = await _chat_object(real_session, admin)
    chat_id = chat.id
    channel = Channel("chat", str(chat_id))
    grant = await _grant_for(real_session, admin, channel)
    registry = DocRegistry()
    with pytest.raises(DocOpRejectedError) as excinfo:
        await _apply(
            real_session,
            registry,
            grant=grant,
            user=admin,
            op=_op("user_message", events=[{"kind": kind, "object_id": str(uuid4())}]),
        )
    assert excinfo.value.code == "forbidden"
    await real_session.rollback()
    assert await _messages(chat_id) == []


@pytest.mark.parametrize(
    "event",
    [
        pytest.param({"kind": "prompt", "client_id": "c"}, id="no_text"),
        pytest.param({"kind": "prompt", "text": "", "client_id": "c"}, id="empty_text"),
        pytest.param({"kind": "prompt", "text": "hi"}, id="no_client_id"),
        pytest.param({"kind": "prompt", "text": "hi", "client_id": 5}, id="client_id_not_a_string"),
    ],
)
async def test_a_malformed_prompt_relay_is_refused(
    real_session: AsyncSession, org_admin: OrgWithAdmin, event: dict[str, Any]
) -> None:
    admin = await _admin(real_session, org_admin)
    chat = await _chat_object(real_session, admin)
    channel = Channel("chat", str(chat.id))
    grant = await _grant_for(real_session, admin, channel)
    registry = DocRegistry()
    with pytest.raises(DocOpRejectedError) as excinfo:
        await _apply(
            real_session,
            registry,
            grant=grant,
            user=admin,
            op=_op("user_message", events=[event]),
        )
    assert excinfo.value.code == "bad_op"
    await real_session.rollback()


async def test_a_relay_with_no_kind_is_refused_on_a_chat_and_travels_on_a_rowless_document(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """A chat's relay lane carries prompts and nothing else: an event with no
    ``kind`` names no message to record, so on a real chat it is refused rather
    than echoed to every other subscriber. The kind-less shape survives only
    where there is no chat to record against — a document whose row is gone —
    which is the one branch that still lets an event travel unchanged.
    """
    admin = await _admin(real_session, org_admin)
    kindless = _op("user_message", events=[{"event_id": "u1", "event_type": "message.created"}])

    orphan_grant = ChannelGrant(
        channel=Channel("chat", str(uuid4())),
        org_id=org_admin.org_id,
        can_write=True,
        owner_user_id=admin.id,
        team_id=None,
    )
    relayed = await _apply(real_session, DocRegistry(), grant=orphan_grant, user=admin, op=kindless)
    await real_session.commit()
    assert relayed.envelope.payload["events"] == [
        {"event_id": "u1", "event_type": "message.created"}
    ]

    chat = await _chat_object(real_session, admin)
    chat_id = chat.id
    grant = await _grant_for(real_session, admin, Channel("chat", str(chat_id)))
    with pytest.raises(DocOpRejectedError) as excinfo:
        await _apply(real_session, DocRegistry(), grant=grant, user=admin, op=kindless)
    assert excinfo.value.code == "unsupported_kind"
    await real_session.rollback()
    assert await _messages(chat_id) == []


async def test_the_rest_post_and_the_socket_relay_write_the_same_entry(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """One service function, two entry points — asserted rather than assumed."""
    admin = await _admin(real_session, org_admin)
    chat = await _chat_object(real_session, admin)
    direct, created = await chat_service.append_user_message(
        real_session, chat=chat, user_id=admin.id, text="typed", client_id="rest-1"
    )
    await real_session.commit()
    assert created is True

    channel = Channel("chat", str(chat.id))
    grant = await _grant_for(real_session, admin, channel)
    registry = DocRegistry()
    await _apply(
        real_session,
        registry,
        grant=grant,
        user=admin,
        op=_op(
            "user_message",
            events=[{"kind": "prompt", "text": "spoken", "client_id": "ws-1"}],
        ),
    )
    await real_session.commit()
    rows = await _messages(chat.id)
    assert [(row.seq, row.role, row.kind) for row in rows] == [
        (1, "user", "prompt"),
        (2, "user", "prompt"),
    ]
    assert rows[0].id == direct.id
    assert set(rows[0].payload) == set(rows[1].payload)


async def test_both_persisted_transcript_shapes_carry_their_schema_version(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """``chat_messages.payload`` is the most-written persisted JSONB here, and
    both shapes it holds have to be migratable: a person's record and the
    envelope the machine publishes. Driven through the real writers, asserted on
    the rows."""
    admin = await _admin(real_session, org_admin)
    chat = await _chat_object(real_session, admin)
    channel = Channel("chat", str(chat.id))
    grant = await _grant_for(real_session, admin, channel)
    registry = DocRegistry()
    await chat_service.append_user_message(
        real_session, chat=chat, user_id=admin.id, text="how many prompts?", client_id="c1"
    )
    await _apply(
        real_session,
        registry,
        grant=grant,
        user=admin,
        op=_op("append", events=[_published_entry("e1", _event(1))]),
    )
    await real_session.commit()
    prompt, published = await _messages(chat.id)
    assert prompt.payload["schema_version"] == ChatPromptRecord.SCHEMA_VERSION
    assert ChatPromptRecord.model_validate(prompt.payload).text == "how many prompts?"
    assert published.payload["schema_version"] == ChatTranscriptEntry.SCHEMA_VERSION
    assert ChatTranscriptEntry.model_validate(published.payload).kind == "message.completed"
    # The inner harness event is untouched — the envelope was the unversioned
    # half, and versioning it must not rewrite what the harness emitted.
    assert published.payload["payload"] == _event(1)


async def test_an_entry_recorded_before_the_stamp_existed_still_loads(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """Widening only: a box still running the writer that predates the envelope
    model publishes the four bare keys, and the server has to record it — and
    read it back — rather than refuse a machine it has not caught up with."""
    admin = await _admin(real_session, org_admin)
    chat = await _chat_object(real_session, admin)
    channel = Channel("chat", str(chat.id))
    grant = await _grant_for(real_session, admin, channel)
    registry = DocRegistry()
    old_writer = {
        "event_id": "e-old",
        "role": "assistant",
        "kind": "message.completed",
        "payload": {"event_type": "message.completed", "tokens": {}},
    }
    await _apply(
        real_session, registry, grant=grant, user=admin, op=_op("append", events=[old_writer])
    )
    await real_session.commit()
    [row] = await _messages(chat.id)
    assert (row.role, row.kind) == ("assistant", "message.completed")
    assert row.payload["payload"] == old_writer["payload"], "the harness event is stored verbatim"
    assert row.payload["schema_version"] == ChatTranscriptEntry.SCHEMA_VERSION
    assert ChatTranscriptEntry.model_validate(old_writer).kind == "message.completed"


@pytest.mark.parametrize(
    ("entry", "role"),
    [
        pytest.param({"event_id": "e", "role": "nonsense"}, "assistant", id="a-role-nobody-knows"),
        pytest.param(
            {"event_id": "e", "payload": "not an object"}, "assistant", id="a-flat-payload"
        ),
    ],
)
async def test_a_publisher_shape_the_reader_does_not_expect_is_recorded_not_refused(
    real_session: AsyncSession, org_admin: OrgWithAdmin, entry: dict[str, Any], role: str
) -> None:
    """Parsing a published entry must not become a new way for a live socket to
    fail: the envelope normalizes the two shapes a publisher can legally send
    instead of raising, exactly as the readers did before it existed."""
    admin = await _admin(real_session, org_admin)
    chat = await _chat_object(real_session, admin)
    channel = Channel("chat", str(chat.id))
    grant = await _grant_for(real_session, admin, channel)
    registry = DocRegistry()
    await _apply(real_session, registry, grant=grant, user=admin, op=_op("append", events=[entry]))
    await real_session.commit()
    [row] = await _messages(chat.id)
    assert row.role == role
    assert isinstance(row.payload["payload"], dict)


async def _chat_updated(org_id: UUID) -> list[Any]:
    async with AsyncSessionLocal() as db:
        rows = await db.execute(
            select(EventOutbox)
            .where(EventOutbox.org_id == org_id, EventOutbox.type == EventType.CHAT_UPDATED.value)
            .order_by(EventOutbox.id)
        )
        return list(rows.scalars().all())


async def test_a_machine_append_rings_the_chat_list_once(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """A machine's appends advance ``last_seq``, and the rail reads that from
    the chat list — which refetches on ``chat.updated``. Without the doorbell
    the list stays stale until something else happens to ring it, so a chat
    that is being answered right now reads as the length it had when it was
    opened. One row per operation: the announcement is the invalidation, not
    the content, so a second event in the same op adds nothing."""
    admin = await _admin(real_session, org_admin)
    chat = await _chat_object(real_session, admin)
    channel = Channel("chat", str(chat.id))
    grant = await _grant_for(real_session, admin, channel)
    registry = DocRegistry()
    before = len(await _chat_updated(org_admin.org_id))
    await _apply(
        real_session,
        registry,
        grant=grant,
        user=admin,
        op=_op("append", events=[_event(1), _event(2)]),
    )
    await real_session.commit()
    rung = await _chat_updated(org_admin.org_id)
    assert len(rung) == before + 1
    assert rung[-1].entity_id == str(chat.id)
    assert rung[-1].payload == {"team_id": str(chat.team_id) if chat.team_id else None}
    assert chat_service.chat_spec_of(chat).last_seq == 2, "the counter the rail reads moved"


async def test_a_burst_of_appends_rings_once_per_window_and_again_after_it(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """A machine streams appends, and the doorbell is coalesced: the chat list
    is invalidated at most once per document per window, so a turn does not ask
    every portal in the org to refetch several times a second. The clock is
    injected so the window is a pinned contract rather than a wall-clock race.

    The trailing edge is the honest cost of that: an append inside a window
    that opened for an earlier one carries no announcement of its own, so a
    turn whose LAST op falls inside the window leaves the rail showing the
    length it had until something rings next. Recorded here, not hidden."""
    admin = await _admin(real_session, org_admin)
    chat = await _chat_object(real_session, admin)
    channel = Channel("chat", str(chat.id))
    grant = await _grant_for(real_session, admin, channel)
    clock = [1000.0]
    registry = DocRegistry(now=lambda: clock[0])
    before = len(await _chat_updated(org_admin.org_id))
    for index in (1, 2):
        clock[0] += 1.0
        await _apply(
            real_session,
            registry,
            grant=grant,
            user=admin,
            op=_op("append", events=[_event(index)]),
        )
    await real_session.commit()
    assert len(await _chat_updated(org_admin.org_id)) == before + 1, "the second rides the first"
    clock[0] += DOMAIN_ANNOUNCE_INTERVAL_SECONDS + 1.0
    await _apply(
        real_session, registry, grant=grant, user=admin, op=_op("append", events=[_event(3)])
    )
    await real_session.commit()
    assert len(await _chat_updated(org_admin.org_id)) == before + 2, "a new window rings again"


async def test_an_append_that_records_nothing_rings_nothing(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """A republished event lands once, and the second publish records no row —
    so it must not invalidate every chat list in the org either."""
    admin = await _admin(real_session, org_admin)
    chat = await _chat_object(real_session, admin)
    channel = Channel("chat", str(chat.id))
    grant = await _grant_for(real_session, admin, channel)
    registry = DocRegistry()
    await _apply(
        real_session, registry, grant=grant, user=admin, op=_op("append", events=[_event(1)])
    )
    await real_session.commit()
    after_first = len(await _chat_updated(org_admin.org_id))
    await _apply(
        real_session, registry, grant=grant, user=admin, op=_op("append", events=[_event(1)])
    )
    await real_session.commit()
    assert len(await _chat_updated(org_admin.org_id)) == after_first


async def test_last_seq_tracks_the_transcript_without_bumping_the_version(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """A client holding version N has not lost a race because somebody spoke."""
    admin = await _admin(real_session, org_admin)
    chat = await _chat_object(real_session, admin)
    before = chat.version
    await chat_service.append_user_message(
        real_session, chat=chat, user_id=admin.id, text="hi", client_id="c1"
    )
    await real_session.commit()
    async with AsyncSessionLocal() as db:
        fresh = await db.get(WorkspaceObject, chat.id)
    assert fresh is not None
    assert fresh.version == before
    assert chat_service.chat_spec_of(fresh).last_seq == 1


async def test_a_chat_document_of_another_org_cannot_reach_this_orgs_transcript(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """The declaration is looked up within the caller's org, so another tenant
    naming this org's chat id is told it does not exist — and nothing of theirs
    can reach this org's transcript."""
    admin = await _admin(real_session, org_admin)
    chat = await _chat_object(real_session, admin)
    from backend.services.org import teams as team_service
    from tests.conftest import _unique_email, _unique_org_name

    async with AsyncSessionLocal() as db:
        _other_org, other_admin = await team_service.create_org_with_admin(
            db,
            org_name=_unique_org_name(),
            admin_email=_unique_email("intruder"),
            admin_first_name="In",
            admin_last_name="Truder",
            admin_password="intruder-pass-12345",
        )
        await db.commit()
        intruder_id = other_admin.id

    async with AsyncSessionLocal() as db:
        intruder = await db.get(User, intruder_id)
        assert intruder is not None
        channel = Channel("chat", str(chat.id))
        with pytest.raises(channel_service.ChannelError) as refused:
            await _grant_for(db, intruder, channel)
        assert refused.value.code == "not_found"
    assert await _messages(chat.id) == [], "nothing crossed the tenant boundary"


# ---------------------------------------------------------------------------
# I3: the REST policy and the channel rules must agree
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("files_on")
async def test_rest_and_socket_agree_on_who_may_see_a_chat(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """K7.3 leaves the socket authoritative for the socket and the policy
    authoritative for REST, on the condition that the two agree by
    construction. They share one predicate; this drives both — the subscribe,
    which IS the socket's read answer, against the policy over the very facts
    the chat route resolves.
    """
    from alkera_core.authz import Action, Effect, Resource, ResourceType, Role, authorize
    from alkera_core.authz.policies import chat as chat_policy
    from backend.services.sharing import access

    admin = await _admin(real_session, org_admin)
    unshared, _password = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    shared, _password = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    chat = await _chat_object(real_session, admin)
    await share_chat(real_session, chat=chat, owner=admin, user=shared, role=ROLE_WRITER)
    channel = Channel("chat", str(chat.id))

    seen: dict[str, bool] = {}
    for user in (admin, unshared, shared):
        ent = await _ent(real_session, user)
        try:
            await _grant_for(real_session, user, channel)
            socket_says = True
        except channel_service.ChannelError as refused:
            assert refused.code == "not_found"
            socket_says = False
        reader = access.Reader(
            ctx=_context_for(user),
            in_org=chat.org_team_id == user.home_org_team_id,
            roles=frozenset({Role.MEMBER}),
            is_org_admin=ent.org_admin,
            team_ids=ent.team_ids,
            email_verified=True,
        )
        rest_says = authorize(
            _context_for(user),
            Action.READ,
            Resource(ResourceType.CHAT, id=str(chat.id), org_id=chat.org_team_id),
            await access.chat_attrs(real_session, chat, reader),
        )
        assert socket_says is rest_says.allowed, user.email
        assert rest_says.policy == chat_policy.POLICY
        if not socket_says:
            assert rest_says.effect is Effect.DENY and rest_says.as_not_found
        seen[user.email] = socket_says
    assert seen[admin.email] is True, "the owner reads their own chat"
    assert seen[shared.email] is True, "the grant is what admits a colleague"
    assert seen[unshared.email] is False, "a chat is private until it is shared"

    # The row-level re-check is a DIFFERENT question, and the reason the
    # subscribe is what the policy is compared against: the ``realtime_docs``
    # row carries the org and the team and nothing finer, so it can narrow a
    # grant that was already issued but can never see the share that issued it.
    doc = await DocRegistry().ensure(
        real_session,
        grant=await _grant_for(real_session, admin, channel),
        user=admin,
        actor=actor_for_user(admin, org_id=admin.home_org_team_id),
        ent=await _ent(real_session, admin),
    )
    await real_session.commit()
    assert doc.team_id is None, "a chat's row is created private"
    assert channel_service.doc_readable(doc, ent=await _ent(real_session, unshared)) is True, (
        "the row alone does not refuse the member the subscribe already refused"
    )


def _context_for(user: User) -> Any:
    from alkera_core.authz import ActingContext

    return ActingContext.for_user(user_id=user.id, org_id=user.home_org_team_id, email=user.email)


async def test_a_sent_message_is_on_the_transcript_page_before_any_machine_has_taken_it(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """A person's message is durable the instant the server accepts it.

    Not "once a box picks the turn up" — the browser that reloads a second
    later reads this page, and a message missing from it is a message the
    reader watched vanish.
    """
    admin = await _admin(real_session, org_admin)
    chat = await _chat_object(real_session, admin)
    message, created = await chat_service.append_user_message(
        real_session, chat=chat, user_id=admin.id, text="what is 6*7?", client_id="web-1"
    )
    await real_session.commit()
    assert created is True

    rows, next_after, resync = await chat_service.list_messages(
        real_session, chat_id=chat.id, after_seq=0, limit=50
    )

    assert [(row.role, row.kind, row.event_id) for row in rows] == [("user", "prompt", "usr:web-1")]
    assert rows[0].payload["text"] == "what is 6*7?"
    assert rows[0].seq == message.seq
    assert next_after == message.seq
    assert resync is None
    assert [row for row in rows if row.role != "user"] == [], (
        "nothing has answered it yet: the message is pending, and the page says so by "
        "carrying no machine row after it"
    )


async def test_a_machine_echoing_a_message_does_not_put_it_on_the_page_twice(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """The box hands the words to the harness, which echoes them back as its
    own user-role event. Both are recorded — the prompt is the record of what
    was SENT, the echo of what the model was ASKED — and the prompt row stays
    exactly one row, under the id the browser gave it."""
    admin = await _admin(real_session, org_admin)
    chat = await _chat_object(real_session, admin)
    await chat_service.append_user_message(
        real_session, chat=chat, user_id=admin.id, text="what is 6*7?", client_id="web-1"
    )
    await real_session.commit()

    await chat_service.persist_published_events(
        real_session,
        chat=chat,
        events=[
            {
                "event_id": "hm1-created",
                "role": "user",
                "kind": "message.created",
                "payload": {
                    "event_id": "hm1-created",
                    "event_type": "message.created",
                    "message_id": "hm1",
                    "role": "user",
                },
            }
        ],
    )
    await real_session.commit()

    rows, _, _ = await chat_service.list_messages(
        real_session, chat_id=chat.id, after_seq=0, limit=50
    )

    assert [row.event_id for row in rows] == ["usr:web-1", "hm1-created"]
    assert [row.kind for row in rows].count("prompt") == 1


def test_the_chat_strategy_still_refuses_what_it_always_refused() -> None:
    """The relay work must not have widened what a chat document accepts."""
    strategy = ChatAppendById()
    with pytest.raises(DocOpRejectedError) as excinfo:
        strategy.apply({"meta": {}, "events": [], "ids": {}}, _op("set_fields"), peer_id="p:1")
    assert excinfo.value.code == "unsupported_kind"
    with pytest.raises(DocOpRejectedError) as chunk:
        strategy.apply({"meta": {}, "events": [], "ids": {}}, _op("chunk"), peer_id="p:1")
    assert chunk.value.code == "unsupported_kind"


# ---------------------------------------------------------------------------
# A chat that is gone
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("intent", "op"),
    [
        pytest.param(
            "append",
            _op(
                "append", events=[{"event_id": "e-after-trash", "event_type": "message.completed"}]
            ),
            id="a-machine-append",
        ),
        pytest.param(
            "user_message",
            _op(
                "user_message",
                events=[{"kind": "prompt", "text": "still there?", "client_id": "c-after-trash"}],
            ),
            id="a-persons-relay",
        ),
    ],
)
async def test_a_write_into_a_trashed_chat_is_refused_and_reaches_nobody(
    real_session: AsyncSession, org_admin: OrgWithAdmin, intent: str, op: OpPayload
) -> None:
    """Trashing a chat leaves its document standing, and both write doors read
    that as the case meant for a document no row backs at all.

    That case exists so a document with no transcript keeps working — nothing
    written, nothing failed — which is the wrong answer for a chat that HAS a
    transcript and has been thrown away. The append was rebroadcast to everyone
    watching with no row and no sequence behind it, so the frame named an entry
    a reader paging the chat can never be served; the relay skipped the SEND
    decision entirely, because there was no chat left to decide against. Every
    reader of a chat already hides a tombstone, so the only honest answer here
    is to write nothing and send nothing.
    """
    admin = await _admin(real_session, org_admin)
    chat = await _chat_object(real_session, admin)
    chat_id = chat.id
    channel = Channel("chat", str(chat_id))
    grant = await _grant_for(real_session, admin, channel)
    registry = DocRegistry()
    # The document exists first: a reader was watching when the chat was binned.
    await registry.ensure(
        real_session,
        grant=grant,
        user=admin,
        actor=actor_for_user(admin, org_id=admin.home_org_team_id),
        ent=await _ent(real_session, admin),
    )
    await real_session.commit()
    before = len(await _doc_op_events(org_admin.org_id, f"doc:chat:{chat_id}"))

    await chat_service.delete_chat(real_session, chat=chat)
    await real_session.commit()

    with pytest.raises(DocOpRejectedError) as refused:
        await _apply(real_session, registry, grant=grant, user=admin, op=op)
    await real_session.rollback()
    assert refused.value.code == "not_found"

    assert await _messages(chat_id) == [], "a trashed chat's transcript took nothing"
    after = await _doc_op_events(org_admin.org_id, f"doc:chat:{chat_id}")
    assert len(after) == before, "nothing was published that the log does not hold"


async def test_a_document_no_chat_row_backs_still_works(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """The case the tombstone refusal must not swallow.

    A document whose id was minted somewhere else — an older session id, a test
    peer — has no chat object and never had a transcript. Its window is the only
    record it gets, and it has to keep taking appends: refusing here would break
    every such document to fix a chat that was thrown away.
    """
    admin = await _admin(real_session, org_admin)
    channel = channel_service.parse_channel(declare_chat(org_admin.org_id, owner_user_id=admin.id))
    grant = await _grant_for(real_session, admin, channel)
    applied = await _apply(
        real_session,
        DocRegistry(),
        grant=grant,
        user=admin,
        op=_op("append", events=[{"event_id": "e-orphan", "event_type": "message.completed"}]),
    )
    assert applied.changed
    assert applied.effect["admitted"][0]["event_id"] == "e-orphan"


async def _authz_decisions(org_id: UUID) -> list[EventOutbox]:
    async with AsyncSessionLocal() as db:
        rows = await db.execute(
            select(EventOutbox)
            .where(
                EventOutbox.org_id == org_id,
                EventOutbox.type == EventType.AUTHZ_DECISION.value,
            )
            .order_by(EventOutbox.id)
        )
        return list(rows.scalars().all())


@pytest.mark.usefixtures("files_on")
async def test_a_read_only_sharee_subscribed_when_the_chat_is_trashed_is_refused_at_the_door(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """The hole the tombstone refusal actually closed, driven by the only
    principal it can be seen through.

    A relay is a RELAYED intent, so ``_require_writable`` never runs on it: the
    whole authorization of a person speaking over the socket is ``_admit_send``,
    inside the ``chat is not None`` arm. A reader already subscribed when the
    chat was binned therefore reached that arm's else-branch and had their
    words relayed to every other subscriber having passed NO send decision at
    all — not an allow, not a deny, nothing on the record either way.

    The chat's owner cannot show this: SEND would admit them anyway, so their
    refusal proves only that the write stopped, never that the door was the
    reason. A reader is the case where the two answers differ.

    The live chat is the control: the same person, the same op, one step
    earlier in the chat's life is refused BY the send gate and leaves the deny
    on the record — which is what makes the trashed chat's silence meaningful
    rather than merely absent.
    """
    admin = await _admin(real_session, org_admin)
    reader_user, _password = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    chat = await _chat_object(real_session, admin)
    chat_id = chat.id
    await share_chat(real_session, chat=chat, owner=admin, user=reader_user, role=ROLE_READER)
    channel = Channel("chat", str(chat_id))
    registry = DocRegistry()

    # Subscribed while the chat was alive — the grant the socket caches, and the
    # one it keeps using after the chat goes away.
    grant = await _grant_for(real_session, reader_user, channel)
    await registry.ensure(
        real_session,
        grant=grant,
        user=admin,
        actor=actor_for_user(admin, org_id=admin.home_org_team_id),
        ent=await _ent(real_session, admin),
    )
    await real_session.commit()

    relay = _op(
        "user_message",
        events=[{"kind": "prompt", "text": "let me in", "client_id": "c-reader"}],
    )

    # The control: on the LIVE chat the send gate is what refuses them, and it
    # says so on the record.
    await real_session.refresh(reader_user)
    with pytest.raises(DocOpRejectedError) as live_refusal:
        await _apply(real_session, registry, grant=grant, user=reader_user, op=relay)
    await real_session.rollback()
    assert live_refusal.value.code == "forbidden"
    denied = await _authz_decisions(org_admin.org_id)
    assert denied, "a refused send is a decision, and it survives the rollback"
    assert denied[-1].payload["effect"] == "deny"
    assert denied[-1].payload["path"] == chat_service.channel_key(chat_id)
    decided = [row.id for row in denied]

    # The rollback above expired the instance the refusal was raised through.
    trashed = await real_session.get(WorkspaceObject, chat_id)
    assert trashed is not None
    await chat_service.delete_chat(real_session, chat=trashed)
    await real_session.commit()
    frames_before = len(await _doc_op_events(org_admin.org_id, f"doc:chat:{chat_id}"))

    await real_session.refresh(reader_user)
    with pytest.raises(DocOpRejectedError) as trashed_refusal:
        await _apply(real_session, registry, grant=grant, user=reader_user, op=relay)
    await real_session.rollback()

    # Refused as a chat that is not there, not as a person who may not speak:
    # the chat is gone, so there is nothing left to decide against.
    assert trashed_refusal.value.code == "not_found"
    assert await _messages(chat_id) == [], "the trashed chat's transcript took nothing"
    assert len(await _doc_op_events(org_admin.org_id, f"doc:chat:{chat_id}")) == frames_before, (
        "no frame reached the owner's socket"
    )
    # The refusal precedes the send gate, so no decision is reached and none is
    # recorded. Stated here so the audit posture is a choice on the record
    # rather than an accident of ordering.
    assert [row.id for row in await _authz_decisions(org_admin.org_id)] == decided, (
        "a chat that is gone is refused before anyone is judged, so nothing is judged"
    )


@pytest.mark.usefixtures("files_on")
async def test_a_subscriber_who_is_not_the_publisher_may_not_rebuild_the_chat(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """The other door onto a chat's state, and the one that publishes without
    persisting.

    A ``snapshot`` frame replaces the window wholesale and is rebroadcast as a
    ``reload`` — nothing is written to the transcript, so whatever it puts on
    the wire is, by construction, state the durable log does not hold. That
    makes who may send one the whole of its safety, and the answer is the
    document's publisher: its owner, or the machine bound to it. A sharee at
    WRITER — the highest rung a share hands out, and enough to speak in the
    chat — is still not that, so the rung that admits a person to the
    conversation does not also let them rewrite its history.
    """
    admin = await _admin(real_session, org_admin)
    writer, _password = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    chat = await _chat_object(real_session, admin)
    chat_id = chat.id
    await share_chat(real_session, chat=chat, owner=admin, user=writer, role=ROLE_WRITER)
    channel = Channel("chat", str(chat_id))
    registry = DocRegistry()
    grant = await _grant_for(real_session, writer, channel)
    doc = await registry.ensure(
        real_session,
        grant=grant,
        user=admin,
        actor=actor_for_user(admin, org_id=admin.home_org_team_id),
        ent=await _ent(real_session, admin),
    )
    epoch_before, seq_before = doc.epoch, doc.seq
    await real_session.commit()
    frames_before = len(await _doc_op_events(org_admin.org_id, f"doc:chat:{chat_id}"))

    with pytest.raises(DocOpRejectedError) as refused:
        await registry.rebuild(
            real_session,
            grant=grant,
            user=writer,
            state={
                STATE_SCHEMA_VERSION_KEY: "1.0.0",
                "meta": {},
                "events": [{"event_id": "e-forged", "event_type": "message.completed"}],
                "ids": {"e-forged": 0},
            },
            reason="publisher",
            actor=actor_for_user(writer, org_id=writer.home_org_team_id),
            ent=await _ent(real_session, writer),
        )
    await real_session.rollback()
    assert refused.value.code == "forbidden"

    async with AsyncSessionLocal() as db:
        after = await db.get(RealtimeDoc, (org_admin.org_id, "chat", str(chat_id)))
        assert after is not None
        assert (after.epoch, after.seq) == (epoch_before, seq_before), "the window did not move"
    assert len(await _doc_op_events(org_admin.org_id, f"doc:chat:{chat_id}")) == frames_before, (
        "a refused rebuild pushes no reload"
    )
