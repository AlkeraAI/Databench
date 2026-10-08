"""Account bans, the IP facts they are decided on, and the per-identity cap on
creating orgs."""

from __future__ import annotations

from types import ModuleType
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from backend.services.abuse.bans import (
        BanConflictError as BanConflictError,
    )
    from backend.services.abuse.bans import (
        BanRefusedError as BanRefusedError,
    )
    from backend.services.abuse.bans import (
        NoActiveBanError as NoActiveBanError,
    )
    from backend.services.abuse.org_creation import (
        org_creation_allowed as org_creation_allowed,
    )

#: Each public name and the submodule that owns it. A name resolves on first
#: use, so importing one submodule of this package never imports its siblings.
_OWNERS: dict[str, str] = {
    "BanConflictError": "backend.services.abuse.bans",
    "BanRefusedError": "backend.services.abuse.bans",
    "NoActiveBanError": "backend.services.abuse.bans",
    "org_creation_allowed": "backend.services.abuse.org_creation",
}

__all__ = [
    "BanConflictError",
    "BanRefusedError",
    "NoActiveBanError",
    "org_creation_allowed",
]


def __getattr__(name: str) -> Any:
    owner = _OWNERS.get(name)
    if owner is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(_owner_module(owner), name)


def _owner_module(owner: str) -> ModuleType:
    """Import ``owner`` through a literal import. The compiled CLI build follows
    only literal imports, so a name resolved by string would be missing there."""
    if owner == "backend.services.abuse.bans":
        from backend.services.abuse import bans

        return bans
    if owner == "backend.services.abuse.org_creation":
        from backend.services.abuse import org_creation

        return org_creation
    raise AssertionError(f"no import for {owner}")
