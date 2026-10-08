"""Who may run a notebook.

Running executes the notebook's code in its kernel, on the machine holding
its workspace, so it takes more than reading the file:

* **Can edit or above.** The caller's Files rung on the notebook is ``writer``
  or stronger. A reader or commenter sees the outputs and runs nothing.
* **Can edit on what the kernel reaches.** The kernel binds the whole folder
  its machine holds (a workspace's tree), so the caller needs Can edit on
  that folder too, not only on the one notebook: a notebook shared on its own
  inside a workspace must not hand its editor the rest of the tree or the
  workspace's connections. While no machine holds the folder nothing runs,
  and the route keeps the folder it checked, so a machine that takes the
  folder after the decision is not where the request goes.
* **A write could land.** The lease covering the notebook admits writes
  (no live lease holds it, or the holder's lease takes inbound writes there).
  A run submits the cells' current text, so it is refused exactly when an
  edit would be.
* **An agent's chat belongs to the workspace.** An agent acts for its chat:
  it runs a notebook only when that chat is a member of the workspace that
  holds the notebook, never through its person's rung on a notebook in a
  workspace the chat is not part of.
* **A machine on its own credential runs nothing here.** A box's engine runs
  what it is asked to; it never asks the platform to run.

One action, :attr:`Action.RUN`, covers a run, a kernel control (interrupt,
restart, shutdown), a widget message and an environment install: each acts
through the kernel. The route resolves every fact; nothing here reads the
database.

Attributes (all required unless noted):

* ``rung`` (``str``): the caller's Files rung on the notebook, ``""`` for none.
* ``scope_held`` (``bool``): a machine holds the folder the kernel binds.
* ``scope_rung`` (``str``): the caller's Files rung on that folder, ``""``
  for none or when no machine holds it.
* ``lease_admits_writes`` (``bool``).
* ``agent_chat_in_workspace`` (``bool``): required when the acting principal
  is an agent.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from alkera_core.authz.decision import Decision, allow, deny
from alkera_core.authz.engine import Policy, register, require_attr
from alkera_core.authz.enums import Action, PrincipalKind, ResourceType
from alkera_core.authz.principal import ActingContext
from alkera_core.authz.resource import Resource

POLICY: Final = "notebook.run"
AUDITED: Final = frozenset(
    {"rung", "scope_held", "scope_rung", "lease_admits_writes", "agent_chat_in_workspace"}
)
SUPPORTED: Final = frozenset({Action.RUN})

#: The body code a visible refusal carries.
REFUSED: Final = "notebook.run_refused"
NOT_FOUND: Final = "Not found"


#: The Files ladder's rungs, lowest first (``alkera_core.files.authz.ladder``,
#: spelled here because importing the Files package from a policy loads the
#: whole Files stack while the authz package is still initialising, which is
#: an import cycle; a test pins the two equal).
RUNGS: Final[tuple[str, ...]] = ("reader", "commenter", "writer", "manager", "owner")
#: The lowest rung that edits (Can edit), and so runs.
RUNS_FROM: Final = "writer"


def _rank(rung: str) -> int:
    return RUNGS.index(rung) if rung in RUNGS else -1


def _refused(reason: str, message: str) -> Decision:
    return deny(POLICY, reason, message=message, error_code=REFUSED)


def decide(
    ctx: ActingContext, action: Action, resource: Resource, attrs: Mapping[str, object]
) -> Decision | None:
    if action not in SUPPORTED:
        return deny(POLICY, "action_not_supported", message="Not allowed")
    rung = require_attr(attrs, "rung", str)
    scope_held = require_attr(attrs, "scope_held", bool)
    scope_rung = require_attr(attrs, "scope_rung", str)
    lease_admits_writes = require_attr(attrs, "lease_admits_writes", bool)
    if ctx.acting_principal.kind is PrincipalKind.MACHINE:
        return _refused("machine_runs_nothing", "A machine cannot run a notebook here")
    if _rank(rung) < 0:
        # No rung at all: the caller may not even learn the notebook exists.
        return deny(POLICY, "no_rung", message=NOT_FOUND, as_not_found=True)
    if _rank(rung) < _rank(RUNS_FROM):
        return _refused("needs_can_edit", "Running a notebook takes Can edit")
    if scope_held and _rank(scope_rung) < _rank(RUNS_FROM):
        return _refused(
            "needs_can_edit_on_folder",
            "Running this notebook takes Can edit on the folder it runs in",
        )
    if ctx.is_agent:
        in_workspace = require_attr(attrs, "agent_chat_in_workspace", bool)
        if not in_workspace:
            return _refused(
                "agent_chat_outside_workspace",
                "This chat is not part of the workspace that holds the notebook",
            )
    if not lease_admits_writes:
        return _refused("lease_refuses_writes", "The notebook's folder is held by another writer")
    return allow(POLICY, "agent_in_workspace_can_edit" if ctx.is_agent else "can_edit")


register(
    Policy(
        name=POLICY,
        resource_type=ResourceType.NOTEBOOK,
        decide=decide,
        audited_attrs=AUDITED,
    )
)

__all__ = ["AUDITED", "NOT_FOUND", "POLICY", "REFUSED", "SUPPORTED", "decide"]
