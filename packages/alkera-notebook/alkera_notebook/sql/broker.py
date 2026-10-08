"""The engine side of ``sql.execute``.

The kernel's service hands each ``sql.execute`` request here with the
kernel's context. The broker checks the request belongs to a run that is
executing on that kernel (attribution and a time window, never containment),
resolves the connection among the workspace's, asks the statement policy,
runs the statement through the provider, and encodes the result for the
kernel. Interrupting a run cancels its statements at the provider.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import pyarrow as pa

from alkera_notebook.rpc import frames
from alkera_notebook.rpc.peer import Call, MethodRegistry
from alkera_notebook.sql.encode import (
    CODEC_FILE,
    CODEC_ROWS,
    CODEC_STREAM,
    INLINE_LIMIT,
    ArrowSink,
    EncodedTable,
    encode_rows_json,
)
from alkera_notebook.sql.errors import (
    InvalidParamsError,
    OutsideRunError,
    QueryCancelledError,
    QueryFailedError,
    SqlError,
    SqlUnavailableError,
    StatementRefusedError,
)
from alkera_notebook.sql.limits import cut_notice, has_own_limit
from alkera_notebook.sql.policy import (
    Confirmer,
    DefaultStatementPolicy,
    NoConfirmer,
    StatementPolicy,
)
from alkera_notebook.sql.provider import (
    ArrowResult,
    Requester,
    SqlEngineProvider,
    SqlProviderRegistry,
    SqlRequest,
    SqlWorkspace,
)

DEFAULT_INFLIGHT_BYTES = 512 * 1024 * 1024


@dataclass(frozen=True)
class RunInfo:
    """A run executing on a kernel, as the engine knows it."""

    run_id: str
    actor: Requester
    notebook: str = ""


class KernelSqlContext(Protocol):
    """What the kernel's session tells the broker."""

    @property
    def kernel_id(self) -> str: ...

    @property
    def workspace(self) -> SqlWorkspace: ...

    @property
    def codecs(self) -> frozenset[str]: ...

    @property
    def data_dir(self) -> Path | None: ...

    @property
    def sql_row_limit(self) -> int | None:
        """The notebook's ``sql_row_limit``: applied to statements without a
        ``LIMIT`` of their own; ``None`` for no limit."""
        ...

    def active_run(self, run_id: str) -> RunInfo | None:
        """The run if it is executing on this kernel now, else ``None``."""
        ...


@dataclass
class SqlKernelContext:
    """A ready-made ``KernelSqlContext`` for an engine session: the run
    lookup is the session's (which run is executing, and who asked)."""

    kernel_id: str
    workspace: SqlWorkspace
    codecs: frozenset[str]
    data_dir: Path | None
    runs: Callable[[str], RunInfo | None]
    row_limit: Callable[[], int | None] = lambda: None

    @property
    def sql_row_limit(self) -> int | None:
        return self.row_limit()

    def active_run(self, run_id: str) -> RunInfo | None:
        return self.runs(run_id)


class ByteBudget:
    """Bytes of results held in memory at once, across one engine."""

    def __init__(self, capacity: int, wait_s: float = 30.0) -> None:
        self.capacity = capacity
        self.available = capacity
        self._wait_s = wait_s
        self._cond = asyncio.Condition()

    async def take(self, n: int) -> None:
        n = min(n, self.capacity)
        async with self._cond:
            try:
                await asyncio.wait_for(
                    self._cond.wait_for(lambda: self.available >= n), self._wait_s
                )
            except TimeoutError as exc:
                raise SqlUnavailableError(
                    "too many query results are in flight; try again"
                ) from exc
            self.available -= n

    async def give(self, n: int) -> None:
        if n <= 0:
            return
        async with self._cond:
            self.available = min(self.capacity, self.available + n)
            self._cond.notify_all()


