"""Team and personal connections: the rows, their verification, credential
leases, connection OAuth and the connection inventory.
"""

from __future__ import annotations

from types import ModuleType
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from backend.services.connections.leases import (
        CREDENTIAL_UNREADABLE_REASON as CREDENTIAL_UNREADABLE_REASON,
    )
    from backend.services.connections.leases import (
        MIN_LEASE_TTL_SECONDS as MIN_LEASE_TTL_SECONDS,
    )
    from backend.services.connections.leases import (
        NO_CREATOR as NO_CREATOR,
    )
    from backend.services.connections.leases import (
        ConnectionNotFoundError as ConnectionNotFoundError,
    )
    from backend.services.connections.leases import (
        CredentialUnreadableError as CredentialUnreadableError,
    )
    from backend.services.connections.leases import (
        Decide as Decide,
    )
    from backend.services.connections.leases import (
        OwnerCredentialOnDeviceError as OwnerCredentialOnDeviceError,
    )
    from backend.services.connections.leases import (
        OwnerReauthRequiredError as OwnerReauthRequiredError,
    )
    from backend.services.connections.leases import (
        badge_of as badge_of,
    )
    from backend.services.connections.leases import (
        can_manage as can_manage,
    )
    from backend.services.connections.leases import (
        decide_credential_fetch as decide_credential_fetch,
    )
    from backend.services.connections.leases import (
        doc_and_groups as doc_and_groups,
    )
    from backend.services.connections.leases import (
        has_shared_credentials as has_shared_credentials,
    )
    from backend.services.connections.leases import (
        lease_for_owner as lease_for_owner,
    )
    from backend.services.connections.leases import (
        lease_of as lease_of,
    )
    from backend.services.connections.leases import (
        mark_credential_unreadable as mark_credential_unreadable,
    )
    from backend.services.connections.leases import (
        member_connections as member_connections,
    )
    from backend.services.connections.leases import (
        reauth_for as reauth_for,
    )
    from backend.services.connections.leases import (
        shared_bundle as shared_bundle,
    )
    from backend.services.connections.ports import (
        CONNECTION_CHECKS as CONNECTION_CHECKS,
    )
    from backend.services.connections.ports import (
        CONNECTION_OAUTH as CONNECTION_OAUTH,
    )
    from backend.services.connections.ports import (
        PROVIDER_UNREACHABLE_CODE as PROVIDER_UNREACHABLE_CODE,
    )
    from backend.services.connections.ports import (
        PROVIDER_UNREACHABLE_MESSAGE as PROVIDER_UNREACHABLE_MESSAGE,
    )
    from backend.services.connections.ports import (
        AfterCommit as AfterCommit,
    )
    from backend.services.connections.ports import (
        GrantOnDeviceError as GrantOnDeviceError,
    )
    from backend.services.connections.ports import (
        ProviderUnreachableError as ProviderUnreachableError,
    )
    from backend.services.connections.ports import (
        ReauthRequiredError as ReauthRequiredError,
    )
    from backend.services.connections.ports import (
        SaveCheckRequiredError as SaveCheckRequiredError,
    )
    from backend.services.connections.ports import (
        connection_checks as connection_checks,
    )
    from backend.services.connections.ports import (
        connection_oauth as connection_oauth,
    )
    from backend.services.connections.team_connections import (
        BuiltUpsert as BuiltUpsert,
    )
    from backend.services.connections.team_connections import (
        ProbeDial as ProbeDial,
    )
    from backend.services.connections.team_connections import (
        SaveRefusedError as SaveRefusedError,
    )
    from backend.services.connections.team_connections import (
        decrypt_named_secrets as decrypt_named_secrets,
    )
    from backend.services.connections.team_connections import (
        decrypt_shared_secret as decrypt_shared_secret,
    )
    from backend.services.connections.team_connections import (
        emit_connection_updated as emit_connection_updated,
    )
    from backend.services.connections.team_connections import (
        get_by_id as get_by_id,
    )
    from backend.services.connections.team_connections import (
        get_by_identity as get_by_identity,
    )
    from backend.services.connections.team_connections import (
        team_delete_refusal as team_delete_refusal,
    )
    from backend.services.connections.team_connections import (
        validate_and_build as validate_and_build,
    )

