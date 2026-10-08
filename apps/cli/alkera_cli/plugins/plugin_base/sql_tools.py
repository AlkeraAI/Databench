"""Capability-backed built-in SQL tools.

``sql.query`` is framework-owned: its ``run`` delegates to the connection's
``RunSQLCapability``. ANY plugin whose connection satisfies that capability
gets this tool for free — it writes no tool code. The agent selects the
connection per task via the typed ``connection`` handle.

Every statement is classified per call, and a non-READ effect is gated at the
connector via a cap-token.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any, ClassVar, Literal, assert_never

from alkera_core.connectors.redaction import redact_secrets
from alkera_core.extensions import ExtensionPoint
from alkera_core.project import ProjectDirectory
from alkera_core.schemas.objects import Scalar
from pydantic import BaseModel, Field, RootModel, model_validator
from pydantic_core import to_jsonable_python

from alkera_cli.contracts.tool_types import CapToken, Effect, RelationMeta
from alkera_cli.plugins.plugin_base.capabilities import (
    EstimateSQLCostCapability,
    IntrospectSchemaCapability,
    RunSQLCapability,
)
from alkera_cli.plugins.plugin_base.connection import Connection
from alkera_cli.plugins.plugin_base.cost import (
    CostDeniedError,
    CostLimits,
    CostReservation,
    gate_cost,
    release_cost,
    reserve_or_refuse,
    settle_cost,
)
from alkera_cli.plugins.plugin_base.delivery import (
    NOTE_NO_BLOB_CLAUSE,
    RESULT_NAME_FIELD,
    SqlQueryResult,
    deliver_rows,
    limit_note,
)
from alkera_cli.plugins.plugin_base.permissions import (
    binding_from_context,
    descriptor_from_sql,
    gate_sql_action,
)
from alkera_cli.plugins.plugin_base.permissions.audit import AuditUnavailableError
from alkera_cli.plugins.plugin_base.permissions.gate import NO_SINK_REASON, denied_error
from alkera_cli.plugins.plugin_base.permissions.resolve import AUDIT_FAILED_REASON
from alkera_cli.plugins.plugin_base.schema_freshness import relations_listed
from alkera_cli.plugins.plugin_base.sql.provenance import provenance_for
from alkera_cli.plugins.plugin_base.sql.run_authorization import RunAuthorization
from alkera_cli.plugins.plugin_base.sql.system_namespaces import hidden_from_listing
from alkera_cli.plugins.plugin_base.sql.timeout import statement_timeout_note
from alkera_cli.plugins.plugin_base.tool import (
    Tool,
    ToolContext,
    ToolError,
    ToolRegistry,
    ToolSpec,
    classify_tool_failure,
)

logger = logging.getLogger(__name__)

#: Failure classes that indict the CONNECTION rather than the query. "error" is
#: excluded on purpose: bad SQL from the agent classifies "error", and recording
#: it would badge a healthy connection broken on every typo.
_CONNECTION_FAULTS = ("invalid_credential", "unreachable", "timeout", "permission")

#: The userinfo in a connection string — ``postgresql://user:secret@host/db``.
#: Scrubbed structurally so a driver that echoes its own DSN is safe even when
#: the secret itself could not be resolved for an exact match.
_DSN_USERINFO = re.compile(r"([A-Za-z][A-Za-z0-9+.\-]*://[^\s:/@]*:)[^@\s]+(@)")

#: A bound on resolving a connection's secrets on the ERROR path. A leased
#: bundle comes from the daemon's warm cache and an env/file credential is a
#: local read, so this only ever bounds a pathological case.
_SECRET_RESOLVE_TIMEOUT_SECONDS = 2.0


async def _connection_secrets(ctx: ToolContext, connection: str) -> tuple[str, ...]:
    """The plaintext THIS connection would have handed the driver.

    Resolved through the same manager the connector resolves with — and through
    the same helper the state recorder scrubs with, so the LLM-visible detail and
    the persisted one can never disagree about what counts as a secret.
    Best-effort by design: a reference that will not resolve simply is not
    matched, and the structural scrub still applies."""
    from alkera_cli.plugins.plugin_base.credential_manager import credential_plaintexts

    try:
        conn = ctx.registry.connection_for(connection)
    except Exception:
        return ()
    if conn is None:
        return ()
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(credential_plaintexts, conn), _SECRET_RESOLVE_TIMEOUT_SECONDS
        )
    except Exception:
        return ()


async def _safe_detail(ctx: ToolContext, connection: str, text: str) -> str:
    """A driver's own words with anything that could be a credential taken out.

    The connector resolves the secret at the I/O boundary and a driver is free
    to put its whole connection string in the error it raises. That text becomes
    a ``ToolError``, which becomes a tool result the model reads and a transcript
    entry a human can page back to — the one LLM-visible sink on this path. So it
    goes through the platform's one redaction seam (:func:`redact_secrets`), plus a
    structural scrub of a DSN's userinfo for the case where the secret itself
    could not be resolved."""
    return redact_secrets(
        _DSN_USERINFO.sub(r"\1***\2", text), await _connection_secrets(ctx, connection)
    )


#: ``(project, connection, exception)``: record a connection-indicting query
#: failure where the connection's standing verdict is kept. Registered by a
#: distribution that keeps one; each recorder is best-effort.
QueryOutcomeRecorder = Callable[[ProjectDirectory, Connection, BaseException], None]

#: Where a query that indicts its connection is reported, in registration order.
QUERY_OUTCOME_RECORDERS: ExtensionPoint[QueryOutcomeRecorder] = ExtensionPoint(
    "sql.query_outcome_recorders"
)


async def _feed_query_health(ctx: ToolContext, connection: str, exc: Exception) -> None:
    """Feed a connection-indicting query failure into the credential-health store.

    Best-effort: a failure recording health must never replace the tool's answer,
    and it fires exactly when the machine is already unhealthy."""
    recorders = QUERY_OUTCOME_RECORDERS.items()
    if not recorders or ctx.alkera_dir is None:
        return
    if classify_tool_failure(exc) not in _CONNECTION_FAULTS:
        return
    project = ProjectDirectory(ctx.alkera_dir)
    for record in recorders:
        try:
            conn = ctx.resolve_connection(connection)
            await asyncio.to_thread(record, project, conn, exc)
        except Exception:
            logger.warning("recording connection health failed", exc_info=True)


#: The background flag, shared by both call shapes (the discriminated union can't
#: carry a field outside its members).
_BACKGROUND_FIELD = Field(
    default=False,
    description=(
        "Run this query in the BACKGROUND: return a job id immediately and notify "
        "you with the rows when it completes. Use for a heavy READ query whose result "
        "you don't need right away. Only read-only queries can be backgrounded (run a "
        "write in the foreground). NOTE: a backgrounded query is cost-checked up front "
        "with no prompt — if it would exceed a cap it is REFUSED, not queued; run it in "
        "the foreground to be prompted to raise the cap. Don't poll; you'll be notified."
    ),
)


#: How much of a statement an error quotes before eliding the rest.
_QUOTED_STATEMENT_CHARS = 200


def _one_line(sql: str) -> str:
    """``sql`` as one bounded, backticked line for an error sentence."""
    flat = " ".join(sql.split())
    if len(flat) > _QUOTED_STATEMENT_CHARS:
        flat = flat[: _QUOTED_STATEMENT_CHARS - 1].rstrip() + "…"
    return f"`{flat}`"


class BreakIntent(BaseModel):
    """A declaration that this write's damage is wanted."""

    assets: list[str] = Field(
        default_factory=list,
        description=(
            "The assets you mean to break, as the refusal named them. A table covers "
            "its own columns."
        ),
    )
    reason: str = Field(
        default="", description="Why breaking them is the right move for this task."
    )


