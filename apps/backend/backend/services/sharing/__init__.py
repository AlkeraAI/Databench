"""Who may reach a workspace object, and the role a node grants."""

from __future__ import annotations

from types import ModuleType
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from backend.services.sharing.access import (
        Reader as Reader,
    )
    from backend.services.sharing.access import (
        admin_reads_private as admin_reads_private,
    )
    from backend.services.sharing.access import (
        bound_machine as bound_machine,
    )
    from backend.services.sharing.access import (
        chat_attrs as chat_attrs,
    )
    from backend.services.sharing.access import (
        load_object as load_object,
    )
    from backend.services.sharing.access import (
        machine_reader as machine_reader,
    )
    from backend.services.sharing.access import (
        new_workspace_attrs as new_workspace_attrs,
    )
    from backend.services.sharing.access import (
        object_resource as object_resource,
    )
    from backend.services.sharing.access import (
        readable_chat_facts as readable_chat_facts,
    )
    from backend.services.sharing.access import (
        readable_workspace_facts as readable_workspace_facts,
    )
    from backend.services.sharing.access import (
        resolve_reader as resolve_reader,
    )
    from backend.services.sharing.access import (
        workspace_attrs as workspace_attrs,
    )
    from backend.services.sharing.access import (
        workspace_machine_id as workspace_machine_id,
    )
    from backend.services.sharing.node_role import (
        SharedRungCache as SharedRungCache,
    )
    from backend.services.sharing.node_role import (
        rung_writes as rung_writes,
    )
    from backend.services.sharing.node_role import (
        shared_object_role as shared_object_role,
    )

#: Each public name and the submodule that owns it. A name resolves on first
#: use, so importing one submodule of this package never imports its siblings.
_OWNERS: dict[str, str] = {
    "load_object": "backend.services.sharing.access",
    "Reader": "backend.services.sharing.access",
    "SharedRungCache": "backend.services.sharing.node_role",
    "admin_reads_private": "backend.services.sharing.access",
    "bound_machine": "backend.services.sharing.access",
    "chat_attrs": "backend.services.sharing.access",
    "machine_reader": "backend.services.sharing.access",
    "new_workspace_attrs": "backend.services.sharing.access",
    "object_resource": "backend.services.sharing.access",
    "readable_chat_facts": "backend.services.sharing.access",
    "readable_workspace_facts": "backend.services.sharing.access",
    "resolve_reader": "backend.services.sharing.access",
    "rung_writes": "backend.services.sharing.node_role",
    "shared_object_role": "backend.services.sharing.node_role",
    "workspace_attrs": "backend.services.sharing.access",
    "workspace_machine_id": "backend.services.sharing.access",
}

__all__ = [
    "Reader",
    "SharedRungCache",
    "admin_reads_private",
    "bound_machine",
    "chat_attrs",
    "load_object",
    "machine_reader",
    "new_workspace_attrs",
    "object_resource",
    "readable_chat_facts",
    "readable_workspace_facts",
    "resolve_reader",
    "rung_writes",
    "shared_object_role",
    "workspace_attrs",
    "workspace_machine_id",
]


def __getattr__(name: str) -> Any:
    owner = _OWNERS.get(name)
    if owner is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(_owner_module(owner), name)


def _owner_module(owner: str) -> ModuleType:
    """Import ``owner`` through a literal import. The compiled CLI build follows
    only literal imports, so a name resolved by string would be missing there."""
    if owner == "backend.services.sharing.access":
        from backend.services.sharing import access

        return access
    if owner == "backend.services.sharing.node_role":
        from backend.services.sharing import node_role

        return node_role
    raise AssertionError(f"no import for {owner}")
