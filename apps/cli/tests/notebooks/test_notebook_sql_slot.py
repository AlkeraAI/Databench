"""A box's notebooks take their SQL from ``NOTEBOOK_SQL_PROVIDERS`` and nowhere else.

With nothing registered (the open platform on its own) the box composes its
notebooks with no SQL provider, so a SQL cell naming a connection is refused as
unknown. A factory registered on the point is handed the box's runtime and its
providers reach the engines, which is what makes the empty case mean
something.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

import pytest
from alkera_cli.notebooks import box_compose, sql_slot
from alkera_cli.notebooks.sql_slot import NotebookSqlContext, NotebookSqlFactory
from alkera_core.extensions import ExtensionPoint
from alkera_notebook.sql.errors import UnknownConnectionError
from alkera_notebook.sql.provider import SqlProviderRegistry, SqlWorkspace

WORKSPACE = SqlWorkspace(id="ws", root="/ws")


class _Provider:
    """Resolves one connection name; never asked to run anything here."""

    name = "recorded"

    def can_resolve(self, connection_name: str, workspace: SqlWorkspace) -> bool:
        return connection_name == "warehouse"

    async def execute(self, request: Any, actor: Any, workspace: SqlWorkspace) -> Any:
        raise AssertionError("not run in this test")

    async def cancel(self, query_id: str) -> None:
        return None


@pytest.fixture
def point(monkeypatch: pytest.MonkeyPatch) -> ExtensionPoint[NotebookSqlFactory]:
    fresh: ExtensionPoint[NotebookSqlFactory] = ExtensionPoint("notebook_sql_providers")
    monkeypatch.setattr(sql_slot, "NOTEBOOK_SQL_PROVIDERS", fresh)
    return fresh


@pytest.fixture
def composed(monkeypatch: pytest.MonkeyPatch) -> list[Sequence[Any]]:
    """The SQL providers each production composition of a box's notebooks gets."""
    seen: list[Sequence[Any]] = []

    def compose(
        custody: Any,
        headers: Callable[[], Mapping[str, str]],
        api_url: str,
        *,
        sql: Sequence[Any] = (),
        **_: Any,
    ) -> Any:
        seen.append(list(sql))
        return object()

    monkeypatch.setattr(box_compose, "compose_box_notebooks", compose)
    return seen


def _start_box(runtime: object) -> None:
    slot = box_compose.box_notebook_slot(lambda: runtime, org_id="org")
    slot.start(object(), dict, "http://api.test")


def test_with_nothing_registered_a_box_s_notebooks_resolve_no_connection(
    point: ExtensionPoint[NotebookSqlFactory], composed: list[Sequence[Any]]
) -> None:
    _start_box(object())

    assert composed == [[]]
    with pytest.raises(UnknownConnectionError):
        SqlProviderRegistry(composed[0]).resolve("warehouse", WORKSPACE)


def test_a_registered_factory_s_providers_reach_the_box_s_notebooks(
    point: ExtensionPoint[NotebookSqlFactory], composed: list[Sequence[Any]]
) -> None:
    runtime, given = object(), []
    provider = _Provider()

    def factory(context: NotebookSqlContext) -> list[Any]:
        given.append(context.runtime)
        return [provider]

    point.register(factory)
    _start_box(runtime)

    assert given == [runtime]
    assert composed == [[provider]]
    assert SqlProviderRegistry(composed[0]).resolve("warehouse", WORKSPACE) is provider