def _infer_mode(value: Any, cues: tuple[tuple[str, str], ...], fallback: str = "") -> Any:
    """Recover the union tag for a call that omits ``mode``.

    The flattened wire schema (``_flatten_union_root``) advertises ``mode`` as
    optional, since no variant requires it, so agents compose mode-less calls,
    and a discriminated union that then demands the tag bounces them before any
    gate logic runs. Each variant is named by a field only it carries, so the
    tag is recovered from the payload's shape. A payload naming no cue and no
    fallback is refused with the legal shapes spelled out."""
    if not isinstance(value, dict) or "mode" in value:
        return value
    for cue_field, tag in cues:
        if cue_field in value:
            return {**value, "mode": tag}
    if fallback:
        return {**value, "mode": fallback}
    legal = " or ".join(f"mode={tag!r} with a {cue!r} field" for cue, tag in cues)
    raise ValueError(f"missing mode: pass {legal}")


class QueryBySql(BaseModel):
    mode: Literal["sql"] = "sql"
    connection: str
    sql: str
    # The compiler's own value type: every value ``compile_query`` binds has to
    # pass this boundary — the date a declared daterange compiles to included —
    # while a free-form object would leave the model and the tool card with no
    # shape to fill.
    params: dict[str, Scalar] | None = Field(
        default=None,
        description=(
            "Values for placeholders in sql, bound by the driver and never written into "
            "the statement: %(name)s on Postgres/Redshift, {name:Type} on ClickHouse, "
            "{{String(name)}} (or Int64/Float64/Date/Boolean) on Tinybird — whose SQL API "
            "reads a ClickHouse {name:Type} as a workspace secret and refuses it — $name "
            "on DuckDB, :name on SQLite. One scalar per placeholder — a string, number, "
            "boolean, date or null; a list is spelled as one placeholder per item. Prefer "
            "a placeholder over quoting a value into the SQL when the value comes from "
            "data or from the user."
        ),
    )
    limit: int = 100
    result_name: str = RESULT_NAME_FIELD
    background: bool = _BACKGROUND_FIELD
    intent: BreakIntent | None = Field(
        default=None,
        description=(
            "Set this only after a write was refused for downstream impact. It records "
            "what you are deliberately breaking and why, and lets the rest of the "
            "migration run without challenging every step."
        ),
    )


class QueryByTable(BaseModel):
    mode: Literal["table"] = "table"
    connection: str
    table: str
    columns: list[str] | None = None
    limit: int = 100
    result_name: str = RESULT_NAME_FIELD
    background: bool = _BACKGROUND_FIELD


class SqlQueryInput(RootModel[Annotated[QueryBySql | QueryByTable, Field(discriminator="mode")]]):
    """Two legal call signatures: raw ``sql`` or a structured ``table`` read.
    The discriminator ``mode`` makes both shapes visible to the model and the
    ``match`` exhaustive to the type-checker. A call that omits ``mode`` (the
    shape the flattened wire schema invites) is accepted, the tag inferred
    from which cue field it carries."""

    @model_validator(mode="before")
    @classmethod
    def _mode_from_shape(cls, value: Any) -> Any:
        return _infer_mode(value, (("sql", "sql"), ("table", "table")))


async def _authorize(
    spec: QueryBySql | QueryByTable, descriptor: Any, ctx: ToolContext, handle: str
) -> CapToken | None:
    """Clear one statement to run, or raise the refusal the model reads.

    A READ needs no token (the connector runs it in a read path) but it IS
    warehouse data access, so it lands in the audit log under the same discipline
    as a decision. A write goes to the gate, which measures what it breaks before
    anyone decides."""
    if descriptor.effect == Effect.READ:
        await _record_read_or_refuse(ctx, descriptor)
        return None
    binding = binding_from_context(ctx)
    if binding.engine(source="sql_gate") is None:
        raise ToolError(f"permission denied: {NO_SINK_REASON}")
    declared = spec.intent if isinstance(spec, QueryBySql) else None
    gate = await gate_sql_action(
        descriptor,
        mode=ctx.permission_mode,
        binding=binding,
        intent=(declared.assets, declared.reason) if declared is not None else None,
    )
    if gate.cap_token is None:
        # The impact path, the judge's verdict, or the policy's reasons. Never a
        # bare "not approved", which tells the agent nothing it can act on.
        detail = gate.reason or f"{_statement_words(descriptor)} on {handle!r} was not approved"
        raise ToolError(denied_error(detail))
    return gate.cap_token


