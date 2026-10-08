"""The agent's per-chat task DAG — the unified TODO system.

A chat-scoped list of tasks the ROOT agent uses to decompose, track, and verify
long-horizon work. Each task has a stable client-provided id (a slug), a title,
an optional description, a lifecycle ``status``, and ``depends_on`` edges to
other tasks — together a directed ACYCLIC graph. "Blocked" is *derived* (a task
is blocked while any prerequisite isn't ``completed``), never stored.

Persisted to ``<chat>/tasks.json`` via :class:`alkera_core.project.chats.tasks.TaskStore`.
Both models are :class:`VersionedChatModel` so a newer writer's fields survive an
older reader (CLAUDE.md — versioned persisted models).

The pure transform + validation lives here (``apply`` / ``validate_dag`` /
``blocked_by`` / ``ready``) so it is unit-testable with plain data, no I/O.
Model-facing *time* formatting (relative "5m ago") happens at the tool boundary,
not here — this layer keeps the real datetimes.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, ClassVar, Literal

from pydantic import Field

from alkera_core.schemas.chat.base import VersionedChatModel

TaskStatus = Literal["pending", "in_progress", "completed", "cancelled"]
"""A task's lifecycle. ``blocked`` is NOT a status — it's derived from
``depends_on`` (a task is blocked while a prerequisite isn't ``completed``)."""

#: A prerequisite CLEARS its dependent only when ``completed`` — a ``cancelled``
#: prerequisite deliberately keeps a dependent blocked so the model notices and
#: either drops the edge or cancels the dependent too.
_CLEARED: frozenset[str] = frozenset({"completed"})

#: A task that is itself ``completed`` or ``cancelled`` is done/abandoned — it is
#: never reported as "blocked" regardless of its prerequisites, so every surface
#: (the JSON result, the cards, the reminder checklist) agrees.
_TERMINAL: frozenset[str] = frozenset({"completed", "cancelled"})

#: A task id is a filesystem-safe, human-readable slug: starts alphanumeric, then
#: only ``[A-Za-z0-9._-]``, ≤64 chars. Keeps ids readable in the checklist and
#: safe to render anywhere; the model picks semantic slugs ("create-schema").
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

#: Three-color DFS marks for cycle detection (white=unseen, grey=on-stack, black=done).
_WHITE, _GREY, _BLACK = 0, 1, 2

#: Status glyphs for the human/agent-facing checklist (reminder + TUI card).
_GLYPH: dict[str, str] = {
    "pending": "[ ]",
    "in_progress": "[~]",
    "completed": "[x]",
    "cancelled": "[-]",
}


def _dedup(items: list[str]) -> list[str]:
    """Dependency ids deduplicated, first-seen order preserved — so a repeated id
    in ``depends_on`` doesn't leak a redundant blocker into the model-facing view.
    Also returns a fresh list (never aliases the caller's)."""
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


class TaskValidationError(ValueError):
    """A model-visible task-mutation error (bad slug, unknown dep, cycle, …).

    Raised by :meth:`TaskList.apply`; the ``manage_tasks`` tool turns it into a
    clean error result so the model self-corrects instead of crashing the turn.
    """


class Task(VersionedChatModel):
    """One node in the task DAG. Every field defaulted so a partial/corrupt
    record degrades gracefully rather than bricking the list."""

    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    id: str = ""
    """Stable client-provided slug, unique within the list (see ``_ID_RE``)."""
    title: str = ""
    description: str = ""
    status: TaskStatus = "pending"
    depends_on: list[str] = Field(default_factory=list)
    """Ids of prerequisite tasks. Forms the DAG; cycles are rejected."""
    created_at: datetime | None = None
    updated_at: datetime | None = None


class TaskList(VersionedChatModel):
    """The whole per-chat task DAG. The persisted unit (``tasks.json``)."""

    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    tasks: list[Task] = Field(default_factory=list)

    # --- queries (pure) ------------------------------------------------

    def by_id(self) -> dict[str, Task]:
        """Tasks keyed by id (last-wins on a malformed duplicate from disk)."""
        return {t.id: t for t in self.tasks}

    def blocked_by(self, task_id: str) -> list[str]:
        """The prerequisite ids of ``task_id`` that aren't yet ``completed`` —
        empty ⇒ not blocked. A task that is itself terminal (completed/cancelled)
        is never blocked. Unknown deps would be reported as blockers (they can't
        be cleared), but a valid list never has them (``validate_dag``)."""
        index = self.by_id()
        task = index.get(task_id)
        if task is None or task.status in _TERMINAL:
            return []
        return [
            dep
            for dep in task.depends_on
            if (dep not in index) or (index[dep].status not in _CLEARED)
        ]

    def is_blocked(self, task_id: str) -> bool:
        return bool(self.blocked_by(task_id))

    def ready(self) -> list[Task]:
        """Pending tasks with every prerequisite ``completed`` — the work the
        agent can start right now."""
        return [t for t in self.tasks if t.status == "pending" and not self.blocked_by(t.id)]

    def display_order(self) -> list[Task]:
        """Tasks in reading order for every model/human-facing surface: still-active
        tasks (pending / in_progress) first, finished ones (completed / cancelled)
        at the end — each group in stable insertion order. Storage (``self.tasks``)
        stays in canonical insertion order; this is the view, so the upcoming work
        is always on top and done items don't bury it."""
        active = [t for t in self.tasks if t.status not in _TERMINAL]
        done = [t for t in self.tasks if t.status in _TERMINAL]
        return [*active, *done]

    def status_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {"pending": 0, "in_progress": 0, "completed": 0, "cancelled": 0}
        for t in self.tasks:
            counts[t.status] = counts.get(t.status, 0) + 1
        return counts

    def render_lines(self) -> list[str]:
        """Compact checklist rows for the model-facing reminder / TUI card, e.g.
        ``[~] create-schema — Create staging schema`` (+ ``(blocked by: …)``).
        No timestamps (time is rendered relative at the tool boundary)."""
        lines: list[str] = []
        for t in self.display_order():
            glyph = _GLYPH.get(t.status, "[ ]")
            row = f"{glyph} {t.id} — {t.title}" if t.title else f"{glyph} {t.id}"
            blockers = self.blocked_by(t.id)  # already [] for a terminal task
            if blockers:
                row += f"  (blocked by: {', '.join(blockers)})"
            lines.append(row)
        return lines

    def is_empty(self) -> bool:
        return not self.tasks

    # --- validation (pure) ---------------------------------------------

    def validate_dag(self) -> None:
        """Raise :class:`TaskValidationError` if the list isn't a valid DAG:
        duplicate id, bad slug, self-dependency, dependency on a missing id, or
        a cycle (the message names the cycle)."""
        seen: set[str] = set()
        for t in self.tasks:
            if not _ID_RE.match(t.id):
                raise TaskValidationError(
                    f"invalid task id {t.id!r}: use letters, digits, '.', '_', '-' "
                    "(start alphanumeric, ≤64 chars)"
                )
            if t.id in seen:
                raise TaskValidationError(f"duplicate task id {t.id!r}")
            seen.add(t.id)
        index = self.by_id()
        for t in self.tasks:
            for dep in t.depends_on:
                if dep == t.id:
                    raise TaskValidationError(f"task {t.id!r} cannot depend on itself")
                if dep not in index:
                    raise TaskValidationError(f"task {t.id!r} depends on unknown task {dep!r}")
        cycle = self._find_cycle(index)
        if cycle is not None:
            raise TaskValidationError("dependency cycle: " + " -> ".join(cycle))

    @staticmethod
    def _find_cycle(index: Mapping[str, Task]) -> list[str] | None:
        """DFS for a back-edge; returns the cycle path (``a -> b -> a``) or None.
        Three-color marking (``_WHITE``/``_GREY``/``_BLACK``) over a bounded
        task graph."""
        color: dict[str, int] = dict.fromkeys(index, _WHITE)
        stack: list[str] = []

        def visit(node: str) -> list[str] | None:
            color[node] = _GREY
            stack.append(node)
            for dep in index[node].depends_on:
                if dep not in color:
                    continue  # unknown dep already rejected by validate_dag
                if color[dep] == _GREY:
                    # Back-edge → cycle from dep's position to here + back to dep.
                    start = stack.index(dep)
                    return [*stack[start:], dep]
                if color[dep] == _WHITE:
                    found = visit(dep)
                    if found is not None:
                        return found
            color[node] = _BLACK
            stack.pop()
            return None

        for node in index:
            if color[node] == _WHITE:
                found = visit(node)
                if found is not None:
                    return found
        return None

    # --- transform (pure) ----------------------------------------------

    def apply(
        self,
        *,
        upsert: Sequence[Mapping[str, Any]] = (),
        delete: Sequence[str] = (),
        clear: bool = False,
        now: datetime,
    ) -> TaskList:
        """Return a NEW validated ``TaskList`` after applying, in order,
        ``clear`` → ``delete`` → ``upsert``. Pure (no I/O); ``now`` stamps
        ``created_at``/``updated_at`` so it's injectable for tests.

        ``upsert`` items are partial: an existing id updates only the provided
        fields; a new id creates a task (``title`` required). Raises
        :class:`TaskValidationError` on any invariant break (the result is never
        persisted on failure)."""
        index: dict[str, Task] = {} if clear else {t.id: t for t in self.tasks}
        order: list[str] = [] if clear else [t.id for t in self.tasks]

        for dead in delete:
            if index.pop(dead, None) is not None and dead in order:
                order.remove(dead)

        for raw in upsert:
            fields = dict(raw)
            task_id = str(fields.get("id", "")).strip()
            if not task_id:
                raise TaskValidationError("each upsert item needs a non-empty 'id'")
            if not _ID_RE.match(task_id):
                raise TaskValidationError(
                    f"invalid task id {task_id!r}: use letters, digits, '.', '_', '-' "
                    "(start alphanumeric, ≤64 chars)"
                )
            existing = index.get(task_id)
            if existing is None:
                if not str(fields.get("title", "")).strip():
                    raise TaskValidationError(f"new task {task_id!r} needs a 'title'")
                index[task_id] = Task(
                    id=task_id,
                    title=str(fields.get("title", "")),
                    description=str(fields.get("description", "")),
                    status=fields.get("status", "pending"),
                    depends_on=_dedup(fields.get("depends_on", []) or []),
                    created_at=now,
                    updated_at=now,
                )
                order.append(task_id)
            else:
                update: dict[str, Any] = {"updated_at": now}
                for key in ("title", "description", "status", "depends_on"):
                    if key in fields and fields[key] is not None:
                        # Copy + dedupe the dep list so the stored Task never aliases
                        # the caller's list and the model-facing view has no dupes.
                        update[key] = _dedup(fields[key]) if key == "depends_on" else fields[key]
                index[task_id] = existing.model_copy(update=update)

        result = TaskList(tasks=[index[tid] for tid in order])
        result.validate_dag()
        return result


__all__ = ["Task", "TaskList", "TaskStatus", "TaskValidationError"]
