"""Which org holds which slot on this box, and what a slot owns.

A slot is an index the supervisor hands an org the first time the org has a
chat here. Slot ``n`` owns, for as long as its data root exists:

* a host uid/gid range ``[ORG_UID_BASE + n * ORG_UID_SPAN, ... + ORG_UID_SPAN)``,
  which the org's user namespace maps from its own ``0..ORG_UID_SPAN``; the
  worker runs as the first id of it and every chat uid it makes lands inside it;
* its data root ``<orgs root>/<n>``;
* a /30 of :data:`ORG_NET` for the link between the host and its network
  namespace.

Ranges never overlap and a slot is never handed to another org while its data
root exists, so a uid, a file or an address can never belong to two orgs.

The table holds org ids, slot numbers and when each org last had a chat routed
here (to the hour), and nothing else, in a root-owned 0600 file: it is the
supervisor's, and a worker never reads it.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Final

from alkera_cli.org_slot import (
    MAX_SLOTS,
    ORG_NET,
    ORG_UID_BASE,
    ORG_UID_SPAN,
    ORG_VETH_PREFIX,
    Slot,
    SlotError,
    canonical_org,
)

#: How stale a slot's last-routed time may get before it is written again.
SEEN_RESOLUTION: Final = 3600.0


class SlotTable:
    """The persisted ``slot -> org`` table. Every change is written whole,
    owner-only, before it is used."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._seen: dict[int, float] = {}
        self._slots: dict[int, str] = self._load()

    def _load(self) -> dict[int, str]:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            # Never guessed around: a slot handed out twice is two orgs on one
            # uid range. The supervisor stops until an operator looks.
            raise SlotError(f"the slot table {self._path} cannot be read: {exc}") from exc
        if not isinstance(raw, Mapping) or not isinstance(raw.get("slots"), Mapping):
            raise SlotError(f"the slot table {self._path} is malformed")
        slots: dict[int, str] = {}
        for key, org in raw["slots"].items():
            index = int(key)
            if not 0 <= index < MAX_SLOTS or not isinstance(org, str):
                raise SlotError(f"the slot table {self._path} names a bad slot {key!r}")
            org = canonical_org(org)
            if org in slots.values():
                raise SlotError(f"the slot table {self._path} gives {org} two slots")
            slots[index] = org
        seen = raw.get("seen")
        for key, at in (seen if isinstance(seen, Mapping) else {}).items():
            if str(key).isdigit() and isinstance(at, int | float):
                self._seen[int(key)] = float(at)
        return slots

    def _save(self) -> None:
        payload = {
            "slots": {str(i): org for i, org in sorted(self._slots.items())},
            "seen": {str(i): at for i, at in sorted(self._seen.items()) if i in self._slots},
        }
        self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = self._path.with_name(f".{self._path.name}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, self._path)

    def slots(self) -> tuple[Slot, ...]:
        return tuple(Slot(i, org) for i, org in sorted(self._slots.items()))

    def find(self, org_id: str) -> Slot | None:
        org = canonical_org(org_id)
        for index, held in self._slots.items():
            if held == org:
                return Slot(index, org)
        return None

    def assign(self, org_id: str) -> Slot:
        """The org's slot, giving it the lowest free one on first sight."""
        found = self.find(org_id)
        if found is not None:
            return found
        org = canonical_org(org_id)
        free = next((i for i in range(MAX_SLOTS) if i not in self._slots), None)
        if free is None:
            raise SlotError("every slot on this box is taken")
        self._slots[free] = org
        self._save()
        return Slot(free, org)

    def free(self) -> int:
        """How many more orgs this box can give a slot."""
        return MAX_SLOTS - len(self._slots)

    def seen_at(self, slot: Slot) -> float | None:
        """When the slot's org last had a chat routed here (wall clock)."""
        return self._seen.get(slot.index)

    def touch(self, slot: Slot, at: float) -> None:
        if at - self._seen.get(slot.index, float("-inf")) >= SEEN_RESOLUTION:
            self._seen[slot.index] = at
            self._save()

    def release(self, org_id: str) -> None:
        """Give the slot back. Only once the slot's data root is gone: a slot
        whose files remain still owns their ids."""
        found = self.find(org_id)
        if found is not None:
            del self._slots[found.index]
            self._seen.pop(found.index, None)
            self._save()


__all__ = [
    "MAX_SLOTS",
    "ORG_NET",
    "ORG_UID_BASE",
    "ORG_UID_SPAN",
    "ORG_VETH_PREFIX",
    "Slot",
    "SlotError",
    "SlotTable",
    "canonical_org",
]
