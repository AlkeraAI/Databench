"""Chats: the chat object, templates, warm spares, duplication, mode changes,
workspace state and the model catalog a chat picks from.
"""

from __future__ import annotations

from types import ModuleType
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from backend.services.chats.attachment_cursor import (
        decode_attachment_cursor as decode_attachment_cursor,
    )
    from backend.services.chats.attachment_cursor import (
        encode_attachment_cursor as encode_attachment_cursor,
    )
    from backend.services.chats.chat_service import (
        ChatMessageTooLargeError as ChatMessageTooLargeError,
    )
    from backend.services.chats.chat_service import (
        announce_chat as announce_chat,
    )
    from backend.services.chats.chat_service import (
        chat_spec_of as chat_spec_of,
    )
    from backend.services.chats.chat_service import (
        delete_chat as delete_chat,
    )
    from backend.services.chats.chat_service import (
        rebind_machine as rebind_machine,
    )
    from backend.services.chats.creation import (
        names_another_workspace as names_another_workspace,
    )
    from backend.services.chats.message_ids import (
        MAX_MESSAGE_SEQ as MAX_MESSAGE_SEQ,
    )
    from backend.services.chats.message_ids import (
        ClientIdReusedError as ClientIdReusedError,
    )
    from backend.services.chats.model_switch import (
        ModelSwitchRefusedError as ModelSwitchRefusedError,
    )
    from backend.services.chats.model_switch import (
        model_options as model_options,
    )
    from backend.services.chats.model_switch import (
        plan_switch as plan_switch,
    )
    from backend.services.chats.read_marks import (
        ReadState as ReadState,
    )
    from backend.services.chats.read_marks import (
        mark_all_read as mark_all_read,
    )
    from backend.services.chats.read_marks import (
        mark_read as mark_read,
    )
    from backend.services.chats.read_marks import (
        mark_unread as mark_unread,
    )
    from backend.services.chats.read_marks import (
        read_states as read_states,
    )
    from backend.services.chats.read_marks import (
        reader_id as reader_id,
    )
    from backend.services.chats.read_marks import (
        workspace_unread_counts as workspace_unread_counts,
    )
    from backend.services.chats.reads import (
        ChatCaps as ChatCaps,
    )
    from backend.services.chats.reads import (
        ChatNode as ChatNode,
    )
    from backend.services.chats.reads import (
        LiveMachines as LiveMachines,
    )
    from backend.services.chats.reads import (
        SandboxLimits as SandboxLimits,
    )
    from backend.services.chats.reads import (
        caps as caps,
    )
    from backend.services.chats.reads import (
        chat_list_items as chat_list_items,
    )
    from backend.services.chats.reads import (
        chat_nodes as chat_nodes,
    )
    from backend.services.chats.reads import (
        chat_read as chat_read,
    )
    from backend.services.chats.reads import (
        instant as instant,
    )
    from backend.services.chats.reads import (
        links as links,
    )
    from backend.services.chats.reads import (
        live_machines as live_machines,
    )
    from backend.services.chats.reads import (
        machine_status as machine_status,
    )
    from backend.services.chats.reads import (
        may_send as may_send,
    )
    from backend.services.chats.reads import (
        owner_names as owner_names,
    )
    from backend.services.chats.reads import (
        sandbox_limits as sandbox_limits,
    )
    from backend.services.chats.routing import (
        routing_page as routing_page,
    )
    from backend.services.chats.send_admission import (
        send_decision as send_decision,
    )
    from backend.services.chats.send_admission import (
        turn_admission as turn_admission,
    )
    from backend.services.chats.wake_intent import (
        WAKE_INTENT_INTERVAL as WAKE_INTENT_INTERVAL,
    )
    from backend.services.chats.wake_intent import (
        WakeRefusedError as WakeRefusedError,
    )
    from backend.services.chats.wake_intent import (
        wake_on_open as wake_on_open,
    )
    from backend.services.chats.wake_intent import (
        wake_standing as wake_standing,
    )

_SERVICE = "backend.services.chats.chat_service"
_READS = "backend.services.chats.reads"
_CREATION = "backend.services.chats.creation"
_READ_MARKS = "backend.services.chats.read_marks"

