"""The LIVE (added) connections store.

Discovery (a plugin's ``ConnectionProvider``) DETECTS candidate connections — a
scanned ``*.duckdb`` file, a ``connections.toml`` / dbt profile, ``SNOWFLAKE_*`` env.
Most do not go live automatically: a detected remote/warehouse connection is a
suggestion a person (or the VS Code extension) explicitly ADDS, so the agent only
ever reaches a warehouse a human deliberately wired in (the prod-by-accident guard).
The exception is a plugin that declares its connections safe to auto-activate
(``Plugin.auto_activates`` — a local DuckDB file, a dbt project's committed
manifest): those are promoted to live on detection, no manual add. This store is the
live allow-list either way, persisted at ``.alkera/connections.json`` so it survives
restarts.

The store is an ordered set keyed by connection identity (``Connection.__eq__`` /
``__hash__`` on ``(plugin, handle)``): adding the same connection twice is idempotent
and never duplicates it, while insertion order is preserved.
"""

from __future__ import annotations

import json
from pathlib import Path

from alkera_core.atomic_io import write_json_atomic

from alkera_cli.plugins.plugin_base.connection import Connection


def parse_connections(text: str | None) -> list[Connection]:
    """The added-connection allow-list from a raw JSON document (None/empty/
    malformed -> ``[]``). Text in, connections out, so the caller decides WHERE the
    document came from -- the working tree, or a pinned git ref (the gate reads the
    classification-affecting metadata from the trusted ref, so a PR cannot retag or
    delete a connection to change how it is judged)."""
    if not text:
        return []
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, dict):
        return []
    parsed = [Connection.model_validate(c) for c in data.get("connections", [])]
    return _ordered_dedup(parsed)


def _ordered_dedup(conns: list[Connection], *, keep_last: bool = False) -> list[Connection]:
    """Dedup by connection identity (``Connection`` ``__eq__``/``__hash__``),
    preserving first-seen order — a dict-backed ordered set. With ``keep_last``, a
    later occurrence's data replaces the earlier one in place (last-writer-wins)."""
    out: dict[Connection, Connection] = {}
    for conn in conns:
        if keep_last or conn not in out:
            out[conn] = conn
    return list(out.values())


class AddedConnectionsStore:
    """Reads/writes the added-connection allow-list. Adds/removes are rare,
    human-driven operations — a last-writer-wins atomic file is sufficient (no
    long-held lock); each write is torn-free via ``write_json_atomic``."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)

    def load(self) -> list[Connection]:
        try:
            raw = self._path.read_text()
        except FileNotFoundError:
            return []
        # parse_connections dedupes + self-heals a file that somehow holds duplicates.
        return parse_connections(raw)

    def handles(self) -> set[str]:
        return {c.handle for c in self.load()}

    def add(self, conn: Connection) -> Connection:
        """Add (or replace, by identity) a connection. Idempotent — adding the same
        connection twice never duplicates it; a re-add updates it in place."""
        self._save(_ordered_dedup([*self.load(), conn], keep_last=True))
        return conn

    def remove(self, handle: str, *, plugin: str | None = None) -> bool:
        """Remove the connection ``handle`` from the allow-list. When ``plugin`` is given,
        remove ONLY that ``(plugin, handle)`` — so removing one of two connections that share a
        bare handle (e.g. ``data.sqlite`` + ``data.duckdb``) doesn't destroy the other (and its
        stored credential). ``plugin=None`` keeps the legacy handle-only behavior."""
        conns = self.load()
        if plugin is None:
            kept = [c for c in conns if c.handle != handle]
        else:
            kept = [c for c in conns if (c.plugin, c.handle) != (plugin, handle)]
        if len(kept) == len(conns):
            return False
        self._save(kept)
        return True

    def _save(self, conns: list[Connection]) -> None:
        write_json_atomic(
            self._path,
            {"connections": [c.model_dump(mode="json") for c in conns]},
        )


__all__ = ["AddedConnectionsStore", "parse_connections"]
