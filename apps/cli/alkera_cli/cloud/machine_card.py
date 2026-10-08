"""What the agent is told about the machine it runs on.

An org machine's box learns its own card from the platform: the claim answer
carries it and every heartbeat answer refreshes it (``card``, the
``BoxMachineCard`` wire shape: name, GPU, vCPU, memory, disk, whether it is
billed per minute, its idle stop). The card becomes one sentence or two in
the hidden context of a chat's turn, so the agent knows what hardware it has
(and that idle time on a billed machine is not free) without the person
having to say:

    You are running on Training box: 2x A100 80GB (80 GB), 32 vCPU, 250 GB
    memory, 412 GB free disk. This machine is billed per minute, and stops
    after 30 minutes idle.

No price figure is ever rendered; only the fact of per-minute billing. A box
in the shared pool has no card, and its chats are told nothing new.

:class:`MachineCardHolder` keeps the latest card the platform sent and counts
its revisions; a mirror hands the note to the agent on the first turn it
serves and again only after the card changed, so the free disk (read live at
render time) never makes it repeat itself every turn.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import psutil
from alkera_core.schemas.compute import BoxMachineCard
from pydantic import ValidationError

from alkera_cli.cloud.machine_resources import WORK_DIR

_GIB = 1024**3


def card_from_answer(answer: Mapping[str, Any] | None) -> BoxMachineCard | None:
    """The card in a claim or heartbeat answer, or ``None`` when it carries
    none (a shared-pool box, a backend that predates cards) or one this build
    cannot read, which is told nothing rather than something wrong."""
    if not isinstance(answer, Mapping):
        return None
    raw = answer.get("card")
    if not isinstance(raw, Mapping):
        return None
    try:
        card = BoxMachineCard.model_validate(raw)
    except ValidationError:
        return None
    return card if card.name.strip() else None


def render_machine_card(card: BoxMachineCard, *, disk_free_gb: int | None) -> str:
    """The note for ``card``. ``disk_free_gb`` is the work volume's free space
    now; ``None`` (unreadable) falls back to the card's disk size."""
    specs: list[str] = []
    if card.gpu is not None and card.gpu.count > 0:
        memory = f" ({card.gpu.memory_gb} GB)" if card.gpu.memory_gb > 0 else ""
        specs.append(f"{card.gpu.count}x {card.gpu.name}{memory}")
    if card.vcpu > 0:
        specs.append(f"{card.vcpu} vCPU")
    if card.memory_gb > 0:
        specs.append(f"{card.memory_gb} GB memory")
    if disk_free_gb is not None:
        specs.append(f"{disk_free_gb} GB free disk")
    elif card.disk_gb > 0:
        specs.append(f"{card.disk_gb} GB disk")
    first = f"You are running on {card.name.strip()}"
    first += f": {', '.join(specs)}." if specs else "."
    idle = card.idle_stop_minutes
    if card.billed_per_minute and idle:
        second = f"This machine is billed per minute, and stops after {idle} minutes idle."
    elif card.billed_per_minute:
        second = "This machine is billed per minute."
    elif idle:
        second = f"This machine stops after {idle} minutes idle."
    else:
        second = ""
    return f"{first} {second}".strip()


def work_volume_free_gb(work_dir: Path = WORK_DIR) -> int | None:
    """Free space on the work volume in whole GB, or ``None`` when it cannot
    be read (no volume mounted reads the root filesystem's)."""
    try:
        usage = psutil.disk_usage(str(work_dir if work_dir.is_dir() else Path("/")))
    except (OSError, RuntimeError):
        return None
    return int(usage.free // _GIB)


class MachineCardHolder:
    """The latest card the platform sent this box, and how many times it changed.

    ``revision`` moves only when the card itself changes, so a mirror that
    remembers the revision it last told the agent repeats the note only when
    there is something new to say."""

    def __init__(self, *, free_disk: Callable[[], int | None] = work_volume_free_gb) -> None:
        self._card: BoxMachineCard | None = None
        self._revision = 0
        self._free_disk = free_disk

    @property
    def revision(self) -> int:
        return self._revision

    @property
    def card(self) -> BoxMachineCard | None:
        return self._card

    def update(self, answer: Mapping[str, Any] | None) -> None:
        """Take the card from a claim or heartbeat answer. An answer with no
        card on a box that had one clears it: the machine left the org's
        hands (a backend that sends no card never had given one)."""
        card = card_from_answer(answer)
        if card != self._card:
            self._card = card
            self._revision += 1

    def note(self) -> str | None:
        """The note for the agent now, or ``None`` for a box with no card."""
        if self._card is None:
            return None
        return render_machine_card(self._card, disk_free_gb=self._free_disk())


class MachineNote:
    """One chat's view of the holder: the note it has not told its agent yet.

    The first turn a mirror serves gets the note; later turns get nothing until
    the platform sends a changed card (a rename, a new idle stop), when the
    next turn gets the new note once. With no holder (a mirror nobody handed
    the box's card) there is never anything to tell."""

    def __init__(self, holder: MachineCardHolder | None) -> None:
        self._holder = holder
        self._told: int | None = None

    def unsaid(self) -> str | None:
        """The note, if this revision of the card has not been told yet."""
        if self._holder is None or self._holder.revision == self._told:
            return None
        self._told = self._holder.revision
        return self._holder.note()


__all__ = [
    "MachineCardHolder",
    "MachineNote",
    "card_from_answer",
    "render_machine_card",
    "work_volume_free_gb",
]
