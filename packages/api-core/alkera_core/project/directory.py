"""`ProjectDirectory` — owner of a workspace's `.alkera/` tree.

Construction is idempotent: pass a path to `.alkera/`, the constructor
ensures the directory and well-known subdirs (`chats/`, `blobs/`) exist.
Sub-managers are returned as fresh handles — no shared mutable state
between them — so callers can pass them around safely.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import TYPE_CHECKING, TypeVar

from alkera_core.versioning import VersionedModel

if TYPE_CHECKING:
    from alkera_core.project.chats.blobs import BlobStore
    from alkera_core.project.chats.store import ChatStore
    from alkera_core.project.cloud_binding import CloudBindingStore
    from alkera_core.project.plugins.store import PluginStore


# Subdirectory layout — single source of truth so callers don't
# string-construct paths.
CHATS_SUBDIR = "chats"
SANDBOX_SUBDIR = "sandbox"
"""Per-chat scratch dir (`chats/<sid>/sandbox/`) — where blob.materialize /
blob.transform write real files the agent can run its own code over, and where
plan mode writes `plan.md`. Lives INSIDE the chat dir so deleting the chat
reclaims it; created on demand by the tool that writes to it."""
BLOBS_SUBDIR = "blobs"
RUNS_SUBDIR = "runs"
TRACES_SUBDIR = "traces"
ARTIFACTS_SUBDIR = "artifacts"
WORKTREES_SUBDIR = "worktrees"
LINEAGE_SUBDIR = "lineage"
LINEAGE_LOG_NAME = "lineage.jsonl"
PLUGINS_SUBDIR = "plugins"
SCHEDULER_SUBDIR = "scheduler"
RUN_TRACE_NAME = "run.jsonl"
COORDINATOR_TRACE_NAME = "coordinator.jsonl"
TRACE_NODES_SUBDIR = "nodes"
RUN_SUMMARY_NAME = "run.yaml"

TState = TypeVar("TState", bound=VersionedModel)


def _path_component(value: str, label: str) -> str:
    """Return `value` only if it is a single safe path component.

    `run_id` / `node_instance_id` become directory and file names, so a value
    with a path separator, a `..` segment, or a leading dot would escape the run
    directory or collide with a sibling. These ids are engine-minted, so a
    violation is a bug at the source: reject loudly rather than sanitize.
    """
    if not value:
        raise ValueError(f"{label} must be non-empty")
    if value in {".", ".."} or value.startswith("."):
        raise ValueError(f"{label} {value!r} must not be a dot segment")
    if "/" in value or "\\" in value or "\x00" in value:
        raise ValueError(f"{label} {value!r} must be a single path component")
    return value


class ProjectDirectory:
    """Handle to a workspace's `.alkera/` directory.

    Typical usage::

        project = ProjectDirectory(workspace_root / ".alkera")
        with project.chats().open(session_id) as chat:
            chat.append_event(...)

    The handle itself is cheap to construct; nothing in it holds OS
    resources. Sub-manager methods return fresh handles each call —
    callers should cache locally if they're hot-pathing.
    """

    def __init__(self, path: Path, *, create_if_missing: bool = True) -> None:
        self._path = Path(path)
        if create_if_missing:
            self._initialize()
        elif not self._path.is_dir():
            raise FileNotFoundError(
                f"{self._path}: directory doesn't exist and create_if_missing=False"
            )

    @property
    def path(self) -> Path:
        return self._path

    @property
    def chats_path(self) -> Path:
        return self._path / CHATS_SUBDIR

    @property
    def blobs_path(self) -> Path:
        return self._path / BLOBS_SUBDIR

    @property
    def runs_path(self) -> Path:
        """Per-run projected summaries + lineage live under `runs/<run_id>/`.
        Trace JSONL is a sibling tree under `traces/<run_id>/`."""
        return self._path / RUNS_SUBDIR

    @property
    def lineage_path(self) -> Path:
        """Cross-run lineage indexes rebuilt from retained run logs."""
        return self._path / LINEAGE_SUBDIR

    @property
    def traces_path(self) -> Path:
        """Per-run JSONL event streams live under `traces/<run_id>/`. A
        sibling of `runs/`, which holds projected summaries + lineage."""
        return self._path / TRACES_SUBDIR

    @property
    def artifacts_path(self) -> Path:
        """Content-addressed artifact object store (`artifacts/objects/sha256/`),
        sha256 two-level fan-out. The local engine's artifact store writes
        here; a test pins that the two never drift."""
        return self._path / ARTIFACTS_SUBDIR / "objects" / "sha256"

    @property
    def worktrees_path(self) -> Path:
        """Per-node detached-HEAD worktrees live under `worktrees/<run_id>/<node_id>/`.
        Created on demand by the scheduler; discard removes a run's subtree."""
        return self._path / WORKTREES_SUBDIR

    def run_worktree_dir(self, run_id: str) -> Path:
        """The worktree directory for one run (`worktrees/<run_id>/`)."""
        return self.worktrees_path / _path_component(run_id, "run_id")

    def run_dir(self, run_id: str) -> Path:
        """Directory holding one run's projected summaries + lineage.
        Created on demand by the writer; not pre-made in `_initialize`."""
        return self.runs_path / _path_component(run_id, "run_id")

    def run_lineage_log_path(self, run_id: str) -> Path:
        """Append-only lineage JSONL for one run."""
        return self.run_dir(run_id) / LINEAGE_LOG_NAME

    @property
    def plugins_path(self) -> Path:
        return self._path / PLUGINS_SUBDIR

    @property
    def scheduler_path(self) -> Path:
        """Where the daemon scheduler persists jobs (`.alkera/scheduler/`). The
        store itself lives in `alkera_cli` (it owns the job model + lease/claim
        logic) and takes this path — api-core only owns the location, never the
        scheduler types."""
        return self._path / SCHEDULER_SUBDIR

    @property
    def context_path(self) -> Path:
        """Where the context (KB) engine persists items (`.alkera/context/`). The
        store + item types live in `alkera_cli`; api-core owns the location."""
        return self._path / "context"

    @property
    def preferences_path(self) -> Path:
        """Per-PROJECT preferences (`.alkera/preferences.yml`) — team-shared (NOT
        gitignored), distinct from the per-user `~/.alkera/preferences.yml`. The
        `ProjectPreferences` model + read/write logic live in `alkera_cli`;
        api-core owns only the location."""
        return self._path / "preferences.yml"

    @property
    def cost_state_path(self) -> Path:
        """The cross-chat day/week spend rollup (`.alkera/cost_state.json`). The
        gateway meters LLM spend but has no chat_id, so warehouse-query cost is
        metered CLI-side; per-chat spend lives in each chat's `cost_ledger.jsonl`
        and the day/week accumulators live here. The store + `CostState` type live
        in `alkera_cli`; api-core owns only the location."""
        return self._path / "cost_state.json"

    def scoped_cost_state_path(self, scope: str) -> Path:
        """The day/week spend rollup of one spend scope
        (`.alkera/cost_state/<digest>.json`), for a machine whose chats belong to
        several people and orgs and so must never meter against one total. The
        scope is hashed into the file name, so no caller-chosen text reaches the
        path."""
        digest = hashlib.sha256(scope.encode("utf-8")).hexdigest()[:32]
        return self._path / "cost_state" / f"{digest}.json"

    @property
    def decisions_path(self) -> Path:
        """The PROJECT-LEVEL permission-decision audit log (`.alkera/
        decisions.jsonl`) — the fallback for decisions made outside a chat
        (e.g. a non-session `tool.call`). A chat's decisions live beside it in
        `chats/<id>/decisions.jsonl`. The `DecisionRecord`/`DecisionSink` types
        live in `alkera_cli`; api-core owns only the location."""
        return self._path / "decisions.jsonl"

    @property
    def connections_path(self) -> Path:
        """The file recording EXPLICITLY-ADDED connections (`.alkera/
        connections.json`). Discovery only DETECTS candidates; a connection goes
        live for the agent only once added. The store lives in `alkera_cli` (it
        owns the `Connection` type) — api-core owns only the location."""
        return self._path / "connections.json"

    @property
    def connection_health_path(self) -> Path:
        """The standing credential verdict per locally-added connection
        (`.alkera/connection-health.json`). Its own document rather than a field
        on `connections.json`, because that allow-list is lock-free
        last-writer-wins on rare human edits and a background sweep writing into
        it would break that. Team rows keep the server's verdict in
        `team-connections.json`. The store lives in `alkera_cli` -- api-core owns
        only the location."""
        return self._path / "connection-health.json"

    @property
    def connection_state_path(self) -> Path:
        """One standing state per connection this workspace can reach, local and
        team alike (`.alkera/connection-state.json`): the last outcome, when it
        was checked, when it was last verified, and what the credential needs.
        Supersedes `connection-health.json`, which is migrated in on first load
        and then ignored. The store lives in `alkera_cli`; api-core owns only
        the location."""
        return self._path / "connection-state.json"

    @property
    def team_connections_path(self) -> Path:
        """The team/org Preconfigured-Connections cache + member state
        (`.alkera/team-connections.json`), reconciled from the backend by the
        cloud-sync connections lane. Deliberately a SEPARATE file from
        ``connections.json`` — sync never touches the member's own store. The
        store lives in `alkera_cli`; api-core owns only the location."""
        return self._path / "team-connections.json"

    @property
    def sources_path(self) -> Path:
        """The workspace's data-source cards (`.alkera/sources.json`): one card
        per live connection — engine, display name, what it holds, and the
        entities this side is the blessed read side for when two engines carry
        the same thing. Regenerated from the connection list (never hand-kept),
        and rendered into the agent's standing instructions so a question is
        routed to one engine with a stated reason. The store lives in
        `alkera_cli`; api-core owns only the location."""
        return self._path / "sources.json"

    @property
    def source_profiles_path(self) -> Path:
        """Optional per-workspace overrides for the engine profiles that fill in
        a source card (`.alkera/source-profiles.json`), keyed by connection
        handle or by engine. Absent on almost every workspace: it exists so a
        deployment can describe its own sources without a code change."""
        return self._path / "source-profiles.json"

    @property
    def cloud_mirror_path(self) -> Path:
        """The cloud mirror's own state directory (`.alkera/cloud-mirror/`): the
        schema-card ledger the mirror keeps per team connection, which
        ``alkera cloud-mirror status`` reads from another process. Created by
        its writer on first use, never by init. The ledger model lives in
        `alkera_cli`; api-core owns only the location."""
        return self._path / "cloud-mirror"

    @property
    def plugins_enabled_path(self) -> Path:
        """The per-project plugin on/off toggle file (`.alkera/plugins_enabled.json`).
        Plugins are on by default once discovered; this records a user's explicit
        DISABLE so a disabled plugin's tools/lineage/context drop off the surface. The
        store lives in `alkera_cli` — api-core owns only the location."""
        return self._path / "plugins_enabled.json"

    def run_summary_path(self, run_id: str) -> Path:
        """Projected `GraphRunSummary` YAML for one run, beside `lineage.jsonl`."""
        return self.run_dir(run_id) / RUN_SUMMARY_NAME

    def node_summary_path(self, run_id: str, node_instance_id: str) -> Path:
        """Projected `NodeRunSummary` YAML, one per node instance."""
        return (
            self.run_dir(run_id) / f"{_path_component(node_instance_id, 'node_instance_id')}.yaml"
        )

    def trace_dir(self, run_id: str) -> Path:
        """Directory holding one run's append-only event streams. Created
        on demand by the trace writer; not pre-made in `_initialize`."""
        return self.traces_path / _path_component(run_id, "run_id")

    def run_trace_path(self, run_id: str) -> Path:
        """Run-scoped + unrouted event stream."""
        return self.trace_dir(run_id) / RUN_TRACE_NAME

    def coordinator_trace_path(self, run_id: str) -> Path:
        """Coordinator-authored event stream."""
        return self.trace_dir(run_id) / COORDINATOR_TRACE_NAME

    def node_trace_path(self, run_id: str, node_instance_id: str) -> Path:
        """One event stream per node instance under `traces/<run_id>/nodes/`."""
        component = _path_component(node_instance_id, "node_instance_id")
        return self.trace_dir(run_id) / TRACE_NODES_SUBDIR / f"{component}.jsonl"

    def chats(self) -> ChatStore:
        """Return a `ChatStore` scoped to this project."""
        # Late import — avoids circular import between project/* modules.
        from alkera_core.project.chats.store import ChatStore

        return ChatStore(self)

    def blobs(self) -> BlobStore:
        """Return a `BlobStore` scoped to this project's global blob dir."""
        from alkera_core.project.chats.blobs import BlobStore

        return BlobStore(self.blobs_path)

    def cloud_binding(self) -> CloudBindingStore:
        """Return the store for the org this project syncs into
        (`.alkera/cloud.json`). Written by the project's first cloud sync."""
        from alkera_core.project.cloud_binding import CLOUD_BINDING_NAME, CloudBindingStore

        return CloudBindingStore(self._path / CLOUD_BINDING_NAME)

    def plugins(self, name: str, state_type: type[TState]) -> PluginStore[TState]:
        """Return a typed `PluginStore` for plugin ``name`` scoped to this
        project. State persists under ``.alkera/plugins/<name>/`` with the
        store's OWN `FileLock` (locks are not inherited). The ``state_type``
        is the plugin's `PluginState` subclass — needed at runtime to
        validate/default ``state.json``.
        """
        # Late import — avoids circular import between project/* modules.
        from alkera_core.project.plugins.store import PluginStore

        return PluginStore(self.plugins_path / name, state_type)

    # ------------------------------------------------------------------

    def _initialize(self) -> None:
        """Create the directory tree if missing. Idempotent.

        `runs/` and `traces/` are created lazily per run by the lineage and
        trace writers, so they aren't pre-made here.
        """
        self._path.mkdir(parents=True, exist_ok=True)
        self.chats_path.mkdir(parents=True, exist_ok=True)
        self.blobs_path.mkdir(parents=True, exist_ok=True)
        self.lineage_path.mkdir(parents=True, exist_ok=True)
        self.plugins_path.mkdir(parents=True, exist_ok=True)
        self.scheduler_path.mkdir(parents=True, exist_ok=True)
        # Sweep orphaned atomic-write temps left by a crashed writer (older than an
        # hour, so a live concurrent write is never reaped). Best-effort + cheap (a
        # single top-level glob); covers the root-level policy/state files.
        from alkera_core.atomic_io import sweep_stale_temps

        sweep_stale_temps(self._path)


__all__ = [
    "ARTIFACTS_SUBDIR",
    "BLOBS_SUBDIR",
    "CHATS_SUBDIR",
    "COORDINATOR_TRACE_NAME",
    "LINEAGE_LOG_NAME",
    "LINEAGE_SUBDIR",
    "PLUGINS_SUBDIR",
    "RUNS_SUBDIR",
    "RUN_SUMMARY_NAME",
    "RUN_TRACE_NAME",
    "SANDBOX_SUBDIR",
    "SCHEDULER_SUBDIR",
    "TRACES_SUBDIR",
    "TRACE_NODES_SUBDIR",
    "WORKTREES_SUBDIR",
    "ProjectDirectory",
]
