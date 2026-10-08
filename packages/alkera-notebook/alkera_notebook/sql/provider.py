"""The seam between the broker and whatever resolves a connection name.

A kernel acts for its workspace, never for a person: a provider resolves a
name among the connections the workspace may use, and receives the run's
requester only for attribution, statement policy and cost. Providers
register with a :class:`SqlProviderRegistry`; the first that can resolve a
name serves it, so a platform provider registered ahead of the open core's
providers wins for the names it knows.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

from alkera_notebook.sql.errors import UnknownConnectionError

if TYPE_CHECKING:
    import pyarrow as pa


class Requester(Protocol):
    """Who asked for the run a statement belongs to. The engine's ``Actor``
    satisfies this."""

    @property
    def kind(self) -> Literal["person", "agent", "system"]: ...

    @property
    def id(self) -> str: ...


@dataclass(frozen=True)
class SqlWorkspace:
    """The workspace a kernel acts for.

    A kernel is driven by everyone the workspace is shared with, and it runs
    on the workspace owner's connections, personal ones included: sharing a
    workspace shares them. ``org_id`` is empty in the open core.
    """

    id: str
    root: str
    org_id: str = ""


@dataclass(frozen=True)
class SqlRequest:
    """One statement, as the kernel sent it."""

    sql: str
    connection: str
    params: Mapping[str, Any] | Sequence[Any] | None = None
    run_id: str = ""
    cell_id: str = ""
    notebook: str = ""


@dataclass
class ArrowResult:
    """A provider's answer: a reader the broker drains batch by batch.

    ``rows`` and ``bytes`` are what the provider knows up front (``None``
    when it does not); the broker counts what it actually sends.
    ``truncated`` is true when the provider stopped early (a row cap of its
    own).
    """

    reader: pa.RecordBatchReader
    query_id: str
    rows: int | None = None
    bytes: int | None = None
    truncated: bool = False
    meta: dict[str, Any] = field(default_factory=dict)
    # Called once the broker is done with the reader (drained or abandoned):
    # where a provider releases the connection behind it.
    release: Callable[[], None] | None = None

    def close(self) -> None:
        try:
            self.reader.close()
        finally:
            if self.release is not None:
                release, self.release = self.release, None
                release()


@runtime_checkable
class SqlEngineProvider(Protocol):
    """Resolves connection names and runs statements against them."""

    @property
    def name(self) -> str: ...

    def can_resolve(self, connection_name: str, workspace: SqlWorkspace) -> bool: ...

    async def execute(
        self, request: SqlRequest, actor: Requester, workspace: SqlWorkspace
    ) -> ArrowResult:
        """Runs the statement. Cancelling this coroutine must stop the
        statement at the data system; once it returns, ``cancel`` with the
        result's ``query_id`` does."""
        ...

    async def cancel(self, query_id: str) -> None: ...


class SqlProviderRegistry:
    """Providers in registration order; the first that resolves a name wins."""

    def __init__(self, providers: Sequence[SqlEngineProvider] = ()) -> None:
        self._providers: list[SqlEngineProvider] = []
        for provider in providers:
            self.register(provider)

    def register(self, provider: SqlEngineProvider, *, first: bool = False) -> None:
        if any(p.name == provider.name for p in self._providers):
            raise ValueError(f"a SQL provider named {provider.name!r} is already registered")
        if first:
            self._providers.insert(0, provider)
        else:
            self._providers.append(provider)

    def __iter__(self) -> Iterator[SqlEngineProvider]:
        return iter(list(self._providers))

    def resolve(self, connection_name: str, workspace: SqlWorkspace) -> SqlEngineProvider:
        for provider in self._providers:
            if provider.can_resolve(connection_name, workspace):
                return provider
        raise UnknownConnectionError(connection_name)

    def provider(self, name: str) -> SqlEngineProvider:
        for provider in self._providers:
            if provider.name == name:
                return provider
        raise KeyError(name)
