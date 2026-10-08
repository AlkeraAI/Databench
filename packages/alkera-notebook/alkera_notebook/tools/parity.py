"""What an agent calls for each thing a person can do in a notebook.

Every command in the notebook editor's command table
(``packages/notebook-ui/src/editor/commands.ts``: the cell menu, the toolbar
and the keyboard all dispatch through it) and every other control a person
uses (a panel, a setting, an environment action) maps here to the agent tool
calls that do the same thing, or to the written reason an agent has no use
for it. :data:`COMMAND_PARITY` and :data:`SURFACE_PARITY` are the one owner of
that mapping; ``packages/alkera-notebook/tests/test_nbagt_command_parity.py`` is its gate. It fails
when the editor gains a command this table does not name, when a path names a
call its tool would refuse, when an action that needs the right to edit or run
is reachable by an agent call the gate treats as a read, and when the list of
exemptions grows (it may only shrink).

The reverse direction, every agent action to the route a person reaches it
through, is ``backend.api.routes.notebooks.agent_parity``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final

from alkera_notebook.format.settings import NOTEBOOK_SETTINGS


@dataclass(frozen=True, slots=True)
class AgentCall:
    """One tool call, without its ``path``, that does (a step of) what the
    person's action does."""

    tool: str
    args: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Exempt:
    """Why an agent has no call for it."""

    reason: str


Parity = tuple[AgentCall, ...] | Exempt


def _run(target: dict[str, Any]) -> AgentCall:
    return AgentCall("notebook.run", {"target": target})


def _edit(*ops: dict[str, Any]) -> AgentCall:
    return AgentCall("notebook.edit", {"ops": list(ops)})


def _cells(action: str, **args: Any) -> AgentCall:
    return AgentCall("notebook.cells", {"action": action, **args})


def _kernel(action: str) -> AgentCall:
    return AgentCall("notebook.kernel", {"action": action})


def _sample_setting(name: str) -> Any:
    """A value a person could pick for setting ``name`` in the settings form."""
    spec = next(s for s in NOTEBOOK_SETTINGS if s.name == name)
    if spec.choices:
        return spec.choices[-1][0]
    if spec.type == "bool":
        return True
    if spec.type == "int":
        return spec.minimum if spec.minimum is not None else 1
    if spec.type == "env":
        return "default"
    return "x"


#: The cell a sample call acts on, written the way an agent may write it.
_CELL = "Cell 2"
_ID = "a7yg9x7evz"

_FOCUS = Exempt("Focus and the editor's mode exist only in a person's editor; tools take a cell.")
_INSERT = {"op": "insert", "after": _ID}

