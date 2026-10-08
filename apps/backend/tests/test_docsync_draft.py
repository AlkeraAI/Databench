"""The op-log lane no longer holds a chat's shared draft.

The draft lives on the Loro lane (``doc:chat_draft:<chat>``) for every
org. A ``set_meta`` that writes ``meta.draft`` is refused as ``draft_moved``
whatever it carries, so a tab still on the old lane reloads instead of typing
into a draft nobody reads. A ``meta.draft`` already stored stays where it is —
it is the first text of the chat's live draft — and a waking machine's
republish must not throw it away.
"""

from __future__ import annotations

import copy
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from alkera_core.config import settings
from alkera_core.events import actor_for_user
from alkera_core.models import EventOutbox, RealtimeDoc, User, WorkspaceObject
from alkera_core.schemas.realtime import DocEnvelope, OpPayload
from backend.services.chats import chat_service
from backend.services.realtime import channels as channel_service
from backend.services.realtime.channels import Channel
from backend.services.realtime.docsync import ChatAppendById, DocOpRejectedError, DocRegistry
from backend.services.realtime.filters import EntitlementSnapshot, load_entitlements
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from tests.conftest import OrgWithAdmin

# ---------------------------------------------------------------------------
# The strategy, pure
# ---------------------------------------------------------------------------


def _op(intent: str, **kwargs: Any) -> OpPayload:
    return OpPayload(op_id=f"op-{uuid.uuid4().hex[:8]}", intent=intent, **kwargs)  # type: ignore[arg-type]


def _draft(text: str, at: float, peer: str = "p:a", by: str = "") -> dict[str, Any]:
    return {"text": text, "at": at, "peer_id": peer, "by_user_id": by}


def _state(draft: dict[str, Any] | None = None) -> dict[str, Any]:
    meta: dict[str, Any] = {"session_id": "s"}
    if draft is not None:
        meta["draft"] = draft
    return {"schema_version": "1.0.0", "meta": meta, "events": [], "ids": {}}


@pytest.mark.parametrize(
    "meta",
    [
        pytest.param({"draft": _draft("half a thought", 11.0)}, id="a-well-formed-draft"),
        pytest.param({"draft": {**_draft("edit", 2.0), "base": "ed"}}, id="a-merge-with-base"),
        pytest.param({"draft": _draft("", 3.0)}, id="a-clear"),
        pytest.param({"draft": None}, id="null"),
        pytest.param({"draft": "just a string"}, id="malformed"),
        pytest.param({"draft": _draft("x", 1.0), "title": "t"}, id="beside-another-key"),
    ],
)
@pytest.mark.parametrize(
    "held", [None, _draft("stored before the lane", 1.0)], ids=["none", "kept"]
)
def test_every_draft_write_is_refused_as_moved(
    meta: dict[str, Any], held: dict[str, Any] | None
) -> None:
    """Whatever the write carries and whatever the chat holds, the answer is
    ``draft_moved``: never a merge, never ``bad_op`` (which would leave an old
    tab retrying instead of reloading), and the state is not touched."""
    before = _state(held)
    pristine = copy.deepcopy(before)
    with pytest.raises(DocOpRejectedError) as refused:
        ChatAppendById().apply(before, _op("set_meta", meta=meta), peer_id="p:a")
    assert refused.value.code == "draft_moved"
    assert before == pristine


def test_other_meta_keys_still_land_beside_a_stored_draft() -> None:
    """Only the draft moved: every other meta write is merged as it was, and a
    draft already stored is left exactly where it is."""
    kept = _draft("stored before the lane", 1.0)
    new_state, effect, changed = ChatAppendById().apply(
        _state(kept), _op("set_meta", meta={"title": "Renamed"}), peer_id="p:a"
    )
    assert changed is True
    assert new_state["meta"] == {"session_id": "s", "draft": kept, "title": "Renamed"}
    assert effect == {"meta_keys": ["title"]}


def test_a_publishers_snapshot_carries_the_draft_forward() -> None:
    """The machine that wakes up republishes the transcript it restored and
    knows nothing about a draft stored before the live lane; that draft is
    still what the chat's live draft starts from."""
    kept = _draft("written while the box slept", 42.0, "p:browser")
    restored = {"schema_version": "1.0.0", "meta": {"session_id": "s"}, "events": [], "ids": {}}
    carried = ChatAppendById().carry_forward(_state(kept), restored)
    assert carried["meta"]["draft"] == kept
    assert carried["events"] == [], "the publisher's own state is otherwise untouched"


def test_a_snapshot_that_names_a_draft_of_its_own_wins() -> None:
    mine = _draft("the publisher's", 1.0, "srv:0")
    carried = ChatAppendById().carry_forward(
        _state(_draft("the browser's", 99.0, "p:a")), _state(mine)
    )
    assert carried["meta"]["draft"] == mine


def test_a_chat_with_no_draft_carries_nothing_forward() -> None:
    restored = _state()
    assert ChatAppendById().carry_forward(_state(), restored) == restored


# ---------------------------------------------------------------------------
# Against the real document
# ---------------------------------------------------------------------------


