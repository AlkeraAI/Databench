"""A shared model the CLI or the box parses never forbids a field it does not know.

The box ships only through a release and a laptop updates when its person
says so, so either can be a release behind the server it talks to. A model in
``alkera_core`` that the CLI hands a server's answer (``model_validate``,
``model_validate_json``, ``TypeAdapter``) with ``extra="forbid"`` anywhere in
it turns the server's next additive field into a refusal on every one of
them. This module finds every such model by reading the CLI's source, and
fails on one that forbids extras, nested models included.
"""

from __future__ import annotations

import ast
import importlib
import typing
from collections.abc import Iterator
from pathlib import Path

import pytest
from alkera_core.schemas.realtime import WsTicketResponse
from pydantic import BaseModel

pytestmark = [pytest.mark.spread]

CLI_ROOT = Path(__file__).resolve().parents[1] / "alkera_cli"
_PARSERS = frozenset({"model_validate", "model_validate_json"})


def parsed_shared_models(root: Path) -> dict[tuple[str, str], set[str]]:
    """``(module, class) -> files`` for every ``alkera_core`` class the source
    under ``root`` parses data with."""
    found: dict[tuple[str, str], set[str]] = {}
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imported: dict[str, tuple[str, str]] = {}
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ImportFrom)
                and node.module
                and node.module.startswith("alkera_core.")
            ):
                for alias in node.names:
                    imported[alias.asname or alias.name] = (node.module, alias.name)
        for node in ast.walk(tree):
            name: str | None = None
            if (
                isinstance(node, ast.Attribute)
                and node.attr in _PARSERS
                and isinstance(node.value, ast.Name)
            ):
                name = node.value.id
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "TypeAdapter"
                and node.args
            ):
                name = next(
                    (
                        n.id
                        for n in ast.walk(node.args[0])
                        if isinstance(n, ast.Name) and n.id in imported
                    ),
                    None,
                )
            if name in imported:
                found.setdefault(imported[name], set()).add(path.name)
    return found


def _models_in(annotation: object) -> Iterator[type[BaseModel]]:
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        yield annotation
    for arg in typing.get_args(annotation):
        yield from _models_in(arg)


def forbidding(model: type[BaseModel]) -> list[str]:
    """The names of ``model`` and every model nested in its fields that forbid extras."""
    seen: set[type[BaseModel]] = set()
    pending = [model]
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        for field in current.model_fields.values():
            pending.extend(_models_in(field.annotation))
    return sorted(m.__name__ for m in seen if m.model_config.get("extra") == "forbid")


def test_every_shared_model_the_cli_parses_ignores_new_fields() -> None:
    parsed = parsed_shared_models(CLI_ROOT)
    strict = {
        f"{module}.{name} (parsed in {', '.join(sorted(files))})": bad
        for (module, name), files in sorted(parsed.items())
        if isinstance(model := getattr(importlib.import_module(module), name), type)
        and issubclass(model, BaseModel)
        and (bad := forbidding(model))
    }
    assert strict == {}


def test_the_scan_reads_the_real_tree() -> None:
    parsed = parsed_shared_models(CLI_ROOT)
    assert ("alkera_core.schemas.realtime", "WsTicketResponse") in parsed
    assert ("alkera_core.schemas.compute_machines", "MachineResources") in parsed


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        pytest.param(
            "from alkera_core.schemas.x import A\nA.model_validate({})\n",
            {("alkera_core.schemas.x", "A")},
            id="model-validate",
        ),
        pytest.param(
            "from alkera_core.compute.y import B as C\nC.model_validate_json('{}')\n",
            {("alkera_core.compute.y", "B")},
            id="aliased-json",
        ),
        pytest.param(
            "from alkera_core.z import D\nT = TypeAdapter(list[D])\n",
            {("alkera_core.z", "D")},
            id="type-adapter",
        ),
        pytest.param(
            "from alkera_cli.local import E\nE.model_validate({})\n", set(), id="cli-own-model"
        ),
    ],
)
def test_a_planted_parse_is_found(tmp_path: Path, source: str, expected: set[object]) -> None:
    (tmp_path / "mod.py").write_text(source, encoding="utf-8")
    assert set(parsed_shared_models(tmp_path)) == expected


def test_a_nested_model_that_forbids_is_named() -> None:
    from pydantic import ConfigDict

    class Inner(BaseModel):
        model_config = ConfigDict(extra="forbid")
        x: int = 0

    class Outer(BaseModel):
        items: list[Inner] = []
        maybe: Inner | None = None

    assert forbidding(Outer) == ["Inner"]


def test_a_ticket_with_a_field_this_build_does_not_know_still_mints() -> None:
    answer = WsTicketResponse.model_validate(
        {"ticket": "t", "expires_in": 30, "path": "/ws", "audience": "box"}
    )
    assert (answer.ticket, answer.expires_in, answer.path) == ("t", 30, "/ws")