#: Each public name and the submodule that owns it. A name resolves on first
#: use, so importing one submodule of this package never imports its siblings.
_OWNERS: dict[str, str] = {
    "ChatCaps": _READS,
    "ChatMessageTooLargeError": _SERVICE,
    "ChatNode": _READS,
    "ClientIdReusedError": "backend.services.chats.message_ids",
    "LiveMachines": _READS,
    "MAX_MESSAGE_SEQ": "backend.services.chats.message_ids",
    "ModelSwitchRefusedError": "backend.services.chats.model_switch",
    "ReadState": _READ_MARKS,
    "SandboxLimits": _READS,
    "announce_chat": _SERVICE,
    "caps": _READS,
    "chat_list_items": _READS,
    "chat_nodes": _READS,
    "chat_read": _READS,
    "chat_spec_of": _SERVICE,
    "decode_attachment_cursor": "backend.services.chats.attachment_cursor",
    "delete_chat": _SERVICE,
    "encode_attachment_cursor": "backend.services.chats.attachment_cursor",
    "instant": _READS,
    "links": _READS,
    "live_machines": _READS,
    "machine_status": _READS,
    "mark_all_read": _READ_MARKS,
    "mark_read": _READ_MARKS,
    "mark_unread": _READ_MARKS,
    "may_send": _READS,
    "model_options": "backend.services.chats.model_switch",
    "names_another_workspace": _CREATION,
    "owner_names": _READS,
    "plan_switch": "backend.services.chats.model_switch",
    "read_states": _READ_MARKS,
    "reader_id": _READ_MARKS,
    "rebind_machine": _SERVICE,
    "routing_page": "backend.services.chats.routing",
    "sandbox_limits": _READS,
    "send_decision": "backend.services.chats.send_admission",
    "turn_admission": "backend.services.chats.send_admission",
    "WAKE_INTENT_INTERVAL": "backend.services.chats.wake_intent",
    "WakeRefusedError": "backend.services.chats.wake_intent",
    "wake_on_open": "backend.services.chats.wake_intent",
    "wake_standing": "backend.services.chats.wake_intent",
    "workspace_unread_counts": _READ_MARKS,
}

__all__ = [
    "MAX_MESSAGE_SEQ",
    "MODEL_SWITCH_CAPABILITY",
    "WAKE_INTENT_INTERVAL",
    "ChatCaps",
    "ChatMessageTooLargeError",
    "ChatNode",
    "ClientIdReusedError",
    "LiveMachines",
    "ModelSwitchRefusedError",
    "ReadState",
    "SandboxLimits",
    "WakeRefusedError",
    "announce_chat",
    "caps",
    "chat_list_items",
    "chat_nodes",
    "chat_read",
    "chat_spec_of",
    "decode_attachment_cursor",
    "delete_chat",
    "encode_attachment_cursor",
    "instant",
    "links",
    "live_machines",
    "machine_status",
    "mark_all_read",
    "mark_read",
    "mark_unread",
    "may_send",
    "model_options",
    "names_another_workspace",
    "owner_names",
    "plan_switch",
    "read_states",
    "reader_id",
    "rebind_machine",
    "routing_page",
    "sandbox_limits",
    "send_decision",
    "turn_admission",
    "wake_on_open",
    "wake_standing",
    "workspace_unread_counts",
]


def __getattr__(name: str) -> Any:
    owner = _OWNERS.get(name)
    if owner is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(_owner_module(owner), name)


def _owner_module(owner: str) -> ModuleType:
    """Import ``owner`` through a literal import. The compiled CLI build follows
    only literal imports, so a name resolved by string would be missing there."""
    if owner == _SERVICE:
        from backend.services.chats import chat_service

        return chat_service
    if owner == "backend.services.chats.send_admission":
        from backend.services.chats import send_admission

        return send_admission
    if owner == _READS:
        from backend.services.chats import reads

        return reads
    if owner == _READ_MARKS:
        from backend.services.chats import read_marks

        return read_marks
    if owner == _CREATION:
        from backend.services.chats import creation

        return creation
    if owner == "backend.services.chats.model_switch":
        from backend.services.chats import model_switch

        return model_switch
    if owner == "backend.services.chats.wake_intent":
        from backend.services.chats import wake_intent

        return wake_intent
    if owner == "backend.services.chats.routing":
        from backend.services.chats import routing

        return routing
    if owner == "backend.services.chats.message_ids":
        from backend.services.chats import message_ids

        return message_ids
    if owner == "backend.services.chats.attachment_cursor":
        from backend.services.chats import attachment_cursor

        return attachment_cursor
    raise AssertionError(f"no import for {owner}")