@pytest.fixture
def files_on(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    root = tmp_path / "files-store"
    root.mkdir()
    monkeypatch.setattr(settings, "files_enabled", True)
    monkeypatch.setattr(settings, "files_store_provider", "filesystem")
    monkeypatch.setattr(settings, "files_store_root", root)


async def _ent(db: AsyncSession, user: User) -> EntitlementSnapshot:
    return await load_entitlements(db, user, org_id=user.home_org_team_id)


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


@pytest_asyncio.fixture
async def owner(real_session: AsyncSession, org_admin: OrgWithAdmin) -> User:
    user = await real_session.get(User, org_admin.admin_id)
    assert user is not None
    return user


@pytest_asyncio.fixture
async def chat(
    real_session: AsyncSession, owner: User, files_on: None
) -> AsyncIterator[WorkspaceObject]:
    created, _ = await chat_service.create_chat(
        real_session,
        owner=owner,
        org_id=owner.home_org_team_id,
        title="Drafting",
        client_id=None,
        machine_id=None,
        machine_status="none",
    )
    await real_session.commit()
    yield created


async def _open(db: AsyncSession, registry: DocRegistry, user: User, channel: Channel) -> Any:
    snapshot = await registry.snapshot(
        db,
        grant=await channel_service.authorize(db, user, channel, ent=await _ent(db, user)),
        user=user,
        actor=actor_for_user(user, org_id=user.home_org_team_id),
        ent=await _ent(db, user),
    )
    await db.commit()
    return snapshot


async def _store_legacy_draft(
    db: AsyncSession, chat: WorkspaceObject, draft: dict[str, Any]
) -> None:
    """A ``meta.draft`` as the op-log lane stored it before the live lane."""
    row = (
        await db.execute(
            select(RealtimeDoc).where(
                RealtimeDoc.doc_type == "chat", RealtimeDoc.doc_id == str(chat.id)
            )
        )
    ).scalar_one()
    state = dict(row.state)
    state["meta"] = {**state.get("meta", {}), "draft": draft}
    await db.execute(
        update(RealtimeDoc)
        .where(RealtimeDoc.doc_type == "chat", RealtimeDoc.doc_id == str(chat.id))
        .values(state=state)
    )
    await db.commit()


async def _doc_ops(db: AsyncSession, chat: WorkspaceObject) -> int:
    count = await db.scalar(
        select(func.count())
        .select_from(EventOutbox)
        .where(EventOutbox.entity_id == f"doc:chat:{chat.id}")
    )
    return int(count or 0)


async def test_the_owner_of_an_org_that_never_touched_a_setting_is_told_the_draft_moved(
    real_session: AsyncSession, owner: User, chat: WorkspaceObject
) -> None:
    """No switch decides this: a fresh org's owner, with full rights on the
    chat, is refused, and nothing is sequenced, stored or fanned out."""
    channel = Channel("chat", str(chat.id))
    registry = DocRegistry()
    await _open(real_session, registry, owner, channel)
    kept = _draft("stored before the lane", 3.0, "p:old", by=str(owner.id))
    await _store_legacy_draft(real_session, chat, kept)
    before = await _open(real_session, registry, owner, channel)
    ops_before = await _doc_ops(real_session, chat)

    with pytest.raises(DocOpRejectedError) as refused:
        await registry.apply_op(
            real_session,
            grant=await channel_service.authorize(
                real_session, owner, channel, ent=await _ent(real_session, owner)
            ),
            user=owner,
            envelope=_envelope(
                channel,
                epoch=before.epoch,
                peer_id="p:old-tab",
                op=_op("set_meta", meta={"draft": {**_draft("typed late", 9.0), "base": ""}}),
            ),
            actor=actor_for_user(owner, org_id=owner.home_org_team_id),
            ent=await _ent(real_session, owner),
        )
    await real_session.rollback()
    await real_session.refresh(owner)
    assert refused.value.code == "draft_moved"

    after = await _open(real_session, registry, owner, channel)
    assert after.state["meta"]["draft"] == kept
    assert (after.epoch, after.seq) == (before.epoch, before.seq)
    assert await _doc_ops(real_session, chat) == ops_before


async def test_a_stored_draft_survives_the_machines_resume(
    real_session: AsyncSession, owner: User, chat: WorkspaceObject
) -> None:
    """End to end: a chat holding a draft from before the live lane is
    republished by a waking machine, and the draft is still there to seed the
    chat's live draft."""
    channel = Channel("chat", str(chat.id))
    registry = DocRegistry()
    await _open(real_session, registry, owner, channel)
    await _store_legacy_draft(real_session, chat, _draft("what about last quarter?", 5.0))
    served = await _open(real_session, registry, owner, channel)

    await registry.rebuild(
        real_session,
        grant=await channel_service.authorize(
            real_session, owner, channel, ent=await _ent(real_session, owner)
        ),
        user=owner,
        state={
            "schema_version": "1.0.0",
            "meta": {"session_id": str(chat.id)},
            "events": [],
            "ids": {},
        },
        reason="publisher_snapshot",
        actor=actor_for_user(owner, org_id=owner.home_org_team_id),
        ent=await _ent(real_session, owner),
    )
    await real_session.commit()
    after = await _open(real_session, registry, owner, channel)
    assert after.epoch == served.epoch + 1, "a rebuild happened"
    assert after.state["meta"]["draft"]["text"] == "what about last quarter?"
