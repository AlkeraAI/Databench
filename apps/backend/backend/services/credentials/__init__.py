"""The credentials that act for a person or a machine: personal access tokens,
proxy and CI tokens, entitlements and machine credentials.
"""

from __future__ import annotations

from backend.services.credentials.machine_credentials import (
    get as get_machine_credential,
)
from backend.services.credentials.machine_credentials import (
    list_personal,
    mint_personal,
)
from backend.services.credentials.machine_credentials import (
    resolve_active as resolve_machine_credential,
)
from backend.services.credentials.machine_credentials import (
    revoke as revoke_machine_credential,
)

__all__ = [
    "get_machine_credential",
    "list_personal",
    "mint_personal",
    "resolve_machine_credential",
    "revoke_machine_credential",
]
