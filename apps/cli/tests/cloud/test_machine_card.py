"""What the agent is told about the machine it runs on.

The card the platform sends an org machine's box becomes a note in the hidden
context of a chat's turn. The note names the hardware, says when minutes are
billed and when the machine stops on its own, and never names a price.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from alkera_cli.cloud.machine_card import (
    MachineCardHolder,
    MachineNote,
    card_from_answer,
    render_machine_card,
)
from alkera_core.schemas.compute import BoxMachineCard
from alkera_core.schemas.org_machines import GpuSpec

GPU_CARD = BoxMachineCard(
    name="Training box",
    gpu=GpuSpec(name="A100 80GB", count=2, memory_gb=80),
    vcpu=32,
    memory_gb=250,
    disk_gb=500,
    billed_per_minute=True,
    idle_stop_minutes=30,
)
CPU_CARD = BoxMachineCard(
    name="Notebook box",
    gpu=None,
    vcpu=8,
    memory_gb=32,
    disk_gb=100,
    billed_per_minute=False,
    idle_stop_minutes=None,
)


@pytest.mark.parametrize(
    ("card", "free", "expected"),
    [
        pytest.param(
            GPU_CARD,
            412,
            "You are running on Training box: 2x A100 80GB (80 GB), 32 vCPU, 250 GB memory, "
            "412 GB free disk. This machine is billed per minute, and stops after 30 minutes "
            "idle.",
            id="gpu-billed-with-idle-stop",
        ),
        pytest.param(
            CPU_CARD,
            90,
            "You are running on Notebook box: 8 vCPU, 32 GB memory, 90 GB free disk.",
            id="cpu-comped-never-stops",
        ),
        pytest.param(
            GPU_CARD.model_copy(update={"billed_per_minute": False}),
            412,
            "You are running on Training box: 2x A100 80GB (80 GB), 32 vCPU, 250 GB memory, "
            "412 GB free disk. This machine stops after 30 minutes idle.",
            id="gpu-comped-with-idle-stop",
        ),
        pytest.param(
            CPU_CARD.model_copy(update={"billed_per_minute": True}),
            90,
            "You are running on Notebook box: 8 vCPU, 32 GB memory, 90 GB free disk. "
            "This machine is billed per minute.",
            id="cpu-billed-never-stops",
        ),
        pytest.param(
            CPU_CARD,
            None,
            "You are running on Notebook box: 8 vCPU, 32 GB memory, 100 GB disk.",
            id="free-disk-unreadable-falls-back-to-the-size",
        ),
    ],
)
def test_the_note(card: BoxMachineCard, free: int | None, expected: str) -> None:
    assert render_machine_card(card, disk_free_gb=free) == expected


def test_the_note_never_carries_a_figure_of_money() -> None:
    note = render_machine_card(GPU_CARD, disk_free_gb=1)
    assert "$" not in note and "nanos" not in note and "price" not in note.lower()


@pytest.mark.parametrize(
    "answer",
    [
        pytest.param(None, id="no-answer"),
        pytest.param({}, id="a-bodiless-beat"),
        pytest.param({"card": None}, id="a-shared-pool-box"),
        pytest.param({"card": {"name": "x"}}, id="a-card-this-build-cannot-read"),
        pytest.param({"card": {**GPU_CARD.model_dump(), "name": "  "}}, id="no-name"),
    ],
)
def test_an_answer_without_a_readable_card_tells_nothing(answer: Any) -> None:
    assert card_from_answer(answer) is None


def test_a_wire_card_reads_back() -> None:
    assert card_from_answer({"card": GPU_CARD.model_dump(mode="json")}) == GPU_CARD


def _holder(free: int | None = 400) -> MachineCardHolder:
    return MachineCardHolder(free_disk=lambda: free)


def test_a_chat_is_told_once_until_the_card_changes() -> None:
    holder = _holder()
    holder.update({"card": GPU_CARD.model_dump(mode="json")})
    note = MachineNote(holder)
    first = note.unsaid()
    assert first is not None and first.startswith("You are running on Training box")
    # The same card on every beat: nothing new to say.
    holder.update({"card": GPU_CARD.model_dump(mode="json")})
    assert note.unsaid() is None
    renamed = GPU_CARD.model_copy(update={"name": "Big trainer"})
    holder.update({"card": renamed.model_dump(mode="json")})
    again = note.unsaid()
    assert again is not None and "Big trainer" in again
    assert note.unsaid() is None


def test_each_chat_is_told_on_its_own_first_turn() -> None:
    holder = _holder()
    holder.update({"card": CPU_CARD.model_dump(mode="json")})
    first_chat, second_chat = MachineNote(holder), MachineNote(holder)
    assert first_chat.unsaid() is not None
    assert second_chat.unsaid() is not None


def test_a_shared_pool_box_tells_its_chats_nothing() -> None:
    holder = _holder()
    holder.update({})
    assert MachineNote(holder).unsaid() is None


def test_a_mirror_handed_no_holder_tells_nothing_on_any_turn() -> None:
    note = MachineNote(None)
    assert note.unsaid() is None
    assert note.unsaid() is None


def test_free_disk_changing_alone_does_not_repeat_the_note() -> None:
    free = [400]
    holder = MachineCardHolder(free_disk=lambda: free[0])
    holder.update({"card": GPU_CARD.model_dump(mode="json")})
    note = MachineNote(holder)
    assert note.unsaid() is not None
    free[0] = 10
    assert note.unsaid() is None


def test_the_work_volume_free_space_is_read_live(tmp_path: Path) -> None:
    from alkera_cli.cloud.machine_card import work_volume_free_gb

    free = work_volume_free_gb(tmp_path)
    assert free is not None and free >= 0
