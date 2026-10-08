"""Platform plumbing every domain uses: the worker nudge and the row-security boot canary."""

from __future__ import annotations

from types import ModuleType
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from backend.services.infra.clock import (
        now as now,
    )
    from backend.services.infra.task_queue import (
        nudge as nudge,
    )
    from backend.services.infra.task_queue import (
        nudge_account_lifecycle_sweep as nudge_account_lifecycle_sweep,
    )
    from backend.services.infra.task_queue import (
        nudge_files_operation as nudge_files_operation,
    )
    from backend.services.infra.task_queue import (
        nudge_org_machine_reconcile as nudge_org_machine_reconcile,
    )
    from backend.services.infra.task_queue import (
        nudge_workspace_deletions as nudge_workspace_deletions,
    )
    from backend.services.infra.task_queue import (
        start_workspace_machine_move as start_workspace_machine_move,
    )

#: Each public name and the submodule that owns it. A name resolves on first
#: use, so importing one submodule of this package never imports its siblings.
_OWNERS: dict[str, str] = {
    "now": "backend.services.infra.clock",
    "nudge": "backend.services.infra.task_queue",
    "nudge_account_lifecycle_sweep": "backend.services.infra.task_queue",
    "nudge_files_operation": "backend.services.infra.task_queue",
    "nudge_workspace_deletions": "backend.services.infra.task_queue",
    "nudge_org_machine_reconcile": "backend.services.infra.task_queue",
    "start_workspace_machine_move": "backend.services.infra.task_queue",
}

__all__ = [
    "now",
    "nudge",
    "nudge_account_lifecycle_sweep",
    "nudge_files_operation",
    "nudge_org_machine_reconcile",
    "nudge_workspace_deletions",
    "start_workspace_machine_move",
]


def __getattr__(name: str) -> Any:
    owner = _OWNERS.get(name)
    if owner is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(_owner_module(owner), name)


def _owner_module(owner: str) -> ModuleType:
    """Import ``owner`` through a literal import. The compiled CLI build follows
    only literal imports, so a name resolved by string would be missing there."""
    if owner == "backend.services.infra.clock":
        from backend.services.infra import clock

        return clock
    if owner == "backend.services.infra.task_queue":
        from backend.services.infra import task_queue

        return task_queue
    raise AssertionError(f"no import for {owner}")
