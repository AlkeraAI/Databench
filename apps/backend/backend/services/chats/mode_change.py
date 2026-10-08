"""A chat's permission mode, or its model and effort, changed by a person: the
one path every surface takes.

The web's ``PUT /chats/{id}/permission-mode``, the Slack card's "Change mode"
menu and ``/alkera mode`` all land here, so a mode change is the same four
steps whichever surface a person used:

1. record the stance on the chat row (the durable answer a box reads when it
   opens the session);
2. write it on the transcript as a ``mode.changed`` entry naming who changed
   it, from where, and from what -- the row every reader renders as a card, the
   web transcript and the Slack thread alike (``outcome.mode_change``);
3. put that entry on the chat's document live, so an open browser folds it;
4. relay the stance to the box running the chat now, and announce the chat.

A model or effort change (``change_model``) takes the same steps with a
``model.changed`` entry, written only when the model or the effort moved, and a
``ModelRelay``; the turn records the box stamps say which model ran each turn.

The caller authorizes and commits.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

import structlog
from alkera_core.models import User, WorkspaceObject
from alkera_core.schemas.objects import (
    DEFAULT_CLOUD_PERMISSION_MODE,
    ChatModelPin,
    CloudPermissionMode,
)
from alkera_core.schemas.objects.transcript import (
    ModelChangeRecord,
    ModelRelay,
    ModeRelay,
    aside_note_id,
    model_changed_entry,
)
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services.chats import chat_service
from backend.services.chats.model_switch import ModelSwitchRefusedError
from backend.services.realtime import docsync

logger = structlog.get_logger(__name__)


async def change_permission_mode(
    db: AsyncSession,
    *,
    chat: WorkspaceObject,
    mode: CloudPermissionMode,
    user: User,
    via: Literal["web", "slack"],
    actor: dict[str, Any] | None,
) -> WorkspaceObject:
    """Switch ``chat`` into ``mode`` on ``user``'s word, from ``via``. Returns
    the locked chat row."""
    # The document before the chat, the order every chat writer takes them in
    # (``lock_chat_for_write``): the transcript entry below locks the document,
    # and a box appending over its socket holds it while it waits for the chat.
    locked = await chat_service.lock_chat_for_write(db, chat.id, org_team_id=chat.org_team_id)
    if locked is None:
        raise LookupError("the chat is gone")
    previous = (locked.spec or {}).get("permission_mode") or DEFAULT_CLOUD_PERMISSION_MODE
    chat = await chat_service.set_permission_mode(db, chat=locked, mode=mode)
    entry = await chat_service.record_mode_change(
        db,
        chat=chat,
        previous_mode=str(previous),
        mode=mode,
        by=user,
        via=via,
    )
    if entry is not None:
        await docsync.publish_server_events(db, chat=chat, user=user, entries=[entry], actor=actor)
    await chat_service.broadcast_relay(
        db,
        org_team_id=chat.org_team_id,
        chat_id=chat.id,
        events=[ModeRelay(mode=mode, user_id=str(user.id)).model_dump(mode="json")],
        actor=actor,
    )
    await chat_service.announce_chat(db, chat=chat, actor=actor)
    return chat


async def record_model_change(
    db: AsyncSession,
    *,
    chat: WorkspaceObject,
    previous: Mapping[str, Any] | None,
    pin: ChatModelPin,
    by: User | None,
    via: str | None,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Write a model or effort change onto the transcript; the entry, or
    ``None`` when neither the model nor the effort changed. ``previous`` is the
    pin the row held before (``None`` for a chat that had none)."""
    before = previous if isinstance(previous, Mapping) else {}
    previous_id = before.get("id") if isinstance(before.get("id"), str) else None
    previous_effort = before.get("effort") if isinstance(before.get("effort"), str) else None
    if previous_id == pin.id and previous_effort == pin.effort:
        return None
    previous_name = before.get("display_name")
    entry = model_changed_entry(
        ModelChangeRecord(
            event_id=aside_note_id(f"model-{uuid4().hex}"),
            time=now or datetime.now(UTC),
            session_id=str(chat.id),
            model_id=pin.id,
            effort=pin.effort,
            display_name=pin.display_name,
            previous_model_id=previous_id,
            previous_effort=previous_effort,
            previous_display_name=previous_name if isinstance(previous_name, str) else None,
            decided_by_user_id=str(by.id) if by is not None else None,
            decided_by_name=(by.display_name or None) if by is not None else None,
            decided_via=via,
        )
    )
    await chat_service.persist_published_events(db, chat=chat, events=[entry])
    return entry


async def change_model(
    db: AsyncSession,
    *,
    chat: WorkspaceObject,
    pin: ChatModelPin,
    user: User,
    via: str,
    actor: dict[str, Any] | None,
    ledger: list[str] | None = None,
    expected_model_id: str | None = None,
) -> WorkspaceObject:
    """Move ``chat`` onto ``pin`` on ``user``'s word, from ``via``. The caller
    has resolved the pick against the catalog and checked it against ``ledger``
    (the reasoning formats the chat's history carries), which rides the relay
    so the box re-checks it. ``expected_model_id`` is checked under the row
    lock: a chat someone moved in between is a 409 ``model_changed``
    (:class:`ModelSwitchRefusedError`) and nothing is written. Returns the locked chat row."""
    # The document before the chat, the order every chat writer takes them in
    # (``lock_chat_for_write``): the transcript entry below locks the document,
    # and a box appending over its socket holds it while it waits for the chat.
    locked = await chat_service.lock_chat_for_write(db, chat.id, org_team_id=chat.org_team_id)
    if locked is None:
        raise LookupError("the chat is gone")
    previous = (locked.spec or {}).get("model")
    previous_id = previous.get("id") if isinstance(previous, dict) else None
    if expected_model_id is not None and expected_model_id != previous_id:
        raise ModelSwitchRefusedError(
            409,
            "model_changed",
            "Someone moved this chat onto another model. Pick again.",
            {"model": previous_id if isinstance(previous_id, str) else None},
        )
    chat = await chat_service.set_model(db, chat=locked, pin=pin)
    entry = await record_model_change(db, chat=chat, previous=previous, pin=pin, by=user, via=via)
    logger.info(
        "chat.model_switch.applied",
        chat_id=str(chat.id),
        org_id=str(chat.org_team_id),
        switcher_id=str(user.id),
        via=via,
        model=pin.id,
        effort=pin.effort,
        previous_model=previous_id if isinstance(previous_id, str) else None,
        ledger=ledger,
    )
    if entry is not None:
        await docsync.publish_server_events(db, chat=chat, user=user, entries=[entry], actor=actor)
    relay = ModelRelay(
        pin=pin.model_dump(mode="json"),
        user_id=str(user.id),
        previous_model_id=previous_id if isinstance(previous_id, str) else "",
        decided_via=via,
        ledger=ledger,
    )
    await chat_service.broadcast_relay(
        db,
        org_team_id=chat.org_team_id,
        chat_id=chat.id,
        events=[relay.model_dump(mode="json")],
        actor=actor,
    )
    await chat_service.announce_chat(db, chat=chat, actor=actor)
    return chat


__all__ = ["change_model", "change_permission_mode", "record_model_change"]
