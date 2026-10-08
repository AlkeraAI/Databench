"""What a box says about itself on every beat beyond its load, and what it hears back.

The machine loop owns the beat's rhythm and the platform's verdicts (a stale
instance, a refused credential, a reaped row). This module owns the box's own
readings, which do not depend on any of that:

* **resources**: the host sample (CPU, memory, disk, GPUs). ``nvidia-smi`` is a
  subprocess, so the sample is taken off the event loop.
* **capabilities**: what the box can do (it serves workspaces, and what its
  GPUs allow given the sandbox it starts chats in).
* **last activity**: when any chat on the box last did work, what an idle stop
  on an org machine is measured from.

And what the platform sends back: an org machine's card (its name, hardware,
billing), kept for each chat's agent to be told what it runs on.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime
from typing import Any, Final, TypedDict

from alkera_core.compute.box_contract import BoxCapability

from alkera_cli.box_capabilities import ClaimedBy, claimed_by
from alkera_cli.cloud.activity import ChatActivity
from alkera_cli.cloud.machine_card import MachineCardHolder
from alkera_cli.cloud.machine_resources import (
    LastActivity,
    gpu_capabilities,
    nvidia_devices_present,
    sample_resources,
)
from alkera_cli.harness.sandbox import SandboxSettings


class PulseFields(TypedDict):
    """The heartbeat fields the box's own readings fill."""

    resources: dict[str, Any] | None
    capabilities: list[str]
    last_activity_at: datetime | None


def _sandbox_mode() -> str:
    return SandboxSettings.from_env().mode


#: Every capability a beat from this module can carry: the hardware claims,
#: when the host has a GPU.
PULSE_CAPABILITIES: Final = claimed_by(ClaimedBy.HARDWARE)


class MachinePulse:
    """The readings a beat carries, and the machine card the beats bring back."""

    def __init__(
        self,
        *,
        machine_card: MachineCardHolder | None = None,
        last_activity: LastActivity | None = None,
        sample: Callable[[], dict[str, Any] | None] = sample_resources,
        sandbox_mode: Callable[[], str] = _sandbox_mode,
        nvidia_devices: Callable[[], bool] = nvidia_devices_present,
    ) -> None:
        #: Handed to every chat mirror the box serves, so its agent is told
        #: what it runs on; refreshed by the claim and heartbeat answers.
        self.machine_card = machine_card if machine_card is not None else MachineCardHolder()
        self._last_activity = last_activity if last_activity is not None else LastActivity()
        self._sample = sample
        self._sandbox_mode = sandbox_mode
        self._nvidia_devices = nvidia_devices

    def turn_ended(self) -> None:
        """A chat's turn just ended: the box did work now."""
        self._last_activity.note()

    def heard(self, answer: Mapping[str, Any] | None, *, carries_card: bool) -> None:
        """Take the card from a claim or heartbeat answer the platform sends a
        card on (a box with a machine credential); any other answer says
        nothing about the card and leaves it as it was."""
        if carries_card:
            self.machine_card.update(answer)

    async def fields(self, activities: Callable[[], Iterable[ChatActivity]]) -> PulseFields:
        """This beat's readings. ``activities`` says what each served chat is
        doing; it is asked after the sample, since chats come and go while the
        sample is taken off the loop."""
        resources = await asyncio.to_thread(self._sample)
        working = [a.holds_a_stop for a in activities()]
        return {
            "resources": resources,
            "capabilities": list(self.gpu_capabilities(resources)),
            "last_activity_at": self._last_activity.observe(working),
        }

    def gpu_capabilities(self, resources: Mapping[str, Any] | None) -> list[BoxCapability]:
        """What this box can do with its GPUs, from the sample just taken. The
        chat launcher does not start gVisor with ``--nvproxy``, so a gVisor box
        passes no GPU through; a box with no sandbox does when the host shows
        the device nodes."""
        gpus = (resources or {}).get("gpus") or []
        if not gpus:
            return []
        return gpu_capabilities(
            gpus,
            sandbox_mode=self._sandbox_mode(),
            nvidia_devices=self._nvidia_devices(),
            gvisor_nvproxy=False,
        )


__all__ = ["PULSE_CAPABILITIES", "MachinePulse", "PulseFields"]
