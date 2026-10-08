"""Who decided is the server's to say, and no machine's to claim.

A resolution now carries the member who answered the ask, and a reader's
transcript renders it as "Allowed once by <name>" over a write. That makes it
an identity claim, and a box — running the customer's code next to the
customer's credentials, the least trustworthy writer in the system — is not a
party that gets to make one about the people in the chat. A compromised box, or
merely one echoing a row it read back, would otherwise be able to put a
colleague's name against a write nobody approved.

So the claim is taken off everything a machine publishes, at each of the doors
its words come through — the append op, and the publisher's rebuild, which
replaces the state wholesale and never passes through the append at all. The
authenticated answer route is the only writer that puts a decider on.
"""

from __future__ import annotations

import ast
import asyncio
import uuid
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.events import EventType, actor_for_user
from alkera_core.models import EventOutbox, RealtimeDoc, User, WorkspaceObject
from alkera_core.schemas.realtime import SERVER_PEER_ID, DocEnvelope, OpPayload
from backend.services.chats import chat_service
from backend.services.realtime import channels as channel_service
from backend.services.realtime.channels import Channel, ChannelGrant
from backend.services.realtime.docsync import (
    ChatAppendById,
    DocOpRejectedError,
    DocRegistry,
    publish_server_events,
)
from backend.services.realtime.filters import EntitlementSnapshot, load_entitlements
from sqlalchemy import func, select
from sqlalchemy import text as sql
from sqlalchemy.ext.asyncio import AsyncSession
from tests.conftest import OrgWithAdmin

CLAIM_KEYS = ("decided_by_user_id", "decided_by_name", "decided_via")


def _state() -> dict[str, Any]:
    return {"schema_version": "1.0.0", "meta": {"session_id": "s"}, "events": [], "ids": {}}


def _op(*events: dict[str, Any]) -> OpPayload:
    return OpPayload(op_id=f"op-{uuid.uuid4().hex[:8]}", intent="append", events=list(events))


def _published(*events: dict[str, Any]) -> list[dict[str, Any]]:
    """The window after a machine publishes ``events`` — which is also what the
    transcript write reads and what the rebroadcast replays, since all three
    are taken from this one state."""
    new_state, _, changed = ChatAppendById().apply(_state(), _op(*events), peer_id="p:box")
    assert changed is True
    return list(new_state["events"])


def _envelope(**payload: Any) -> dict[str, Any]:
    """A resolution in the shape a machine's mirror actually publishes it."""
    return {
        "event_id": "ev-1",
        "role": "assistant",
        "kind": "permission.resolved",
        "payload": {
            "event_type": "permission.resolved",
            "schema_version": "1.1.0",
            "session_id": "s",
            "request_id": "per-1",
            "option_id": "allow_once",
            "decided_by": "user",
            **payload,
        },
    }


@pytest.mark.parametrize(
    ("published", "read_back"),
    [
        pytest.param(
            _envelope(
                decided_by_user_id="u-victim", decided_by_name="Dana Okafor", decided_via="web"
            ),
            lambda event: event["payload"],
            id="the-envelope-a-mirror-publishes",
        ),
        pytest.param(
            {
                "event_id": "ev-1",
                "event_type": "question.answered",
                "session_id": "s",
                "request_id": "q-1",
                "answers": [["yes"]],
                "decided_by": "user",
                "decided_by_user_id": "u-victim",
                "decided_by_name": "Dana Okafor",
            },
            lambda event: event,
            id="a-flat-event",
        ),
    ],
)
def test_a_machine_cannot_say_which_member_decided(
    published: dict[str, Any], read_back: Any
) -> None:
    """The resolution still stands — the harness really did settle that ask —
    and it arrives naming nobody."""
    stored = read_back(_published(published)[0])
    assert stored["request_id"] in {"per-1", "q-1"}, "the resolution itself is kept"
    assert stored["decided_by"] == "user"
    for key in CLAIM_KEYS:
        assert key not in stored, f"a machine published {key} and it reached the window"
    assert "Dana Okafor" not in repr(_published(published)), "the name is nowhere in the state"


