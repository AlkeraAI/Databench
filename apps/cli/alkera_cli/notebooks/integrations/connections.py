"""Notebook SQL against a workspace's Alkera connections.

A notebook kernel acts for its workspace, never for a person. A connection
name resolves among the connections attached to the workspace, read again on
every statement: every connection the workspace OWNER may use (sharing a
workspace shares them all, the owner's personal ones and per-user ones with
the owner's own sign-in included). The statement then runs through the path
``sql.query`` uses: the classifier, the read audit, the cost gate and
settlement, with its credential leased for this workspace alone. A write is
not asked about again: the run a person ran or approved is its approval, and
the decision is recorded naming that run and its requester. The run's
requester is used for attribution, the permission mode and the cost ledger's
key, never to widen what the kernel may reach.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from alkera_notebook.sql.errors import (
    ConnectionNotInWorkspaceError,
    QueryFailedError,
    SqlUnavailableError,
    StatementRefusedError,
    UnknownConnectionError,
)
from alkera_notebook.sql.provider import ArrowResult, Requester, SqlRequest, SqlWorkspace

from alkera_cli.cloud_sync.lease_scope import LEASE_WORKSPACE
from alkera_cli.contracts.tool_types import CapToken, Effect, QueryResult
from alkera_cli.notebooks.integrations.arrow_sql import (
    ArrowRun,
    RunSQLArrowCapability,
    arrow_capability,
)
from alkera_cli.plugins.plugin_base.capabilities import RunSQLCapability
from alkera_cli.plugins.plugin_base.sql import RunAuthorization
from alkera_cli.plugins.plugin_base.sql_tools import run_metered_statement
from alkera_cli.plugins.plugin_base.team_connections import TeamConnectionRecord
from alkera_cli.plugins.plugin_base.tool import ToolError, ToolRegistry


class WorkspaceConnectionScope(Protocol):
    """The team-connection record ids attached to a workspace, as the server
    answers today (asked on every statement). A read that gets no answer
    raises; it is never taken for "none attached"."""

    async def connection_ids(self, workspace: SqlWorkspace) -> frozenset[str]: ...


class WorkspaceConnectionsReader(Protocol):
    """The box's client for the workspace door (``ChatConnectionsClient``)."""

    async def records_for_workspace(self, workspace_id: str) -> frozenset[str]: ...


@dataclass(frozen=True)
class ServerWorkspaceScope:
    """The workspace's attached connections as the server answers them, on
    the box's machine credential (``GET /workspaces/{id}/connections``)."""

    client: WorkspaceConnectionsReader

    async def connection_ids(self, workspace: SqlWorkspace) -> frozenset[str]:
        return await self.client.records_for_workspace(workspace.id)


@dataclass(frozen=True)
class NotebookToolEnvironment:
    """What a statement's tool context carries besides the registry: the
    decisions log, the permission file, the prompt broker, the project
    directory, the box's credential and fence."""

    decision_sink: Any = None
    permissions: Any = None
    broker: Any = None
    alkera_dir: Any = None
    credential: Any = None
    fence: Any = None
    judge: Any = None


def default_permission_mode(actor: Requester) -> str:
    """The mode a statement's decisions are recorded under (and the cost gate
    asks in): the actor's own when the engine passes one, else ``default``."""
    return str(getattr(actor, "permission_mode", "") or "default")


def run_authorization(request: SqlRequest, actor: Requester) -> RunAuthorization | None:
    """The authorization of the run the broker says this statement belongs
    to. The broker hands over a request only for a run the engine is
    executing now, with that run's own requester, so nothing the kernel or a
    model sends decides it. ``None`` for a requester that is neither a person
    nor an agent: its writes keep the ordinary gate."""
    acting_for = getattr(getattr(actor, "acting_for", None), "id", "") or ""
    return RunAuthorization.for_run(
        run_id=request.run_id,
        requester_kind=actor.kind,
        requester_id=actor.id,
        notebook=request.notebook,
        acting_for=str(acting_for),
    )


class _ArrowTap(RunSQLCapability):
    """The connection's SQL capability for one notebook statement: the metered
    path settles cost on the receipt, the notebook takes the Arrow table."""

    def __init__(self, arrow: RunSQLArrowCapability) -> None:
        self.arrow = arrow
        self.runs: list[ArrowRun] = []

    async def run(
        self,
        sql: str,
        *,
        effect: Effect,
        cap_token: CapToken | None,
        limit: int | None,
        params: Mapping[str, Any] | None = None,
    ) -> QueryResult:
        result = await self.arrow.run_arrow(sql, effect=effect, cap_token=cap_token, params=params)
        self.runs.append(result)
        return result.receipt


_REFUSALS = ("permission denied", "cost limit", "denied", "not approved")