def _statement_words(descriptor: Any) -> str:
    """How a refusal names the statement: its operation, or "this statement"
    when the classifier could not read it. The classifier's ``unknown`` is
    its own bookkeeping, not a word a reader can act on."""
    if descriptor.confidence == "unknown" or descriptor.operation in ("", "unknown"):
        return "this statement"
    return str(descriptor.operation)


async def _authorize_run(
    descriptor: Any, ctx: ToolContext, authorization: RunAuthorization
) -> CapToken:
    """Clear a non-READ statement of a notebook run: the run is the approval,
    so no one is asked again, but the decision is recorded naming the run and
    who asked for it, and refused when it cannot be. The cap token is minted
    for this statement alone, as the gate mints one."""
    engine = binding_from_context(ctx).engine(source="sql_gate")
    if engine is None:
        raise ToolError(f"permission denied: {NO_SINK_REASON}")
    try:
        await engine.record_run_allow(
            descriptor, mode=ctx.permission_mode, reasons=authorization.audit_reasons()
        )
    except AuditUnavailableError as exc:
        raise ToolError(f"permission denied: {AUDIT_FAILED_REASON}") from exc
    return CapToken.mint(descriptor)


_BARE_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")
_QUOTED_IDENTIFIER = re.compile(r'"[^"]+"|`[^`]+`|\[[^\]]+\]')


def _is_identifier_path(text: str) -> bool:
    """True when ``text`` is a dotted identifier path (``schema.table``,
    ``"Weird Name".col``, ``[dbo].[t]``) and nothing else: no whitespace outside
    quotes, no operators, no second statement. The compiled statement is still
    classified and gated afterwards; this refuses the shapes that only an
    injection produces before they are ever spelled into SQL."""
    for part in text.split("."):
        if not part:
            return False
        if _BARE_IDENTIFIER.fullmatch(part) or _QUOTED_IDENTIFIER.fullmatch(part):
            continue
        return False
    return True


def _compile_table_query(args: QueryByTable, dialect: str = "") -> str:
    """The statement a structured table read runs, with its row limit IN the
    statement — the engine stops at ``limit`` rows rather than the tool fetching
    more and cutting them afterwards — spelled the engine's way (``LIMIT n``,
    ``TOP n``, ``FETCH FIRST n ROWS ONLY``). A dialect sqlglot does not know, or a
    name it cannot parse, falls back to a plain ``LIMIT``."""
    if not _is_identifier_path(args.table):
        raise ToolError(f"table must be a table name such as schema.table, not {args.table!r}")
    for column in args.columns or ():
        if column != "*" and not _is_identifier_path(column):
            raise ToolError(f"columns must be column names, not {column!r}")
    cols = ", ".join(args.columns) if args.columns else "*"
    base = f"SELECT {cols} FROM {args.table}"  # noqa: S608 -- identifiers only; classified and gated downstream
    try:
        import sqlglot

        read = dialect or None
        if read is not None:
            sqlglot.Dialect.get_or_raise(read)
        select = sqlglot.parse_one(base, read=read)
        if isinstance(select, sqlglot.exp.Select):
            return select.limit(args.limit).sql(dialect=read)
    except Exception:  # an unknown dialect or an unparseable name keeps the plain LIMIT
        logger.debug("table read %r compiled without sqlglot", args.table, exc_info=True)
    return f"{base} LIMIT {args.limit}"


async def _record_read_or_refuse(ctx: ToolContext, descriptor: Any) -> None:
    """Audit a READ, refusing when there is no audit sink — the floor every read
    crosses (``sql.query`` and ``data.join`` alike), so no read runs untracked."""
    engine = binding_from_context(ctx).engine(source="sql_gate")
    if engine is None:
        raise ToolError(f"permission denied: {NO_SINK_REASON}")
    try:
        await engine.record_read(descriptor, mode=ctx.permission_mode)
    except AuditUnavailableError as exc:
        raise ToolError(f"permission denied: {AUDIT_FAILED_REASON}") from exc


async def _gate_read_cost(
    ctx: ToolContext, descriptor: Any, *, connection: str, caps: Any
) -> tuple[CostReservation | None, Any]:
    """Foreground cost gate: reserve against the per-query/chat/day/week caps
    (may prompt to raise one) and return ``(reservation, cost_capability)``.
    ``(None, None)`` when no ledger is bound. The background path reserves
    differently (refuse-or-run, no prompt), so it stays in ``sql.query``."""
    if ctx.cost_ledger is None:
        return None, None
    cost_cap = caps.get(EstimateSQLCostCapability) if caps is not None else None
    limits = CostLimits.from_config(
        getattr(ctx.permissions, "cost", None), connection=connection, tool="sql.query"
    )
    try:
        reservation = await gate_cost(
            descriptor,
            limits=limits,
            ledger=ctx.cost_ledger,
            session_id=ctx.session_id,
            connection=connection,
            operation=descriptor.operation or "query",
            cost_capability=cost_cap,
            broker=ctx.broker,
            now=datetime.now(UTC),
        )
    except CostDeniedError as exc:
        raise ToolError(f"cost limit: {exc}") from exc
    return reservation, cost_cap


@dataclass(frozen=True)
class MeteredExec:
    """One classified statement plus the cost reservation to reconcile against it."""

    cap: Any
    sql: str
    descriptor: Any
    cap_token: Any
    limit: int | None
    connection: str
    reservation: CostReservation | None
    cost_cap: Any
    params: dict[str, Any] | None = None
    """Bound values for the placeholders in ``sql`` — beside the statement, never in it."""