def test_a_machine_cannot_write_a_mode_change() -> None:
    """A mode change names the member who made it and where; only the server,
    which knows the roster, writes one. A box that publishes one is refused
    outright rather than having its claim stripped: a mode change with nobody
    behind it would be a card saying the stance moved when it did not."""
    forged = {
        "event_id": "aside-mode-forged",
        "role": "system",
        "kind": "mode.changed",
        "payload": {
            "event_type": "mode.changed",
            "event_id": "aside-mode-forged",
            "mode": "bypass",
            "decided_by_name": "Dana Okafor",
        },
    }
    with pytest.raises(DocOpRejectedError) as refused:
        ChatAppendById().apply(_state(), _op(forged), peer_id="p:box")
    assert refused.value.code == "forbidden"
    # The server's own write of the same entry is taken.
    _, _, changed = ChatAppendById().apply(_state(), _op(forged), peer_id=SERVER_PEER_ID)
    assert changed is True


def test_a_machine_cannot_write_a_model_change() -> None:
    """A model change names who moved the chat; a box that publishes one is
    refused the same way a forged mode change is, and the server's own write
    of it is taken."""
    forged = {
        "event_id": "aside-model-forged",
        "role": "system",
        "kind": "model.changed",
        "payload": {
            "event_type": "model.changed",
            "event_id": "aside-model-forged",
            "model_id": "claude-opus-4.5",
            "decided_by_name": "Dana Okafor",
        },
    }
    with pytest.raises(DocOpRejectedError) as refused:
        ChatAppendById().apply(_state(), _op(forged), peer_id="p:box")
    assert refused.value.code == "forbidden"
    _, _, changed = ChatAppendById().apply(_state(), _op(forged), peer_id=SERVER_PEER_ID)
    assert changed is True


def test_a_resolution_with_no_claim_on_it_is_passed_through_untouched() -> None:
    """The common case copies nothing: a clean event comes out as the very
    object that went in, so the strip costs a machine's ordinary traffic
    nothing."""
    clean = _envelope()
    assert chat_service.without_decider_claim(clean) is clean


def test_only_the_claim_is_taken_off() -> None:
    """Everything else the mirror published survives — the strip is a scalpel,
    not a rewrite of what the machine is allowed to say."""
    stored = _published(_envelope(decided_by_name="Dana Okafor", provider_metadata={"a": 1}))[0]
    assert stored["kind"] == "permission.resolved"
    assert stored["payload"]["option_id"] == "allow_once"
    assert stored["payload"]["provider_metadata"] == {"a": 1}


def test_a_claim_at_both_levels_is_taken_off_both() -> None:
    """The envelope and the event it wraps are read by two different readers —
    a browser folding the live frame takes ``payload``, a page catching up over
    REST reads the stored entry itself as the harness event. Cleaning only the
    deeper one leaves the claim standing for the other, which is the same
    forgery by a different route."""
    both = _envelope(decided_by_user_id="u-victim", decided_by_name="Dana Okafor")
    both["decided_by_user_id"] = "u-victim"
    both["decided_by_name"] = "Dana Okafor"

    stored = _published(both)[0]
    for key in CLAIM_KEYS:
        assert key not in stored, f"the envelope kept {key}"
        assert key not in stored["payload"], f"the wrapped event kept {key}"
    assert "Dana Okafor" not in repr(stored)


def test_an_ordinary_event_that_merely_mentions_a_name_is_left_alone() -> None:
    """The strip keys off the field, not the word: a message whose text happens
    to name somebody is not a claim and is not touched."""
    text = {
        "event_id": "ev-2",
        "event_type": "part.created",
        "session_id": "s",
        "part": {"part_type": "text", "part_id": "p1", "text": "ask Dana Okafor about it"},
    }
    assert _published(text)[0] == text


# ---------------------------------------------------------------------------
# The rebuild door: a publisher's snapshot replaces the state wholesale
# ---------------------------------------------------------------------------


async def _grant(db: AsyncSession, user: User, channel: Channel) -> ChannelGrant:
    """The socket's grant for a chat nothing has declared yet — what the
    channel rules mint once the chat's owner and audience are on record."""
    scope = await channel_service.lookup_chat_doc(
        db, org_id=user.home_org_team_id, chat_id=channel.doc_id
    )
    if scope is None:
        return ChannelGrant(
            channel=channel,
            org_id=user.home_org_team_id,
            can_write=True,
            owner_user_id=user.id,
            team_id=None,
        )
    return await channel_service.authorize(
        db, user, channel, ent=await load_entitlements(db, user, org_id=user.home_org_team_id)
    )


