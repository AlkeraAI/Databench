"""Every notebook agent tool action has a person's route, or a written reason
it is the agent's alone; the agent-only list may only shrink."""

from __future__ import annotations

import types
import typing
from typing import Any

from alkera_notebook.tools import TOOLS
from backend.api.routes.files import build_files_router
from backend.api.routes.notebooks import router as notebooks_router
from backend.api.routes.notebooks.agent_parity import PARITY, AgentOnly, Route
from fastapi import FastAPI
from fastapi.routing import APIRoute

#: The notebook routes and the Files routes (a person creates a notebook as a
#: Files upload), mounted as the product mounts them.
app = FastAPI()
app.include_router(notebooks_router)
app.include_router(build_files_router())

#: The agent-only actions today. Lower it when one gains a route; never raise it.
AGENT_ONLY_CEILING = 5
_AXES = ("action", "what", "part", "direction")


def _literals(annotation: Any) -> list[str]:
    origin = typing.get_origin(annotation)
    if origin is typing.Literal:
        return [str(a) for a in typing.get_args(annotation)]
    if origin in (typing.Union, types.UnionType, list, typing.Annotated):
        args = typing.get_args(annotation)
        found: list[str] = []
        for arg in args[:1] if origin is typing.Annotated else args:
            found += _literals(arg)
        return found
    fields = getattr(annotation, "model_fields", None)
    if isinstance(fields, dict) and "op" in fields:
        return _literals(fields["op"].annotation)
    return []


def tool_actions() -> set[str]:
    found: set[str] = set()
    for name, tool in TOOLS.items():
        fields = tool.input.model_fields
        axis = next((k for k in (*_AXES, "ops") if k in fields), None)
        values = _literals(fields[axis].annotation) if axis is not None else []
        found |= {f"{name}:{v}" for v in values} if values else {f"{name}:*"}
    return found


def test_every_agent_action_is_named_and_nothing_else_is() -> None:
    actions = tool_actions()
    assert actions - set(PARITY) == set(), "an agent action with no route and no reason"
    assert set(PARITY) - actions == set(), "a parity entry for an action no tool has"


def test_every_route_named_exists() -> None:
    served = {
        (method, route.path)
        for route in app.routes
        if isinstance(route, APIRoute)
        for method in route.methods
    }
    missing = {(r.method, r.path) for r in PARITY.values() if isinstance(r, Route)} - served
    assert missing == set()


def test_the_agent_only_list_does_not_grow_and_each_says_why() -> None:
    only = {k: v for k, v in PARITY.items() if isinstance(v, AgentOnly)}
    assert len(only) <= AGENT_ONLY_CEILING
    assert all(len(v.reason) > 40 and v.reason.endswith(".") for v in only.values())


def test_an_action_a_tool_gains_is_caught(monkeypatch: Any) -> None:
    """The gate reads the tools' own vocabularies: a new action fails it."""
    from alkera_notebook.tools.models import NotebookEnvInput

    widened = typing.Literal["info", "upgrade"]
    monkeypatch.setattr(NotebookEnvInput.model_fields["action"], "annotation", widened)
    assert "notebook.env:upgrade" in tool_actions() - set(PARITY)