def _check_param(value: Any) -> Any:
    """A bound parameter, as the RPC decoded it. Segments, files and bytes
    are refused outright: the engine never decodes binary data a kernel
    produced."""
    if isinstance(value, (frames.Segment, frames.FileRef, bytes, bytearray, memoryview)):
        raise InvalidParamsError(
            "sql.execute parameters must be JSON values; binary values are not accepted"
        )
    if isinstance(value, dict):
        return {str(k): _check_param(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_check_param(v) for v in value]
    return value


def parse_request(params: Mapping[str, Any], ctx: Mapping[str, Any]) -> SqlRequest:
    sql = params.get("sql")
    if not isinstance(sql, str) or not sql.strip():
        raise InvalidParamsError("sql must be a non-empty string")
    connection = params.get("connection")
    if not isinstance(connection, str) or not connection.strip():
        raise InvalidParamsError("connection must name one of the workspace's connections")
    raw = params.get("params")
    bound: Any = None
    if raw is not None:
        if not isinstance(raw, (dict, list)):
            raise InvalidParamsError("params must be an object or a list")
        bound = _check_param(raw)
    run_id = ctx.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        raise OutsideRunError()
    cell_id = ctx.get("cell_id")
    return SqlRequest(
        sql=sql, connection=connection, params=bound, run_id=run_id, cell_id=str(cell_id or "")
    )


@dataclass
class _Held:
    value: int = 0
    # The batch read in flight, so the reader is never closed under it.
    reading: asyncio.Future[pa.RecordBatch | None] | None = None


@dataclass
class _InFlight:
    provider: SqlEngineProvider
    query_id: str | None = None
    task: asyncio.Task[Any] | None = None


class SqlBroker:
    """Serves ``sql.execute`` for one engine."""

    def __init__(
        self,
        registry: SqlProviderRegistry,
        *,
        policy: StatementPolicy | None = None,
        confirmer: Confirmer | None = None,
        inflight_bytes: int = DEFAULT_INFLIGHT_BYTES,
        inline_limit: int = INLINE_LIMIT,
        budget_wait_s: float = 30.0,
    ) -> None:
        self.registry = registry
        self.policy = policy or DefaultStatementPolicy()
        self.confirmer = confirmer or NoConfirmer()
        self.budget = ByteBudget(inflight_bytes, budget_wait_s)
        self.inline_limit = inline_limit
        self._confirmed: set[tuple[str, str]] = set()
        self._inflight: dict[tuple[str, str], list[_InFlight]] = {}

    # ------------------------------------------------------------ lifecycle

    def run_finished(self, kernel_id: str, run_id: str) -> None:
        """Forgets the run's confirmation; the next run asks again."""
        self._confirmed.discard((kernel_id, run_id))

    async def cancel_run(self, kernel_id: str, run_id: str) -> int:
        """Cancels every statement the run has in flight (the interrupt path).
        Returns how many were cancelled."""
        entries = list(self._inflight.get((kernel_id, run_id), ()))
        for entry in entries:
            if entry.task is not None and not entry.task.done():
                entry.task.cancel()
        return len(entries)

    # ------------------------------------------------------------ execute

    async def execute(
        self, kernel: KernelSqlContext, params: Mapping[str, Any], ctx: Mapping[str, Any]
    ) -> dict[str, Any]:
        request = parse_request(params, ctx)
        run = kernel.active_run(request.run_id)
        if run is None:
            raise OutsideRunError()
        request = SqlRequest(
            sql=request.sql,
            connection=request.connection,
            params=request.params,
            run_id=request.run_id,
            cell_id=request.cell_id,
            notebook=run.notebook,
        )
        provider = self.registry.resolve(request.connection, kernel.workspace)
        await self._check_policy(kernel, request, run)
        key = (kernel.kernel_id, request.run_id)
        entry = _InFlight(provider=provider)
        task = asyncio.ensure_future(self._run(kernel, provider, request, run, entry))
        entry.task = task
        self._inflight.setdefault(key, []).append(entry)
        try:
            return await task
        except asyncio.CancelledError:
            outer = asyncio.current_task()
            if outer is not None and outer.cancelling():
                task.cancel()
                raise
            # The run was interrupted: the kernel gets an answer, not silence.
            raise QueryCancelledError("the run was interrupted") from None
        finally:
            remaining = [e for e in self._inflight.get(key, []) if e is not entry]
            if remaining:
                self._inflight[key] = remaining
            else:
                self._inflight.pop(key, None)

    async def _check_policy(
        self, kernel: KernelSqlContext, request: SqlRequest, run: RunInfo
    ) -> None:
        decision = self.policy.check(request.sql, run.actor)
        if decision.decision == "allow":
            return
        if decision.decision == "refuse":
            raise StatementRefusedError(
                decision.reason or "this statement is not allowed here", decision.kind
            )
        key = (kernel.kernel_id, request.run_id)
        if key in self._confirmed:
            return
        if await self.confirmer.confirm(request.run_id, run.actor, request.sql, decision.kind):
            # The answer covers the rest of this run, and only while it runs.
            if kernel.active_run(request.run_id) is not None:
                self._confirmed.add(key)
            return
        raise StatementRefusedError(
            decision.reason or "the statement was not confirmed", decision.kind
        )

    async def _run(
        self,
        kernel: KernelSqlContext,
        provider: SqlEngineProvider,
        request: SqlRequest,
        run: RunInfo,
        entry: _InFlight,
    ) -> dict[str, Any]:
        try:
            result = await provider.execute(request, run.actor, kernel.workspace)
        except (SqlError, asyncio.CancelledError):
            raise
        except Exception as exc:
            raise QueryFailedError(str(exc) or type(exc).__name__) from exc
        entry.query_id = result.query_id
        held = _Held()
        try:
            limit = kernel.sql_row_limit
            if limit is not None and has_own_limit(request.sql):
                limit = None
            encoded, cut = await self._encode(kernel, result, held, limit)
            if cut:
                # Stop the data system's work on rows nobody will see.
                with contextlib.suppress(Exception):
                    await provider.cancel(result.query_id)
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                await asyncio.shield(provider.cancel(result.query_id))
            raise
        except SqlError:
            with contextlib.suppress(Exception):
                await provider.cancel(result.query_id)
            raise
        except Exception as exc:
            raise QueryFailedError(str(exc) or type(exc).__name__) from exc
        finally:
            await self.budget.give(held.value)
            if held.reading is not None and not held.reading.done():
                # Interrupted mid-read: let the read end before closing.
                await asyncio.wait([held.reading], timeout=10)
            with contextlib.suppress(Exception):
                result.close()
        response: dict[str, Any] = {
            "table": encoded.value(),
            "rows": encoded.rows,
            "bytes": encoded.nbytes,
            "truncated": result.truncated or cut,
            "query_id": result.query_id,
            "result_sha256": encoded.sha256,
        }
        if cut and limit is not None:
            response["row_limit"] = limit
            response["notice"] = cut_notice(limit)
        return response

    async def _next(self, reader: pa.RecordBatchReader, held: _Held) -> pa.RecordBatch | None:
        def read() -> pa.RecordBatch | None:
            try:
                return reader.read_next_batch()
            except StopIteration:
                return None

        held.reading = asyncio.ensure_future(asyncio.to_thread(read))
        return await asyncio.shield(held.reading)

    async def _limited(
        self, reader: pa.RecordBatchReader, held: _Held, limit: int | None, cut: list[bool]
    ) -> AsyncIterator[pa.RecordBatch]:
        """The reader's batches, stopping at ``limit`` rows; ``cut[0]`` is set
        when rows beyond the limit existed."""
        taken = 0
        while (batch := await self._next(reader, held)) is not None:
            if limit is None:
                yield batch
                continue
            room = limit - taken
            if batch.num_rows > room:
                if room > 0:
                    yield batch.slice(0, room)
                cut[0] = True
                return
            taken += batch.num_rows
            yield batch
            if taken == limit:
                # Exactly at the limit: one more non-empty batch means more rows.
                while (extra := await self._next(reader, held)) is not None:
                    if extra.num_rows:
                        cut[0] = True
                        return
                return

    async def _encode(
        self, kernel: KernelSqlContext, result: ArrowResult, held: _Held, limit: int | None
    ) -> tuple[EncodedTable, bool]:
        codecs = kernel.codecs
        reader = result.reader
        cut = [False]
        if CODEC_STREAM in codecs:
            data_dir = kernel.data_dir if CODEC_FILE in codecs else None
            sink = ArrowSink(
                reader.schema,
                data_dir=data_dir,
                inline_limit=self.inline_limit,
            )
            try:
                async for batch in self._limited(reader, held, limit, cut):
                    before = sink.size
                    sink.write(batch)
                    grown = sink.size - before
                    if grown > 0 and sink.size <= self.inline_limit and not sink.spilled:
                        await self.budget.take(grown)
                        held.value += grown
                    elif sink.spilled and held.value:
                        # Spilled to the data directory: nothing large stays in memory.
                        await self.budget.give(held.value)
                        held.value = 0
                return sink.finish(), cut[0]
            except BaseException:
                sink.abort()
                raise
        if CODEC_ROWS in codecs:
            batches: list[pa.RecordBatch] = []
            async for batch in self._limited(reader, held, limit, cut):
                await self.budget.take(batch.nbytes)
                held.value += batch.nbytes
                batches.append(batch)
            return encode_rows_json(reader.schema, batches, limit=self.inline_limit), cut[0]
        raise InvalidParamsError("the kernel can decode no table codec")


def register_sql_methods(
    registry: MethodRegistry, broker: SqlBroker, kernel: KernelSqlContext
) -> None:
    """Hosts ``sql.execute`` on a kernel's service. The service's run scope
    decides the time window before the handler runs; the broker asks the
    kernel context which run (and so which requester) that is."""

    async def sql_execute(call: Call) -> dict[str, Any]:
        ctx = call.extra or call.ctx or {}
        return await broker.execute(kernel, call.params, ctx)

    registry.register(SQL_EXECUTE, sql_execute, run_scoped=True)


SQL_EXECUTE = "sql.execute"

__all__ = [
    "SQL_EXECUTE",
    "ByteBudget",
    "KernelSqlContext",
    "QueryCancelledError",
    "RunInfo",
    "SqlBroker",
    "SqlKernelContext",
    "parse_request",
    "register_sql_methods",
]
