"""The drive-default ACLs match the permission model for both org policies.

The expectations are written as ``(principal kind, whose id, role, admins only)``
tuples transcribed from A6 rather than derived from ``DEFAULT_ACLS``, so the
table and the product decision have to agree independently.
"""

from __future__ import annotations

import uuid

import pytest
from alkera_core.files.authz.defaults import (
    ADMIN_CONDITION,
    DEFAULT_ACLS,
    ORG_POLICY_OPEN,
    ORG_POLICY_RESTRICTED,
    DriveFolder,
    default_acl,
)
from alkera_core.files.authz.grants import ORIGIN_DRIVE_DEFAULT
from alkera_core.files.authz.ladder import DEFAULT_LADDER

ORG = uuid.UUID("00000000-0000-4000-8000-0000000000aa")
SUBJECT = uuid.UUID("00000000-0000-4000-8000-0000000000bb")

#: (folder, org policy) → the ACEs A6 specifies, as
#: (principal kind, principal id, role, admins only).
EXPECTED: dict[tuple[DriveFolder, str], tuple[tuple[str, uuid.UUID, str, bool], ...]] = {
    (DriveFolder.ROOT, ORG_POLICY_OPEN): (),
    (DriveFolder.ROOT, ORG_POLICY_RESTRICTED): (),
    (DriveFolder.SHARED, ORG_POLICY_OPEN): (
        ("org", ORG, "writer", False),
        ("team", ORG, "manager", True),
    ),
    (DriveFolder.SHARED, ORG_POLICY_RESTRICTED): (
        ("org", ORG, "reader", False),
        ("team", ORG, "manager", True),
    ),
    (DriveFolder.HOME, ORG_POLICY_OPEN): (("user", SUBJECT, "owner", False),),
    (DriveFolder.HOME, ORG_POLICY_RESTRICTED): (("user", SUBJECT, "owner", False),),
    (DriveFolder.TEAM, ORG_POLICY_OPEN): (
        ("team", SUBJECT, "writer", False),
        ("team", SUBJECT, "manager", True),
    ),
    (DriveFolder.TEAM, ORG_POLICY_RESTRICTED): (
        ("team", SUBJECT, "reader", False),
        ("team", SUBJECT, "manager", True),
    ),
}

_NEEDS_SUBJECT = (DriveFolder.HOME, DriveFolder.TEAM)


@pytest.mark.parametrize(
    ("folder", "policy"),
    list(EXPECTED),
    ids=[f"{folder.value}-{policy}" for folder, policy in EXPECTED],
)
def test_each_default_acl_matches_the_permission_model(folder: DriveFolder, policy: str) -> None:
    subject = SUBJECT if folder in _NEEDS_SUBJECT else None

    grants = default_acl(folder, org_policy=policy, org_team_id=ORG, subject_id=subject)

    assert (
        tuple(
            (
                grant.principal.kind,
                grant.principal.id,
                grant.role,
                grant.conditions == dict(ADMIN_CONDITION),
            )
            for grant in grants
        )
        == EXPECTED[(folder, policy)]
    )


@pytest.mark.parametrize(
    ("folder", "policy"),
    list(EXPECTED),
    ids=[f"{folder.value}-{policy}" for folder, policy in EXPECTED],
)
def test_every_default_grant_is_a_rung_and_carries_the_drive_default_origin(
    folder: DriveFolder, policy: str
) -> None:
    subject = SUBJECT if folder in _NEEDS_SUBJECT else None

    for grant in default_acl(folder, org_policy=policy, org_team_id=ORG, subject_id=subject):
        assert grant.role in DEFAULT_LADDER.roles
        assert grant.origin.kind == ORIGIN_DRIVE_DEFAULT
        assert grant.expires_at is None


def test_the_org_root_carries_no_grant_at_all() -> None:
    """It is a traversal-only container: a grant here would hand every drive in
    the org to every member."""
    assert DEFAULT_ACLS[DriveFolder.ROOT] == ()
    assert default_acl(DriveFolder.ROOT, org_policy=ORG_POLICY_OPEN, org_team_id=ORG) == ()


def test_a_home_folder_needs_the_user_it_belongs_to() -> None:
    with pytest.raises(ValueError, match="home needs a subject id"):
        default_acl(DriveFolder.HOME, org_policy=ORG_POLICY_OPEN, org_team_id=ORG)


def test_a_shared_folder_refuses_a_subject_it_would_ignore() -> None:
    """Refusing beats ignoring: a caller who passes a user id here has the wrong
    folder, and silently dropping it would create /Shared with the wrong ACL."""
    with pytest.raises(ValueError, match="shared takes no subject id"):
        default_acl(
            DriveFolder.SHARED,
            org_policy=ORG_POLICY_OPEN,
            org_team_id=ORG,
            subject_id=SUBJECT,
        )


def test_an_unknown_org_policy_is_refused_not_defaulted() -> None:
    with pytest.raises(ValueError, match="unknown org sharing policy"):
        default_acl(DriveFolder.SHARED, org_policy="public", org_team_id=ORG)
