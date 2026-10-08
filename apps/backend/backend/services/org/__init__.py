"""An organization's shape: teams, memberships, invitations, org settings and member preferences."""

from __future__ import annotations

from types import ModuleType
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from backend.services.org.creation_hooks import (
        ORG_CREATED as ORG_CREATED,
    )
    from backend.services.org.creation_hooks import (
        OrgCreated as OrgCreated,
    )
    from backend.services.org.invitations import (
        InvitationError as InvitationError,
    )
    from backend.services.org.invitations import (
        accept_invitation as accept_invitation,
    )
    from backend.services.org.invitations import (
        create_invitation as create_invitation,
    )
    from backend.services.org.invitations import (
        invitation_by_token as invitation_by_token,
    )
    from backend.services.org.invitations import (
        invitation_link_closed_reason as invitation_link_closed_reason,
    )
    from backend.services.org.invitations import (
        list_pending_for_email as list_pending_for_email,
    )
    from backend.services.org.invitations import (
        masked_invited_address as masked_invited_address,
    )
    from backend.services.org.memberships import (
        MembershipError as MembershipError,
    )
    from backend.services.org.memberships import (
        RosterEntry as RosterEntry,
    )
    from backend.services.org.memberships import (
        add_member as add_member,
    )
    from backend.services.org.org_memberships import (
        LastActiveAdminError as LastActiveAdminError,
    )
    from backend.services.org.org_memberships import (
        NotPendingError as NotPendingError,
    )
    from backend.services.org.org_memberships import (
        activate_pending_membership as activate_pending_membership,
    )
    from backend.services.org.org_memberships import (
        active_membership_in as active_membership_in,
    )
    from backend.services.org.org_memberships import (
        active_memberships_of as active_memberships_of,
    )
    from backend.services.org.org_memberships import (
        create_org_membership as create_org_membership,
    )
    from backend.services.org.org_memberships import (
        deactivate_membership as deactivate_membership,
    )
    from backend.services.org.org_memberships import (
        is_active_member as is_active_member,
    )
    from backend.services.org.org_memberships import (
        is_org_admin as is_org_admin,
    )
    from backend.services.org.org_memberships import (
        members_of as members_of,
    )
    from backend.services.org.org_memberships import (
        membership_in as membership_in,
    )
    from backend.services.org.org_memberships import (
        org_admin_ids as org_admin_ids,
    )
    from backend.services.org.org_memberships import (
        org_ids_of as org_ids_of,
    )
    from backend.services.org.org_memberships import (
        reactivate_membership as reactivate_membership,
    )
    from backend.services.org.org_memberships import (
        remove_from_org as remove_from_org,
    )
    from backend.services.org.org_memberships import (
        withdraw_pending_membership as withdraw_pending_membership,
    )
    from backend.services.org.own_orgs import (
        OrgCreationLimitedError as OrgCreationLimitedError,
    )
    from backend.services.org.own_orgs import (
        found_org as found_org,
    )
    from backend.services.org.own_orgs import (
        leave_org as leave_org,
    )
    from backend.services.org.own_orgs import (
        next_org_after_leaving as next_org_after_leaving,
    )
    from backend.services.org.removal_hooks import (
        TEAM_DELETE_BLOCKERS as TEAM_DELETE_BLOCKERS,
    )
    from backend.services.org.removal_hooks import (
        MemberLeft as MemberLeft,
    )
    from backend.services.org.removal_hooks import (
        NamedHook as NamedHook,
    )
    from backend.services.org.removal_hooks import (
        OrgDeleted as OrgDeleted,
    )
    from backend.services.org.removal_hooks import (
        TeamDeleteBlocker as TeamDeleteBlocker,
    )
    from backend.services.org.removal_hooks import (
        TeamDeleted as TeamDeleted,
    )
    from backend.services.org.removal_hooks import (
        on_member_left as on_member_left,
    )
    from backend.services.org.removal_hooks import (
        on_org_deleted as on_org_deleted,
    )
    from backend.services.org.removal_hooks import (
        on_team_deleted as on_team_deleted,
    )
    from backend.services.org.settings import (
        sandbox_limits as sandbox_limits,
    )
    from backend.services.org.settings_sections import (
        ORG_SETTINGS_SECTIONS as ORG_SETTINGS_SECTIONS,
    )
    from backend.services.org.settings_sections import (
        OrgSettingsSection as OrgSettingsSection,
    )
    from backend.services.org.storage_limits import (
        MemberStorage as MemberStorage,
    )
    from backend.services.org.storage_limits import (
        ceilings_resolver as ceilings_resolver,
    )
    from backend.services.org.storage_limits import (
        clear_org_override as clear_org_override,
    )
    from backend.services.org.storage_limits import (
        clear_user_limit as clear_user_limit,
    )
    from backend.services.org.storage_limits import (
        drive_default_bytes as drive_default_bytes,
    )
    from backend.services.org.storage_limits import (
        drive_used_bytes as drive_used_bytes,
    )
    from backend.services.org.storage_limits import (
        effective_org_limit as effective_org_limit,
    )
    from backend.services.org.storage_limits import (
        member_picture as member_picture,
    )
    from backend.services.org.storage_limits import (
        member_storage as member_storage,
    )
    from backend.services.org.storage_limits import (
        org_drive as org_drive,
    )
    from backend.services.org.storage_limits import (
        org_override as org_override,
    )
    from backend.services.org.storage_limits import (
        org_storage_facts as org_storage_facts,
    )
    from backend.services.org.storage_limits import (
        set_org_override as set_org_override,
    )
    from backend.services.org.storage_limits import (
        set_user_limit as set_user_limit,
    )
    from backend.services.org.storage_limits import (
        summed_team_limit as summed_team_limit,
    )
    from backend.services.org.storage_limits import (
        user_limit_version as user_limit_version,
    )
    from backend.services.org.storage_limits import (
        user_limits_for as user_limits_for,
    )
    from backend.services.org.teams import (
        TeamConflictError as TeamConflictError,
    )
    from backend.services.org.teams import (
        admin_team_ids as admin_team_ids,
    )
    from backend.services.org.teams import (
        ancestor_chain as ancestor_chain,
    )
    from backend.services.org.teams import (
        belongs_to_org as belongs_to_org,
    )
    from backend.services.org.teams import (
        descendant_ids as descendant_ids,
    )
    from backend.services.org.teams import (
        files_transaction as files_transaction,
    )
    from backend.services.org.teams import (
        org_root_of as org_root_of,
    )
    from backend.services.org.teams import (
        teams_administered_by as teams_administered_by,
    )

