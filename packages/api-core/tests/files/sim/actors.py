"""Toy actors that exercise the harness before the real Files services exist.

Two pairs, each in a correct and a buggy variant, because a simulation harness
that has never caught a bug is not known to work:

* a counter incrementer that loses an update when it does not lock the row;
* a folder lease holder that keeps writing after its lease was reaped when it
  does not re-check the epoch inside the write transaction.
"""

from __future__ import annotations

from typing import Any

from scheduler import Sim
from store_model import ModelStore

COUNTERS = "counters"
COUNTER_KEY = "c"
LEASES = "leases"
LEASE_KEY = "folder"
WRITE_LOG = "write_log"
LOG_KEY = "log"


async def counter_incrementer(sim: Sim, store: ModelStore, name: str, *, locked: bool) -> None:
    """Read-modify-write a counter; without the row lock this loses updates."""
    tx = store.begin(name)
    await sim.step("begin")
    if locked:
        await tx.lock(COUNTERS, COUNTER_KEY)
    row = await tx.read(COUNTERS, COUNTER_KEY)
    value = 0 if row is None else int(row["value"])
    await sim.step("read")
    await tx.write(COUNTERS, COUNTER_KEY, {"value": value + 1})
    await tx.commit()


def counter_value(store: ModelStore) -> int:
    """The committed counter, or 0 if nobody wrote it."""
    row = store.committed(COUNTERS, COUNTER_KEY)
    return 0 if row is None else int(row["value"])


async def lease_holder(
    sim: Sim,
    store: ModelStore,
    name: str,
    *,
    fenced: bool,
    ttl: float,
    writes: int,
) -> None:
    """Acquire the folder lease, then heartbeat and write under it.

    `fenced=True` is the correct implementation: the heartbeat and every write
    are conditional on still holding the epoch acquired, so a holder that was
    paused past the TTL and reaped stops instead of writing behind a newer
    holder. `fenced=False` is the bug this harness must find.
    """
    epoch = await _acquire(sim, store, name, ttl)
    for _ in range(writes):
        await sim.sleep(1.0)
        await sim.step("work")
        tx = store.begin(name)
        await tx.lock(LEASES, LEASE_KEY)
        row = await tx.read(LEASES, LEASE_KEY)
        if fenced and not _still_holds(row, name, epoch):
            await tx.rollback()
            return
        await tx.write(
            LEASES,
            LEASE_KEY,
            {"holder": name, "epoch": epoch, "expires_at": sim.time + ttl},
        )
        log = await tx.read(WRITE_LOG, LOG_KEY)
        entries: list[dict[str, Any]] = list(log["entries"]) if log is not None else []
        entries.append({"epoch": epoch, "holder": name})
        await tx.write(WRITE_LOG, LOG_KEY, {"entries": entries})
        await tx.commit()


async def lease_reaper(sim: Sim, store: ModelStore, *, rounds: int) -> None:
    """Expire a lease whose holder stopped heartbeating, so a new one can take it."""
    for _ in range(rounds):
        await sim.sleep(1.0)
        tx = store.begin("reaper")
        await tx.lock(LEASES, LEASE_KEY)
        row = await tx.read(LEASES, LEASE_KEY)
        if row is not None and row["holder"] is not None and float(row["expires_at"]) <= sim.time:
            await tx.write(LEASES, LEASE_KEY, {**row, "holder": None})
            await tx.commit()
        else:
            await tx.rollback()


async def _acquire(sim: Sim, store: ModelStore, name: str, ttl: float) -> int:
    while True:
        tx = store.begin(name)
        await tx.lock(LEASES, LEASE_KEY)
        row = await tx.read(LEASES, LEASE_KEY)
        free = row is None or row["holder"] is None or float(row["expires_at"]) <= sim.time
        if free:
            epoch = 1 if row is None else int(row["epoch"]) + 1
            await tx.write(
                LEASES,
                LEASE_KEY,
                {"holder": name, "epoch": epoch, "expires_at": sim.time + ttl},
            )
            await tx.commit()
            return epoch
        await tx.rollback()
        await sim.sleep(1.0)


def _still_holds(row: dict[str, Any] | None, name: str, epoch: int) -> bool:
    return row is not None and row["holder"] == name and int(row["epoch"]) == epoch


def write_log(store: ModelStore) -> list[dict[str, Any]]:
    """Every write that landed, in commit order."""
    row = store.committed(WRITE_LOG, LOG_KEY)
    entries: list[dict[str, Any]] = list(row["entries"]) if row is not None else []
    return entries


def assert_single_writer_per_epoch(store: ModelStore) -> None:
    """Invariant 15: epochs never go backwards and an epoch has one holder."""
    entries = write_log(store)
    epochs = [int(e["epoch"]) for e in entries]
    if epochs != sorted(epochs):
        msg = f"a write at a stale epoch landed after a newer one: {entries}"
        raise AssertionError(msg)
    holders: dict[int, str] = {}
    for entry in entries:
        epoch = int(entry["epoch"])
        holder = str(entry["holder"])
        if holders.setdefault(epoch, holder) != holder:
            msg = f"epoch {epoch} was written by two holders: {entries}"
            raise AssertionError(msg)
