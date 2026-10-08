"""The memberships list's pending state, the join request, and the SSO link
confirmation shapes: round trips and the values each one refuses."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from alkera_core.schemas.identity.membership import JoinMembershipRequest, MembershipRead
from alkera_core.schemas.identity.sso import (
    SsoLinkCancelResponse,
    SsoLinkConfirmResponse,
    SsoLinkRead,
)
from pydantic import BaseModel, ValidationError


def _membership(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "org_team_id": str(uuid4()),
        "org_name": "Acme",
        "role": "member",
        "sso_required": False,
    }
    base.update(overrides)
    return base


def test_a_membership_without_a_status_reads_as_active() -> None:
    assert MembershipRead.model_validate(_membership()).status == "active"


@pytest.mark.parametrize("state", ["active", "pending"])
def test_a_membership_round_trips_its_status(state: str) -> None:
    read = MembershipRead.model_validate(_membership(status=state))
    assert MembershipRead.model_validate(read.model_dump(mode="json")).status == state


@pytest.mark.parametrize(
    "state",
    [
        pytest.param("deactivated", id="a-deactivated-membership-is-never-listed"),
        pytest.param("PENDING", id="case-matters"),
        pytest.param("", id="empty"),
    ],
)
def test_a_membership_refuses_any_other_status(state: str) -> None:
    with pytest.raises(ValidationError):
        MembershipRead.model_validate(_membership(status=state))


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({}, id="missing"),
        pytest.param({"org_team_id": "not-a-uuid"}, id="not-a-uuid"),
        pytest.param({"org_team_id": None}, id="null"),
    ],
)
def test_the_join_request_needs_an_org_id(body: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        JoinMembershipRequest.model_validate(body)


@pytest.mark.parametrize(
    ("model", "payload"),
    [
        pytest.param(
            SsoLinkRead,
            {"org_team_id": str(uuid4()), "org_name": "Acme", "email_masked": "a***@acme.com"},
            id="link-read",
        ),
        pytest.param(
            SsoLinkConfirmResponse,
            {"linked": True, "joined": False, "org_team_id": str(uuid4()), "message": "Ask."},
            id="confirm",
        ),
        pytest.param(SsoLinkCancelResponse, {"cancelled": True}, id="cancel"),
        pytest.param(JoinMembershipRequest, {"org_team_id": str(uuid4())}, id="join"),
    ],
)
def test_round_trip(model: type[BaseModel], payload: dict[str, Any]) -> None:
    dumped = model.model_validate(payload).model_dump(mode="json")
    assert dumped == payload


def test_a_confirmation_without_a_message_carries_none() -> None:
    confirm = SsoLinkConfirmResponse.model_validate(
        {"linked": True, "joined": True, "org_team_id": str(uuid4())}
    )
    assert confirm.message is None