#: Every command in the editor's command table, by its ``CommandId``.
COMMAND_PARITY: Final[dict[str, Parity]] = {
    "run.cell": (_run({"kind": "cells", "ids": [_CELL]}),),
    "run.advance": (_run({"kind": "cells", "ids": [_CELL]}),),
    "run.insert_below": (_run({"kind": "cells", "ids": [_CELL]}), _edit(_INSERT)),
    "run.all": (_run({"kind": "all"}),),
    "run.stale": (_run({"kind": "stale"}),),
    "run.above": (_run({"kind": "above", "id": _CELL}),),
    "run.below": (_run({"kind": "below", "id": _CELL}),),
    "kernel.interrupt": (_kernel("interrupt"),),
    "kernel.interrupt_clear": (_kernel("interrupt_all"),),
    "kernel.restart": (_kernel("restart"),),
    "kernel.restart_run_all": (_run({"kind": "all", "restart": True}),),
    "kernel.shutdown": (_kernel("shutdown"),),
    "cell.insert_above": (_edit({"op": "insert", "before": _ID}),),
    "cell.insert_below": (_edit(_INSERT),),
    "cell.insert_markdown_below": (_edit({**_INSERT, "kind": "markdown"}),),
    "cell.insert_sql_below": (_edit({**_INSERT, "kind": "sql"}),),
    "cell.insert_markdown_above": (_edit({"op": "insert", "before": _ID, "kind": "markdown"}),),
    "cell.insert_sql_above": (_edit({"op": "insert", "before": _ID, "kind": "sql"}),),
    "cell.delete": (_edit({"op": "delete", "cell_id": _ID}),),
    "cell.restore": (_edit({"op": "restore", "cell_id": _ID}),),
    "cell.duplicate": (_cells("duplicate", cells=[_CELL]),),
    "cell.split": (_edit({"op": "replace", "cell_id": _ID, "source": "a"}, _INSERT),),
    "cell.merge_next": (
        _edit({"op": "replace", "cell_id": _ID, "source": "a"}, {"op": "delete", "cell_id": _ID}),
    ),
    "cell.move_up": (_cells("move", cells=[_CELL], to="up"),),
    "cell.move_down": (_cells("move", cells=[_CELL], to="down"),),
    "cell.to_python": (_cells("set_kind", cells=[_CELL], kind="python"),),
    "cell.to_markdown": (_cells("set_kind", cells=[_CELL], kind="markdown"),),
    "cell.to_sql": (_cells("set_kind", cells=[_CELL], kind="sql"),),
    "cell.toggle_disabled": (
        _cells("disable", cells=[_CELL]),
        _cells("enable", cells=[_CELL]),
    ),
    "cell.toggle_hide_code": (
        _edit({"op": "set_config", "cell_id": _ID, "config": {"hide_code": True}}),
    ),
    "cell.toggle_expand_output": (
        _edit({"op": "set_config", "cell_id": _ID, "config": {"expand_output": True}}),
    ),
    "cell.toggle_show_result": (
        _edit({"op": "set_meta", "cell_id": _ID, "meta": {"show_output": False}}),
    ),
    "cell.copy_link": Exempt(
        "A link is for a person to paste; an agent names a cell by id, name or position."
    ),
    "cell.rename": (_edit({"op": "rename", "cell_id": _ID, "name": "total"}),),
    "output.clear": (_cells("clear_outputs", cells=[_CELL]),),
    "output.clear_all": (_cells("clear_outputs"),),
    "output.toggle_collapse": Exempt(
        "Collapsing is one viewer's view and is not saved; an agent reads outputs whole."
    ),
    "output.open_tab": (AgentCall("notebook.output", {"cell": _CELL}),),
    "output.download_image": (AgentCall("notebook.output", {"cell": _CELL, "part": "image"}),),
    "nav.previous": _FOCUS,
    "nav.next": _FOCUS,
    "mode.edit": _FOCUS,
    "mode.command": _FOCUS,
    "doc.undo": Exempt(
        "Undo steps back through one person's own edits in their editor; an agent reverses "
        "its change with notebook.edit."
    ),
    "doc.redo": Exempt(
        "Redo replays one person's own undone edits in their editor; an agent makes the "
        "change again with notebook.edit."
    ),
    "find.open": Exempt(
        "Find highlights matches on a person's screen; an agent reads every cell's source "
        "with notebook.read."
    ),
    "find.replace": (_edit({"op": "edit", "cell_id": _ID, "edits": [{"old": "a", "new": "b"}]}),),
    "edit.toggle_comment": (
        _edit({"op": "edit", "cell_id": _ID, "edits": [{"old": "a", "new": "# a"}]}),
    ),
}

#: What a person does outside the command table: the side panels, the
#: settings form (one entry per notebook setting) and the environment panel.
SURFACE_PARITY: Final[dict[str, Parity]] = {
    # A person sees a cell's output under it; the agent puts the same output
    # in front of the person in the chat.
    "output.view": (AgentCall("notebook.show_output", {"cell": _CELL}),),
    "panel.graph": (
        AgentCall("notebook.graph", {"cell": _CELL}),
        AgentCall("notebook.graph", {"cell": _CELL, "depth": "all"}),
    ),
    "panel.variables": (AgentCall("notebook.inspect", {"what": "variables"}),),
    "panel.outline": (AgentCall("notebook.read", {"include_source": False}),),
    "panel.table": (AgentCall("notebook.inspect", {"what": "frame", "name": "df"}),),
    "panel.widget": (
        AgentCall("notebook.widget", {"action": "set", "model_id": "m", "state": {}}),
    ),
    "env.install": (AgentCall("notebook.env", {"action": "install", "packages": ["polars"]}),),
    "env.remove": (AgentCall("notebook.env", {"action": "remove", "packages": ["polars"]}),),
    "env.build": (AgentCall("notebook.env", {"action": "materialize"}),),
    "env.cancel": (AgentCall("notebook.env", {"action": "cancel"}),),
    "env.list": (AgentCall("notebook.env", {"action": "list"}),),
    "env.packages": (AgentCall("notebook.env", {"action": "packages"}),),
    **{
        f"settings.{spec.name}": (
            AgentCall("notebook.settings", {spec.name: _sample_setting(spec.name)}),
        )
        for spec in NOTEBOOK_SETTINGS
    },
}


__all__ = ["COMMAND_PARITY", "SURFACE_PARITY", "AgentCall", "Exempt", "Parity"]