async def _run_and_settle(req: MeteredExec, ctx: ToolContext) -> Any:
    """Run one gated query and reconcile its cost, returning the raw ``QueryResult``.
    The single money chokepoint every read producer shares, so ``sql.query`` and
    ``data.join`` meter identically. Every non-success exit refunds the reservation
    (including ``CancelledError``, a ``BaseException`` a bare ``except Exception``
    would leak). Settle/release stamp a fresh time so a query that crossed a
    day/week boundary bills the right window."""
    try:
        # Bound values travel only when there are some: a capability that binds
        # nothing (a plain statement) is called exactly as before.
        extra: dict[str, Any] = {"params": req.params} if req.params else {}
        result = await req.cap.run(
            req.sql,
            effect=req.descriptor.effect,
            cap_token=req.cap_token,
            limit=req.limit,
            **extra,
        )
    except BaseException as exc:
        if req.reservation is not None and ctx.cost_ledger is not None:
            release_cost(
                req.reservation,
                ledger=ctx.cost_ledger,
                session_id=ctx.session_id,
                now=datetime.now(UTC),
            )
        if isinstance(exc, Exception):
            # The driver's text is the ONE thing on this path that could carry a
            # resolved secret (a connect failure echoing its DSN), and it lands
            # in the model's context and the transcript. Redact before it does.
            timeout_note = statement_timeout_note(exc)
            if timeout_note is not None:
                raise ToolError(await _safe_detail(ctx, req.connection, timeout_note)) from exc
            await _feed_query_health(ctx, req.connection, exc)
            raise ToolError(
                await _safe_detail(ctx, req.connection, f"query failed: {exc}")
            ) from exc
        raise
    if req.reservation is not None and ctx.cost_ledger is not None:
        await settle_cost(
            req.descriptor,
            result,
            req.reservation,
            ledger=ctx.cost_ledger,
            session_id=ctx.session_id,
            cost_capability=req.cost_cap,
            now=datetime.now(UTC),
        )
    return result


#: Wraps a connection's SQL capability for one statement: how a caller that
#: wants the result in another form (a notebook's Arrow) runs on the same path.
CapabilityAdapter = Callable[[RunSQLCapability], RunSQLCapability]


async def run_metered_statement(
    ctx: ToolContext,
    *,
    connection: str,
    sql: str,
    params: dict[str, Any] | None = None,
    limit: int | None = None,
    intent: BreakIntent | None = None,
    adapt: CapabilityAdapter | None = None,
    authorization: RunAuthorization | None = None,
) -> Any:
    """Run one statement through ``sql.query``'s gate and cost path and return
    the raw ``QueryResult``, for callers outside the tool (``data.join``, a
    notebook's ``sql.execute``).

    A read is audited (refused with no audit sink); anything else goes to the
    write gate for a cap token, as ``sql.query``'s writes do, with ``intent``
    when a refused write is being re-run deliberately. Then the cost gate
    reserves, the statement runs, and the reservation is settled or refunded.
    ``params`` are bound beside the statement, never rendered into it.
    ``adapt`` wraps the connection's capability for this statement only; the
    classification, gates and settlement are unchanged.

    ``authorization`` is the notebook run this statement belongs to, built by
    code from the run the engine is executing (no tool input carries it). With
    it, a non-READ statement is not sent to the write gate's ask: the run is
    the approval, recorded with its run and requester before a token is
    minted. The read audit, the cost gate and settlement are the same either
    way. Raises ``ToolError`` with the refusal or the (redacted) failure."""
    conn = ctx.resolve_connection(connection)
    caps = ctx.capabilities_for(connection)
    cap = caps.get(RunSQLCapability) if caps is not None else None  # type: ignore[type-abstract]
    if cap is None:
        raise ToolError(f"connection {connection!r} has no SQL capability")
    descriptor = descriptor_from_sql(
        sql, dialect=conn.dialect or "", capability="sql", connection=conn.handle
    )
    if descriptor.effect == Effect.READ:
        await _record_read_or_refuse(ctx, descriptor)
        cap_token = None
    elif authorization is not None:
        cap_token = await _authorize_run(descriptor, ctx, authorization)
    else:
        spec = QueryBySql(connection=connection, sql=sql, params=params, intent=intent)
        cap_token = await _authorize(spec, descriptor, ctx, conn.handle)
    reservation, cost_cap = await _gate_read_cost(
        ctx, descriptor, connection=conn.handle, caps=caps
    )
    return await _run_and_settle(
        MeteredExec(
            cap=adapt(cap) if adapt is not None else cap,
            sql=sql,
            descriptor=descriptor,
            cap_token=cap_token,
            limit=limit,
            connection=conn.handle,
            reservation=reservation,
            cost_cap=cost_cap,
            params=params,
        ),
        ctx,
    )


async def run_metered_read(
    ctx: ToolContext, *, connection: str, sql: str, limit: int | None
) -> Any:
    """Run one read-only statement through ``sql.query``'s gate + cost path and
    return the raw ``QueryResult`` (rows plus the connector's own ``truncated``,
    which says the source itself was capped — a distinction the delivered
    ``SqlQueryResult`` loses). For callers that consume the rows, like ``data.join``.
    Refuses a non-read statement."""
    conn = ctx.resolve_connection(connection)
    descriptor = descriptor_from_sql(
        sql, dialect=conn.dialect or "", capability="sql", connection=conn.handle
    )
    if descriptor.effect != Effect.READ:
        raise ToolError(
            f"metered reads are read-only; {descriptor.operation or 'that'} on "
            f"{connection!r} is not"
        )
    return await run_metered_statement(ctx, connection=connection, sql=sql, limit=limit)


