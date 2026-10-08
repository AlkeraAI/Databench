"""The connection wire shapes.

Three surfaces read these and nothing else: the portal plate, the member
sync-down, and the add dialog's poll. What is pinned here is the part a route
test cannot see — that each shape round-trips through JSON unchanged, that the
derived status is a CLOSED vocabulary rather than a free string, and which
fields are genuinely optional, because a field that quietly defaults is a field
a client will read as a fact nobody stated.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from alkera_core.connections import Badge, CredentialState, Outcome, Reauth, VerificationState
from alkera_core.connections.schemas import (
    MemberCredentialCounts,
    MemberTeamConnection,
    TeamConnectionRead,
    TeamConnectionUpsertRequest,
    VerificationEndpointOutcome,
    VerificationRead,
)
from pydantic import BaseModel, ValidationError

NOW = datetime(2026, 5, 4, 10, 0, tzinfo=UTC)


def _read(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": str(uuid4()),
        "team_id": str(uuid4()),
        "team_name": "Acme",
        "plugin": "postgres",
        "handle": "wh_main",
        "shared_values": {"host": "db.example.com"},
        "auth_mode": "shared",
        "auth_method": "password",
        "member_fields": [],
        "badge": "connected",
        "badge_reason": "",
        "outcome": "ok",
        "last_detail": "",
        "last_verified_at": NOW.isoformat(),
        "verification_state": None,
        "credential_state": "present",
        "reauth": None,
        "members": None,
        "created_at": NOW.isoformat(),
        "updated_at": NOW.isoformat(),
    }
    body.update(overrides)
    return body


def _member(**overrides: Any) -> dict[str, Any]:
    body = {
        key: value for key, value in _read().items() if key in MemberTeamConnection.model_fields
    }
    body["updated_at"] = NOW.isoformat()
    body.update(overrides)
    return body


def _verification(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": str(uuid4()),
        "connection_id": None,
        "state": "queued",
        "outcome": None,
        "detail": "",
        "endpoint_results": {},
        "requested_at": NOW.isoformat(),
        "dispatched_at": None,
        "dispatch_attempts": 0,
        "last_dispatch_error": "",
        "started_at": None,
        "finished_at": None,
    }
    body.update(overrides)
    return body


@pytest.mark.parametrize(
    ("model", "body"),
    [
        pytest.param(TeamConnectionRead, _read(), id="admin-view"),
        pytest.param(
            TeamConnectionRead,
            _read(members={"authorized": 3, "needs_reauth": 1}, auth_mode="per_user"),
            id="admin-view-with-member-counts",
        ),
        pytest.param(MemberTeamConnection, _member(), id="member-view"),
        pytest.param(
            MemberTeamConnection,
            _member(badge="needs_reauth", credential_state="needs_reauth", reauth="browser"),
            id="member-view-of-their-own-trouble",
        ),
        pytest.param(VerificationRead, _verification(), id="a-queued-check"),
        pytest.param(
            VerificationRead,
            _verification(
                connection_id=str(uuid4()),
                state="settled",
                outcome="unreachable",
                detail="cannot reach Kafka Connect",
                endpoint_results={
                    "broker": {"outcome": "ok", "detail": "", "service_identity": "cluster-17"},
                    "connect": {
                        "outcome": "unreachable",
                        "detail": "cannot reach Kafka Connect",
                        "service_identity": "",
                    },
                },
                dispatched_at=NOW.isoformat(),
                dispatch_attempts=2,
                started_at=NOW.isoformat(),
                finished_at=NOW.isoformat(),
            ),
            id="a-settled-check-naming-its-endpoint",
        ),
        pytest.param(
            VerificationRead,
            _verification(state="abandoned", outcome="infrastructure", last_dispatch_error="busy"),
            id="a-check-nobody-took",
        ),
    ],
)
def test_each_shape_round_trips_through_json_unchanged(
    model: type[BaseModel], body: dict[str, Any]
) -> None:
    """Parsed and re-serialized, a shape has to come back the same document. A
    field that drops on the way out is one the client never sees; one that
    appears is one nobody wrote."""
    parsed = model.model_validate(body)
    dumped = parsed.model_dump(mode="json")
    # Re-read what was written, not what was handed in: a timestamp comes back
    # in the serializer's own spelling, and a shape that lost a field on the way
    # out would parse back into a different object.
    assert model.model_validate(dumped) == parsed
    # Every value the caller stated has to come back out, compared against the
    # document that went IN rather than against a second parse of it — parsing
    # twice would agree with itself no matter what the shape did to the value.
    for key, stated in body.items():
        written = dumped[key]
        if isinstance(stated, str) and key.endswith(("_at", "_time")):
            assert datetime.fromisoformat(written) == datetime.fromisoformat(stated), key
        else:
            assert written == stated, key


@pytest.mark.parametrize(
    ("model", "field", "value"),
    [
        pytest.param(TeamConnectionRead, "badge", "healthy", id="admin-badge"),
        pytest.param(MemberTeamConnection, "badge", "healthy", id="member-badge"),
        pytest.param(TeamConnectionRead, "outcome", "failed", id="outcome"),
        pytest.param(TeamConnectionRead, "credential_state", "fine", id="credential-state"),
        pytest.param(TeamConnectionRead, "reauth", "call-support", id="reauth"),
        pytest.param(
            TeamConnectionRead, "verification_state", "validating", id="the-retired-status-word"
        ),
        pytest.param(VerificationRead, "state", "pending", id="verification-state"),
        pytest.param(VerificationRead, "outcome", "error!", id="verification-outcome"),
    ],
)
def test_a_status_outside_the_vocabulary_is_refused(
    model: type[BaseModel], field: str, value: str
) -> None:
    """The derived status is a closed set, shared by the backend, the daemon and
    both front ends. Admitted as a free string, a typo or a retired word (the
    "validating" these replaced) would reach a client that has no label for it
    and render as nothing at all."""
    body = _read() if model is TeamConnectionRead else _verification()
    if model is MemberTeamConnection:
        body = _member()
    with pytest.raises(ValidationError) as excinfo:
        model.model_validate({**body, field: value})
    assert field in str(excinfo.value)


def test_a_shape_the_server_left_out_reads_as_nothing_known_not_as_healthy() -> None:
    """The defaults are the answer to "an older writer said nothing about
    this". They have to be the cautious answer: a missing badge that defaulted
    to connected would show a green row for a connection nobody has ever
    checked."""
    minimal = TeamConnectionRead.model_validate(
        {
            "id": str(uuid4()),
            "team_id": str(uuid4()),
            "plugin": "postgres",
            "handle": "wh_main",
            "auth_mode": "shared",
            "created_at": NOW.isoformat(),
            "updated_at": NOW.isoformat(),
        }
    )
    assert minimal.badge is Badge.not_checked
    assert minimal.outcome is None
    assert minimal.last_verified_at is None
    assert minimal.verification_state is None
    assert minimal.credential_state is CredentialState.present
    assert minimal.reauth is None
    assert minimal.members is None, "a count of nobody is not the same as no count"


def test_the_member_counts_are_absent_on_a_row_where_the_question_does_not_arise() -> None:
    """A shared row queries as one stored credential, so "3 authorized" would be
    a number about nothing. None and {0, 0} are different answers and the plate
    shows different captions for them."""
    shared = TeamConnectionRead.model_validate(_read())
    per_user = TeamConnectionRead.model_validate(
        _read(auth_mode="per_user", members={"authorized": 0, "needs_reauth": 2})
    )
    assert shared.members is None
    assert per_user.members == MemberCredentialCounts(authorized=0, needs_reauth=2)
    assert per_user.model_dump(mode="json")["members"] == {"authorized": 0, "needs_reauth": 2}


def test_a_save_may_omit_the_check_it_is_then_refused_for() -> None:
    """The same shape asks for a verification and carries one, so it cannot
    demand the id of the verification it is asking for. Absence is admitted
    here and refused by the save, which can say why."""
    asking = TeamConnectionUpsertRequest(plugin="postgres", handle="wh_main")
    assert asking.verification_id is None
    carrying = TeamConnectionUpsertRequest(
        plugin="postgres", handle="wh_main", verification_id=uuid4()
    )
    assert carrying.verification_id is not None


def test_an_endpoints_verdict_speaks_the_same_vocabulary_as_the_whole_check() -> None:
    """One word for one thing: the per-endpoint map used to spell its verdict
    ``classification`` while the record spelled it ``outcome``, and a reader had
    to know which level it was looking at."""
    endpoint = VerificationEndpointOutcome.model_validate(
        {"outcome": "permission", "detail": "that role may not read the schema"}
    )
    assert endpoint.outcome is Outcome.permission
    assert endpoint.service_identity == ""
    with pytest.raises(ValidationError):
        VerificationEndpointOutcome.model_validate({"classification": "ok"})


def test_every_badge_and_every_outcome_is_accepted_by_the_shapes_that_carry_them() -> None:
    """The vocabularies and the wire cannot drift apart: a value the derivation
    can produce that the shape refuses is a 500 on a row somebody owns."""
    for badge in Badge:
        assert TeamConnectionRead.model_validate(_read(badge=badge.value)).badge is badge
        assert MemberTeamConnection.model_validate(_member(badge=badge.value)).badge is badge
    for outcome in Outcome:
        assert TeamConnectionRead.model_validate(_read(outcome=outcome.value)).outcome is outcome
        assert (
            VerificationRead.model_validate(
                _verification(state="settled", outcome=outcome.value)
            ).outcome
            is outcome
        )
    for state in VerificationState:
        assert VerificationRead.model_validate(_verification(state=state.value)).state is state
    for reauth in Reauth:
        assert TeamConnectionRead.model_validate(_read(reauth=reauth.value)).reauth is reauth
    for credential in CredentialState:
        assert (
            TeamConnectionRead.model_validate(
                _read(credential_state=credential.value)
            ).credential_state
            is credential
        )


# --------------------------------------------------------------------------- #
# What a shape says when the server said nothing
# --------------------------------------------------------------------------- #

#: Every field of the admin read shape that the server may leave unstated, and
#: the literal value a client is then entitled to read. Written out rather than
#: derived from the model, so flipping a default in the schema fails here.
READ_DEFAULTS: dict[str, Any] = {
    "owner_user_id": None,
    "created_by_id": None,
    "created_by_name": "",
    "can_manage": False,
    "shared_values": {},
    "auth_method": "",
    "member_fields": [],
    "values_doc": [],
    "ask_groups": [],
    "shared_consent": None,
    "auto_add": False,
    "enabled": True,
    "has_shared_secret": False,
    "has_primary_secret": False,
    "credential_version": 0,
    "oauth_client_id": None,
    "has_oauth_client_secret": False,
    "oauth_config": None,
    "badge": "not_checked",
    "badge_reason": "",
    "outcome": None,
    "last_detail": "",
    "last_verified_at": None,
    "verification_state": None,
    "credential_state": "present",
    "reauth": None,
    "members": None,
}

#: The same for the member sync-down shape.
MEMBER_DEFAULTS: dict[str, Any] = {
    "owner_user_id": None,
    "created_by_id": None,
    "created_by_name": "",
    "can_manage": False,
    "shared_values": {},
    "auth_method": "",
    "member_fields": [],
    "values_doc": [],
    "ask_groups": [],
    "auto_add": False,
    "enabled": True,
    "has_shared_secret": False,
    "has_primary_secret": False,
    "credential_version": 0,
    "shared_custody": "",
    "oauth_client_id": None,
    "badge": "not_checked",
    "badge_reason": "",
    "outcome": None,
    "last_verified_at": None,
    "credential_state": "present",
    "reauth": None,
    "lease_refusal": "",
}

MINIMUM_READ: dict[str, Any] = {
    "id": str(uuid4()),
    "team_id": str(uuid4()),
    "team_name": "Acme",
    "plugin": "postgres",
    "handle": "wh_main",
    "auth_mode": "shared",
    "created_at": NOW.isoformat(),
    "updated_at": NOW.isoformat(),
}


@pytest.mark.parametrize(
    ("model", "stated", "defaults"),
    [
        pytest.param(TeamConnectionRead, MINIMUM_READ, READ_DEFAULTS, id="admin-read"),
        pytest.param(
            MemberTeamConnection,
            {k: v for k, v in MINIMUM_READ.items() if k != "created_at"},
            MEMBER_DEFAULTS,
            id="member-sync-down",
        ),
    ],
)
def test_a_shape_the_server_left_unstated_defaults_to_a_named_document(
    model: type[BaseModel], stated: dict[str, Any], defaults: dict[str, Any]
) -> None:
    """The half of the document nobody wrote, spelled out.

    Read against a literal table rather than against the model, so a default
    that changes meaning — ``can_manage`` above all, which is the flag every
    portal surface gates edit, rotate and remove on — cannot change quietly.
    """
    dumped = model.model_validate(stated).model_dump(mode="json")
    assert {key: dumped[key] for key in defaults} == defaults
    # The table is the whole unstated half: nothing was left out of it.
    assert set(dumped) == set(stated) | set(defaults)


@pytest.mark.parametrize(
    "model", [TeamConnectionRead, MemberTeamConnection], ids=["admin-read", "member-sync-down"]
)
def test_the_manage_flag_is_the_servers_to_state_and_is_denied_until_it_does(
    model: type[BaseModel],
) -> None:
    """A row arrives unmanageable and is stamped otherwise only deliberately.

    The asymmetry is the point: the absence of an answer has to read as "no",
    because the portal shows edit / rotate / remove on the strength of this one
    boolean, and a row nobody vouched for is not one to hand those to.
    """
    body = dict(MINIMUM_READ)
    if model is MemberTeamConnection:
        body.pop("created_at")
    assert model.model_validate(body).can_manage is False
    assert model.model_validate({**body, "can_manage": True}).can_manage is True
    assert model.model_validate({**body, "can_manage": False}).can_manage is False
