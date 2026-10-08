"""An org that has left a box loses its data there, and only then its slot;
a box out of slots says so on its heartbeat."""

from __future__ import annotations

import contextlib
import logging
import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from alkera_cli.supervisor import org_events
from alkera_cli.supervisor.org_reap import (
    REAP_IDLE_SECONDS,
    ReapError,
    remove_org_data,
    remove_tree,
)
from alkera_cli.supervisor.org_rootfs import org_rootfs_path
from alkera_cli.supervisor.service import FileRoutingFeed, RouteEntry, Supervisor, Worker
from alkera_cli.supervisor.slots import MAX_SLOTS, Slot, SlotTable
from alkera_core.compute import box_logs
from alkera_core.compute.box_isolation import IsolationMechanism, IsolationReport
from freezegun import freeze_time

#: A box whose probe proved every mechanism but systemd: namespaced workers,
#: spawned directly.
NAMESPACED = IsolationReport(frozenset(IsolationMechanism) - {IsolationMechanism.SYSTEMD})


ORG_A = "8ef423bc-531e-4d2a-baa5-8bfb6e9e8863"
ORG_B = "be0a3393-082f-4acf-918d-6eac00904c9b"


@contextlib.contextmanager
def capture_logs() -> Iterator[list[dict[str, Any]]]:
    """The supervisor's events, as the fields each line carries."""
    seen: list[dict[str, Any]] = []

    class Keep(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            fields = getattr(record, "event_fields", {})
            seen.append({"event": record.msg, "log_level": record.levelname.lower(), **fields})

    log = logging.getLogger(org_events.LOGGER)
    keep, level = Keep(), log.level
    log.addHandler(keep)
    log.setLevel(logging.DEBUG)
    try:
        yield seen
    finally:
        log.removeHandler(keep)
        log.setLevel(level)


DAY = timedelta(days=1)
START = datetime(2026, 9, 1, 12, tzinfo=UTC)


def _org_data(orgs: Path, slot: Slot) -> tuple[Path, Path]:
    """An org root and a copied rootfs, the way a worker leaves them."""
    root = orgs / str(slot.index)
    (root / "work" / ".alkera" / "chats").mkdir(parents=True)
    (root / "work" / ".alkera" / "chats" / "t.jsonl").write_text("{}")
    (root / "home" / ".aws" / "sso" / "cache").mkdir(parents=True)
    (root / "home" / ".aws" / "sso" / "cache" / "token.json").write_text("{}")
    rootfs = org_rootfs_path(orgs, slot)
    (rootfs / "usr" / "bin").mkdir(parents=True)
    (rootfs / "usr" / "bin" / "sh").write_text("#!")
    return root, rootfs


class _Fleet:
    def __init__(self, table: SlotTable) -> None:
        self.table = table
        self.dead: set[str] = set()

    async def start(self, org_id: str) -> Worker:
        slot = self.table.assign(org_id)

        async def send(frame: Any) -> None:
            return None

        return Worker(slot=slot, alive=lambda: org_id not in self.dead, send=send)


def _supervisor(tmp_path: Path) -> tuple[Supervisor, _Fleet]:
    table = SlotTable(tmp_path / "state" / "slots.json")
    fleet = _Fleet(table)
    sup = Supervisor(
        api=None,  # type: ignore[arg-type]
        feed=FileRoutingFeed(tmp_path / "routing.json"),
        slots=table,
        orgs_root=tmp_path / "orgs",
        env={"ALKERA_ORG_WORKER_BUDGET": "64"},
        start_worker=fleet.start,
        isolation=NAMESPACED,
        idle_grace_seconds=60,
    )
    sup._remove_org = lambda orgs, slot: remove_org_data(orgs, slot, mountpoints=list)
    return sup, fleet


async def test_an_org_idle_past_the_reap_age_loses_its_data_then_its_slot(
    tmp_path: Path,
) -> None:
    sup, fleet = _supervisor(tmp_path)
    with freeze_time(START, real_asyncio=True) as frozen:
        await sup.reconcile([RouteEntry("a1", ORG_A), RouteEntry("b1", ORG_B)], now=0)
        slot_a = sup._slots.find(ORG_A)
        assert slot_a is not None
        root, rootfs = _org_data(tmp_path / "orgs", slot_a)
        # ORG_A leaves the box; its worker is let go after the grace.
        await sup.reconcile([RouteEntry("b1", ORG_B)], now=10)
        await sup.reconcile([RouteEntry("b1", ORG_B)], now=100)
        fleet.dead.add(ORG_A)

        frozen.move_to(START + DAY * 13)
        await sup.reconcile([RouteEntry("b1", ORG_B)], now=10_000)
        assert root.exists() and sup._slots.find(ORG_A) is not None

        frozen.move_to(START + DAY * 14 + timedelta(hours=2))
        with capture_logs() as logs:
            await sup.reconcile([RouteEntry("b1", ORG_B)], now=20_000)

    assert not root.exists() and not root.is_symlink() and not rootfs.exists()
    assert sup._slots.find(ORG_A) is None
    assert sup._slots.find(ORG_B) is not None
    assert SlotTable(tmp_path / "state" / "slots.json").find(ORG_A) is None, "persisted"
    reaped = [e for e in logs if e["event"] == box_logs.ORG_REAPED]
    assert [(e["slot"], e["org_id"]) for e in reaped] == [(slot_a.index, ORG_A)]


async def test_an_org_still_routed_here_is_never_reaped(tmp_path: Path) -> None:
    """An org whose chats all sleep keeps no worker, but its chats are still
    this box's: their data stays."""
    sup, _fleet = _supervisor(tmp_path)
    with freeze_time(START, real_asyncio=True) as frozen:
        asleep = [RouteEntry("a1", ORG_A, "asleep")]
        await sup.reconcile([RouteEntry("a1", ORG_A)], now=0)
        slot = sup._slots.find(ORG_A)
        assert slot is not None
        root, _ = _org_data(tmp_path / "orgs", slot)
        for day in range(1, 40):
            frozen.move_to(START + DAY * day)
            await sup.reconcile(asleep, now=day * 4000.0)
    assert root.exists() and sup._slots.find(ORG_A) is not None


async def test_the_last_routed_time_survives_a_supervisor_restart(tmp_path: Path) -> None:
    """Without it a box whose supervisor restarts more often than the reap age
    (every release) would never reap anything."""
    with freeze_time(START, real_asyncio=True) as frozen:
        sup, _fleet = _supervisor(tmp_path)
        await sup.reconcile([RouteEntry("a1", ORG_A)], now=0)
        slot = sup._slots.find(ORG_A)
        assert slot is not None
        root, _ = _org_data(tmp_path / "orgs", slot)
        frozen.move_to(START + timedelta(seconds=REAP_IDLE_SECONDS) + DAY)
        again, _ = _supervisor(tmp_path)
        await again.reconcile([], now=0)
    assert not root.exists() and again._slots.find(ORG_A) is None


async def test_a_slot_whose_data_could_not_be_removed_is_kept(tmp_path: Path) -> None:
    sup, fleet = _supervisor(tmp_path)
    fleet.dead.add(ORG_A)
    with freeze_time(START, real_asyncio=True) as frozen:
        await sup.reconcile([RouteEntry("a1", ORG_A)], now=0)
        slot = sup._slots.find(ORG_A)
        assert slot is not None
        root, _ = _org_data(tmp_path / "orgs", slot)
        mounted = str((tmp_path / "orgs" / str(slot.index) / "work").resolve())
        sup._remove_org = lambda orgs, s: remove_org_data(orgs, s, mountpoints=lambda: [mounted])
        frozen.move_to(START + timedelta(seconds=REAP_IDLE_SECONDS) + DAY)
        with capture_logs() as logs:
            await sup.reconcile([], now=10_000)
    assert root.exists() and sup._slots.find(ORG_A) == slot
    assert [e["slot"] for e in logs if e["event"] == box_logs.ORG_REAP_FAILED] == [slot.index]


async def test_a_unit_still_up_keeps_its_org_on_a_systemd_host(tmp_path: Path) -> None:
    sup, _fleet = _supervisor(tmp_path)
    sup._systemd = True
    sup._unit_state = lambda unit: "deactivating"
    with freeze_time(START, real_asyncio=True) as frozen:
        await sup.reconcile([RouteEntry("a1", ORG_A)], now=0)
        slot = sup._slots.find(ORG_A)
        assert slot is not None
        root, _ = _org_data(tmp_path / "orgs", slot)
        frozen.move_to(START + timedelta(seconds=REAP_IDLE_SECONDS) + DAY)
        sup._workers.clear()
        await sup.reconcile([], now=10_000)
    assert root.exists() and sup._slots.find(ORG_A) is not None


def test_the_removal_never_follows_a_link_out_of_the_tree(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("not the org's")
    tree = tmp_path / "orgs" / "3"
    (tree / "deep" / "deeper").mkdir(parents=True)
    (tree / "deep" / "deeper" / "f").write_text("x")
    (tree / "deep" / "to-dir").symlink_to(outside)
    (tree / "to-file").symlink_to(outside / "keep.txt")
    (tree / "dangling").symlink_to(tmp_path / "nowhere")

    remove_tree(tmp_path / "orgs", "3")

    assert not os.path.lexists(tree)
    assert (outside / "keep.txt").read_text() == "not the org's"


def test_a_root_that_is_itself_a_link_is_removed_as_a_link(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("x")
    (tmp_path / "orgs").mkdir()
    (tmp_path / "orgs" / "3").symlink_to(outside)
    remove_tree(tmp_path / "orgs", "3")
    assert not os.path.lexists(tmp_path / "orgs" / "3")
    assert (outside / "keep.txt").exists()


def test_a_mount_beneath_the_org_stops_the_removal(tmp_path: Path) -> None:
    table = SlotTable(tmp_path / "slots.json")
    slot = table.assign(ORG_A)
    root, _rootfs = _org_data(tmp_path / "orgs", slot)
    with pytest.raises(ReapError, match="mounted beneath"):
        remove_org_data(
            tmp_path / "orgs",
            slot,
            mountpoints=lambda: [str((root / "home").resolve())],
        )
    assert (root / "home" / ".aws").exists()


def test_an_id_mapped_rootfs_is_detached_before_its_directory_goes(tmp_path: Path) -> None:
    table = SlotTable(tmp_path / "slots.json")
    slot = table.assign(ORG_A)
    _root, rootfs = _org_data(tmp_path / "orgs", slot)
    mounted = {rootfs}
    detached: list[Path] = []

    def detach(path: Path) -> None:
        detached.append(path)
        mounted.discard(path)
        # What a detach leaves is the empty mountpoint.
        for child in sorted(path.rglob("*"), reverse=True):
            if child.is_file():
                child.unlink()
            else:
                child.rmdir()

    remove_org_data(
        tmp_path / "orgs", slot, is_mount=lambda p: p in mounted, detach=detach, mountpoints=list
    )
    assert detached == [rootfs] and not rootfs.exists()


def test_a_rootfs_still_mounted_is_never_walked(tmp_path: Path) -> None:
    """Removing through a bind of the shared staged rootfs would delete it for
    every org on the box."""
    table = SlotTable(tmp_path / "slots.json")
    slot = table.assign(ORG_A)
    _root, rootfs = _org_data(tmp_path / "orgs", slot)
    with pytest.raises(ReapError):
        remove_org_data(
            tmp_path / "orgs",
            slot,
            is_mount=lambda p: p == rootfs,
            detach=lambda p: None,
            mountpoints=lambda: [str(rootfs.resolve())],
        )
    assert (rootfs / "usr" / "bin" / "sh").exists()


async def test_a_box_out_of_slots_says_so_on_its_heartbeat(tmp_path: Path) -> None:
    sup, _fleet = _supervisor(tmp_path)
    table = sup._slots
    first = table.assign(ORG_A)
    for n in range(1, MAX_SLOTS):
        table._slots[n] = f"00000000-0000-4000-8000-{n:012d}"
    assert sup.resources()["org_slots_free"] == 0
    body = sup.heartbeat_body()
    assert body["resources"]["org_slots_free"] == 0  # type: ignore[index]
    # The budget keeps its meaning: placement reads 0 there as "not gated".
    assert body["resources"]["org_worker_capacity"] == 64  # type: ignore[index]

    with capture_logs() as logs:
        await sup.reconcile([RouteEntry("z1", "11111111-1111-4111-8111-111111111111")], now=0)
    assert [e["event"] for e in logs if e["event"] == box_logs.SLOTS_EXHAUSTED]

    table.release(first.org_id)
    assert sup.resources()["org_slots_free"] == 1
