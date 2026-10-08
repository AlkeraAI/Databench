"""Who may read and set an org's storage ceiling by hand.

The override replaces what the org's plan includes, so it is the platform's
own act on somebody else's tenant — the resource carries no ``org_id`` (the
engine's cross-org guard must not fire). Reading the ceiling and the usage is
open to all platform staff: support reads everything and writes nothing.
Setting or clearing the override is platform ADMIN only — support staff do not
decide how much a customer may store.

The staff check comes before the admin check so the two refusals read
differently in the decision row. Every fact is required on both actions, so a
caller that forgets one is denied rather than allowed by a branch that
happened not to need it.
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

POLICY = "platform.org_storage"

decide = staff_reads_admin_changes(POLICY)

register(
    Policy(
        name=POLICY,
        resource_type=ResourceType.PLATFORM_ORG_STORAGE,
        decide=decide,
        audited_attrs=AUDITED,
    )
)

__all__ = ["ADMIN_MESSAGE", "AUDITED", "POLICY", "STAFF_MESSAGE", "SUPPORTED", "decide"]
