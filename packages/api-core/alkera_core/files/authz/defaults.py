"""The drive-default ACLs, as data.

Every folder a drive creates for itself — the org root, ``/Shared``,
``/home/<user>``, ``/Teams/<team>`` — starts with a fixed grant list, and
``drives.py`` imports these by name rather than spelling roles at the creation
site. Two of the rows depend on the org's sharing policy (a closed org gets
``reader`` where an open one gets ``writer``), so the role is a small table
keyed by policy rather than an ``if``.

The admin rows ("org admins are managers on ``/Shared``") are expressed as an
ABAC condition on the team grant — ``conditions={"team_role": "admin"}`` — not
as a separate principal kind. That is what the day-one ``conditions`` column is
for, and it means the later engine reads the row as-is instead of learning a
Files-only vocabulary.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from alkera_core.files.authz.grants import Grant, GrantOrigin, Principal

#: How widely an org shares its ``/Shared`` space and its team folders.
ORG_POLICY_OPEN = "open"
ORG_POLICY_RESTRICTED = "restricted"
ORG_POLICIES: Final[tuple[str, ...]] = (ORG_POLICY_OPEN, ORG_POLICY_RESTRICTED)

#: The condition that narrows a team grant to that team's admins.
ADMIN_CONDITION: Final[Mapping[str, str]] = {"team_role": "admin"}


class DefaultSubject(StrEnum):
    """Who a default ACE names, resolved to a real id when the folder is made."""

    #: Everyone in the organization (the org root team id).
    ORG = "org"
    #: The organization's admins, by condition on the org root team grant.
    ORG_ADMINS = "org_admins"
    #: The user the folder belongs to (``/home/<user>``).
    SUBJECT_USER = "subject_user"
    #: The team the folder belongs to (``/Teams/<team>``).
    SUBJECT_TEAM = "subject_team"
    #: That team's admins.
    TEAM_ADMINS = "team_admins"


class DriveFolder(StrEnum):
    """The folders a drive creates for itself."""

    ROOT = "root"
    SHARED = "shared"
    HOME = "home"
    TEAM = "team"


@dataclass(frozen=True, slots=True)
class DefaultAce:
    """One default grant: who, what role per org policy, and whether the role is
    narrowed to the subject team's admins."""

    subject: DefaultSubject
    roles: Mapping[str, str]
    admins_only: bool = False


_OWNER_EVERYWHERE: Mapping[str, str] = dict.fromkeys(ORG_POLICIES, "owner")
_MANAGER_EVERYWHERE: Mapping[str, str] = dict.fromkeys(ORG_POLICIES, "manager")
_WRITER_OR_READER: Mapping[str, str] = {
    ORG_POLICY_OPEN: "writer",
    ORG_POLICY_RESTRICTED: "reader",
}

#: The table. The org root carries nothing at all: it is a traversal-only
#: container, and a grant there would leak every drive to every member.
DEFAULT_ACLS: Final[Mapping[DriveFolder, tuple[DefaultAce, ...]]] = {
    DriveFolder.ROOT: (),
    DriveFolder.SHARED: (
        DefaultAce(DefaultSubject.ORG, _WRITER_OR_READER),
        DefaultAce(DefaultSubject.ORG_ADMINS, _MANAGER_EVERYWHERE, admins_only=True),
    ),
    DriveFolder.HOME: (DefaultAce(DefaultSubject.SUBJECT_USER, _OWNER_EVERYWHERE),),
    DriveFolder.TEAM: (
        DefaultAce(DefaultSubject.SUBJECT_TEAM, _WRITER_OR_READER),
        DefaultAce(DefaultSubject.TEAM_ADMINS, _MANAGER_EVERYWHERE, admins_only=True),
    ),
}

_PRINCIPAL_KINDS: Mapping[DefaultSubject, str] = {
    DefaultSubject.ORG: "org",
    DefaultSubject.ORG_ADMINS: "team",
    DefaultSubject.SUBJECT_USER: "user",
    DefaultSubject.SUBJECT_TEAM: "team",
    DefaultSubject.TEAM_ADMINS: "team",
}

_ORG_SUBJECTS = (DefaultSubject.ORG, DefaultSubject.ORG_ADMINS)


def default_acl(
    folder: DriveFolder,
    *,
    org_policy: str,
    org_team_id: uuid.UUID,
    subject_id: uuid.UUID | None = None,
) -> tuple[Grant, ...]:
    """The drive-default grants for ``folder``, bound to real ids.

    ``subject_id`` is the user of a ``/home/<user>`` or the team of a
    ``/Teams/<team>``; it is refused for the folders that have no subject, so a
    caller cannot silently bind a home folder to the wrong person.
    """
    if org_policy not in ORG_POLICIES:
        raise ValueError(f"unknown org sharing policy {org_policy!r}")
    aces = DEFAULT_ACLS[folder]
    needs_subject = any(ace.subject not in _ORG_SUBJECTS for ace in aces)
    if needs_subject and subject_id is None:
        raise ValueError(f"{folder.value} needs a subject id")
    if not needs_subject and subject_id is not None:
        raise ValueError(f"{folder.value} takes no subject id")
    grants: list[Grant] = []
    for ace in aces:
        principal_id = org_team_id if ace.subject in _ORG_SUBJECTS else subject_id
        assert principal_id is not None
        grants.append(
            Grant(
                principal=Principal(kind=_PRINCIPAL_KINDS[ace.subject], id=principal_id),
                role=ace.roles[org_policy],
                origin=GrantOrigin.drive_default(),
                conditions=dict(ADMIN_CONDITION) if ace.admins_only else None,
            )
        )
    return tuple(grants)


__all__ = [
    "ADMIN_CONDITION",
    "DEFAULT_ACLS",
    "ORG_POLICIES",
    "ORG_POLICY_OPEN",
    "ORG_POLICY_RESTRICTED",
    "DefaultAce",
    "DefaultSubject",
    "DriveFolder",
    "default_acl",
]