#: Each public name and the submodule that owns it. A name resolves on first
#: use, so importing one submodule of this package never imports its siblings.
_OWNERS: dict[str, str] = {
    "BuiltUpsert": "backend.services.connections.team_connections",
    "ProbeDial": "backend.services.connections.team_connections",
    "SaveRefusedError": "backend.services.connections.team_connections",
    "decrypt_named_secrets": "backend.services.connections.team_connections",
    "decrypt_shared_secret": "backend.services.connections.team_connections",
    "emit_connection_updated": "backend.services.connections.team_connections",
    "get_by_id": "backend.services.connections.team_connections",
    "get_by_identity": "backend.services.connections.team_connections",
    "validate_and_build": "backend.services.connections.team_connections",
    "CONNECTION_CHECKS": "backend.services.connections.ports",
    "CONNECTION_OAUTH": "backend.services.connections.ports",
    "AfterCommit": "backend.services.connections.ports",
    "GrantOnDeviceError": "backend.services.connections.ports",
    "SaveCheckRequiredError": "backend.services.connections.ports",
    "connection_checks": "backend.services.connections.ports",
    "connection_oauth": "backend.services.connections.ports",
    "Decide": "backend.services.connections.leases",
    "OwnerCredentialOnDeviceError": "backend.services.connections.leases",
    "OwnerReauthRequiredError": "backend.services.connections.leases",
    "shared_bundle": "backend.services.connections.leases",
    "reauth_for": "backend.services.connections.leases",
    "member_connections": "backend.services.connections.leases",
    "mark_credential_unreadable": "backend.services.connections.leases",
    "lease_of": "backend.services.connections.leases",
    "lease_for_owner": "backend.services.connections.leases",
    "has_shared_credentials": "backend.services.connections.leases",
    "doc_and_groups": "backend.services.connections.leases",
    "decide_credential_fetch": "backend.services.connections.leases",
    "can_manage": "backend.services.connections.leases",
    "badge_of": "backend.services.connections.leases",
    "NO_CREATOR": "backend.services.connections.leases",
    "MIN_LEASE_TTL_SECONDS": "backend.services.connections.leases",
    "CredentialUnreadableError": "backend.services.connections.leases",
    "ConnectionNotFoundError": "backend.services.connections.leases",
    "CREDENTIAL_UNREADABLE_REASON": "backend.services.connections.leases",
    "PROVIDER_UNREACHABLE_CODE": "backend.services.connections.ports",
    "PROVIDER_UNREACHABLE_MESSAGE": "backend.services.connections.ports",
    "ProviderUnreachableError": "backend.services.connections.ports",
    "ReauthRequiredError": "backend.services.connections.ports",
    "team_delete_refusal": "backend.services.connections.team_connections",
}

__all__ = [
    "CONNECTION_CHECKS",
    "CONNECTION_OAUTH",
    "CREDENTIAL_UNREADABLE_REASON",
    "MIN_LEASE_TTL_SECONDS",
    "NO_CREATOR",
    "PROVIDER_UNREACHABLE_CODE",
    "PROVIDER_UNREACHABLE_MESSAGE",
    "AfterCommit",
    "BuiltUpsert",
    "ConnectionNotFoundError",
    "CredentialUnreadableError",
    "Decide",
    "GrantOnDeviceError",
    "OwnerCredentialOnDeviceError",
    "OwnerReauthRequiredError",
    "ProbeDial",
    "ProviderUnreachableError",
    "ReauthRequiredError",
    "SaveCheckRequiredError",
    "SaveRefusedError",
    "badge_of",
    "can_manage",
    "connection_checks",
    "connection_oauth",
    "decide_credential_fetch",
    "decrypt_named_secrets",
    "decrypt_shared_secret",
    "doc_and_groups",
    "emit_connection_updated",
    "get_by_id",
    "get_by_identity",
    "has_shared_credentials",
    "lease_for_owner",
    "lease_of",
    "mark_credential_unreadable",
    "member_connections",
    "reauth_for",
    "shared_bundle",
    "team_delete_refusal",
    "validate_and_build",
]


def __getattr__(name: str) -> Any:
    owner = _OWNERS.get(name)
    if owner is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(_owner_module(owner), name)


def _owner_module(owner: str) -> ModuleType:
    """Import ``owner`` through a literal import. The compiled CLI build follows
    only literal imports, so a name resolved by string would be missing there."""
    if owner == "backend.services.connections.leases":
        from backend.services.connections import leases

        return leases
    if owner == "backend.services.connections.ports":
        from backend.services.connections import ports

        return ports
    if owner == "backend.services.connections.team_connections":
        from backend.services.connections import team_connections

        return team_connections
    raise AssertionError(f"no import for {owner}")
