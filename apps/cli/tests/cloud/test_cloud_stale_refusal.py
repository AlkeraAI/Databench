"""A refusal the box put on a chat never outlives the box serving it.

The box clears a chat's refusal by reporting ``publishing`` when a mirror
starts. A refusal said while the chat's mirror kept running (or one whose
clearing report did not land) stayed on the row, and the reader's composer
read "This chat can't run right now" over a chat the box was answering. The
box now re-says ``publishing`` when the row it reads for a chat it serves
still carries a refusal.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from _mirror_service import Clock, build_service
from alkera_cli.cloud import CloudRestClient

CHAT = "chat-a"
MACHINE = "6a2c1b4d-3e1f-4b3c-8e6d-82b4d0a1c222"


class RecordingRest(CloudRestClient):
    def __init__(self) -> None:
        super().__init__(api_url="http://127.0.0.1:1", token="t", agent_id="machine:x")
        self.reports: list[tuple[str, str]] = []

    def for_agent(self, agent_id: str) -> CloudRestClient:
        return self

    async def report_publisher_state(
        self, chat_id: str, *, state: str, reason: str = "", **_: Any
    ) -> dict[str, Any]:
        self.reports.append((chat_id, state))
        return {}


async def _serving(tmp_path: Path) -> tuple[Any, dict[str, Any], RecordingRest]:
    rest = RecordingRest()
    service, built = build_service(tmp_path, clock=Clock(), rest=rest)
    await service._adopt_machine(MACHINE)
    await service._ensure_mirror(CHAT, {"id": CHAT, "last_seq": 4})
    assert rest.reports == [(CHAT, "publishing")]
    assert built[CHAT].state == "running"
    return service, built, rest


async def test_a_refusal_left_on_a_chat_this_box_serves_is_cleared(tmp_path: Path) -> None:
    service, _built, rest = await _serving(tmp_path)
    stale = {
        "id": CHAT,
        "last_seq": 4,
        "machine_status": "refused",
        "machine_refusal_reason": "this chat's files could not be reached",
    }
    await service._ensure_mirror(CHAT, dict(stale), polled=True)
    assert rest.reports[-1] == (CHAT, "publishing")
    # Said once for the refusal it saw, not on every pass that reads it again.
    await service._ensure_mirror(CHAT, dict(stale), polled=True)
    assert rest.reports.count((CHAT, "publishing")) == 2


async def test_a_row_with_no_refusal_is_not_reported_again(tmp_path: Path) -> None:
    service, _built, rest = await _serving(tmp_path)
    await service._ensure_mirror(CHAT, {"id": CHAT, "last_seq": 4}, polled=True)
    await service._ensure_mirror(
        CHAT, {"id": CHAT, "last_seq": 4, "machine_status": "ready"}, polled=True
    )
    assert rest.reports == [(CHAT, "publishing")]


async def test_a_chat_whose_mirror_is_still_starting_keeps_its_refusal(tmp_path: Path) -> None:
    """Not served yet: the start's own report clears it once it is."""
    service, built, rest = await _serving(tmp_path)
    built[CHAT].state = "starting"
    await service._ensure_mirror(
        CHAT,
        {"id": CHAT, "last_seq": 4, "machine_status": "refused", "machine_refusal_reason": "x"},
        polled=True,
    )
    assert rest.reports == [(CHAT, "publishing")]
