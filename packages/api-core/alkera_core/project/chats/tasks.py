"""Per-chat task DAG persistence — ``<chat>/tasks.json``.

A tiny atomic read-modify-write store over a single :class:`TaskList` document
(the materialized current state — every consumer wants the full list, so an
event log would only add replay cost). One ``TaskStore`` instance lives on the
session's tool binding; an ``asyncio.Lock`` serializes concurrent same-turn
mutations (OpenCode dispatches a turn's tool calls in parallel — Claude doesn't),
and ``write_json_atomic`` makes each write crash-safe. Cross-process safety is
already guaranteed by the chat ``.lock`` (one mutator process per chat).

Reads never raise: a missing or corrupt file yields an empty list so a chat can
always proceed. Mutations validate the DAG *before* writing, so a rejected
mutation leaves the on-disk list byte-identical.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from alkera_core.atomic_io import write_json_atomic
from alkera_core.schemas.chat.tasks import Task, TaskList


class TaskStore:
    """Atomic, in-process-serialized store for one chat's ``tasks.json``."""

    def __init__(self, tasks_path: Path) -> None:
        self._path = Path(tasks_path)
        self._lock = asyncio.Lock()

    @property
    def path(self) -> Path:
        return self._path

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def _read_sync(self) -> TaskList:
        """Parse the on-disk list, or the best salvage if partially corrupt. Never
        raises — a broken file (any ``OSError`` on read, malformed JSON, or a bad
        node) must not stop the chat."""
        try:
            raw = self._path.read_bytes()
        except FileNotFoundError:
            return TaskList()
        except OSError:
            # A clobbered mode / a tasks.json/ directory / a transient I/O error —
            # the reminder is best-effort, so never let it wedge a turn.
            return TaskList()
        try:
            data: Any = json.loads(raw)
        except json.JSONDecodeError:
            return TaskList()
        try:
            return TaskList.model_validate(data)
        except ValidationError:
            # One unparseable task must NOT discard the rest (that loss would then
            # be persisted by the next mutation). Degrade PER TASK: keep every node
            # that still validates, drop only the bad ones, and preserve any
            # top-level fields a newer writer added.
            return self._salvage(data)

    @staticmethod
    def _salvage(data: Any) -> TaskList:
        """Reconstruct a ``TaskList`` from a doc whose full validation failed,
        keeping only the tasks that individually validate. Returns an empty list
        if the shell itself is unusable."""
        if not isinstance(data, dict) or not isinstance(data.get("tasks"), list):
            return TaskList()
        good: list[dict[str, Any]] = []
        for node in data["tasks"]:
            try:
                Task.model_validate(node)
            except ValidationError:
                continue
            good.append(node)
        salvaged = {**data, "tasks": good}
        try:
            return TaskList.model_validate(salvaged)
        except ValidationError:
            return TaskList(tasks=[Task.model_validate(node) for node in good])

    async def load(self) -> TaskList:
        """The current task list (empty when none/corrupt)."""
        return await asyncio.to_thread(self._read_sync)

    # ------------------------------------------------------------------
    # Mutate
    # ------------------------------------------------------------------

    async def apply(
        self,
        *,
        upsert: Sequence[Mapping[str, Any]] = (),
        delete: Sequence[str] = (),
        clear: bool = False,
        now: datetime | None = None,
    ) -> TaskList:
        """Apply ``clear`` → ``delete`` → ``upsert`` under the lock and persist
        atomically. Returns the new list. Raises ``TaskValidationError`` (from
        :meth:`TaskList.apply`) on a bad mutation — nothing is written then."""
        stamp = now or datetime.now(UTC)
        async with self._lock:
            current = await asyncio.to_thread(self._read_sync)
            updated = current.apply(upsert=upsert, delete=delete, clear=clear, now=stamp)
            await asyncio.to_thread(write_json_atomic, self._path, updated.model_dump(mode="json"))
            return updated


__all__ = ["TaskStore"]
