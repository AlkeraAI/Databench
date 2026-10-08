"""Event-driven lineage and knowledge-base refresh: watch the repo, nudge the right refresh.

One watcher serves two scopes (they share the OS watch, the debounce and the
gitignore filter):

* **Lineage is per connection.** A connection seeded from a local artifact (a
  dbt ``manifest.json``, a ``.duckdb``, a Tableau workbook, an Airflow ``dags/``
  folder) re-seeds when that file changes. A changed path is mapped back to the
  connections that own it (:meth:`WatchSpec.matches`) and only their refresh is
  nudged.
* **The knowledge base is repo-wide.** The context index walks the whole
  non-gitignored repo, so any tracked source edit marks it dirty. The whole
  workspace root is watched recursively for that.

The OS watcher (:func:`alkera_cli.files.tree_watch.watch_tree`) never follows a
link. The usual junk (``.git``, ``node_modules``, ``__pycache__``, editor temp
files) and the project's own ``.alkera/`` are ignored, so the watcher never
reacts to its own store writes. Added, modified and deleted files all count; a
removed artifact still matches its connection by path, so the seed reconciles
it away.

Lifecycle mirrors :class:`AuthFileWatcher`: ``start_async()`` spawns a
background task and ``stop()`` cancels it. The runtime owns one per project and
restarts it when the connection set changes. ``force_polling`` falls back to
polling where OS notifications do not fire (network mounts, Docker, WSL).

Each watch holds an event-loop worker thread while it waits for OS events, and
a watch task keeps itself alive, so an unstopped watch leaks a thread from a
bounded pool. :func:`stop_all_watchers` stops every watch for a supervisor with
no owners left to ask. Stopping is bounded: after :data:`STOP_TIMEOUT_S` a
wedged watch is abandoned with a log line. A caller cancelled while it waits is
cancelled; the wait re-raises its ``CancelledError``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import weakref
from collections.abc import Awaitable, Callable, Iterable
from pathlib import Path
from typing import Any

from alkera_cli.files import tree_watch
from alkera_cli.files.tree_watch import Change
from alkera_cli.harness import repo_files
from alkera_cli.harness.tasks import stop_task
from alkera_cli.plugins.plugin_base.artifacts import WatchSpec, connection_watch_spec

logger = logging.getLogger(__name__)

#: Quiet period (ms) after the LAST change before we fire — coalesces a "Save All" / a
#: ``dbt build`` burst into one trigger.
DEFAULT_DEBOUNCE_MS = 400

#: Backoff before re-entering the watch after its loop crashes on a transient error
#: (a momentary OS error) — bounds the restart/log rate so a
#: persistently-failing watch can't busy-spin.
_RESTART_BACKOFF_S = 1.0

#: How long :meth:`ProjectFileWatcher.stop` waits for a cancelled watch to end before abandoning
#: it. A watch ends as soon as its cancellation is delivered, so on a healthy loop the wait is
#: milliseconds; the bound is for a watch wedged inside a call that never returns, which must not
#: turn a shutdown into a hang.
STOP_TIMEOUT_S = 5.0

#: Every watcher whose watch task is running, so a supervisor can end them all. A running task
#: keeps its own watcher alive, so membership is exactly "still watching" and the references can
#: be weak: a watcher whose task has finished drops out on its own.
_watching: weakref.WeakSet[ProjectFileWatcher] = weakref.WeakSet()


def stop_all_watchers() -> None:
    """End every watch running in this process, without waiting for any of them.

    For a supervisor that no longer has the owners to ask. It needs no running loop: each watch
    is cancelled, and the cancellation — and the worker thread it gives back — lands whenever the
    loop next runs.
    """
    for watcher in list(_watching):
        watcher.request_stop()


#: Directory names whose changes never matter to a project watch: tool caches,
#: virtualenvs, other version-control stores, and the project's own ``.alkera/``,
#: where reacting to OUR OWN writes (scheduler job files, progress sidecars, the
#: lineage SQLite, chat events) would self-trigger an endless refresh.
IGNORED_DIRS: frozenset[str] = frozenset(
    {
        "__pycache__",
        ".git",
        ".hg",
        ".svn",
        ".tox",
        ".venv",
        ".idea",
        "node_modules",
        ".mypy_cache",
        ".pytest_cache",
        ".hypothesis",
        ".alkera",
    }
)

#: Final path components that are editor or interpreter litter: compiled Python,
#: JetBrains and vim swap files, backups, Emacs lock and flycheck files, Finder's
#: ``.DS_Store``.
IGNORED_NAMES: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern)
    for pattern in (
        r"\.py[cod]$",
        r"\.___jb_...___$",
        r"\.sw.$",
        "~$",
        r"^\.\#",
        r"^\.DS_Store$",
        r"^flycheck_",
    )
)


def watch_filter(_change: Change, path: str) -> bool:
    """The OS watch's coarse per-event filter: keep ``path`` unless one of its
    components is an :data:`IGNORED_DIRS` name or its last component is
    :data:`IGNORED_NAMES` litter. Cheap on purpose; the precise gitignore decision
    is made per batch in :meth:`ProjectFileWatcher.classify`."""
    parts = path.lstrip(os.sep).split(os.sep)
    if any(part in IGNORED_DIRS for part in parts):
        return False
    return not any(pattern.search(parts[-1]) for pattern in IGNORED_NAMES)


# ===========================================================================
# VCS (git) awareness — ISOLATED ON PURPOSE.
#
# Honoring .gitignore is a VERSION-CONTROL concern. It lives here for now but is
# DESTINED FOR A FUTURE GIT PLUGIN (the plugin system's VCS capability): when that
# lands, lift `_GitignoreFilter` out wholesale and have the watcher ask the active VCS
# plugin "is this path ignored?" instead. ALL git-specific code in the watcher is
# confined to this one class so the extraction is a clean cut — the rest of the file
# knows nothing about git.
# ===========================================================================
class _GitignoreFilter:
    """Is a path gitignored? Reuses the existing gitignore-aware discovery
    (``harness.repo_files.discover_files`` — honors ``.gitignore``/``.ignore`` + git excludes via
    ripgrep, with a plain-walk fallback). Per BATCH: one scan per directory, cached, so a
    burst of changes costs a single ``rg`` call per dir. FAIL-OPEN — if the scan can't run, a
    path is treated as NOT ignored, so a VCS hiccup never silently drops a real change.

    The only git-aware code in this module; slated for a git/VCS plugin (keep self-contained).
    """

    def __init__(self) -> None:
        self._tracked: dict[Path, set[Path] | None] = {}

    def is_ignored(self, path: Path) -> bool:
        directory = path.parent
        if directory not in self._tracked:
            self._tracked[directory] = self._scan(directory)
        tracked = self._tracked[directory]
        if tracked is None:
            return False  # couldn't determine → fail OPEN (react), never drop a real change
        return path not in tracked

    @staticmethod
    def _scan(directory: Path) -> set[Path] | None:
        # repo_files.discover_files is gitignore-aware (rg --files, with a walk fallback).
        try:
            return {p.resolve() for p in repo_files.discover_files(directory)}
        except Exception:
            logger.debug("file_watcher: scan of %s failed", directory, exc_info=True)
            return None


# ===========================================================================
# The watcher (git-agnostic — talks to _GitignoreFilter through one call site).
# ===========================================================================
class ProjectFileWatcher:
    """Watch the workspace (repo-wide, for the KB) + a snapshot of connections' source files (for
    lineage); on each debounced batch call ``on_change(handles, kb_dirty)`` — ``handles`` = the
    connections whose source changed, ``kb_dirty`` = a repo-tracked file changed. The runtime
    rebuilds + restarts this when its connection set changes, so the snapshot stays current."""

    def __init__(
        self,
        connections: Iterable[Any],
        on_change: Callable[[set[str], bool], Awaitable[None]],
        *,
        workspace_root: Path | None = None,
        debounce_ms: int = DEFAULT_DEBOUNCE_MS,
        force_polling: bool = False,
    ) -> None:
        self._on_change = on_change
        # The repo root drives the whole-repo KB watch; resolve it so the under-root check
        # (and macOS /var→/private/var symlinks) line up with the resolved event paths.
        self._workspace_root = workspace_root.resolve() if workspace_root is not None else None
        self._debounce_ms = debounce_ms
        self._force_polling = force_polling
        self._task: asyncio.Task[None] | None = None
        # Precompute (connection, spec) for offline connections + the dirs to hand the watcher.
        self._specs: list[tuple[Any, WatchSpec]] = [
            (c, spec) for c in connections if (spec := connection_watch_spec(c)) is not None
        ]
        self._dirs: list[str] = self._existing_dirs()

    def _existing_dirs(self) -> list[str]:
        """The dirs handed to the OS watcher: the WORKSPACE ROOT (recursive → the whole repo, for
        the repo-wide KB) PLUS any connection artifact dir that lives OUTSIDE the root (an
        out-of-tree ``.duckdb`` etc.; in-tree ones are already covered by the recursive root watch,
        so they're skipped to avoid a redundant nested watch). Recomputed on each (re)start so a
        since-vanished dir is dropped instead of crashing the watch forever."""
        dirs: set[str] = set()
        root = self._workspace_root  # already resolved in __init__
        if root is not None and root.exists():
            dirs.add(str(root))
        for _conn, spec in self._specs:
            for d in spec.watch_dirs():
                if not d.exists():
                    continue
                rd = d.resolve()  # resolve so the under-root check + the watched path match events
                if root is not None and rd.is_relative_to(root):
                    continue  # already covered by the recursive root watch
                dirs.add(str(rd))
        return sorted(dirs)

    @property
    def is_watching(self) -> bool:
        """Whether this watcher currently has a watch running."""
        return self._task is not None and not self._task.done()

    @property
    def watch_dirs(self) -> list[str]:
        """The directories handed to the OS watcher (the workspace root + any out-of-tree
        artifact dir that exists) — empty only with no root AND nothing offline to watch."""
        return self._dirs

    def classify(self, changes: Iterable[tuple[Change, str]]) -> tuple[set[str], bool]:
        """Map a debounced batch to ``(handles, kb_dirty)``. **Lineage handles** — ONE-TO-MANY (a
        shared file nudges every owner): a connection's explicit ARTIFACT file is always honored
        (the source of truth — dbt's ``target/manifest.json`` is itself gitignored); a file merely
        UNDER a watched dir respects ``.gitignore``. **kb_dirty** — the KB indexes the WHOLE repo,
        so ANY repo-tracked (non-gitignored, under the root) change marks it dirty, even one no
        connection owns; a file outside the root (an out-of-tree artifact) is not in the KB walk.
        A deletion can't be gitignore-checked once gone, so it always reacts (a spurious one is a
        gated no-op). One ``_GitignoreFilter`` per batch (cached per dir) serves both signals."""
        ignore = _GitignoreFilter()  # fresh per batch → reflects the current tracked state
        root = self._workspace_root
        handles: set[str] = set()
        kb_dirty = False
        for change, path in changes:
            rp = Path(path).resolve()
            for conn, spec in self._specs:
                if spec.is_artifact_file(path):
                    handles.add(conn.handle)  # the connection's own artifact — always
                elif spec.matches(path) and (change == Change.deleted or not ignore.is_ignored(rp)):
                    handles.add(conn.handle)  # a file under a watched dir → VCS-filtered
            if (
                not kb_dirty
                and root is not None
                and rp.is_relative_to(root)
                and (change == Change.deleted or not ignore.is_ignored(rp))
            ):
                kb_dirty = True  # a repo-tracked change → re-seed the whole-repo KB
        return handles, kb_dirty

    def handles_for_changes(self, changes: Iterable[tuple[Change, str]]) -> set[str]:
        """The connection handles to refresh for lineage (the ``classify`` lineage half) — a
        focused accessor for tests; ``_run`` uses ``classify`` for both signals."""
        return self.classify(changes)[0]

    def start_async(self) -> asyncio.Task[None] | None:
        """Begin watching in a background task. Returns the task, or ``None`` when there's
        nothing to watch (no offline connection / its dirs are absent). Idempotent."""
        if not self._dirs:
            return None
        if self._task is not None and not self._task.done():
            return self._task
        self._task = asyncio.create_task(self._run(), name="alkera-file-watcher")
        _watching.add(self)
        return self._task

    def request_stop(self) -> None:
        """Ask the watch to end, without waiting for it. Idempotent.

        The awaiting form is :meth:`stop`; this one is for a caller with no loop to await on,
        and the cancellation lands whenever the loop next runs.
        """
        _watching.discard(self)
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()

    async def stop(self) -> None:
        """Stop watching, and wait until it has. Idempotent.

        Bounded by :data:`STOP_TIMEOUT_S`: a watch that does not end in that time is
        abandoned with a warning. A cancellation of the CALLER propagates — the watch is
        cancelled either way, and the caller's deadline is the caller's to hear.
        """
        task = self._task
        self.request_stop()
        if task is None or task.done():
            return
        if not await stop_task(task, grace=STOP_TIMEOUT_S):
            logger.warning(
                "file watcher for %s did not stop within %.0fs; abandoning it",
                ", ".join(self._dirs),
                STOP_TIMEOUT_S,
            )

    async def _run(self) -> None:
        try:
            await self._watch()
        finally:
            _watching.discard(self)  # a watch that ended on its own is no longer watching

    async def _watch(self) -> None:
        # Auto-restart on a transient failure. The beat starts the watcher ONCE (then only on
        # connection mutations), so without this an unhandled watch/OS error would silence
        # file-driven refresh until the daemon restarts. CancelledError (``stop()``) still exits
        # cleanly; a crash is logged, backed off, and re-watched. Re-checking existing dirs each
        # pass means a vanished dir is dropped (and an empty set exits — revived on the next
        # connection change) rather than crash-looping forever.
        while True:
            dirs = self._existing_dirs()
            if not dirs:
                return
            try:
                async for changes in tree_watch.watch_tree(
                    *dirs,
                    watch_filter=watch_filter,
                    debounce_ms=self._debounce_ms,
                    force_polling=self._force_polling,
                ):
                    # classify runs the gitignore scan (``rg``), which over the repo-wide watch can
                    # touch a large subtree — offload it so a big scan never stalls the event loop.
                    handles, kb_dirty = await asyncio.to_thread(self.classify, list(changes))
                    if handles or kb_dirty:
                        with contextlib.suppress(Exception):
                            await self._on_change(handles, kb_dirty)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning(
                    "file watcher loop crashed for %s; restarting after backoff",
                    dirs,
                    exc_info=True,
                )
                await asyncio.sleep(_RESTART_BACKOFF_S)
            else:
                return  # the watch returned on its own (no stop_event passed) → nothing to restart


__all__ = ["DEFAULT_DEBOUNCE_MS", "STOP_TIMEOUT_S", "ProjectFileWatcher", "stop_all_watchers"]
