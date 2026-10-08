"""What a person does through the platform for each thing the notebook agent
tools can do.

Every action of every notebook agent tool maps to the route a person (the
portal, VS Code) reaches the same capability through, or to the written
reason it is the agent's alone. A test walks the tool catalog and fails on
an action this table does not name, on a route that does not exist, and on
any growth of the agent-only list, which may only shrink.

An action is the value of the tool input's ``action``, ``what``, ``part``
or ``direction`` field, or the ``op`` of each edit; a tool with none of them
is one action, ``*``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from backend.api.routes.notebooks import PREFIX


@dataclass(frozen=True, slots=True)
class Route:
    """The route a person reaches the capability through."""

    method: str
    path: str


@dataclass(frozen=True, slots=True)
class AgentOnly:
    """Why a person has no route for it."""

    reason: str


NB: Final = PREFIX + "/{drive_id}/{item_id}"

VIEW: Final = Route("GET", NB)
OPS: Final = Route("POST", NB + "/ops")
KERNEL: Final = Route("POST", NB + "/kernel")
ENVS: Final = Route("GET", NB + "/envs")

PARITY: Final[dict[str, Route | AgentOnly]] = {
    "notebook.read:*": VIEW,
    "notebook.create:*": Route("POST", "/api/v1/files/uploads"),
    **{
        f"notebook.edit:{op}": OPS
        for op in (
            "insert",
            "edit",
            "replace",
            "delete",
            "restore",
            "move",
            "rename",
            "set_kind",
            "set_config",
            "set_meta",
            "set_setting",
        )
    },
    "notebook.run:*": Route("POST", NB + "/runs"),
    **{
        f"notebook.kernel:{a}": KERNEL
        for a in ("status", "interrupt", "interrupt_all", "restart", "shutdown")
    },
    "notebook.cells:clear_outputs": Route("POST", NB + "/outputs/clear"),
    **{f"notebook.cells:{a}": OPS for a in ("enable", "disable", "duplicate", "move", "set_kind")},
    **{f"notebook.output:{p}": VIEW for p in ("all", "text", "error", "chart")},
    "notebook.output:image": Route("GET", NB + "/blobs/{sha256}"),
    "notebook.output:table": Route("GET", NB + "/cells/{cell_id}/table"),
    "notebook.output:widget": Route("POST", NB + "/frames"),
    **{f"notebook.show_output:{p}": VIEW for p in ("auto", "chart", "markdown", "text")},
    "notebook.show_output:image": Route("GET", NB + "/blobs/{sha256}"),
    "notebook.show_output:table": Route("GET", NB + "/cells/{cell_id}/table"),
    "notebook.inspect:variables": AgentOnly(
        "A person sees each cell's variables live in the Variables panel, from the "
        "notebook channel's cell.variables events; there is nothing to ask for."
    ),
    "notebook.inspect:frame": Route("GET", NB + "/cells/{cell_id}/table"),
    "notebook.inspect:value": AgentOnly(
        "Summarizing a live value by name runs code in the shared kernel; a person "
        "writes a cell that shows it instead."
    ),
    **{
        f"notebook.graph:{d}": AgentOnly(
            "A person sees a cell's readers and writers drawn in the notebook from the "
            "view's defs and refs; the agent asks for them as text."
        )
        for d in ("both", "up", "down")
    },
    "notebook.widget:list": Route("POST", NB + "/frames"),
    "notebook.widget:get": Route("POST", NB + "/frames"),
    "notebook.widget:set": Route("POST", NB + "/comm"),
    "notebook.env:info": ENVS,
    "notebook.env:list": ENVS,
    "notebook.env:packages": Route("GET", NB + "/envs/{env_id:path}/packages"),
    "notebook.env:install": Route("POST", NB + "/env/install"),
    "notebook.env:materialize": Route("POST", NB + "/env/{action}"),
    "notebook.env:remove": Route("POST", NB + "/env/{action}"),
    "notebook.env:cancel": Route("POST", NB + "/env/{action}"),
    "notebook.env:switch": OPS,
    "notebook.settings:*": OPS,
}

__all__ = ["PARITY", "AgentOnly", "Route"]