async def _ent(db: AsyncSession, user: User) -> EntitlementSnapshot:
    return await load_entitlements(db, user, org_id=user.home_org_team_id)


async def _chat_row(db: AsyncSession, owner: User) -> WorkspaceObject:
    """A real chat object, so the document has one to persist its rows under."""
    chat, _ = await chat_service.create_chat(
        db,
        owner=owner,
        org_id=owner.home_org_team_id,
        title="Ops",
        client_id=uuid4().hex[:8],
        machine_id=None,
        machine_status="none",
    )
    await db.commit()
    return chat


async def _outbox_head(org_id: UUID) -> int:
    async with AsyncSessionLocal() as db:
        return int(
            (
                await db.execute(
                    select(func.max(EventOutbox.id)).where(EventOutbox.org_id == org_id)
                )
            ).scalar_one()
            or 0
        )


async def _doc_frames(org_id: UUID, doc_id: str, *, after: int) -> list[dict[str, Any]]:
    """The document frames other subscribers receive for this doc, in order."""
    async with AsyncSessionLocal() as db:
        rows = (
            await db.execute(
                select(EventOutbox)
                .where(
                    EventOutbox.org_id == org_id,
                    EventOutbox.type == EventType.DOC_OP.value,
                    EventOutbox.entity_id == f"doc:chat:{doc_id}",
                    EventOutbox.id > after,
                )
                .order_by(EventOutbox.id)
            )
        ).scalars()
        return [dict(row.payload) for row in rows]


@pytest.mark.parametrize(
    "where",
    [
        pytest.param("payload", id="in-the-wrapped-event"),
        pytest.param("envelope", id="on-the-envelope"),
        pytest.param("both", id="at-both-levels"),
    ],
)
async def test_a_peers_forged_decider_is_not_rebroadcast_to_the_other_viewers(
    real_session: AsyncSession, org_admin: OrgWithAdmin, where: str
) -> None:
    """Other viewers fold the FRAME, not the window.

    The op a peer sends is echoed to every other subscriber, and the echo was
    built from what the peer sent rather than from what was stored — so a claim
    taken out of the document still reached every screen watching it, by the
    shorter route. On a reader that keeps the first name it is given, that name
    is then unclearable.
    """
    admin = await real_session.get(User, org_admin.admin_id)
    assert admin is not None
    channel = Channel("chat", f"sess-{uuid4().hex[:8]}")
    grant = await _grant(real_session, admin, channel)
    registry = DocRegistry()
    await registry.ensure(
        real_session,
        grant=grant,
        user=admin,
        actor=actor_for_user(admin, org_id=admin.home_org_team_id),
        ent=await _ent(real_session, admin),
    )
    await real_session.commit()

    forged = _envelope()
    if where in ("payload", "both"):
        forged["payload"]["decided_by_user_id"] = "u-victim"
        forged["payload"]["decided_by_name"] = "Dana Okafor"
    if where in ("envelope", "both"):
        forged["decided_by_user_id"] = "u-victim"
        forged["decided_by_name"] = "Dana Okafor"

    before = await _outbox_head(org_admin.org_id)
    doc = await real_session.get(RealtimeDoc, (org_admin.org_id, "chat", channel.doc_id))
    assert doc is not None
    await registry.apply_op(
        real_session,
        grant=grant,
        user=admin,
        envelope=DocEnvelope(
            doc_id=channel.doc_id,
            doc_type="chat",
            epoch=doc.epoch,
            peer_id="p:box",
            seq=0,
            kind="op",
            payload=OpPayload(
                op_id=f"box-{uuid4().hex[:8]}", intent="append", events=[forged]
            ).model_dump(mode="json"),
        ),
        actor=actor_for_user(admin, org_id=admin.home_org_team_id),
        ent=await _ent(real_session, admin),
    )
    await real_session.commit()

    frames = await _doc_frames(org_admin.org_id, channel.doc_id, after=before)
    assert frames, "the op is echoed to the other subscribers"
    carried = [
        event
        for frame in frames
        for event in frame["envelope"]["payload"].get("events") or []
        if event.get("kind") == "permission.resolved"
    ]
    assert carried, "the resolution itself is echoed"
    for event in carried:
        for key in CLAIM_KEYS:
            assert key not in event, f"the frame carried {key} on the envelope"
            assert key not in event["payload"], f"the frame carried {key} in the event"
    assert "Dana Okafor" not in repr(frames) and "u-victim" not in repr(frames)

    async with AsyncSessionLocal() as db:
        stored = await db.get(RealtimeDoc, (org_admin.org_id, "chat", channel.doc_id))
    assert stored is not None
    assert "Dana Okafor" not in repr(stored.state), "and the window is clean too"


