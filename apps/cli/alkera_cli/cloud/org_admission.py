"""Which chats an org worker may serve: its org's, and only the routed ones.

The supervisor routes chats to the worker by org, and the worker does not
take that on trust: a chat is served only when its own server row names the
worker's org (in the one spelling org ids are keyed by) and the supervisor
routed it here. A routing mistake therefore fails closed on the server's row.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import Any

from alkera_cli.cloud.mirror_factory import chat_org_id


class OrgAdmission:
    def __init__(self, org_id: str) -> None:
        self.org_id = org_id
        #: The supervisor's word; ``None`` until it has said anything.
        self.routed: frozenset[str] | None = None
        #: Told each refusal: ids and a reason, for the supervisor's log.
        self.on_refused: Callable[[str, str], None] = lambda chat_id, reason: None

    def route(self, chat_ids: Iterable[str]) -> None:
        self.routed = frozenset(chat_ids)

    def refusal(self, chat: Mapping[str, Any]) -> str | None:
        """Why ``chat`` is not this worker's to serve, or ``None``."""
        if chat_org_id(chat) != self.org_id:
            reason = "the chat's row names another org"
        elif self.routed is None or chat.get("id") not in self.routed:
            reason = "the supervisor has not routed the chat here"
        else:
            return None
        self.on_refused(str(chat.get("id")), reason)
        return reason


__all__ = ["OrgAdmission"]
