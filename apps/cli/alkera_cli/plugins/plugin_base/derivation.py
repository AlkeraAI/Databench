"""Which live connection a detected one was derived from.

Most detected connections come from what the machine itself can see: a dbt
project in the workspace, an AWS profile in ``~/.aws/config``. Some are learned
THROUGH an added connection instead: the buckets and databases an AWS account
listed, the warehouses a Hex, Fivetran or Sigma account named. On a machine that
serves several orgs' chats those belong to the org whose connection found them,
and a chat may be shown one only when the connection it came from is in that
chat's scope.

The producer stamps the parent on the detected connection's attributes; the
listing carries it beside the row (never on the wire), and a scoped view keeps a
detected row only when its parent is a connection the chat holds. A row with no
stamp proves nothing about whose it is, so a scoped view drops it.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping

from alkera_cli.plugins.plugin_base.connection import Connection
from alkera_cli.plugins.plugin_base.plugin import PluginConnectionInfo

#: The attribute a producer stamps the parent connection under.
DERIVED_FROM_ATTRIBUTE = "derived_from"


def derived_from_attribute(plugin: str, handle: str) -> dict[str, str]:
    """The stamp a producer puts under :data:`DERIVED_FROM_ATTRIBUTE`."""
    return {"plugin": plugin, "handle": handle}


def derived_from(conn: Connection) -> tuple[str, str] | None:
    """The ``(plugin, handle)`` of the connection ``conn`` was learned through,
    or ``None`` when it carries no well-formed stamp."""
    raw = conn.attributes.get(DERIVED_FROM_ATTRIBUTE)
    if not isinstance(raw, Mapping):
        return None
    plugin, handle = raw.get("plugin"), raw.get("handle")
    if not isinstance(plugin, str) or not isinstance(handle, str) or not plugin or not handle:
        return None
    return plugin, handle


#: How a discovery pass files a detected row: its identity plus the connection it
#: was learned through, so two parents' rows never share a key.
DetectedKey = tuple[str, str, tuple[str, str] | None]


def detected_key(conn: Connection) -> DetectedKey:
    """The key a discovery pass files ``conn`` under."""
    return conn.plugin, conn.handle, derived_from(conn)


def keyed_detected(rows: Iterable[Connection]) -> dict[tuple[str, str], Connection]:
    """The detected rows by the ``(plugin, handle)`` identity they are listed and
    added under, with no two parents ever sharing one.

    Two vendors (on a box, two orgs' accounts) can name one warehouse under one
    label and so mint the same handle. When rows with different parents share a
    handle, every row learned through a parent takes a suffix hashed from that
    parent, and a row with no parent keeps the bare handle. The outcome depends
    only on which rows exist, never on the order discovery reported them, so a
    re-discovery names every row the same way."""
    groups: dict[tuple[str, str], list[Connection]] = {}
    for conn in rows:
        groups.setdefault((conn.plugin, conn.handle), []).append(conn)
    out: dict[tuple[str, str], Connection] = {}
    for (plugin, handle), group in groups.items():
        if len({derived_from(c) for c in group}) == 1:
            out[(plugin, handle)] = group[-1]
            continue
        for conn in group:
            parent = derived_from(conn)
            if parent is None:
                out[(plugin, handle)] = conn
                continue
            digest = hashlib.sha256("\0".join(parent).encode()).hexdigest()[:6]
            renamed = conn.model_copy(update={"handle": f"{handle}_{digest}"})
            out[(plugin, renamed.handle)] = renamed
    return out


def connection_info(conn: Connection, *, added: bool) -> PluginConnectionInfo:
    """One listing row for ``conn``, carrying where it was derived from."""
    info = PluginConnectionInfo(
        handle=conn.handle, plugin=conn.plugin, environment=str(conn.environment), added=added
    )
    info._derived_from = derived_from(conn)
    return info


__all__ = [
    "DERIVED_FROM_ATTRIBUTE",
    "DetectedKey",
    "connection_info",
    "derived_from",
    "derived_from_attribute",
    "detected_key",
    "keyed_detected",
]
