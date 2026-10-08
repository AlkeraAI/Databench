"""User schema read/write asymmetry.

Reads must tolerate empty names (the DB JIT/SSO sentinel); writes must require
real names. A regression here either 500s on reading a half-provisioned user
or lets blank names through registration.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from alkera_core.schemas.identity.user import UserCreate, UserRead
from pydantic import ValidationError


def _user_read_payload(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "id": str(uuid4()),
        "org_team_id": str(uuid4()),
        "email": "u@example.com",
        "first_name": "Ada",
        "last_name": "Lovelace",
        "display_name": "Ada Lovelace",
        "has_password": True,
        "created_at": datetime.now(UTC).isoformat(),
    }
    base.update(overrides)
    return base


def test_user_read_accepts_empty_names() -> None:
    # A JIT/SSO-provisioned user with no name must still serialize.
    parsed = UserRead.model_validate(
        _user_read_payload(first_name="", last_name="", display_name="")
    )
    assert parsed.first_name == ""
    assert parsed.display_name == ""


def test_user_read_round_trips() -> None:
    original = UserRead.model_validate(_user_read_payload())
    restored = UserRead.model_validate_json(original.model_dump_json())
    assert restored == original


def test_user_create_requires_non_empty_names() -> None:
    org = uuid4()
    with pytest.raises(ValidationError):
        UserCreate(email="u@example.com", first_name="", last_name="X", org_team_id=org)
    with pytest.raises(ValidationError):
        UserCreate(email="u@example.com", first_name="X", last_name="", org_team_id=org)


def test_user_create_accepts_oauth_user_without_password() -> None:
    created = UserCreate(email="u@example.com", first_name="A", last_name="B", org_team_id=uuid4())
    assert created.password is None