class SqlQueryTool(Tool[SqlQueryInput, SqlQueryResult]):
    spec: ClassVar[ToolSpec] = ToolSpec(
        name="sql.query",
        title="Run SQL query",
        description=(
            "Run a SQL query against a configured data connection and return the "
            "rows. Pass mode='sql' with raw SQL, or mode='table' to read a table. "
            "A value that comes from data or from the user goes in params, bound "
            "to a placeholder in the SQL, never quoted into the statement. "
            "Always pass the connection handle to target the right system (call "
            "sql.connections first if you don't know it). Always pass result_name: "
            "a short, human-friendly label for the result you expect — it becomes "
            "the result's name in the editor when the rows are large. "
            "Non-finite numbers (NaN, ±Infinity) appear as "
            '{"$nonfinite": "nan"|"inf"|"-inf"} — read these as the NaN / +Infinity '
            "/ -Infinity values they stand for (JSON can't hold them as numbers). "
            "If truncated is true and blob is set, the preview is a prefix of the "
            "fetched rows: page the rest with fetch_result, or skip paging and "
            "compute over the handle with blob.profile / blob.query. "
            + NOTE_NO_BLOB_CLAUSE
            + " Note also says when your limit stopped retrieval (raise it if you "
            "need more); never re-run a write to recover missing rows."
        ),
        app="sql",
        hot=False,
        effect_hint=Effect.READ,
    )
    Input: ClassVar[type[BaseModel]] = SqlQueryInput
    Output: ClassVar[type[BaseModel]] = SqlQueryResult

    async def run(self, args: SqlQueryInput, ctx: ToolContext) -> SqlQueryResult:
        spec = args.root
        conn = ctx.resolve_connection(spec.connection)
        caps = ctx.capabilities_for(spec.connection)
        # type-abstract: the capability ABC is used purely as a lookup KEY here,
        # never instantiated — mypy's abstract-instantiation guard is wrong.
        cap = caps.get(RunSQLCapability) if caps is not None else None  # type: ignore[type-abstract]
        if cap is None:
            raise ToolError(f"connection {spec.connection!r} has no SQL capability")

        match spec:
            case QueryBySql():
                sql = spec.sql
            case QueryByTable():
                sql = _compile_table_query(spec, conn.dialect or "")
            case _ as unreachable:
                assert_never(unreachable)

        # Classify per-call (the effect depends on the SQL, not the tool). READ
        # runs free; write/destroy/egress is gated (policy → broker prompt → a
        # cap-token the connector chokepoint validates). The connector ALSO
        # re-classifies + re-checks, so a wrong claim here cannot reach the engine.
        descriptor = descriptor_from_sql(
            sql,
            dialect=conn.dialect or "",
            capability="sql",
            connection=conn.handle,
        )
        # A backgrounded query is READ-ONLY (a write needs foreground supervision)
        # and only when a registry is available (a root chat); a subagent's flag is
        # ignored — it runs foreground, since the subagent is already the unit of work.
        do_background = spec.background and ctx.background is not None
        if do_background and descriptor.effect != Effect.READ:
            raise ToolError(
                "only read-only queries can be backgrounded — run a write query in the "
                "foreground (where it can be approved)"
            )
        if do_background and not ctx.background.can_accept():
            raise ToolError(
                "too many background jobs already running; cancel one with "
                "background_cancel or run this query in the foreground"
            )
        if descriptor.confidence == "unknown" and cap.read_only_reason:
            # The classifier could not parse the statement, so it fails closed to a
            # write — but a connection with no write path cannot be harmed by it,
            # and calling a typo a write sends the agent after a permission it
            # does not need. It is refused as the syntax error it is.
            raise ToolError(
                f"sql.query could not parse the statement {_one_line(sql)}; "
                "fix its syntax and run it again."
            )
        if descriptor.effect != Effect.READ and cap.read_only_reason:
            # A connection that cannot be written by its nature refuses the write in
            # every mode, before the gate — the mode decides whether a reader is
            # asked, never whether a wire with no write path grows one.
            raise ToolError(
                f"permission denied: connection {conn.handle!r} is read-only by its "
                f"nature — {cap.read_only_reason}. No permission mode changes that; "
                "offer the closest read instead."
            )

        cap_token = await _authorize(spec, descriptor, ctx, conn.handle)

        # Cost metering: estimate → atomically CHECK+RESERVE the
        # per-query/chat/day/week caps BEFORE execution (so concurrent queries can't
        # both slip a cap) → execute → settle the actual (or release on failure).
        # Skipped when no ledger is bound. The foreground path may PROMPT to raise a
        # cap; a backgrounded job has no human in the loop, so it RESERVES-OR-REFUSES
        # — a query that would need approval is refused (told to run in the foreground),
        # never silently bypassed and never blocked on a prompt no one will see.
        reservation: CostReservation | None = None
        cost_cap = None
        if do_background and ctx.cost_ledger is not None:
            # Background reserves without a prompt (no human in the loop): refuse a
            # query that would need approval rather than block on an unseen prompt.
            cost_cap = caps.get(EstimateSQLCostCapability) if caps is not None else None  # type: ignore[type-abstract]
            limits = CostLimits.from_config(
                getattr(ctx.permissions, "cost", None), connection=conn.handle, tool="sql.query"
            )
            pre = await reserve_or_refuse(
                descriptor,
                limits=limits,
                ledger=ctx.cost_ledger,
                session_id=ctx.session_id,
                connection=conn.handle,
                operation=descriptor.operation or "query",
                cost_capability=cost_cap,
                now=datetime.now(UTC),
            )
            if isinstance(pre, str):
                raise ToolError(
                    f"cost limit: this query can't be backgrounded — {pre}. Run it "
                    "in the foreground to approve the cost, or narrow the query."
                )
            reservation = pre
        elif not do_background:
            reservation, cost_cap = await _gate_read_cost(
                ctx, descriptor, connection=conn.handle, caps=caps
            )

        if do_background:
            # Detach execution as a supervised job; settle/release happen INSIDE it
            # (the same chokepoint the foreground uses). cap_token is None — background
            # is READ-only. The native rows are delivered via the completion
            # notification + are inspectable via background_status meanwhile.
            registry = ctx.background

            async def _job() -> SqlQueryResult:
                return await self._execute_and_build(
                    cap=cap,
                    conn=conn,
                    sql=sql,
                    descriptor=descriptor,
                    spec=spec,
                    reservation=reservation,
                    cost_cap=cost_cap,
                    cap_token=None,
                    ctx=ctx,
                )

            try:
                job = registry.submit(
                    _job,
                    kind="sql",
                    title=spec.result_name or "background query",
                    input=spec.model_dump(mode="json"),
                )
            except Exception as exc:
                # Submit failed (e.g. the cap filled between can_accept() and here) —
                # refund the reservation we just made so it isn't billed for nothing.
                if reservation is not None and ctx.cost_ledger is not None:
                    release_cost(
                        reservation,
                        ledger=ctx.cost_ledger,
                        session_id=ctx.session_id,
                        now=datetime.now(UTC),
                    )
                raise ToolError(f"could not start the background query: {exc}") from exc
            return SqlQueryResult(
                job_id=str(job.job_id),
                result_name=spec.result_name,
                note=(
                    "Query running in the background; you'll be notified with the rows "
                    "when it completes. Do not poll."
                ),
            )

        if cap_token is not None:
            # The token was minted at the PERMISSION gate, but the cost gate just above
            # can block on a human prompt for longer than the token TTL — expiring an
            # action the human approved twice. Restart the TTL now that every blocking
            # step is done and execution starts immediately.
            cap_token = cap_token.refreshed()
        return await self._execute_and_build(
            cap=cap,
            conn=conn,
            sql=sql,
            descriptor=descriptor,
            spec=spec,
            reservation=reservation,
            cost_cap=cost_cap,
            cap_token=cap_token,
            ctx=ctx,
        )

    async def _execute_and_build(
        self,
        *,
        cap: Any,
        conn: Connection,
        sql: str,
        descriptor: Any,
        spec: QueryBySql | QueryByTable,
        reservation: CostReservation | None,
        cost_cap: Any,
        cap_token: Any,
        ctx: ToolContext,
    ) -> SqlQueryResult:
        """Run the query through the shared money chokepoint, then deliver the
        rows as a ``SqlQueryResult`` (spilling a large result to a blob) with
        the provenance the card and the receipt show beside them."""
        # The tool's own clock around the capability call: the stand-in for a
        # capability that does not stamp the result itself.
        started = datetime.now(UTC)
        clock_start = time.monotonic()
        result = await _run_and_settle(
            MeteredExec(
                cap=cap,
                sql=sql,
                descriptor=descriptor,
                cap_token=cap_token,
                limit=spec.limit,
                connection=spec.connection,
                reservation=reservation,
                cost_cap=cost_cap,
                params=spec.params if isinstance(spec, QueryBySql) and spec.params else None,
            ),
            ctx,
        )

        # Connectors return raw driver values (DATE → date, DECIMAL → Decimal,
        # BLOB → bytes). Coerce to JSON-safe ONCE so the size check, the inline
        # preview, and the spilled blob all agree. ``bytes_mode="base64"`` is
        # load-bearing: the default decodes bytes as UTF-8, which raises on any
        # non-UTF-8 BLOB (real binary data — image bytes, hashes).
        rows = to_jsonable_python(result.rows, bytes_mode="base64")
        cost_warnings = list(reservation.warnings) if reservation is not None else []
        for warning in cost_warnings:
            logger.warning("cost: %s", warning)
        base = SqlQueryResult(
            columns=result.columns,
            row_count=result.row_count,
            truncated=result.truncated,
            result_name=spec.result_name,
            cost_warnings=cost_warnings,
            note=limit_note(spec.limit, effect=descriptor.effect) if result.truncated else "",
            provenance=provenance_for(
                conn,
                result,
                sql=sql,
                started=started,
                elapsed_ms=int((time.monotonic() - clock_start) * 1000),
            ),
        )
        return await asyncio.to_thread(deliver_rows, ctx.blobs, rows, base=base)