def resolve_workspace_connection(
    records: Iterable[TeamConnectionRecord], attached: frozenset[str], name: str
) -> TeamConnectionRecord:
    """The one team record ``name`` means in a workspace whose attached
    record ids are ``attached``, by the name the server gave it (the name the
    picker offers and a cell stores). Refused, never guessed: two enabled
    attached records under the name are ambiguous, a name only other records
    carry is not shared with the workspace, and a name nothing carries is
    unknown. When some attached record has not reached this machine yet, an
    unresolved name may be that one, so it is not called unknown."""
    named = [r for r in records if r.handle == name and r.enabled]
    mine = [r for r in named if r.id in attached]
    if len(mine) > 1:
        raise StatementRefusedError(
            f"more than one connection in this workspace is named {name!r}",
            "ambiguous_connection",
        )
    if mine:
        return mine[0]
    if named:
        raise ConnectionNotInWorkspaceError(name, "not_attached")
    synced = {r.id for r in named} | {r.id for r in records}
    if attached - synced:
        raise SqlUnavailableError(
            f"connection {name!r} may not have reached this machine yet; try again in a moment",
            reason="not_synced",
            connection=name,
        )
    raise UnknownConnectionError(name)


class AlkeraConnectionsProvider:
    """The platform's ``SqlEngineProvider``: every name in a workspace's
    notebooks resolves here (registered first), so a name the workspace does
    not have is unknown rather than found somewhere else."""

    name = "alkera-connections"

    def __init__(
        self,
        *,
        tools: Callable[[], Awaitable[ToolRegistry]],
        records: Callable[[], Awaitable[Iterable[TeamConnectionRecord]]],
        scope: WorkspaceConnectionScope,
        environment: NotebookToolEnvironment | None = None,
        permission_mode: Callable[[Requester], str] = default_permission_mode,
    ) -> None:
        self._tools = tools
        self._records = records
        self._scope = scope
        self._environment = environment or NotebookToolEnvironment()
        self._permission_mode = permission_mode

    def can_resolve(self, connection_name: str, workspace: SqlWorkspace) -> bool:
        """Every name of a workspace the box runs: the server answers which
        connections that workspace has, by the workspace's id alone. Never
        decided on ``workspace.org_id``: a box on its machine credential
        serves many orgs and composes its engines with none, and a provider
        that waited for one claimed no name there, so every connection read
        as unknown."""
        return bool(workspace.id)

    async def cancel(self, query_id: str) -> None:
        # Statements end with their run: cancelling execute() abandons the
        # driver call, and the engine's statement timeout bounds the server
        # side. The receipt arrives only once the result is whole.
        return None

    async def _attached(self, workspace: SqlWorkspace) -> frozenset[str]:
        try:
            return await self._scope.connection_ids(workspace)
        except Exception as exc:
            # Not an answer: "none attached" would send the person looking for
            # a sharing problem that does not exist.
            raise SqlUnavailableError(
                f"this workspace's connections could not be read: {exc}",
                reason="unavailable",
            ) from exc

    async def execute(
        self, request: SqlRequest, actor: Requester, workspace: SqlWorkspace
    ) -> ArrowResult:
        attached = await self._attached(workspace)
        record = resolve_workspace_connection(
            list(await self._records()), attached, request.connection
        )
        # From here the record is the identity: the statement runs on exactly
        # that connection, whatever local handle it took on this machine.
        view = (await self._tools()).restricted(frozenset(), connection_ids=frozenset({record.id}))
        conn = view.connection_by_record(record.id)
        if conn is None:
            raise SqlUnavailableError(
                f"connection {request.connection!r} is not set up on this machine yet; "
                "try again in a moment",
                reason="not_synced",
                connection=request.connection,
            )
        env = self._environment
        ctx = view.build_context(
            permission_mode=self._permission_mode(actor),
            permissions=env.permissions,
            broker=env.broker,
            decision_sink=env.decision_sink,
            judge=env.judge,
            alkera_dir=env.alkera_dir,
            credential=env.credential,
            fence=env.fence,
            # The cost ledger and the audit key on the notebook and requester.
            session_id=f"notebook:{workspace.id}:{request.notebook}:{actor.kind}:{actor.id}",
            task_goal=(
                f"notebook {request.notebook} run {request.run_id} for {actor.kind} {actor.id}"
            ),
        )
        # Every credential this statement leases is leased for this workspace
        # and cached under it (the connector reads it on its worker thread).
        token = LEASE_WORKSPACE.set(workspace.id)
        try:
            return await self._run(ctx, request, conn.handle, run_authorization(request, actor))
        except ToolError as exc:
            text = str(exc)
            if text.lower().startswith(_REFUSALS):
                raise StatementRefusedError(text, "gate") from exc
            raise QueryFailedError(text) from exc
        finally:
            LEASE_WORKSPACE.reset(token)

    async def _run(
        self,
        ctx: Any,
        request: SqlRequest,
        handle: str,
        authorization: RunAuthorization | None,
    ) -> ArrowResult:
        params = dict(request.params) if isinstance(request.params, Mapping) else None
        if request.params is not None and params is None:
            raise QueryFailedError("connection statements take named parameters")
        taps: list[_ArrowTap] = []

        def adapt(cap: RunSQLCapability) -> RunSQLCapability:
            taps.append(_ArrowTap(arrow_capability(cap)))
            return taps[-1]

        receipt = await run_metered_statement(
            ctx,
            connection=handle,
            sql=request.sql,
            params=params,
            adapt=adapt,
            authorization=authorization,
        )
        run = taps[-1].runs[-1]
        return ArrowResult(
            reader=run.reader(),
            query_id=str(receipt.query_id or uuid.uuid4().hex),
            rows=run.table.num_rows,
            bytes=run.table.nbytes,
            truncated=bool(receipt.truncated),
            meta={"engine": receipt.engine, "connection": request.connection},
        )
