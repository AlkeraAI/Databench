"""What a schema listing just proved about the workspace's schema cards.

The cards an agent reasons over are built on a cadence — half an hour on a
cloud box — so a connection whose DATA changed after they were built stays
stale until the next sweep. A live rehearsal hit exactly that: the box's cards
held two extension views from before the warehouse was seeded, and the agent
wrote ``customers``, then ``geo.customers``, against a schema it could not see.

A ``sql.schema mode=list`` call has just read the truth from the engine, for
free, on the way to answering something else. This is where that answer is
offered to whoever holds the cards for that workspace: the cloud mirror's card
loader subscribes, compares, and rebuilds a connection whose listing names
relations it has never carded.

Nothing here knows what a card IS — the watcher does. Deliberately: the tool
layer must not depend on the cloud layer, and a workspace with no watcher (a
laptop, a test) simply drops the notice.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from pathlib import Path

logger = logging.getLogger(__name__)

#: ``(connection handle, the relation names the engine just listed)``.
ListingWatcher = Callable[[str, tuple[str, ...]], None]

#: One watcher per workspace, keyed by the resolved ``.alkera/`` path — the same
#: key the tool layer already carries on its context. A process serves one
#: workspace today; the key is what keeps that from being an assumption.
_watchers: dict[str, ListingWatcher] = {}


def _key(alkera_dir: object) -> str | None:
    if alkera_dir is None:
        return None
    try:
        return str(Path(str(alkera_dir)).resolve())
    except (OSError, ValueError):  # pragma: no cover - a path that cannot resolve
        return None


def watch_listings(alkera_dir: object, watcher: ListingWatcher) -> None:
    """Hear about every relation listing read against this workspace."""
    key = _key(alkera_dir)
    if key is not None:
        _watchers[key] = watcher


def stop_watching_listings(alkera_dir: object, watcher: ListingWatcher | None = None) -> None:
    """Stop hearing about them. Naming the watcher makes this idempotent when a
    second holder has since taken the workspace over — a stop then leaves the
    live one in place rather than silencing it. Unknown workspaces are ignored."""
    key = _key(alkera_dir)
    if key is None:
        return
    if watcher is not None and _watchers.get(key) is not watcher:
        return
    _watchers.pop(key, None)


def relations_listed(alkera_dir: object, connection: str, names: Iterable[str]) -> None:
    """Offer a workspace's watcher what the engine just listed.

    Never raises and never blocks on anything the watcher does with it: this
    runs inside a tool call that is answering a reader's question, and a stale
    card is a smaller problem than a failed turn."""
    key = _key(alkera_dir)
    watcher = _watchers.get(key) if key is not None else None
    if watcher is None:
        return
    try:
        watcher(connection, tuple(names))
    except Exception:
        logger.debug("schema listing notice failed for %s", connection, exc_info=True)


__all__ = [
    "ListingWatcher",
    "relations_listed",
    "stop_watching_listings",
    "watch_listings",
]