# ---------------------------------------------------------------------------
# sql.schema — capability-backed schema introspection
# ---------------------------------------------------------------------------


_SCHEMA_CONNECTION_DOC = "The connection handle from sql.connections, e.g. 'pg'."


class SchemaList(BaseModel):
    mode: Literal["list"] = "list"
    connection: str = Field(description=_SCHEMA_CONNECTION_DOC)
    verbose: bool = Field(
        default=False,
        description=(
            "Include each relation's URN and the engine's own metadata schemas. "
            "Off by default: a listing is read by a model that pays for every "
            "row, and a URN restates the name beside it."
        ),
    )


class SchemaDescribe(BaseModel):
    mode: Literal["describe"] = "describe"
    connection: str = Field(description=_SCHEMA_CONNECTION_DOC)
    table: str = Field(
        description=(
            "The table or view to describe, as a top-level string: bare ('orders') "
            "or schema-qualified ('public.orders'). Required when mode='describe'."
        ),
    )


#: Keys agents send in place of ``table`` when they mean the table to describe.
#: None of them names anything else this tool reads, so reading one as ``table``
#: cannot describe a table other than the one the caller named.
_TABLE_SPELLINGS = ("table_name", "relation", "relation_name", "name", "describe.table")
#: Keys agents send beside a bare table to name its schema.
_SCHEMA_SPELLINGS = ("schema", "schema_name")
_SCHEMA_INPUT_KEYS = frozenset({"mode", "connection", "verbose", "table"})


def _describe_example(value: dict[str, Any]) -> str:
    """The exact describe call to send, on the caller's own connection."""
    connection = value.get("connection")
    handle = connection if isinstance(connection, str) and connection else "pg"
    return json.dumps({"connection": handle, "mode": "describe", "table": "public.orders"})


def _table_spellings(value: dict[str, Any]) -> list[tuple[str, Any]]:
    """Every place a call names its table, as ``(key, value)`` pairs: the
    contract's ``table``, the keys agents send instead, and the nested
    ``describe: {table}`` an earlier error message taught them."""
    found = [(key, value[key]) for key in ("table", *_TABLE_SPELLINGS) if key in value]
    nested = value.get("describe")
    if isinstance(nested, dict) and "table" in nested:
        found.append(("describe.table", nested["table"]))
    return found


def _qualify(table: str, value: dict[str, Any]) -> str:
    """A bare table prefixed with the ``schema`` named beside it. A qualified
    table in a different schema, or two different schemas, is refused."""
    schemas = {
        value[key].strip()
        for key in _SCHEMA_SPELLINGS
        if isinstance(value.get(key), str) and value[key].strip()
    }
    if not schemas:
        return table
    if len(schemas) > 1:
        raise ValueError(
            f"the call names two schemas ({', '.join(sorted(map(repr, schemas)))}). "
            f"Send one qualified `table`, e.g. {_describe_example(value)}"
        )
    schema = schemas.pop()
    parts = _name_parts(table)
    if len(parts) == 1:
        return f"{schema}.{table.strip()}"
    if parts[-2] == _name_parts(schema)[-1]:
        return table
    raise ValueError(
        f"`table` {table!r} is not in `schema` {schema!r}. Send one qualified `table`, "
        f"e.g. {_describe_example(value)}"
    )


