"""Prune stale Nuitka onefile extraction caches left behind by upgrades.

The compiled ``alkera`` binary extracts its payload once into
``{CACHE_DIR}/{PRODUCT}/{VERSION}`` and reuses it across runs (the
``--onefile-cache-mode=cached`` setup in the binary build).
Each released version therefore parks a ~300MB directory under the product
cache dir; after an upgrade the old version's directory is dead weight. The
daemon calls :func:`prune_stale_onefile_caches_in_background` at startup to
sweep them.

Best-effort by design: on Windows a still-running older daemon holds locks on
its files, so its directory simply fails to delete and is retried on a future
startup. On POSIX a concurrently-running older version self-heals — its next
launch re-extracts via the onefile bootstrap's CRC check (one slow start).
"""

from __future__ import annotations

import logging
import re
import shutil
import sys
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

# The {VERSION} token expands from --product-version (VERSION.txt padded to
# four parts, e.g. "0.0.1.0"). Only dirs shaped like that are ever touched.
_VERSION_DIR_RE = re.compile(r"^\d+(\.\d+){1,3}$")

# A data file our build physically extracts (see --include-data-files in
# the binary build). Proves a sibling dir is an alkera
# extraction and not someone else's cache that happens to sit nearby.
_ALKERA_MARKER = ("runtime", ".alkera-sha")


def current_extract_dir() -> Path | None:
    """The running binary's onefile extraction dir, or ``None`` outside a
    compiled binary.

    Same sentinel as ``harness/opencode_binary.py``: Nuitka sets
    ``__compiled__`` on ``__main__``. Compiled modules carry dist-relative
    ``__file__`` paths even though the code lives in the binary, so this
    module's path is ``<extract_dir>/alkera_cli/host/onefile_cache.py``.
    """
    if not getattr(sys.modules.get("__main__"), "__compiled__", None):
        return None
    try:
        return Path(__file__).resolve().parents[2]
    except (OSError, IndexError):
        return None


def prune_stale_version_dirs(extract_dir: Path) -> list[Path]:
    """Remove sibling version dirs next to ``extract_dir``; return what was
    actually removed.

    ``extract_dir`` is the running version's ``{CACHE_DIR}/{PRODUCT}/{VERSION}``
    directory. A sibling is deleted only when it is ALSO a version-shaped
    alkera extraction (version-pattern name + the bundled-data marker file) —
    and never ``extract_dir`` itself. If ``extract_dir`` doesn't look like a
    cached-mode version dir (e.g. a build reverted to the per-run temp spec,
    whose parent is the shared temp dir), the whole sweep is a no-op.
    """
    if not _VERSION_DIR_RE.match(extract_dir.name):
        return []
    removed: list[Path] = []
    try:
        siblings = list(extract_dir.parent.iterdir())
    except OSError:
        return removed
    for sibling in siblings:
        if sibling.name == extract_dir.name:
            continue
        if not _VERSION_DIR_RE.match(sibling.name):
            continue
        if not sibling.joinpath(*_ALKERA_MARKER).is_file():
            continue
        try:
            shutil.rmtree(sibling)
        except OSError:
            logger.debug("Onefile cache prune skipped %s (still in use?)", sibling)
            continue
        removed.append(sibling)
        logger.info("Pruned stale onefile cache %s", sibling)
    return removed


def prune_stale_onefile_caches() -> list[Path]:
    """Sweep stale version caches for the running binary. No-op (and no
    filesystem access) outside a compiled binary. Never raises."""
    try:
        extract_dir = current_extract_dir()
        if extract_dir is None:
            return []
        return prune_stale_version_dirs(extract_dir)
    except Exception:  # cleanup must never take the daemon down
        logger.debug("Onefile cache prune failed", exc_info=True)
        return []


def prune_stale_onefile_caches_in_background() -> None:
    """Fire-and-forget :func:`prune_stale_onefile_caches` on a daemon thread,
    so deleting a multi-hundred-MB directory never delays daemon startup."""
    threading.Thread(
        target=prune_stale_onefile_caches,
        name="onefile-cache-prune",
        daemon=True,
    ).start()
