"""The per-turn notebook digest: what others did in the agent's notebooks since
its last turn, as structure only.

The digest rides every turn of a chat that has touched a notebook, so it is
read by a model that may act on it. It therefore carries only structure: who
(a display name reduced to printable text), which cell (by name and id), what
happened (edited, ran, deleted), a run's statuses, an error's class, a kernel
restart's reason, and which cells are now stale. It never carries a cell's
source or anything a cell printed: an output is the one place a stranger's
words can reach the agent, and the digest is not where it reads them.

Bounds: at most :data:`PER_NOTEBOOK_CHARS` per notebook and
:data:`TOTAL_CHARS` in all. What does not fit is counted on an overflow line,
so nothing that happened is silently dropped: it is either shown or counted.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

from alkera_notebook.tools.port import Activity, ActivityItem, NotebookHost
from alkera_notebook.tools.untrusted import display_name, safe_ename

logger = logging.getLogger(__name__)

PER_NOTEBOOK_CHARS = 1_500
TOTAL_CHARS = 4_000

HEADER = (
    "Notebook activity by others since your last turn "
    "(structure only; read a notebook for details):"
)

_CELL_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_CELL_ID = re.compile(r"^[0-9a-hjkmnp-tv-z]{10}$")
_WORD = re.compile(r"^[a-z][a-z_]{0,31}$")

_VERBS: dict[str, str] = {
    "edit": "edited",
    "insert": "added",
    "delete": "deleted",
    "restore": "restored",
    "move": "moved",
    "rename": "renamed",
    "kind": "changed the kind of",
}


def _cell(cell_id: str | None, cell_name: str | None) -> str:
    """A cell as ``name (id)``, from names and ids that pass their patterns only."""
    cid = cell_id if cell_id and _CELL_ID.match(cell_id) else None
    name = cell_name if cell_name and _CELL_NAME.match(cell_name) and cell_name != "_" else None
    if name and cid:
        return f"{name} ({cid})"
    return name or cid or "a cell"


def _word(value: str | None, fallback: str) -> str:
    return value if value and _WORD.match(value) else fallback


def _who(item: ActivityItem) -> str:
    actor = item.actor
    name = display_name(actor.label())
    # An agent acting for a person is already named as one.
    return f"{name} (agent)" if actor.kind == "agent" and actor.acting_for is None else name


def _lines_for(items: Sequence[ActivityItem]) -> list[tuple[str, int]]:
    """Each line with how many items it accounts for. Consecutive document
    changes by one actor with one verb fold into a single line."""
    lines: list[tuple[str, int]] = []
    pending: tuple[str, str, list[str]] | None = None
    pending_count = 0

    def flush() -> None:
        nonlocal pending, pending_count
        if pending is not None:
            who, verb, cells = pending
            unique = list(dict.fromkeys(cells))
            lines.append((f"- {who} {verb} {', '.join(unique)}", pending_count))
        pending, pending_count = None, 0

    for item in items:
        if item.kind in _VERBS:
            who, verb = _who(item), _VERBS[item.kind]
            cell = _cell(item.cell_id, item.cell_name)
            if pending is not None and pending[0] == who and pending[1] == verb:
                pending[2].append(cell)
                pending_count += 1
            else:
                flush()
                pending, pending_count = (who, verb, [cell]), 1
            continue
        flush()
        lines.append((_line(item), 1))
    flush()
    return lines


def _line(item: ActivityItem) -> str:
    who = _who(item)
    if item.kind == "run":
        status = _word(item.status, "ended")
        parts = []
        for cid, name, cell_status in item.cells[:12]:
            parts.append(f"{_cell(cid, name)} {_word(cell_status, 'unknown')}")
        if len(item.cells) > 12:
            parts.append(f"{len(item.cells) - 12} more")
        text = f"- {who} ran {len(item.cells)} cell{'s' if len(item.cells) != 1 else ''}"
        text += f" ({status})"
        if parts:
            text += ": " + ", ".join(parts)
        if item.error_class:
            text += f"; error {safe_ename(item.error_class)}"
        return text
    if item.kind == "kernel":
        reason = _word(item.reason, "other")
        return f"- kernel {_word(item.status, 'restarted')} ({reason}), by {who}"
    if item.kind == "settings":
        return f"- {who} changed settings"
    if item.kind == "env":
        return f"- {who} changed the environment ({_word(item.status, 'changed')})"
    return f"- {who} {_word(item.kind, 'changed')} {_cell(item.cell_id, item.cell_name)}"


@dataclass(frozen=True)
class NotebookDigest:
    """One notebook's part of the digest, with the accounting the simulator checks."""

    path: str
    text: str
    shown: int
    """Activity items the text names."""
    omitted: int
    """Activity items counted on the overflow line instead."""


@dataclass(frozen=True)
class Digest:
    text: str
    notebooks: list[NotebookDigest] = field(default_factory=list)
    omitted_notebooks: list[str] = field(default_factory=list)
    """Notebooks with activity that did not fit, named on the total overflow line."""


