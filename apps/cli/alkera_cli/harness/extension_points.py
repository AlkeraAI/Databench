"""What a distribution adds to the harness, by registration.

The harness runs chats, a per-project scheduler and the connection surface of a
workspace. What a chat is told about the workspace's data, which background jobs
keep that data current, and how a connection's state is recorded belong to the
data product, which the open harness never imports. It reads these points
instead, and a distribution registers into them from an
:class:`alkera_core.extensions.Extension` its composition root installs.

With nothing registered the harness runs on its own: a chat gets only the
platform's own guidance and briefs, the scheduler runs only the platform's own
standing jobs, and no connection state is recorded or announced.

- :data:`HARNESS_CONTEXT_PROVIDERS`: knowledge sources. Each may add a block to a
  session's standing instructions, a brief to its first root turn, and an index
  of the workspace's own files to the project's knowledge base.
- :data:`HARNESS_STANDING_JOBS`: singleton background jobs every runtime binds
  and arms, and the workspace events that bring each one forward.
- :data:`HARNESS_CONNECTION_JOBS`: per-connection background jobs, armed with the
  connection refresh jobs whenever the connection set changes.
- :data:`CONNECTION_RECORDS`: the one record of each connection row's identity and
  state. At most one is registered.
- :data:`SESSION_SPEND`: what a session's queries cost. At most one is registered.
- :data:`SHARED_PROJECT_STEPS`: what a store does when one project is shared by
  several tenants (a box), run once as the runtime is composed.
- :data:`WORKSPACE_SEEDERS`: what fills a workspace's stores from its
  connections' providers, and arms each connection's refresh. At most one is
  registered.
- :data:`OPENED_PROJECT_STEPS`: what a store does when the daemon first opens a
  project (bring its schema to head), best-effort.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, NamedTuple, Protocol

from alkera_core.extensions import ExtensionError, ExtensionPoint

if TYPE_CHECKING:
    import asyncio
    from datetime import datetime

    from alkera_core.project.directory import ProjectDirectory

    from alkera_cli.plugins.plugin_base import (
        Connection,
        ConnectionFormSchema,
        PluginRegistry,
        ToolRegistry,
    )
    from alkera_cli.plugins.plugin_base.scheduler import JobRunner, Scheduler


# ---------------------------------------------------------------------------
# Context providers
# ---------------------------------------------------------------------------


_logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class InstructionRequest:
    """A session about to start: the project, the team connections its chat may
    see (``None`` on a runtime with no scope, where every connection is the one
    person's), and the permission mode it opens in."""

    project: ProjectDirectory
    connection_ids: frozenset[str] | None
    mode: str


@dataclass(frozen=True, slots=True)
class BriefRequest:
    """A session's first root turn.

    ``tools`` is the session's own view of the agent tools, ``None`` for a
    session with no binding. ``scoped`` says that view is limited to the chat's
    own connections (a shared box), so a brief must read through it and never
    through the project's whole store. ``seeding`` says a background seed is in
    flight. ``plugins`` resolves the project's plugin registry on demand."""

    project: ProjectDirectory
    tools: ToolRegistry | None
    scoped: bool
    seeding: bool
    plugins: Callable[[], Awaitable[PluginRegistry]]


#: A block for the session's standing instructions, or ``""`` for none.
InstructionsHook = Callable[[InstructionRequest], Awaitable[str]]
#: A brief for the session's first root turn, or ``""`` for none. Never raises.
BriefHook = Callable[[BriefRequest], Awaitable[str]]
#: Index the workspace's own files into the project's knowledge base, without
#: embedding. Synchronous; the harness runs it off the event loop.
WorkspaceSeedHook = Callable[["ProjectDirectory"], None]


@dataclass(frozen=True, slots=True)
class HarnessContextProvider:
    """One knowledge source a chat is told about. Every hook is optional."""

    name: str
    instructions: InstructionsHook | None = None
    opening_brief: BriefHook | None = None
    seed_workspace: WorkspaceSeedHook | None = None


#: Knowledge sources, in the order their blocks and briefs appear.
HARNESS_CONTEXT_PROVIDERS: ExtensionPoint[HarnessContextProvider] = ExtensionPoint(
    "harness_context_providers"
)


# ---------------------------------------------------------------------------
# Background jobs
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class JobContext:
    """What a job binds its runner and arms its job with.

    ``kb_worker`` and ``column_worker`` run the knowledge index and the column
    lineage drain in a child process; both are ``None`` in-process (the CLI),
    where a runner does its work on a worker thread. ``column_lock`` bounds the
    column drain to one at a time across every path that runs it."""

    project: ProjectDirectory
    scheduler: Scheduler
    now: datetime
    kb_worker: Callable[[str, bool], Awaitable[None]] | None
    column_worker: Callable[[str], Awaitable[None]] | None
    column_lock: asyncio.Lock


#: The workspace events that bring a standing job forward.
#: ``files_changed``: a repository file changed. ``connections_changed``: the
#: connection set changed. ``table_lineage_settled``: every per-connection
#: table lineage refresh has finished.
JobNudge = Literal["files_changed", "connections_changed", "table_lineage_settled"]


@dataclass(frozen=True, slots=True)
class StandingJob:
    """A singleton job every runtime knows. Its job id is its ``kind``.

    ``runner`` binds the runner, ``arm`` registers the job and returns its id;
    both are idempotent. A standing kind is never pruned as an orphan."""

    kind: str
    runner: Callable[[JobContext], JobRunner]
    arm: Callable[[JobContext], str]
    nudged_on: frozenset[JobNudge] = field(default_factory=frozenset)


@dataclass(frozen=True, slots=True)
class ConnectionJob:
    """A per-connection job family, armed with the connection refresh jobs.

    ``arm`` registers one job per connection it applies to (and its runner) and
    returns the job ids. Its kind is never pruned as an orphan, and its jobs are
    not lineage table refreshes, so a run of one never holds the column drain
    back."""

    kind: str
    arm: Callable[[PluginRegistry, JobContext], list[str]]


#: Standing jobs a distribution adds, armed after the platform's own.
HARNESS_STANDING_JOBS: ExtensionPoint[StandingJob] = ExtensionPoint("harness_standing_jobs")

#: Per-connection job families, armed in registration order.
HARNESS_CONNECTION_JOBS: ExtensionPoint[ConnectionJob] = ExtensionPoint("harness_connection_jobs")


# ---------------------------------------------------------------------------
# Connection records
# ---------------------------------------------------------------------------


class ConnectionIdentity(NamedTuple):
    """Who a connection key names: ``origin`` is ``local`` or ``team``."""

    origin: str
    plugin: str
    handle: str
    record_id: str


class ConnectionRecords(Protocol):
    """The record of each connection row a workspace can reach: its key, its
    standing state, and the team rows synced from the organisation."""

    def key_for(self, conn: Connection) -> str:
        """The key ``conn``'s state is recorded under."""
        ...

    def local_key(self, plugin: str, handle: str) -> str:
        """The key of one of the member's own connections."""
        ...

    def team_key(self, record_id: str) -> str:
        """The key of a team row."""
        ...

    def identity_of(self, key: str) -> ConnectionIdentity:
        """The identity a key names."""
        ...

    def on_state_written(self, listener: Callable[[Path, str], None]) -> Callable[[], None]:
        """Call ``listener(project_path, key)`` after any writer records a row's
        state. Returns the unsubscribe."""
        ...

    def records_verification(
        self, project: ProjectDirectory, conn: Connection
    ) -> AbstractAsyncContextManager[None]:
        """Record the wrapped check's outcome as ``conn``'s verified state."""
        ...

    def pending_hints(self, project: ProjectDirectory) -> list[str]:
        """One line per shared connection the member still has to act on. A
        record this machine cannot read offers no hint."""
        ...

    def team_row_identity(
        self, project: ProjectDirectory, record_id: str
    ) -> tuple[str, str] | None:
        """The ``(plugin, local_handle)`` a team row's jobs are keyed on."""
        ...

    def team_connection(
        self, project: ProjectDirectory, registry: PluginRegistry, plugin: str, handle: str
    ) -> Connection | None:
        """The configured team row that is ``plugin``/``handle`` here."""
        ...

    def local_form(self, schema: ConnectionFormSchema) -> ConnectionFormSchema:
        """``schema`` with only the auth methods a member may fill in locally."""
        ...


#: The connection record. At most one is registered.
CONNECTION_RECORDS: ExtensionPoint[ConnectionRecords] = ExtensionPoint("connection_records")


def connection_records() -> ConnectionRecords | None:
    """The registered :class:`ConnectionRecords`, or ``None``: with no record
    nothing is recorded or announced about a connection."""
    records = CONNECTION_RECORDS.items()
    if len(records) > 1:
        raise ExtensionError("more than one extension records connection state")
    return records[0] if records else None


# ---------------------------------------------------------------------------
# Session spend
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SessionSpend:
    """What one session's queries cost: how many were recorded, and the charged
    dollars per connection."""

    queries: int = 0
    by_connection: Mapping[str, float] = field(default_factory=dict)


class SessionSpendLedger(Protocol):
    """Reads one session's recorded query spend."""

    def spend(self, project: ProjectDirectory, session_id: str) -> SessionSpend: ...


#: The session spend ledger. At most one is registered.
SESSION_SPEND: ExtensionPoint[SessionSpendLedger] = ExtensionPoint("session_spend")


def session_spend(project: ProjectDirectory, session_id: str) -> SessionSpend:
    """``session_id``'s recorded spend; nothing spent when no ledger is
    registered."""
    ledgers = SESSION_SPEND.items()
    if len(ledgers) > 1:
        raise ExtensionError("more than one extension records session spend")
    return ledgers[0].spend(project, session_id) if ledgers else SessionSpend()


#: A step run once when a runtime is composed for a project several tenants
#: share (a box serving many orgs' chats): a store that keeps per-tenant facts in
#: the one project marks itself so it never mixes them.
SharedProjectStep = Callable[["ProjectDirectory"], None]

SHARED_PROJECT_STEPS: ExtensionPoint[SharedProjectStep] = ExtensionPoint("shared_project_steps")


def prepare_shared_project(
    project: ProjectDirectory,
    point: ExtensionPoint[SharedProjectStep] = SHARED_PROJECT_STEPS,
) -> None:
    """Run every step registered on ``point`` for a project several tenants share."""
    for step in point.items():
        step(project)


#: A step run once when the daemon first opens a project, so a store is ready
#: before the first chat or producer reaches it. Best-effort: a step that fails
#: leaves the store to its own lazy path.
OpenedProjectStep = Callable[["ProjectDirectory"], None]

OPENED_PROJECT_STEPS: ExtensionPoint[OpenedProjectStep] = ExtensionPoint("opened_project_steps")


def prepare_opened_project(
    project: ProjectDirectory,
    point: ExtensionPoint[OpenedProjectStep] = OPENED_PROJECT_STEPS,
) -> None:
    """Run every step registered on ``point`` for a project the daemon just
    opened. A step that fails is logged and the rest still run."""
    for step in point.items():
        try:
            step(project)
        except Exception:
            _logger.warning("opened-project step failed", exc_info=True)


class WorkspaceSeeder(Protocol):
    """Fills a workspace's stores from what its connections' providers emit, and
    arms each connection's background refresh. The harness decides when and for
    which connections; the seeder owns the stores and the passes."""

    async def seed_table(
        self, registry: Any, project: ProjectDirectory, work: Any, *, lock: Any, force: bool
    ) -> None:
        """Seed ``work``'s table-grain facts, serialized on ``lock``."""
        ...

    async def seed(
        self,
        registry: Any,
        project: ProjectDirectory,
        work: Any,
        *,
        lock: Any,
        column_lock: Any,
        do_lineage: bool,
        do_context: bool,
        force: bool,
    ) -> None:
        """Seed ``work`` for the selected engines, then drain what they derive."""
        ...

    async def seed_column(self, project: ProjectDirectory, *, lock: Any) -> None:
        """Drain the project-wide column grain, serialized on ``lock``."""
        ...

    async def drain_embeddings(self, project: ProjectDirectory) -> None:
        """Backfill the vectors of every card written without one."""
        ...

    def schedule_refresh(
        self,
        registry: Any,
        scheduler: Any,
        project: ProjectDirectory,
        *,
        now: Any,
        seed_lock: Any,
        seed_subprocess: Any,
        on_seeded: Any,
    ) -> list[str]:
        """Register each refreshable connection's refresh job and its runner;
        the job ids touched."""
        ...


#: The workspace seeder. At most one is registered.
WORKSPACE_SEEDERS: ExtensionPoint[WorkspaceSeeder] = ExtensionPoint("workspace_seeders")


def workspace_seeder(
    point: ExtensionPoint[WorkspaceSeeder] = WORKSPACE_SEEDERS,
) -> WorkspaceSeeder | None:
    """The seeder registered on ``point``, or ``None``: a build with none seeds
    nothing and arms no refresh."""
    registered = point.items()
    if len(registered) > 1:
        raise ExtensionError("more than one extension seeds the workspace")
    return registered[0] if registered else None


__all__ = [
    "CONNECTION_RECORDS",
    "HARNESS_CONNECTION_JOBS",
    "HARNESS_CONTEXT_PROVIDERS",
    "HARNESS_STANDING_JOBS",
    "OPENED_PROJECT_STEPS",
    "SESSION_SPEND",
    "SHARED_PROJECT_STEPS",
    "WORKSPACE_SEEDERS",
    "BriefHook",
    "BriefRequest",
    "ConnectionIdentity",
    "ConnectionJob",
    "ConnectionRecords",
    "HarnessContextProvider",
    "InstructionRequest",
    "InstructionsHook",
    "JobContext",
    "JobNudge",
    "OpenedProjectStep",
    "SessionSpend",
    "SessionSpendLedger",
    "SharedProjectStep",
    "StandingJob",
    "WorkspaceSeedHook",
    "WorkspaceSeeder",
    "connection_records",
    "prepare_opened_project",
    "prepare_shared_project",
    "session_spend",
    "workspace_seeder",
]
