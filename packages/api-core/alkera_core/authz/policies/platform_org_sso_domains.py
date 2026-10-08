"""Who may read and set the email domains an org's SSO speaks for.

An email domain is shared by every org, and the org that holds one decides
where that domain's people sign in and which of their addresses its SCIM may
create. So the assignment is the platform's decision, never the org's: every
platform staff member may read an org's domains; only a platform ADMIN assigns
or removes them. The resource carries no ``org_id`` (the engine's cross-org
guard must not fire): it is about the platform's namespace, not the tenant's.
"""

from __future__ import annotations

from alkera_core.authz.engine import Policy, register
from alkera_core.authz.enums import ResourceType
from alkera_core.authz.policies._staff_reads_admin_changes import (
    ADMIN_MESSAGE,
    AUDITED,
    STAFF_MESSAGE,
    SUPPORTED,
    staff_reads_admin_changes,
)

POLICY = "platform.org_sso_domains"

decide = staff_reads_admin_changes(POLICY)

register(
    Policy(
        name=POLICY,
        resource_type=ResourceType.PLATFORM_ORG_SSO_DOMAINS,
        decide=decide,
        audited_attrs=AUDITED,
    )
)

__all__ = ["ADMIN_MESSAGE", "AUDITED", "POLICY", "STAFF_MESSAGE", "SUPPORTED", "decide"]