#: Each public name and the submodule that owns it. A name resolves on first
#: use, so importing one submodule of this package never imports its siblings.
_OWNERS: dict[str, str] = {
    "belongs_to_org": "backend.services.org.teams",
    "MemberStorage": "backend.services.org.storage_limits",
    "ceilings_resolver": "backend.services.org.storage_limits",
    "clear_org_override": "backend.services.org.storage_limits",
    "clear_user_limit": "backend.services.org.storage_limits",
    "drive_default_bytes": "backend.services.org.storage_limits",
    "drive_used_bytes": "backend.services.org.storage_limits",
    "effective_org_limit": "backend.services.org.storage_limits",
    "member_picture": "backend.services.org.storage_limits",
    "member_storage": "backend.services.org.storage_limits",
    "org_drive": "backend.services.org.storage_limits",
    "org_override": "backend.services.org.storage_limits",
    "org_storage_facts": "backend.services.org.storage_limits",
    "set_org_override": "backend.services.org.storage_limits",
    "set_user_limit": "backend.services.org.storage_limits",
    "summed_team_limit": "backend.services.org.storage_limits",
    "user_limit_version": "backend.services.org.storage_limits",
    "user_limits_for": "backend.services.org.storage_limits",
    "admin_team_ids": "backend.services.org.teams",
    "org_root_of": "backend.services.org.teams",
    "teams_administered_by": "backend.services.org.teams",
    "invitation_by_token": "backend.services.org.invitations",
    "invitation_link_closed_reason": "backend.services.org.invitations",
    "masked_invited_address": "backend.services.org.invitations",
    "OrgCreationLimitedError": "backend.services.org.own_orgs",
    "found_org": "backend.services.org.own_orgs",
    "leave_org": "backend.services.org.own_orgs",
    "next_org_after_leaving": "backend.services.org.own_orgs",
    "accept_invitation": "backend.services.org.invitations",
    "list_pending_for_email": "backend.services.org.invitations",
    "NotPendingError": "backend.services.org.org_memberships",
    "activate_pending_membership": "backend.services.org.org_memberships",
    "withdraw_pending_membership": "backend.services.org.org_memberships",
    "create_org_membership": "backend.services.org.org_memberships",
    "InvitationError": "backend.services.org.invitations",
    "MembershipError": "backend.services.org.memberships",
    "RosterEntry": "backend.services.org.memberships",
    "TeamConflictError": "backend.services.org.teams",
    "ancestor_chain": "backend.services.org.teams",
    "files_transaction": "backend.services.org.teams",
    "sandbox_limits": "backend.services.org.settings",
    "LastActiveAdminError": "backend.services.org.org_memberships",
    "add_member": "backend.services.org.memberships",
    "create_invitation": "backend.services.org.invitations",
    "org_ids_of": "backend.services.org.org_memberships",
    "active_membership_in": "backend.services.org.org_memberships",
    "active_memberships_of": "backend.services.org.org_memberships",
    "deactivate_membership": "backend.services.org.org_memberships",
    "is_active_member": "backend.services.org.org_memberships",
    "is_org_admin": "backend.services.org.org_memberships",
    "org_admin_ids": "backend.services.org.org_memberships",
    "members_of": "backend.services.org.org_memberships",
    "membership_in": "backend.services.org.org_memberships",
    "reactivate_membership": "backend.services.org.org_memberships",
    "remove_from_org": "backend.services.org.org_memberships",
    "MemberLeft": "backend.services.org.removal_hooks",
    "NamedHook": "backend.services.org.removal_hooks",
    "ORG_CREATED": "backend.services.org.creation_hooks",
    "OrgCreated": "backend.services.org.creation_hooks",
    "ORG_SETTINGS_SECTIONS": "backend.services.org.settings_sections",
    "OrgSettingsSection": "backend.services.org.settings_sections",
    "TEAM_DELETE_BLOCKERS": "backend.services.org.removal_hooks",
    "TeamDeleteBlocker": "backend.services.org.removal_hooks",
    "OrgDeleted": "backend.services.org.removal_hooks",
    "TeamDeleted": "backend.services.org.removal_hooks",
    "on_member_left": "backend.services.org.removal_hooks",
    "on_org_deleted": "backend.services.org.removal_hooks",
    "on_team_deleted": "backend.services.org.removal_hooks",
    "descendant_ids": "backend.services.org.teams",
}

