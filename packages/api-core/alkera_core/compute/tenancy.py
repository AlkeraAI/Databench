"""Who a box serves: the tenancy vocabulary the backend stores on an allocation
and the bootstrap hands the box (``ALKERA_MACHINE_TENANCY``).

Standard library only: the box's supervisor reads it without the ORM.
"""

from __future__ import annotations

from typing import Literal

ComputeTenancy = Literal["org", "pool", "dedicated", "personal"]
COMPUTE_TENANCIES: tuple[str, ...] = ("org", "pool", "dedicated", "personal")
#: The org's own box (registered by one of its members, admitted by its grant):
#: serves that org's chats and no other's.
ORG_TENANCY: ComputeTenancy = "org"
#: A platform box in the shared pool: serves the chats of any org that has no
#: machine of its own, spread by load.
POOL_TENANCY: ComputeTenancy = "pool"
#: A platform box assigned to exactly one org as its dedicated compute: serves
#: that org's chats and never another's.
DEDICATED_TENANCY: ComputeTenancy = "dedicated"
#: A person's own box, registered through the device flow on a machine
#: credential bound to one org: serves that person's own private chats in that
#: org and nobody else's, is never placed for anyone else, and is never billed
#: (it is their hardware). It stands only while its owner is an active member
#: of the org.
PERSONAL_TENANCY: ComputeTenancy = "personal"


__all__ = [
    "COMPUTE_TENANCIES",
    "DEDICATED_TENANCY",
    "ORG_TENANCY",
    "PERSONAL_TENANCY",
    "POOL_TENANCY",
    "ComputeTenancy",
]
