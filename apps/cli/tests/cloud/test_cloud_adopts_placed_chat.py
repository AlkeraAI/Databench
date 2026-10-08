"""A chat the backend binds to this box after the fact is adopted on the next list.

A box lists the org's chats every poll and serves the ones bound to its own
machine id — so a chat that was opened while the org had no machine (bound to
nothing) is invisible to it, however long the question in it waits. The
backend now binds such a chat to the box when the box comes up; what this
pins is the box's half: a chat that was unbound on one pass and bound to this
machine on the next is picked up, its mirror started, and the question it
holds runs through the catch-up — with no second message from anyone.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from _mirror_service import Clock, build_service
from alkera_cli.cloud.service import CloudMirrorService

MACHINE = "machine:box"


def _serve_list(service: CloudMirrorService, rows: list[dict[str, Any]]) -> None:
    """What ``GET /api/v1/chats`` answers this box, as one mutable page."""

    async def _list(*, cursor: str | None = None, limit: int = 50) -> dict[str, object]:
        return {"items": [dict(row) for row in rows], "next_cursor": None}

    service._rest.list_chats = _list  # type: ignore[method-assign]


async def test_a_chat_bound_to_this_box_after_the_fact_is_adopted_on_the_next_list(
    tmp_path: Path,
) -> None:
    clock = Clock()
    service, built = build_service(tmp_path, clock=clock)
    service._machine_id = MACHINE
    rows = [{"id": "chat-waiting", "machine_id": None, "machine_status": "none", "last_seq": 1}]
    _serve_list(service, rows)

    await service.sync_once()
    assert built == {}, "a chat bound to nothing is nobody's to serve"
    assert service.mirrors == {}

    # The backend placed it on this box when the box came up.
    rows[0]["machine_id"] = MACHINE
    rows[0]["machine_status"] = "ready"
    await service.sync_once()
    assert set(built) == {"chat-waiting"}
    assert built["chat-waiting"].state == "running"
    assert set(service.mirrors) == {"chat-waiting"}

    # And the pass after that keeps it, re-reading the row's counter so a
    # question recorded meanwhile is caught up rather than waited on.
    rows[0]["last_seq"] = 2
    await service.sync_once()
    assert set(service.mirrors) == {"chat-waiting"}
    assert built["chat-waiting"].catch_ups == 1


async def test_a_chat_bound_to_another_box_is_still_not_this_boxs(tmp_path: Path) -> None:
    """The negative twin: binding moved the chat somewhere, not here."""
    clock = Clock()
    service, built = build_service(tmp_path, clock=clock)
    service._machine_id = MACHINE
    rows = [{"id": "chat-elsewhere", "machine_id": None, "last_seq": 1}]
    _serve_list(service, rows)
    await service.sync_once()
    rows[0]["machine_id"] = "machine:other"
    await service.sync_once()
    assert built == {}
    assert service.mirrors == {}
