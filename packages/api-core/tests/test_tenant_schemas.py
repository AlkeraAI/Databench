"""Round-trip + validation tests for the tenant Pydantic schemas.

These cover the actual contract that backend producers and CLI / SDK
consumers depend on — schema-shape correctness, not ORM internals.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from alkera_core.models._enums import PlatformRole, TeamRole
from alkera_core.schemas.identity.user import UserCreate, UserRead
from alkera_core.schemas.tenancy.team import TeamCreate, TeamRead
from alkera_core.schemas.tenancy.team_membership import (
    TeamMemberRead,
    TeamMembershipBase,
    TeamMembershipRead,
)
from pydantic import ValidationError


def _now() -> datetime:
    return datetime.now(UTC)


# ---------- Team ----------


def test_team_read_validates_root_payload():
    payload = {
        "id": str(uuid4()),
        "parent_team_id": None,
        "name": "Acme Corp",
        "is_root": True,
        "created_at": _now().isoformat(),
    }
    parsed = TeamRead.model_validate(payload)
    assert parsed.is_root is True
    assert parsed.parent_team_id is None


def test_team_read_validates_subteam_payload():
    parent_id = uuid4()
    parsed = TeamRead.model_validate(
        {
            "id": str(uuid4()),
            "parent_team_id": str(parent_id),
            "name": "Engineering",
            "is_root": False,
            "created_at": _now().isoformat(),
        }
    )
    assert parsed.parent_team_id == parent_id
    assert parsed.is_root is False


def test_team_create_rejects_empty_name():
    with pytest.raises(ValidationError):
        TeamCreate(name="")


def test_team_create_accepts_no_parent():
    payload = TeamCreate(name="Acme")
    assert payload.parent_team_id is None


def test_team_read_defaults_member_count_to_zero():
    parsed = TeamRead.model_validate(
        {
            "id": str(uuid4()),
            "parent_team_id": None,
            "name": "Acme",
            "is_root": True,
            "created_at": _now().isoformat(),
        }
    )
    assert parsed.member_count == 0


def test_team_read_round_trips_member_count():
    original = TeamRead(
        id=uuid4(),
        parent_team_id=None,
        name="Acme",
        is_root=True,
        created_at=_now(),
        member_count=7,
    )
    restored = TeamRead.model_validate_json(original.model_dump_json())
    assert restored.member_count == 7
    assert restored == original


def test_team_member_read_round_trips():
    original = TeamMemberRead(
        user_id=uuid4(),
        display_name="Alice Liddell",
        email="alice@example.com",
        first_name="Alice",
        last_name="Liddell",
        role=TeamRole.ADMIN,
        team_id=uuid4(),
        team_name="Engineering",
        created_at=_now(),
    )
    restored = TeamMemberRead.model_validate_json(original.model_dump_json())
    assert restored.role is TeamRole.ADMIN
    # The computed friendly label is serialized for clients.
    assert original.role_display == "Admin"
    assert original.model_dump()["role_display"] == "Admin"


# ---------- Role display names ----------


def test_team_role_display_names():
    assert TeamRole.MEMBER.display_name == "Member"
    assert TeamRole.ADMIN.display_name == "Admin"


def test_platform_role_display_names():
    assert PlatformRole.ALKERA_SUPPORT.display_name == "Platform support"
    assert PlatformRole.ALKERA_ADMIN.display_name == "Platform admin"


def test_membership_read_exposes_role_display():
    m = TeamMembershipRead(
        id=uuid4(), user_id=uuid4(), team_id=uuid4(), role=TeamRole.MEMBER, created_at=_now()
    )
    assert m.model_dump()["role_display"] == "Member"


def test_user_read_exposes_platform_role_display():
    staff = UserRead(
        id=uuid4(),
        org_team_id=uuid4(),
        email="s@example.com",
        first_name="S",
        last_name="T",
        display_name="S T",
        platform_role=PlatformRole.ALKERA_SUPPORT,
        created_at=_now(),
    )
    assert staff.model_dump()["platform_role_display"] == "Platform support"
    plain = UserRead(
        id=uuid4(),
        org_team_id=uuid4(),
        email="p@example.com",
        first_name="P",
        last_name="U",
        display_name="P U",
        platform_role=None,
        created_at=_now(),
    )
    assert plain.model_dump()["platform_role_display"] is None


# ---------- User ----------


def test_user_read_round_trips_without_platform_role():
    original = UserRead(
        id=uuid4(),
        org_team_id=uuid4(),
        email="alice@example.com",
        first_name="Alice",
        last_name="Liddell",
        display_name="Alice Liddell",
        platform_role=None,
        created_at=_now(),
    )
    restored = UserRead.model_validate_json(original.model_dump_json())
    assert restored == original
    assert restored.platform_role is None


def test_user_read_round_trips_with_platform_role():
    original = UserRead(
        id=uuid4(),
        org_team_id=uuid4(),
        email="staff@example.com",
        first_name="Staff",
        last_name="Member",
        display_name="Staff Member",
        platform_role=PlatformRole.ALKERA_ADMIN,
        created_at=_now(),
    )
    restored = UserRead.model_validate_json(original.model_dump_json())
    assert restored.platform_role is PlatformRole.ALKERA_ADMIN


def test_user_create_rejects_malformed_email():
    with pytest.raises(ValidationError):
        UserCreate(email="not-an-email", first_name="x", last_name="y", org_team_id=uuid4())


def test_user_create_accepts_no_password():
    payload = UserCreate(
        email="oauth@example.com", first_name="OAuth", last_name="User", org_team_id=uuid4()
    )
    assert payload.password is None


def test_user_create_rejects_short_password():
    with pytest.raises(ValidationError):
        UserCreate(
            email="x@example.com",
            first_name="X",
            last_name="Y",
            org_team_id=uuid4(),
            password="short",
        )


# ---------- TeamMembership ----------


def test_team_membership_base_defaults_to_member_role():
    base = TeamMembershipBase(user_id=uuid4(), team_id=uuid4())
    assert base.role is TeamRole.MEMBER


def test_team_membership_read_round_trips_admin():
    original = TeamMembershipRead(
        id=uuid4(),
        user_id=uuid4(),
        team_id=uuid4(),
        role=TeamRole.ADMIN,
        created_at=_now(),
    )
    restored = TeamMembershipRead.model_validate_json(original.model_dump_json())
    assert restored.role is TeamRole.ADMIN
    assert restored == original