def render_notebook(
    activity: Activity, *, exclude_actor: str | None = None, budget: int = PER_NOTEBOOK_CHARS
) -> NotebookDigest | None:
    """One notebook's digest, or ``None`` when nobody else did anything there."""
    items = [i for i in activity.items if exclude_actor is None or i.actor.id != exclude_actor]
    stale = [_cell(cid, name) for cid, name in activity.stale]
    if not items and not stale:
        return None
    title = f"{activity.path}:"
    lines = _lines_for(items)
    total = sum(count for _, count in lines)
    stale_line = f"- stale now: {', '.join(stale[:20])}" + (
        f" and {len(stale) - 20} more" if len(stale) > 20 else ""
    )
    out = [title]
    shown = 0
    used = len(title)
    reserve = len(stale_line) + 1 if stale else 0
    for line, count in lines:
        remaining_after = total - shown - count
        overflow = _overflow_line(remaining_after, activity.path) if remaining_after else ""
        need = used + 1 + len(line) + reserve + (len(overflow) + 1 if overflow else 0)
        if need > budget:
            # This line does not fit with what must follow it; count it and the rest.
            out.append(_overflow_line(total - shown, activity.path))
            break
        out.append(line)
        used += 1 + len(line)
        shown += count
    if stale:
        out.append(stale_line)
    text = "\n".join(out)
    if len(text) > budget:
        # Only a pathological stale list gets here; the stale line is the one cut.
        text = text[: budget - 1] + "…"
    return NotebookDigest(activity.path, text, shown, total - shown)


def _overflow_line(count: int, path: str) -> str:
    noun = "change" if count == 1 else "changes"
    return f"- … {count} more {noun}; call notebook.read on {path} to see the current state"


def render(
    activities: Sequence[Activity],
    *,
    exclude_actor: str | None = None,
    total_budget: int = TOTAL_CHARS,
    per_notebook: int = PER_NOTEBOOK_CHARS,
) -> Digest:
    """The whole digest over every notebook's activity, bounded in total."""
    parts: list[NotebookDigest] = []
    omitted: list[str] = []
    used = len(HEADER)
    for activity in activities:
        part = render_notebook(activity, exclude_actor=exclude_actor, budget=per_notebook)
        if part is None:
            continue
        # Room for this part and, if anything after it is cut, the overflow line.
        if omitted or used + 2 + len(part.text) + 160 > total_budget:
            omitted.append(activity.path)
            continue
        parts.append(part)
        used += 2 + len(part.text)
    if not parts and not omitted:
        return Digest("")
    blocks = [HEADER, *(p.text for p in parts)]
    if omitted:
        names = ", ".join(omitted[:5]) + (
            f" and {len(omitted) - 5} more" if len(omitted) > 5 else ""
        )
        blocks.append(f"… {len(omitted)} more notebooks changed: {names}")
    text = "\n\n".join(blocks)
    return Digest(text[:total_budget], parts, omitted)


class CursorStore(Protocol):
    """Where a chat keeps, per notebook, the time up to which it has been told."""

    async def get(self, path: str) -> datetime | None: ...

    async def set(self, path: str, at: datetime) -> None: ...

    async def paths(self) -> list[str]: ...


async def turn_digest(host: NotebookHost, cursors: CursorStore) -> Digest:
    """The digest for this turn over every notebook the chat has touched, and
    each cursor advanced past what it reports.

    A notebook that can no longer be opened (deleted, moved out of reach) drops
    out of the digest silently: the digest is a courtesy, never a failure of the
    turn."""
    activities: list[Activity] = []
    for path in await cursors.paths():
        since = await cursors.get(path)
        if since is None:
            continue
        try:
            port = await host.open(path)
            activity = await port.activity(since)
        except Exception:
            logger.info("notebook digest: %s could not be read", path, exc_info=True)
            continue
        activities.append(activity)
    digest = render(activities, exclude_actor=host.actor.id)
    for activity in activities:
        # The engine's clock, never the caller's: a cursor is a point in the
        # notebook's own history.
        await cursors.set(activity.path, activity.cursor)
    return digest


#: Before anything a notebook records.
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


async def follow(cursors: CursorStore, host: NotebookHost, path: str) -> None:
    """Start following ``path`` from what it has recorded so far (the agent's
    first use of it). The cursor is the notebook's own newest activity time,
    on the engine's clock, so nothing before the agent's first look is
    reported and nothing after it is missed. A notebook already followed keeps
    its cursor."""
    if await cursors.get(path) is not None:
        return
    port = await host.open(path)
    activity = await port.activity(EPOCH)
    await cursors.set(path, activity.cursor)


async def touch(cursors: CursorStore, path: str, at: datetime) -> None:
    """Start following ``path`` from ``at`` (the agent's first use of it). A
    notebook already followed keeps its cursor."""
    if await cursors.get(path) is None:
        await cursors.set(path, at)


__all__ = [
    "EPOCH",
    "HEADER",
    "PER_NOTEBOOK_CHARS",
    "TOTAL_CHARS",
    "CursorStore",
    "Digest",
    "NotebookDigest",
    "follow",
    "render",
    "render_notebook",
    "touch",
    "turn_digest",
]
