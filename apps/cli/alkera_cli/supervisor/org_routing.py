"""What the supervisor reads to know which chats of which org this box serves.

Ids and states only: the backend's ids-only routing for the machine, or a
development file. An entry that does not fit is dropped and said, never
guessed at.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from alkera_cli.supervisor.slots import canonical_org

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RouteEntry:
    chat_id: str
    org_id: str
    desired_state: str = "open"


class RoutingFeed(Protocol):
    async def read(self) -> Sequence[RouteEntry]: ...


def desired_state(entry: Mapping[str, object]) -> str:
    """Whether a routed chat should be held now, from the backend's routing
    entry (its session state, an unanswered turn, a wake asked for) or a
    development file's ``desired_state``."""
    explicit = entry.get("desired_state")
    if isinstance(explicit, str):
        return explicit
    if "state" not in entry:
        return "open"
    awake = entry.get("state") not in (None, "asleep")
    return "open" if awake or entry.get("pending_turn") or entry.get("wake_requested") else "asleep"


def parse_routing(payload: object) -> list[RouteEntry]:
    """The routing a feed answered: the backend's ``{"items": [{chat_id,
    org_id, state, pending_turn, wake_requested, ...}]}`` or a development
    file's ``{"chats": [{chat_id, org_id, desired_state}]}``. An entry that does
    not fit (a bad org id among them) is dropped and said, never guessed at."""
    entries: list[RouteEntry] = []
    chats = None
    if isinstance(payload, Mapping):
        chats = payload.get("items", payload.get("chats"))
    if not isinstance(chats, list):
        raise ValueError("a routing feed answered no chat list")
    for raw in chats:
        if not isinstance(raw, Mapping):
            continue
        chat_id, org_id = raw.get("chat_id"), raw.get("org_id")
        if not isinstance(chat_id, str) or not isinstance(org_id, str):
            continue
        try:
            org = canonical_org(org_id)
        except RuntimeError:
            logger.warning("routing names chat %s under a malformed org id; dropped", chat_id)
            continue
        entries.append(RouteEntry(chat_id, org, desired_state(raw)))
    return entries


@dataclass(frozen=True, slots=True)
class FileRoutingFeed:
    """Routing from a file, for development and the rig, until the backend's
    ids-only routing route is there."""

    path: Path

    async def read(self) -> Sequence[RouteEntry]:
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        return parse_routing(json.loads(text))


__all__ = ["FileRoutingFeed", "RouteEntry", "RoutingFeed", "desired_state", "parse_routing"]
