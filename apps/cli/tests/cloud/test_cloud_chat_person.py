"""A box names the person each chat's agent acts for (the chat's owner) to
the notebooks it serves, so the agent's runs read "<agent> for <name>"."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from _mirror_service import Clock, build_service
from alkera_cli.notebooks.box_compose import BoxNotebookSlot
from alkera_notebook.actors import ActingFor, actor_label, actor_names

CHAT = "chat-a"
OWNER = "0b6e1c4f-0000-4000-8000-00000000beef"
OTHER = "0b6e1c4f-0000-4000-8000-00000000cafe"
MACHINE = "6a2c1b4d-3e1f-4b3c-8e6d-82b4d0a1c222"


class _Box:
    def __init__(self) -> None:
        self.people: Any = None

    def use_people(self, person_of: Any) -> None:
        self.people = person_of


async def test_the_box_hands_its_notebooks_each_chat_s_person(tmp_path: Path) -> None:
    box = _Box()
    slot = BoxNotebookSlot(compose=lambda *_: box)  # type: ignore[arg-type,return-value]
    service, _ = build_service(tmp_path, clock=Clock(), notebooks=slot)
    await service._adopt_machine(MACHINE)
    assert box.people is not None
    assert box.people(CHAT) is None  # not one this box has read
    row = {"id": CHAT, "last_seq": 1, "owner_user_id": OWNER, "owner_display_name": "Ada King"}
    await service._ensure_mirror(CHAT, row)
    person = box.people(CHAT)
    assert person == ActingFor(id=f"user:{OWNER}", display_name="Ada King")
    assert actor_label("agent", "", person) == f"{actor_names().agent} for Ada King"


async def test_a_read_without_the_name_keeps_the_one_known(tmp_path: Path) -> None:
    box = _Box()
    slot = BoxNotebookSlot(compose=lambda *_: box)  # type: ignore[arg-type,return-value]
    service, _ = build_service(tmp_path, clock=Clock(), notebooks=slot)
    await service._adopt_machine(MACHINE)
    named = {"id": CHAT, "last_seq": 1, "owner_user_id": OWNER, "owner_display_name": "Ada King"}
    await service._ensure_mirror(CHAT, named)
    # A server that predates the field (or a read that does not carry it).
    await service._ensure_mirror(CHAT, {"id": CHAT, "last_seq": 2, "owner_user_id": OWNER})
    assert box.people(CHAT) == ActingFor(id=f"user:{OWNER}", display_name="Ada King")
    # A different owner is a different person: the old name is not theirs.
    await service._ensure_mirror(CHAT, {"id": CHAT, "last_seq": 3, "owner_user_id": OTHER})
    assert box.people(CHAT) == ActingFor(id=f"user:{OTHER}", display_name="")
    # A renamed person reads under their new name.
    renamed = {"id": CHAT, "last_seq": 4, "owner_user_id": OTHER, "owner_display_name": "Bo Li"}
    await service._ensure_mirror(CHAT, renamed)
    assert box.people(CHAT) == ActingFor(id=f"user:{OTHER}", display_name="Bo Li")