async def test_a_client_cannot_write_as_the_server(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """Writing under the server's peer id is what says "the server wrote
    this", and what that buys is the right to name a member. The socket
    already overwrites a peer id a client sends, so nothing legitimate reaches
    the registry claiming it — which is why the refusal belongs at the
    mechanism and not at the one caller that guards it today."""
    admin = await real_session.get(User, org_admin.admin_id)
    assert admin is not None
    channel = Channel("chat", f"sess-{uuid4().hex[:8]}")
    grant = await _grant(real_session, admin, channel)
    registry = DocRegistry()
    await registry.ensure(
        real_session,
        grant=grant,
        user=admin,
        actor=actor_for_user(admin, org_id=admin.home_org_team_id),
        ent=await _ent(real_session, admin),
    )
    await real_session.commit()
    doc = await real_session.get(RealtimeDoc, (org_admin.org_id, "chat", channel.doc_id))
    assert doc is not None

    with pytest.raises(DocOpRejectedError) as excinfo:
        await registry.apply_op(
            real_session,
            grant=grant,
            user=admin,
            envelope=DocEnvelope(
                doc_id=channel.doc_id,
                doc_type="chat",
                epoch=doc.epoch,
                peer_id=SERVER_PEER_ID,
                seq=0,
                kind="op",
                payload=OpPayload(
                    op_id=f"c-{uuid4().hex[:8]}",
                    intent="append",
                    events=[_envelope(decided_by_name="Dana Okafor")],
                ).model_dump(mode="json"),
            ),
            actor=actor_for_user(admin, org_id=admin.home_org_team_id),
            ent=await _ent(real_session, admin),
        )
    assert excinfo.value.code == "bad_op"
    await real_session.rollback()


async def test_the_servers_write_and_a_boxs_append_do_not_lose_each_other(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """Two writers, one window.

    The server writes its own events onto the chat document — a decision, a
    stop note — while the box that serves the chat is appending over its
    socket. Both read the window, both store their own successor of it, and
    whichever commits first is simply gone from the live window: a reader sees
    a chat that skipped what the box said, or a decision that never reached
    anybody, and only a reload puts it back. The document row is the lock both
    take, so the second writer waits and builds on what the first stored.
    """
    admin = await real_session.get(User, org_admin.admin_id)
    assert admin is not None
    chat = await _chat_row(real_session, admin)
    channel = Channel("chat", str(chat.id))
    grant = await _grant(real_session, admin, channel)
    registry = DocRegistry()
    await registry.ensure(
        real_session,
        grant=grant,
        user=admin,
        actor=actor_for_user(admin, org_id=admin.home_org_team_id),
        ent=await _ent(real_session, admin),
    )
    await real_session.commit()

    async def box_appends() -> None:
        async with AsyncSessionLocal() as db:
            user = await db.get(User, org_admin.admin_id)
            assert user is not None
            doc = await db.get(RealtimeDoc, (org_admin.org_id, "chat", str(chat.id)))
            assert doc is not None
            await registry.apply_op(
                db,
                grant=await _grant(db, user, channel),
                user=user,
                envelope=DocEnvelope(
                    doc_id=str(chat.id),
                    doc_type="chat",
                    epoch=doc.epoch,
                    peer_id="p:box",
                    seq=0,
                    kind="op",
                    payload=OpPayload(
                        op_id=f"box-{uuid4().hex[:8]}",
                        intent="append",
                        events=[
                            {
                                "event_id": "ev-box",
                                "role": "assistant",
                                "kind": "message.created",
                                "payload": {
                                    "event_type": "message.created",
                                    "session_id": str(chat.id),
                                    "message_id": "m-box",
                                    "role": "assistant",
                                },
                            }
                        ],
                    ).model_dump(mode="json"),
                ),
                actor=actor_for_user(user, org_id=user.home_org_team_id),
                ent=await load_entitlements(db, user, org_id=user.home_org_team_id),
            )
            await db.commit()

    async def server_writes() -> None:
        async with AsyncSessionLocal() as db:
            user = await db.get(User, org_admin.admin_id)
            assert user is not None
            held = await db.get(WorkspaceObject, chat.id)
            assert held is not None
            await publish_server_events(
                db,
                chat=held,
                user=user,
                entries=[_envelope(decided_by_name="Dana Okafor")],
                actor=None,
            )
            await db.commit()

    await asyncio.gather(box_appends(), server_writes())

    async with AsyncSessionLocal() as db:
        doc = await db.get(RealtimeDoc, (org_admin.org_id, "chat", str(chat.id)))
    assert doc is not None
    ids = [event.get("event_id") for event in doc.state["events"]]
    assert sorted(ids) == ["ev-1", "ev-box"], "neither writer's entry was dropped"


@pytest.mark.parametrize(
    "route",
    [
        pytest.param("answer", id="an-answer"),
        pytest.param("stop", id="a-stop-with-a-waiting-prompt"),
    ],
)
async def test_a_box_appending_and_a_reader_writing_do_not_deadlock(
    real_session: AsyncSession, org_admin: OrgWithAdmin, route: str
) -> None:
    """One order for every writer, or Postgres picks a victim.

    A chat has two rows a writer serialises on — its realtime document and the
    chat object — and a socket's op takes them document-first. A reader's
    answer or Stop that took the chat row first and then reached for the
    document held exactly what the box was waiting for while the box held what
    it was waiting for: the deadlock detector aborts one of them, and the one
    it aborts is the request, so the reader is told their answer could not be
    sent over a chat that was working perfectly.

    Both sides are run against a short ``lock_timeout`` so the old order fails
    as a refusal rather than hanging this suite.
    """
    admin = await real_session.get(User, org_admin.admin_id)
    assert admin is not None
    chat = await _chat_row(real_session, admin)
    channel = Channel("chat", str(chat.id))
    grant = await _grant(real_session, admin, channel)
    registry = DocRegistry()
    await registry.ensure(
        real_session,
        grant=grant,
        user=admin,
        actor=actor_for_user(admin, org_id=admin.home_org_team_id),
        ent=await _ent(real_session, admin),
    )
    if route == "stop":
        await chat_service.append_user_message(
            real_session, chat=chat, user_id=admin.id, text="go", client_id="a"
        )
    await real_session.commit()

    # Both sides hold their first lock before either reaches for its second,
    # which is the interleave a deadlock needs and a passing order survives.
    took_first = asyncio.Event()
    reader_ready = asyncio.Event()

    async def box() -> None:
        async with AsyncSessionLocal() as db:
            await db.execute(sql("SET LOCAL lock_timeout = '5s'"))
            user = await db.get(User, org_admin.admin_id)
            assert user is not None
            doc = await db.get(RealtimeDoc, (org_admin.org_id, "chat", str(chat.id)))
            assert doc is not None
            epoch = doc.epoch
            db.expunge(doc)
            # The document first, exactly as the registry's own locked read
            # takes it — not through the shared helper, because the helper's
            # order is the thing under test.
            await db.execute(
                select(RealtimeDoc)
                .where(
                    RealtimeDoc.org_id == chat.org_team_id,
                    RealtimeDoc.doc_type == "chat",
                    RealtimeDoc.doc_id == str(chat.id),
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            took_first.set()
            await reader_ready.wait()
            await registry.apply_op(
                db,
                grant=await _grant(db, user, channel),
                user=user,
                envelope=DocEnvelope(
                    doc_id=str(chat.id),
                    doc_type="chat",
                    epoch=epoch,
                    peer_id="p:box",
                    seq=0,
                    kind="op",
                    payload=OpPayload(
                        op_id=f"box-{uuid4().hex[:8]}",
                        intent="append",
                        events=[
                            {
                                "event_id": "ev-box",
                                "role": "assistant",
                                "kind": "message.created",
                                "payload": {
                                    "event_type": "message.created",
                                    "session_id": str(chat.id),
                                    "message_id": "m-box",
                                    "role": "assistant",
                                },
                            }
                        ],
                    ).model_dump(mode="json"),
                ),
                actor=actor_for_user(user, org_id=user.home_org_team_id),
                ent=await load_entitlements(db, user, org_id=user.home_org_team_id),
            )
            await db.commit()

    async def reader() -> None:
        async with AsyncSessionLocal() as db:
            await db.execute(sql("SET LOCAL lock_timeout = '5s'"))
            user = await db.get(User, org_admin.admin_id)
            assert user is not None
            held = await db.get(WorkspaceObject, chat.id)
            assert held is not None
            await took_first.wait()
            reader_ready.set()
            if route == "stop":
                note = await chat_service.record_turn_stopped(db, chat=held, who="Dana Okafor")
                entries = [row.payload for row in note]
            else:
                entries = [_envelope(decided_by_name="Dana Okafor")]
            await publish_server_events(db, chat=held, user=user, entries=entries, actor=None)
            await db.commit()

    await asyncio.gather(box(), reader())

    async with AsyncSessionLocal() as db:
        doc = await db.get(RealtimeDoc, (org_admin.org_id, "chat", str(chat.id)))
    assert doc is not None
    ids = {event.get("event_id") for event in doc.state["events"]}
    assert "ev-box" in ids, "the box's append survived"
    assert any(str(i).startswith("stop-") for i in ids) if route == "stop" else "ev-1" in ids


def test_every_transcript_writer_takes_the_locks_in_one_order() -> None:
    """The invariant IS the order, so it is pinned on the shape of the module.

    Anything that writes a chat's transcript rows serialises on the chat
    object, and every one of them has to reach it through
    ``lock_chat_for_write`` — which takes the realtime document first. A
    writer that calls ``object_service.lock`` directly is a writer taking the
    chat row without the document, which is the other half of a deadlock the
    moment it publishes anything.
    """
    source = Path(chat_service.__file__).read_text()
    writers = ("append_user_message", "persist_published_events", "record_turn_stopped")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef) or node.name not in writers:
            continue
        calls = {
            f"{c.func.value.id}.{c.func.attr}"
            if isinstance(c.func, ast.Attribute) and isinstance(c.func.value, ast.Name)
            else getattr(c.func, "id", "")
            for c in ast.walk(node)
            if isinstance(c, ast.Call)
        }
        assert "lock_chat_for_write" in calls, f"{node.name} locks the chat its own way"
        assert "object_service.lock" not in calls, (
            f"{node.name} takes the chat row without the document above it"
        )


async def test_a_rebuilt_snapshot_cannot_carry_a_decider_into_every_readers_window(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """A rebuild is the second way a machine's words reach a reader, and it
    never passes through the append.

    A chat that has been asleep comes back on a fresh machine, and the first
    thing that machine does is republish the transcript it restored — a
    snapshot that REPLACES the document state. That state is what every
    browser's window opens on, so a claim left standing in it is rendered as
    "Allowed once by <a colleague>" to every reader of the chat, over a write
    nobody approved. A machine restoring rows it read off disk is not evidence
    about a person, however the rows got there.
    """
    admin = await real_session.get(User, org_admin.admin_id)
    assert admin is not None
    channel = Channel("chat", f"sess-{uuid4().hex[:8]}")
    grant = await _grant(real_session, admin, channel)
    registry = DocRegistry()
    await registry.ensure(
        real_session,
        grant=grant,
        user=admin,
        actor=actor_for_user(admin, org_id=admin.home_org_team_id),
        ent=await _ent(real_session, admin),
    )
    await real_session.commit()

    forged = _envelope(decided_by_user_id="u-victim", decided_by_name="Dana Okafor")
    forged["decided_by_name"] = "Dana Okafor"
    snapshot = {
        "meta": {"session_id": channel.doc_id},
        "events": [forged],
        "ids": {"ev-1": 0},
    }
    await registry.rebuild(
        real_session,
        grant=grant,
        user=admin,
        state=snapshot,
        reason="publisher_snapshot",
        actor=actor_for_user(admin, org_id=admin.home_org_team_id),
        ent=await _ent(real_session, admin),
    )
    await real_session.commit()

    async with AsyncSessionLocal() as db:
        doc = await db.get(RealtimeDoc, (org_admin.org_id, "chat", channel.doc_id))
    assert doc is not None
    held = doc.state["events"][0]
    assert held["payload"]["option_id"] == "allow_once", "the resolution itself is kept"
    for key in CLAIM_KEYS:
        assert key not in held and key not in held["payload"], f"the rebuild carried {key}"
    assert "Dana Okafor" not in repr(doc.state) and "u-victim" not in repr(doc.state)