def _normalize_schema_call(value: Any) -> Any:
    """Bring the shapes agents send for a describe onto the one ``SchemaDescribe``
    reads, refusing any call whose table cannot be named without guessing.

    The table may arrive as ``table`` (the contract), under a key that can only
    mean the table, nested as ``describe: {table}``, or bare with a ``schema``
    beside it. Two spellings that disagree, a schema that disagrees with a
    qualified table, and a describe naming no table are refused with the exact
    call to send instead."""
    if not isinstance(value, dict):
        return value
    mode = value.get("mode")
    spellings = _table_spellings(value)
    if mode not in (None, "describe") or (mode is None and not spellings):
        return value
    if not spellings:
        unread = sorted(k for k in value if k not in _SCHEMA_INPUT_KEYS)
        note = f" It does not read {', '.join(f'`{k}`' for k in unread)}." if unread else ""
        raise ValueError(
            "mode='describe' needs `table`, the table to describe, as a top-level string. "
            f"Send {_describe_example(value)}, or mode='list' to see the tables.{note}"
        )
    named = {v.strip() for _, v in spellings if isinstance(v, str)}
    if len(named) > 1:
        listed = ", ".join(f"`{k}`={v!r}" for k, v in spellings)
        raise ValueError(
            f"the call names more than one table ({listed}). "
            f"Send one `table`, e.g. {_describe_example(value)}"
        )
    table = spellings[0][1]
    normalized = {
        k: v
        for k, v in value.items()
        if k not in (*_TABLE_SPELLINGS, *_SCHEMA_SPELLINGS, "describe")
    }
    normalized["mode"] = "describe"
    normalized["table"] = _qualify(table, value) if isinstance(table, str) else table
    return normalized


class SqlSchemaInput(
    RootModel[Annotated[SchemaList | SchemaDescribe, Field(discriminator="mode")]]
):
    """List relations on a connection, or describe one table's columns. A call
    that omits ``mode`` describes when it names a ``table`` and lists otherwise."""

    @model_validator(mode="before")
    @classmethod
    def _mode_from_shape(cls, value: Any) -> Any:
        return _infer_mode(_normalize_schema_call(value), (("table", "describe"),), fallback="list")


class RelationCard(BaseModel):
    name: str
    """The relation's dotted name — ``[catalog.]schema.table``."""
    kind: str = "table"
    urn: str | None = None
    """The fold identity, sent only when the caller asked to be verbose. The
    agent addresses a relation by NAME (``sql.query``, ``sql.schema
    mode=describe``), so a URN per row is context spent on something nothing
    downstream of the model reads."""


class ColumnCard(BaseModel):
    name: str
    data_type: str = ""
    nullable: bool = True


class SqlSchemaResult(BaseModel):
    relations: list[RelationCard] = Field(default_factory=list)
    """Populated for ``mode="list"``."""
    table: str | None = None
    columns: list[ColumnCard] = Field(default_factory=list)
    """Populated for ``mode="describe"``. Name, type and nullability, one line
    per column — a describe is already the narrowest answer this capability can
    give: ``ColumnMeta`` carries no comment or row estimate for any connector,
    so a slot for one would be a null on every call."""


async def _list_relations(
    cap: IntrospectSchemaCapability, ctx: ToolContext, connection: str
) -> list[RelationMeta]:
    """List a connection's relations, shaping any driver failure into a
    ``ToolError`` so a bad credential or a dropped socket reads as a tool
    result the agent can react to, never a crashed turn. The driver's words are
    redacted first: this is the failure most likely to be a CONNECT failure, and
    a connect failure is what echoes a DSN."""
    try:
        return await cap.list_relations()
    except Exception as exc:
        raise ToolError(
            await _safe_detail(ctx, connection, f"schema listing failed: {exc}")
        ) from exc


def _name_parts(name: str) -> list[str]:
    """A dotted relation name as its parts, each de-quoted and lower-cased, so
    ``"Public"."Orders"`` and ``public.orders`` compare equal."""
    return [part.strip().strip('"`[]').lower() for part in name.strip().split(".")]


def _match_relation(relations: list[RelationMeta], table: str) -> list[RelationMeta]:
    """The relations a ``describe`` request can mean.

    An exact full-name match wins. Otherwise the request matches every relation
    whose name ends in the same parts: ``public.orders`` finds
    ``postgres.public.orders``, and a bare ``orders`` finds an ``orders`` in any
    schema. A qualified request never falls back to its last part, so
    ``sales.orders`` cannot describe ``public.orders``. The caller refuses more
    than one match as ambiguous, unless exactly one of them is a relation the
    listing shows: the engine's own metadata tables never shadow the customer's."""
    want = _name_parts(table)
    exact = [r for r in relations if _name_parts(r.name) == want]
    if exact:
        return exact[:1]
    matches = [r for r in relations if _name_parts(r.name)[-len(want) :] == want]
    listed = [r for r in matches if not hidden_from_listing(r.name)]
    return listed if len(matches) > 1 and len(listed) == 1 else matches


