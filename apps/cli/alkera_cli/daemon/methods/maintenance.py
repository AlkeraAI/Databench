"""Daemon method for reclaiming stale entries from the Alkera cache.

`maintenance.cleanupCache` deletes stale entries under `~/.alkera/cache/`
that haven't been used within the retention window. Today the only cache kind
is extracted opencode binaries (`opencode-<sha>/`), and we protect the
currently-active one; as Alkera caches more kinds of derived data here, their
sweeps get aggregated behind this one generic method. The daemon owns the
cache layout, so the delete logic lives here rather than in the editor — the
VS Code extension just calls this on a periodic timer (see `DaemonManager`),
keeping it a thin client that never touches `~/.alkera/` directly.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from alkera_cli.daemon.protocol import _DaemonModel, method
from alkera_cli.harness.opencode_binary import (
    OpencodeBinaryNotFoundError,
    cleanup_stale_opencode_caches,
    resolve_opencode_binary,
)

if TYPE_CHECKING:
    from alkera_cli.daemon.server import JsonRpcServer


# An opencode cache dir untouched for this long is eligible for deletion.
# Deliberately generous: a user may run several editor windows for days, an
# older one still on a previous version — last-use recency (not version age)
# is what gates removal, so a shared older version isn't reclaimed out from
# under a still-running window.
_MAX_AGE_SECONDS = 14 * 24 * 60 * 60


class MaintenanceCleanupCacheRequest(_DaemonModel):
    pass


class MaintenanceCleanupCacheResponse(_DaemonModel):
    removed: list[str]
    """Identifiers of the cache entries removed this sweep (today: opencode SHAs)."""
    kept_count: int
    """Number of cache entries left in place (still fresh, or the active one)."""
    errors: list[str]
    """Non-fatal per-entry failures (e.g. an entry still locked/in use)."""


@method("maintenance.cleanupCache")
async def maintenance_cleanup_cache(
    server: JsonRpcServer, params: MaintenanceCleanupCacheRequest
) -> MaintenanceCleanupCacheResponse:
    def _run() -> MaintenanceCleanupCacheResponse:
        # Never delete the version this daemon would itself spawn. Resolving
        # also refreshes that dir's `.last-used` marker, so the active SHA
        # stays fresh even across long-lived daemons. Best-effort: if nothing
        # resolves (dev checkout without a built binary), fall back to pure
        # staleness with no protected SHA.
        try:
            keep_sha = resolve_opencode_binary().sha
        except OpencodeBinaryNotFoundError:
            keep_sha = None
        # Today the Alkera cache holds only extracted opencode binaries; as
        # more cache kinds land, aggregate their sweeps into `removed`/`errors`.
        result = cleanup_stale_opencode_caches(max_age_seconds=_MAX_AGE_SECONDS, keep_sha=keep_sha)
        return MaintenanceCleanupCacheResponse(
            removed=list(result.removed),
            kept_count=len(result.kept),
            errors=list(result.errors),
        )

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _run)


__all__ = [
    "MaintenanceCleanupCacheRequest",
    "MaintenanceCleanupCacheResponse",
]
