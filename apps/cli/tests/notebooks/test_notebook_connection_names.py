"""A connection name means one connection, read the same way everywhere a
notebook reads it.

A cell stores the name the server gave a connection (the picker offers it).
On a box holding two orgs' workspaces, or a team connection beside a
member's own, the second record of a name materializes under a suffixed
local handle (``pg-team``). The run gate's classifier reads a SQL step on
the connection the name means in the session, and a name that does not
mean exactly one connection is not cleared as a read. The notebook code
never looks a connection up by local handle: a gate below refuses it, and
refuses a broad ``except`` that turns a failure into an empty answer.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

import pytest
from alkera_cli.contracts.tool_types import Effect
from alkera_cli.notebooks.tools import classify_step
from alkera_cli.plugins.plugin_base import Connection, ToolRegistry
from alkera_cli.plugins.plugin_base.tool import TEAM_RECORD_HANDLE, TEAM_RECORD_ID
from alkera_core.project.directory import ProjectDirectory
from alkera_notebook.tools.port import PlanStepRecord

NOTEBOOKS = Path(__file__).resolve().parents[2] / "alkera_cli" / "notebooks"


def _connection(local: str, record_id: str, server_name: str) -> Connection:
    conn = Connection(handle=local, plugin="postgres", dialect="postgres")
    conn._runtime_bindings[TEAM_RECORD_ID] = record_id
    conn._runtime_bindings[TEAM_RECORD_HANDLE] = server_name
    return conn


@dataclass
class Session:
    """The session a step is classified in: its registry, scoped to the
    records the server answered for it."""

    registry: ToolRegistry


def _session(tmp_path: Path) -> Session:
    registry = ToolRegistry(ProjectDirectory(tmp_path / ".alkera").blobs())
    # Another org's pg took the plain handle; this workspace's is pg-team.
    registry.register_connection(_connection("pg", "rec-elsewhere", "pg"))
    registry.register_connection(_connection("pg-team", "rec-pg", "pg"))
    return Session(registry.restricted(frozenset(), connection_ids=frozenset({"rec-pg"})))


def _step(connection: str) -> PlanStepRecord:
    return PlanStepRecord(
        cell_id="c1",
        name="orders",
        reason="target",
        kind="sql",
        code="select count(*) from orders",
        sql="select count(*) from orders",
        connection=connection,
    )


def test_the_registry_finds_the_sessions_connection_by_the_servers_name(tmp_path: Path) -> None:
    session = _session(tmp_path)
    found = session.registry.connection_named("pg")
    assert found is not None
    assert found._runtime_bindings[TEAM_RECORD_ID] == "rec-pg"
    assert session.registry.connection_by_record("rec-pg") is found
    assert session.registry.connection_by_record("rec-elsewhere") is None


def test_a_read_on_the_named_connection_is_a_read(tmp_path: Path) -> None:
    classified = classify_step(_step("pg"), _session(tmp_path))  # type: ignore[arg-type]
    assert classified.descriptor.effect == Effect.READ


@pytest.mark.parametrize("name", ["warehouse", "pg-team-typo"])
def test_a_name_that_means_no_connection_is_not_cleared_as_a_read(
    tmp_path: Path, name: str
) -> None:
    """Classified in no connection's dialect it would be read as DuckDB SQL
    and could pass as a read; it is treated as a write instead."""
    classified = classify_step(_step(name), _session(tmp_path))  # type: ignore[arg-type]
    assert (classified.descriptor.effect, classified.descriptor.confidence) == (
        Effect.WRITE,
        "unknown",
    )


def test_two_connections_under_one_name_are_never_guessed_between(tmp_path: Path) -> None:
    registry = ToolRegistry(ProjectDirectory(tmp_path / ".alkera").blobs())
    registry.register_connection(_connection("pg", "rec-a", "pg"))
    registry.register_connection(_connection("pg-team", "rec-b", "pg"))
    assert registry.connection_named("pg") is None


# -- gates over the notebook code -----------------------------------------------------


def _modules() -> list[Path]:
    return sorted(NOTEBOOKS.rglob("*.py"))


def test_no_notebook_code_looks_a_connection_up_by_local_handle() -> None:
    """A name a cell stores is the server's; a local handle may carry a
    suffix. Notebook code resolves by name (``connection_named``) or by
    record (``connection_by_record``), never ``connection_for``."""
    offenders = [
        f"{path.relative_to(NOTEBOOKS)}:{node.lineno}"
        for path in _modules()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Attribute) and node.attr == "connection_for"
    ]
    assert offenders == []


#: Handlers in the notebook code that catch everything and answer with an
#: empty collection. May only shrink.
EMPTY_ON_FAILURE_ALLOWED: frozenset[str] = frozenset()


def _returns_empty(handler: ast.ExceptHandler) -> bool:
    for node in ast.walk(handler):
        if not isinstance(node, ast.Return) or node.value is None:
            continue
        value = node.value
        if isinstance(value, ast.List | ast.Set | ast.Dict | ast.Tuple) and not (
            getattr(value, "elts", None) or getattr(value, "keys", None)
        ):
            return True
        if (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Name)
            and value.func.id in {"frozenset", "set", "list", "dict", "tuple"}
            and not value.args
        ):
            return True
    return False


def test_no_notebook_code_turns_a_failure_into_an_empty_answer() -> None:
    """A read that fails is said with its cause; an empty answer reads as
    "there is none", and sends a person after a problem that does not exist."""
    found = set()
    for path in _modules():
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            broad = isinstance(node, ast.ExceptHandler) and (
                node.type is None
                or (
                    isinstance(node.type, ast.Name)
                    and node.type.id in {"Exception", "BaseException"}
                )
            )
            if broad and _returns_empty(node):  # type: ignore[arg-type]
                found.add(f"{path.relative_to(NOTEBOOKS)}:{node.lineno}")
    assert found <= EMPTY_ON_FAILURE_ALLOWED, sorted(found - EMPTY_ON_FAILURE_ALLOWED)
