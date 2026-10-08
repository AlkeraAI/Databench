"""The org audit chain, the platform audit log and the identity security log.

The names another domain may use, resolved on first use so importing this
package never imports its submodules.
"""

from __future__ import annotations

from types import ModuleType
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from backend.services.audit.identity_security import (
        SECURITY_EVENTS as SECURITY_EVENTS,
    )
    from backend.services.audit.identity_security import (
        SECURITY_EVENTS_PAGE_MAX as SECURITY_EVENTS_PAGE_MAX,
    )
    from backend.services.audit.identity_security import (
        ClientHint as ClientHint,
    )
    from backend.services.audit.identity_security import (
        record_security_event as record_security_event,
    )
    from backend.services.audit.identity_security import (
        record_security_events as record_security_events,
    )
    from backend.services.audit.identity_security import (
        security_events_of as security_events_of,
    )
    from backend.services.audit.org_audit import (
        PLATFORM_SCOPES as PLATFORM_SCOPES,
    )
    from backend.services.audit.org_audit import (
        OrgScopeFacts as OrgScopeFacts,
    )
    from backend.services.audit.org_audit import (
        path_param as path_param,
    )
    from backend.services.audit.org_audit import (
        record_offboarding as record_offboarding,
    )
    from backend.services.audit.org_audit import (
        record_org_audit as record_org_audit,
    )

_ORG_AUDIT = "backend.services.audit.org_audit"
_IDENTITY_SECURITY = "backend.services.audit.identity_security"

#: Each public name and the submodule that owns it.
_OWNERS: dict[str, str] = {
    "PLATFORM_SCOPES": "backend.services.audit.org_audit",
    "OrgScopeFacts": "backend.services.audit.org_audit",
    "path_param": "backend.services.audit.org_audit",
    "record_offboarding": _ORG_AUDIT,
    "record_org_audit": _ORG_AUDIT,
    "ClientHint": _IDENTITY_SECURITY,
    "SECURITY_EVENTS": _IDENTITY_SECURITY,
    "SECURITY_EVENTS_PAGE_MAX": _IDENTITY_SECURITY,
    "record_security_event": _IDENTITY_SECURITY,
    "record_security_events": _IDENTITY_SECURITY,
    "security_events_of": _IDENTITY_SECURITY,
}

__all__ = [
    "PLATFORM_SCOPES",
    "SECURITY_EVENTS",
    "SECURITY_EVENTS_PAGE_MAX",
    "ClientHint",
    "OrgScopeFacts",
    "path_param",
    "record_offboarding",
    "record_org_audit",
    "record_security_event",
    "record_security_events",
    "security_events_of",
]


def __getattr__(name: str) -> Any:
    owner = _OWNERS.get(name)
    if owner is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(_owner_module(owner), name)


def _owner_module(owner: str) -> ModuleType:
    """Import ``owner`` through a literal import. The compiled CLI build follows
    only literal imports, so a name resolved by string would be missing there."""
    if owner == _IDENTITY_SECURITY:
        from backend.services.audit import identity_security

        return identity_security
    if owner == _ORG_AUDIT:
        from backend.services.audit import org_audit

        return org_audit
    raise AssertionError(f"no import for {owner}")