class SqlSchemaTool(Tool[SqlSchemaInput, SqlSchemaResult]):
    spec: ClassVar[ToolSpec] = ToolSpec(
        name="sql.schema",
        title="Inspect schema",
        description=(
            "Inspect a data connection's schema. Two uses. "
            'List its tables and views: {"connection": "pg", "mode": "list"}. '
            "Describe one table's columns and types: "
            '{"connection": "pg", "mode": "describe", "table": "public.orders"}. '
            "`table` is a top-level string, bare or schema-qualified. Get the "
            "connection handle from sql.connections. A listing hides the engine's "
            "own metadata schemas; pass verbose=true to see them and each "
            "relation's URN."
        ),
        app="sql",
        hot=False,
        effect_hint=Effect.READ,
    )
    Input: ClassVar[type[BaseModel]] = SqlSchemaInput
    Output: ClassVar[type[BaseModel]] = SqlSchemaResult

    async def run(self, args: SqlSchemaInput, ctx: ToolContext) -> SqlSchemaResult:
        spec = args.root
        # Validate the handle (raises a recover-friendly "unknown connection" error).
        ctx.resolve_connection(spec.connection)
        caps = ctx.capabilities_for(spec.connection)
        # type-abstract: the ABC is a lookup KEY, never instantiated (mypy's
        # abstract-instantiation guard is wrong here).
        cap = caps.get(IntrospectSchemaCapability) if caps is not None else None  # type: ignore[type-abstract]
        if cap is None:
            raise ToolError(f"connection {spec.connection!r} has no schema introspection")

        match spec:
            case SchemaList():
                relations = await _list_relations(cap, ctx, spec.connection)
                if not spec.verbose:
                    relations = [r for r in relations if not hidden_from_listing(r.name)]
                # The engine has just said what this connection holds. Whoever
                # keeps this workspace's schema cards can compare that against
                # what it carded and rebuild if the warehouse has moved on —
                # a listing the agent read is the cheapest freshness signal
                # there is. Never raises; nobody may be listening.
                relations_listed(ctx.alkera_dir, spec.connection, [r.name for r in relations])
                return SqlSchemaResult(
                    relations=[
                        RelationCard(
                            name=r.name,
                            kind=r.kind,
                            urn=str(r.urn) if spec.verbose else None,
                        )
                        for r in relations
                    ]
                )
            case SchemaDescribe():
                # Resolve the table NAME to its real fold URN via the relation list. The
                # describe capability mints per-system URNs (carrying authority + the
                # case-folded relation parts); a hand-built "{namespace}/{table}" string is
                # not one and the generic introspect can't parse it. Matching the listed
                # relations is correct for every connector and accepts a bare or qualified
                # name. (One extra list query — describe is a deliberate, low-frequency op.)
                relations = await _list_relations(cap, ctx, spec.connection)
                matches = _match_relation(relations, spec.table)
                if not matches:
                    available = ", ".join(sorted(r.name for r in relations)[:20])
                    raise ToolError(
                        f"table {spec.table!r} not found on connection {spec.connection!r}"
                        + (f" (available: {available})" if available else "")
                    )
                if len(matches) > 1:
                    candidates = ", ".join(sorted(r.name for r in matches)[:20])
                    raise ToolError(
                        f"table {spec.table!r} is ambiguous on connection {spec.connection!r}: "
                        f"it could be {candidates}. Pass the qualified name as `table`."
                    )
                target = matches[0]
                try:
                    meta = await cap.describe(target.urn)
                except Exception as exc:
                    raise ToolError(
                        await _safe_detail(
                            ctx, spec.connection, f"describe failed for {spec.table!r}: {exc}"
                        )
                    ) from exc
                return SqlSchemaResult(
                    table=meta.name,
                    columns=[
                        ColumnCard(name=c.name, data_type=c.data_type, nullable=c.nullable)
                        for c in meta.columns
                    ],
                )
            case _ as unreachable:
                assert_never(unreachable)


# ---------------------------------------------------------------------------
# sql.connections — the discovery entry point (what can I query?)
# ---------------------------------------------------------------------------


class SqlConnectionsInput(BaseModel):
    """No arguments — lists every connection you can target."""


class ConnectionCard(BaseModel):
    handle: str
    """The identifier to pass as ``connection`` to sql.query / sql.schema."""
    dialect: str = ""
    environment: str = ""


class SqlConnectionsResult(BaseModel):
    connections: list[ConnectionCard] = Field(default_factory=list)


class SqlConnectionsTool(Tool[SqlConnectionsInput, SqlConnectionsResult]):
    spec: ClassVar[ToolSpec] = ToolSpec(
        name="sql.connections",
        title="List data connections",
        description=(
            "List the data connections you can query. Returns each connection's "
            "handle (the identifier to pass as 'connection' to sql.query and "
            "sql.schema) and its SQL dialect. Call this FIRST "
            "when you don't already know which connection handle to use — never "
            "guess a handle."
        ),
        hot=True,
        app="sql",
        effect_hint=Effect.READ,
    )
    Input: ClassVar[type[BaseModel]] = SqlConnectionsInput
    Output: ClassVar[type[BaseModel]] = SqlConnectionsResult

    async def run(self, args: SqlConnectionsInput, ctx: ToolContext) -> SqlConnectionsResult:
        return SqlConnectionsResult(
            connections=[
                ConnectionCard(
                    handle=c.handle,
                    dialect=c.dialect or "",
                    environment=str(c.environment),
                )
                for c in ctx.registry.connections()
            ]
        )


def register_sql_tools(registry: ToolRegistry) -> None:
    """Register the capability-backed SQL built-ins. Call only when at least one
    connection provides ``RunSQLCapability``, so the tools appear without the plugin writing any.
    ``sql.schema`` rides along; it errors cleanly on a connection without schema
    introspection. ``sql.connections`` is HOT — without knowing a handle no SQL
    tool works, so the agent must be able to discover handles without a search."""
    registry.register(SqlConnectionsTool)
    registry.register(SqlQueryTool)
    registry.register(SqlSchemaTool)


__all__ = [
    "QUERY_OUTCOME_RECORDERS",
    "BreakIntent",
    "CapabilityAdapter",
    "ColumnCard",
    "ConnectionCard",
    "QueryBySql",
    "QueryByTable",
    "QueryOutcomeRecorder",
    "RelationCard",
    "SqlConnectionsInput",
    "SqlConnectionsResult",
    "SqlConnectionsTool",
    "SqlQueryInput",
    "SqlQueryTool",
    "SqlSchemaInput",
    "SqlSchemaResult",
    "SqlSchemaTool",
    "register_sql_tools",
    "run_metered_read",
    "run_metered_statement",
]
