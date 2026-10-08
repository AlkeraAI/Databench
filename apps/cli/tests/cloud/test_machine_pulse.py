"""What a box's beat carries about itself, and the card it hears back.

The sample, the sandbox mode and the device check are injected, so each case
pins what the platform is told for one host shape without a GPU or a driver.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from alkera_cli.cloud.activity import ChatActivity
from alkera_cli.cloud.machine_card import MachineCardHolder
from alkera_cli.cloud.machine_pulse import MachinePulse
from alkera_cli.cloud.machine_resources import (
    LastActivity,
)
from alkera_core.compute.box_contract import BoxCapability
from alkera_core.schemas.compute import BoxMachineCard

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
ONE_GPU: dict[str, Any] = {"cpu_percent": 3.0, "gpus": [{"index": 0, "name": "NVIDIA A40"}]}
NO_GPU: dict[str, Any] = {"cpu_percent": 3.0, "gpus": []}
CARD = BoxMachineCard(
    name="Training box",
    gpu=None,
    vcpu=8,
    memory_gb=32,
    disk_gb=100,
    billed_per_minute=True,
    idle_stop_minutes=30,
)


def _pulse(
    sample: dict[str, Any] | None,
    *,
    mode: str = "none",
    devices: bool = True,
    clock: list[datetime] | None = None,
) -> MachinePulse:
    ticks = clock if clock is not None else [NOW]
    return MachinePulse(
        machine_card=MachineCardHolder(free_disk=lambda: None),
        last_activity=LastActivity(clock=lambda: ticks[0]),
        sample=lambda: sample,
        sandbox_mode=lambda: mode,
        nvidia_devices=lambda: devices,
    )


@pytest.mark.parametrize(
    ("sample", "mode", "devices", "expected"),
    [
        pytest.param(
            ONE_GPU,
            "none",
            True,
            [BoxCapability.GPU, BoxCapability.GPU_PASSTHROUGH],
            id="no-sandbox-with-device-nodes-passes-the-gpu-through",
        ),
        pytest.param(
            ONE_GPU,
            "none",
            False,
            [BoxCapability.GPU],
            id="no-device-nodes-has-a-gpu-it-cannot-pass",
        ),
        pytest.param(
            ONE_GPU,
            "gvisor",
            True,
            [BoxCapability.GPU],
            id="gvisor-without-nvproxy-passes-nothing",
        ),
        pytest.param(NO_GPU, "none", True, [], id="no-gpu"),
        pytest.param(None, "none", True, [], id="host-refused-to-say"),
    ],
)
async def test_the_beat_names_what_the_box_can_do(
    sample: dict[str, Any] | None, mode: str, devices: bool, expected: list[str]
) -> None:
    fields = await _pulse(sample, mode=mode, devices=devices).fields(list)
    assert fields["resources"] == sample
    assert fields["capabilities"] == expected


async def test_last_activity_is_stamped_only_while_a_chat_works() -> None:
    clock = [NOW]
    pulse = _pulse(NO_GPU, clock=clock)
    idle = [ChatActivity.IDLE, ChatActivity.AWAITING_USER]
    assert (await pulse.fields(lambda: idle))["last_activity_at"] is None
    assert (await pulse.fields(lambda: [ChatActivity.WORKING]))["last_activity_at"] == NOW
    clock[0] = NOW + timedelta(minutes=5)
    # A chat parked on a person is not work: the stamp stays where the work was.
    assert (await pulse.fields(lambda: idle))["last_activity_at"] == NOW
    pulse.turn_ended()
    assert (await pulse.fields(list))["last_activity_at"] == NOW + timedelta(minutes=5)


def test_a_card_is_taken_only_from_an_answer_that_carries_one() -> None:
    pulse = _pulse(NO_GPU)
    answer = {"card": CARD.model_dump(mode="json")}
    pulse.heard(answer, carries_card=False)
    assert pulse.machine_card.card is None
    pulse.heard(answer, carries_card=True)
    assert pulse.machine_card.card == CARD
    # An answer from a box that sends no card says nothing about it.
    pulse.heard({}, carries_card=False)
    assert pulse.machine_card.card == CARD
    # A carrying answer with no card clears it: the machine left the org.
    pulse.heard({}, carries_card=True)
    assert pulse.machine_card.card is None


async def test_the_chats_are_read_after_the_sample_not_before() -> None:
    # A chat that starts working while the sample is taken off the loop is
    # seen on this beat, and a dict of chats that changes meanwhile is fine.
    chats: dict[str, ChatActivity] = {}

    def sample() -> dict[str, Any]:
        chats["new"] = ChatActivity.WORKING
        return NO_GPU

    pulse = MachinePulse(
        last_activity=LastActivity(clock=lambda: NOW),
        sample=sample,
        sandbox_mode=lambda: "none",
        nvidia_devices=lambda: False,
    )
    assert (await pulse.fields(chats.values))["last_activity_at"] == NOW