__all__ = [
    "ORG_CREATED",
    "ORG_SETTINGS_SECTIONS",
    "TEAM_DELETE_BLOCKERS",
    "InvitationError",
    "LastActiveAdminError",
    "MemberLeft",
    "MemberStorage",
    "MembershipError",
    "NamedHook",
    "NotPendingError",
    "OrgCreated",
    "OrgCreationLimitedError",
    "OrgDeleted",
    "OrgSettingsSection",
    "RosterEntry",
    "TeamConflictError",
    "TeamDeleteBlocker",
    "TeamDeleted",
    "accept_invitation",
    "activate_pending_membership",
    "active_membership_in",
    "active_memberships_of",
    "add_member",
    "admin_team_ids",
    "ancestor_chain",
    "belongs_to_org",
    "ceilings_resolver",
    "clear_org_override",
    "clear_user_limit",
    "create_invitation",
    "create_org_membership",
    "deactivate_membership",
    "descendant_ids",
    "drive_default_bytes",
    "drive_used_bytes",
    "effective_org_limit",
    "files_transaction",
    "found_org",
    "invitation_by_token",
    "invitation_link_closed_reason",
    "is_active_member",
    "is_org_admin",
    "leave_org",
    "list_pending_for_email",
    "masked_invited_address",
    "member_picture",
    "member_storage",
    "members_of",
    "membership_in",
    "next_org_after_leaving",
    "on_member_left",
    "on_org_deleted",
    "on_team_deleted",
    "org_admin_ids",
    "org_drive",
    "org_ids_of",
    "org_override",
    "org_root_of",
    "org_storage_facts",
    "reactivate_membership",
    "remove_from_org",
    "sandbox_limits",
    "set_org_override",
    "set_user_limit",
    "summed_team_limit",
    "teams_administered_by",
    "user_limit_version",
    "user_limits_for",
    "withdraw_pending_membership",
]


def __getattr__(name: str) -> Any:
    owner = _OWNERS.get(name)
    if owner is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(_owner_module(owner), name)


def _owner_module(owner: str) -> ModuleType:
    """Import ``owner`` through a literal import. The compiled CLI build follows
    only literal imports, so a name resolved by string would be missing there."""
    if owner == "backend.services.org.invitations":
        from backend.services.org import invitations

        return invitations
    if owner == "backend.services.org.memberships":
        from backend.services.org import memberships

        return memberships
    if owner == "backend.services.org.settings":
        from backend.services.org import settings

        return settings
    if owner == "backend.services.org.teams":
        from backend.services.org import teams

        return teams
    if owner == "backend.services.org.org_memberships":
        from backend.services.org import org_memberships

        return org_memberships
    if owner == "backend.services.org.removal_hooks":
        from backend.services.org import removal_hooks

        return removal_hooks
    if owner == "backend.services.org.settings_sections":
        from backend.services.org import settings_sections

        return settings_sections
    if owner == "backend.services.org.creation_hooks":
        from backend.services.org import creation_hooks

        return creation_hooks
    if owner == "backend.services.org.own_orgs":
        from backend.services.org import own_orgs

        return own_orgs
    if owner == "backend.services.org.storage_limits":
        from backend.services.org import storage_limits

        return storage_limits
    raise AssertionError(f"no import for {owner}")
