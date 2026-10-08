"""How a person's message is named: its transcript id and its ``client_id``.

A message's transcript id is derived from the ``client_id`` its sender chose,
so a retried send lands on the message it already made. These are the rules
that keep that one name honest.
"""

from __future__ import annotations

from uuid import UUID

from alkera_core.models import ChatMessage
from alkera_core.schemas.objects.transcript import prompt_entry_id

#: The largest transcript position a page may name: ``seq`` is a 32-bit column,
#: so a larger cursor is refused as a bad request rather than reaching the
#: database as a bind it cannot make.
MAX_MESSAGE_SEQ = 2**31 - 1


class ClientIdReusedError(ValueError):
    """A message ``client_id`` already names a different message in this chat.

    A retry resends the same words under the same id and gets the one message
    back; different words, or another person, under an id already recorded is a
    client bug, and answering with the original would tell the sender their new
    message landed when it did not.
    """

    def __init__(self, client_id: str) -> None:
        super().__init__("client_id already names a different message in this chat")
        self.client_id = client_id


def event_id_for_client_message(client_id: str) -> str:
    """The transcript id a person's message gets — the one shape the server,
    the box and every reader name that message by."""
    return prompt_entry_id(client_id)


def refuse_reused_id(recorded: ChatMessage, *, text: str, user_id: UUID, client_id: str) -> None:
    """Raise :class:`ClientIdReusedError` unless ``recorded`` (the message the
    ``client_id`` already names) is this same person saying the same words."""
    payload = recorded.payload
    if payload.get("text") != text or payload.get("user_id") != str(user_id):
        raise ClientIdReusedError(client_id)


__all__ = [
    "MAX_MESSAGE_SEQ",
    "ClientIdReusedError",
    "event_id_for_client_message",
    "refuse_reused_id",
]
