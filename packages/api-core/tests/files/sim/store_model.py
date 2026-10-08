"""An in-memory, Postgres-shaped store for offline simulation runs.

Tables are dicts of rows, a transaction sees committed rows plus its own
uncommitted writes (READ COMMITTED), `lock` is `SELECT … FOR UPDATE` with the
wait modelled as scheduler steps, and `commit` publishes the overlay in one
indivisible move. Every transaction boundary also calls the same
`Checkpoints` hook the production code calls, so the actors written against
this model run unchanged against real Postgres later — the interface is the
part that has to survive, not the storage.
"""

from __future__ import annotations

from typing import Any

from alkera_core.files.checkpoints import Checkpoints, NoopCheckpoints
from scheduler import Sim

Row = dict[str, Any]

MAX_LOCK_WAITS = 1000
"""A lock nobody releases is a livelock in an actor; fail instead of spinning."""


class ModelStore:
    """Committed state plus the row locks transactions hold against it."""

    def __init__(self, sim: Sim, *, checkpoints: Checkpoints | None = None) -> None:
        self.sim = sim
        self.checkpoints: Checkpoints = (
            checkpoints if checkpoints is not None else NoopCheckpoints()
        )
        self._tables: dict[str, dict[str, Row]] = {}
        self._locks: dict[tuple[str, str], str] = {}

    def begin(self, actor: str) -> Tx:
        """Open a transaction for `actor`."""
        return Tx(self, actor)

    def committed(self, table: str, key: str) -> Row | None:
        """The committed row, as every other transaction would read it."""
        row = self._tables.get(table, {}).get(key)
        return None if row is None else dict(row)

    def rows(self, table: str) -> dict[str, Row]:
        """A copy of a whole committed table."""
        return {key: dict(row) for key, row in self._tables.get(table, {}).items()}

    def _publish(self, writes: dict[tuple[str, str], Row]) -> None:
        for (table, key), row in writes.items():
            self._tables.setdefault(table, {})[key] = dict(row)


class Tx:
    """One transaction: an overlay of writes plus the locks it holds."""

    def __init__(self, store: ModelStore, actor: str) -> None:
        self.store = store
        self.actor = actor
        self.open = True
        self._writes: dict[tuple[str, str], Row] = {}
        self._held: set[tuple[str, str]] = set()

    async def lock(self, table: str, key: str) -> None:
        """Take the row lock, waiting (as scheduler steps) until it is free."""
        self._require_open()
        slot = (table, key)
        waits = 0
        while True:
            holder = self.store._locks.get(slot)
            if holder is None or holder == self.actor:
                break
            waits += 1
            if waits > MAX_LOCK_WAITS:
                msg = f"{self.actor} waited {waits} steps for {slot} held by {holder}"
                raise AssertionError(msg)
            await self.store.sim.step("lock_wait")
        self.store._locks[slot] = self.actor
        self._held.add(slot)
        await self.store.checkpoints.reach("files.sim.locked")

    async def read(self, table: str, key: str) -> Row | None:
        """This transaction's own write if it has one, else the committed row."""
        self._require_open()
        slot = (table, key)
        if slot in self._writes:
            return dict(self._writes[slot])
        return self.store.committed(table, key)

    async def write(self, table: str, key: str, row: Row) -> None:
        """Stage a row; nobody else can see it until `commit`."""
        self._require_open()
        self._writes[(table, key)] = dict(row)

    async def commit(self) -> None:
        """Publish every staged row at once and release the locks."""
        self._require_open()
        await self.store.sim.step("commit")
        await self.store.checkpoints.reach("files.sim.before_commit")
        self.store._publish(self._writes)
        self._finish()

    async def rollback(self) -> None:
        """Drop every staged row and release the locks."""
        self._require_open()
        self._finish()

    def _finish(self) -> None:
        for slot in self._held:
            if self.store._locks.get(slot) == self.actor:
                del self.store._locks[slot]
        self._held.clear()
        self._writes.clear()
        self.open = False

    def _require_open(self) -> None:
        if not self.open:
            msg = f"{self.actor} used a transaction that is already finished"
            raise AssertionError(msg)
