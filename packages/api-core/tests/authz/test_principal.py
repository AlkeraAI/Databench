"""Who acts, for whom, and the persisted record of it."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from typing import Any
from uuid import UUID, uuid4

import pytest
from alkera_core.authz import (
    AGENT_ID_ASSERTED_BY_CLIENT,
    ActingContext,
    ActorChainRecord,
    CredentialKind,
    Principal,
    PrincipalKind,
)

ORG = UUID("00000000-0000-4000-8000-00000000000a")
OTHER_ORG = UUID("00000000-0000-4000-8000-00000000000f")
USER_ID = UUID("00000000-0000-4000-8000-000000000001")
TOKEN_ID = UUID("00000000-0000-4000-8000-000000000002")
EMAIL = "member@example.com"
SESSION = "sess-42"


def _user(org: UUID = ORG, credential: CredentialKind | None = CredentialKind.JWT) -> Principal:
    return Principal(PrincipalKind.USER, str(USER_ID), org, label=EMAIL, credential=credential)


def _agent(org: UUID = ORG) -> Principal:
    return Principal(
        PrincipalKind.AGENT, SESSION, org, label=SESSION, credential=CredentialKind.AGENT_HEADER
    )


def _service(org: UUID = ORG) -> Principal:
    return Principal(
        PrincipalKind.SERVICE, str(TOKEN_ID), org, label="ci", credential=CredentialKind.CI_TOKEN
    )


def _pat(org: UUID = ORG) -> Principal:
    return Principal(
        PrincipalKind.PAT, str(TOKEN_ID), org, label="pat", credential=CredentialKind.PAT
    )


# ---------------------------------------------------------------------------
# Principal
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        pytest.param({"kind": "user", "id": str(USER_ID), "org_id": ORG}, id="kind-not-an-enum"),
        pytest.param({"kind": PrincipalKind.USER, "id": "", "org_id": ORG}, id="empty-id"),
        pytest.param({"kind": PrincipalKind.AGENT, "id": "", "org_id": ORG}, id="empty-agent-id"),
        pytest.param(
            {"kind": PrincipalKind.USER, "id": "not-a-uuid", "org_id": ORG}, id="user-id-not-uuid"
        ),
    ],
)
def test_principal_refuses_malformed_links(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        Principal(**kwargs)


def test_agent_id_need_not_be_a_uuid() -> None:
    assert Principal(PrincipalKind.AGENT, "sess-1", ORG).id == "sess-1"


def test_principal_defaults_and_immutability() -> None:
    principal = Principal(PrincipalKind.SERVICE, str(TOKEN_ID), ORG)
    assert principal.label == ""
    assert principal.credential is None
    with pytest.raises(FrozenInstanceError):
        principal.label = "x"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Constructors — one per credential shape
# ---------------------------------------------------------------------------


def test_for_user_acts_alone() -> None:
    ctx = ActingContext.for_user(user_id=USER_ID, org_id=ORG, email=EMAIL)
    assert ctx.acting_principal == _user()
    assert ctx.delegating_user is None
    assert ctx.delegation_chain == (_user(),)
    assert ctx.subject == ctx.acting_principal
    assert ctx.effective_user_id == USER_ID
    assert ctx.org_id == ORG
    assert ctx.is_agent is False
    assert ctx.acting_principal.credential is CredentialKind.JWT


def test_for_user_carries_an_explicit_credential() -> None:
    ctx = ActingContext.for_user(
        user_id=USER_ID, org_id=ORG, email=EMAIL, credential=CredentialKind.PAT
    )
    assert ctx.acting_principal.credential is CredentialKind.PAT


def test_for_agent_is_the_user_then_the_agent() -> None:
    ctx = ActingContext.for_agent(user_id=USER_ID, org_id=ORG, email=EMAIL, session_id=SESSION)
    assert ctx.acting_principal == _agent()
    assert ctx.delegating_user == _user()
    assert ctx.delegation_chain == (_user(), _agent())
    assert [link.kind for link in ctx.delegation_chain] == [PrincipalKind.USER, PrincipalKind.AGENT]
    assert ctx.subject == _user(), "the user's roles apply, not the agent's"
    assert ctx.effective_user_id == USER_ID
    assert ctx.is_agent is True
    assert ctx.acting_principal.id == SESSION
    assert ctx.acting_principal.label == SESSION
    assert ctx.acting_principal.credential is CredentialKind.AGENT_HEADER


def test_for_agent_refuses_a_malformed_session_id() -> None:
    with pytest.raises(ValueError):
        ActingContext.for_agent(user_id=USER_ID, org_id=ORG, email=EMAIL, session_id="")


@pytest.mark.parametrize(
    "credential", [CredentialKind.CI_TOKEN, CredentialKind.PROXY_TOKEN], ids=["ci", "proxy"]
)
def test_for_service_has_no_human_behind_it(credential: CredentialKind) -> None:
    ctx = ActingContext.for_service(
        token_id=TOKEN_ID, org_id=ORG, label="ci: main", credential=credential
    )
    assert ctx.acting_principal.kind is PrincipalKind.SERVICE
    assert ctx.acting_principal.id == str(TOKEN_ID)
    assert ctx.acting_principal.label == "ci: main"
    assert ctx.acting_principal.credential is credential
    assert ctx.delegating_user is None
    assert ctx.delegation_chain == (ctx.acting_principal,)
    assert ctx.subject == ctx.acting_principal
    assert ctx.effective_user_id is None
    assert ctx.is_agent is False


def test_for_pat_is_the_owner_then_the_token() -> None:
    ctx = ActingContext.for_pat(
        token_id=TOKEN_ID, org_id=ORG, label="pat: nightly", user_id=USER_ID, email=EMAIL
    )
    assert [link.kind for link in ctx.delegation_chain] == [PrincipalKind.USER, PrincipalKind.PAT]
    assert ctx.acting_principal.kind is PrincipalKind.PAT
    assert ctx.acting_principal.id == str(TOKEN_ID)
    assert ctx.acting_principal.label == "pat: nightly"
    assert ctx.acting_principal.credential is CredentialKind.PAT
    assert ctx.delegating_user == _user(credential=None), "the owner presented no credential"
    assert ctx.subject == ctx.delegating_user
    assert ctx.effective_user_id == USER_ID
    assert ctx.is_agent is False


# ---------------------------------------------------------------------------
# Invariants
# ---------------------------------------------------------------------------


def test_chain_defaults_to_the_acting_principal() -> None:
    ctx = ActingContext(acting_principal=_service())
    assert ctx.delegation_chain == (_service(),)


def test_chain_is_normalised_to_a_tuple() -> None:
    ctx = ActingContext(
        acting_principal=_agent(),
        delegating_user=_user(),
        delegation_chain=[_user(), _agent()],  # type: ignore[arg-type]
    )
    assert isinstance(ctx.delegation_chain, tuple)
    assert ctx.delegation_chain == (_user(), _agent())


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        pytest.param(
            {
                "acting_principal": _agent(),
                "delegating_user": _user(),
                "delegation_chain": (_agent(), _user()),
            },
            "must end with the acting principal",
            id="chain-tail-is-not-acting",
        ),
        pytest.param(
            {
                "acting_principal": _agent(),
                "delegating_user": _user(),
                "delegation_chain": (_agent(),),
            },
            "must start with the delegating user",
            id="delegating-user-not-first",
        ),
        pytest.param(
            {
                "acting_principal": _agent(),
                "delegating_user": _service(),
                "delegation_chain": (_service(), _agent()),
            },
            "only a user can delegate",
            id="delegating-kind-not-user",
        ),
        pytest.param(
            {
                "acting_principal": _agent(),
                "delegating_user": _user(),
                "delegation_chain": (_user(OTHER_ORG), _agent()),
            },
            "same org",
            id="mixed-org-ids-in-chain",
        ),
        pytest.param(
            {
                "acting_principal": _agent(),
                "delegating_user": _user(OTHER_ORG),
                "delegation_chain": (_user(OTHER_ORG), _agent()),
            },
            "same org",
            id="delegating-user-in-another-org",
        ),
        pytest.param(
            {"acting_principal": _user(), "delegating_user": _user()},
            "no delegating user",
            id="user-acting-with-a-delegating-user",
        ),
        pytest.param(
            {"acting_principal": _user(), "delegation_chain": (_service(), _user())},
            "single-link chain",
            id="user-acting-with-a-multi-link-chain",
        ),
        pytest.param(
            {"acting_principal": _agent(), "delegation_chain": (_user(), _agent())},
            "must name that user as the delegating user",
            id="user-in-chain-but-no-delegating-user",
        ),
        pytest.param(
            {
                "acting_principal": _agent(),
                "delegating_user": _user(),
                "delegation_chain": (
                    _user(),
                    Principal(PrincipalKind.USER, str(uuid4()), ORG, label="other@example.com"),
                    _agent(),
                ),
            },
            "exactly one user",
            id="two-users-in-chain",
        ),
    ],
)
def test_invariant_violations_cannot_be_constructed(kwargs: dict[str, Any], match: str) -> None:
    with pytest.raises(ValueError, match=match):
        ActingContext(**kwargs)


def test_service_chain_may_hold_several_service_links() -> None:
    """A chain with no human in it is still valid when every link is a
    service in the same org (a future service-to-service hop)."""
    upstream = Principal(PrincipalKind.SERVICE, str(uuid4()), ORG, label="upstream")
    ctx = ActingContext(acting_principal=_service(), delegation_chain=(upstream, _service()))
    assert ctx.delegating_user is None
    assert ctx.effective_user_id is None


def test_acting_context_is_immutable() -> None:
    ctx = ActingContext.for_user(user_id=USER_ID, org_id=ORG, email=EMAIL)
    with pytest.raises(FrozenInstanceError):
        ctx.delegating_user = _user()  # type: ignore[misc]


# ---------------------------------------------------------------------------
# audit_dict — the persisted actor document
# ---------------------------------------------------------------------------

SHAPES: dict[str, ActingContext] = {
    "user": ActingContext.for_user(user_id=USER_ID, org_id=ORG, email=EMAIL),
    "agent": ActingContext.for_agent(user_id=USER_ID, org_id=ORG, email=EMAIL, session_id=SESSION),
    "service_ci": ActingContext.for_service(
        token_id=TOKEN_ID, org_id=ORG, label="ci", credential=CredentialKind.CI_TOKEN
    ),
    "service_proxy": ActingContext.for_service(
        token_id=TOKEN_ID, org_id=ORG, label="proxy", credential=CredentialKind.PROXY_TOKEN
    ),
    "pat": ActingContext.for_pat(
        token_id=TOKEN_ID, org_id=ORG, label="pat", user_id=USER_ID, email=EMAIL
    ),
}

EXPECTED_CHAIN_KINDS: dict[str, list[str]] = {
    "user": ["user"],
    "agent": ["user", "agent"],
    "service_ci": ["service"],
    "service_proxy": ["service"],
    "pat": ["user", "pat"],
}

EXPECTED_ACTING_CREDENTIAL: dict[str, str] = {
    "user": "jwt",
    "agent": "agent_header",
    "service_ci": "ci_token",
    "service_proxy": "proxy_token",
    "pat": "pat",
}


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_audit_dict_round_trips_through_the_record(shape: str) -> None:
    ctx = SHAPES[shape]
    doc = ctx.audit_dict()
    record = ActorChainRecord.model_validate(doc)
    assert record == ActorChainRecord.from_context(ctx)
    assert record.model_dump(mode="json") == doc


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_audit_dict_records_the_chain_as_strings(shape: str) -> None:
    doc = SHAPES[shape].audit_dict()
    assert [link["kind"] for link in doc["chain"]] == EXPECTED_CHAIN_KINDS[shape]
    assert doc["acting"] == doc["chain"][-1]
    assert doc["acting"]["credential"] == EXPECTED_ACTING_CREDENTIAL[shape]
    assert doc["schema_version"] == ActorChainRecord.SCHEMA_VERSION
    for link in doc["chain"]:
        assert isinstance(link["id"], str)
        assert link["org_id"] == str(ORG)
        assert isinstance(link["kind"], str)
        assert link["credential"] is None or isinstance(link["credential"], str)


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_audit_dict_names_the_delegating_user_only_when_there_is_one(shape: str) -> None:
    ctx = SHAPES[shape]
    doc = ctx.audit_dict()
    if ctx.delegating_user is None:
        assert doc["delegating_user"] is None
    else:
        assert doc["delegating_user"] == doc["chain"][0]
        assert doc["delegating_user"]["kind"] == "user"
        assert doc["delegating_user"]["id"] == str(USER_ID)
        assert doc["delegating_user"]["label"] == EMAIL


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_only_an_agent_acted_row_is_marked_client_asserted(shape: str) -> None:
    doc = SHAPES[shape].audit_dict()
    if shape == "agent":
        assert doc["agent_id_asserted_by"] == AGENT_ID_ASSERTED_BY_CLIENT == "client"
    else:
        assert doc["agent_id_asserted_by"] is None


def test_audit_dict_top_level_keys_are_the_persisted_contract() -> None:
    doc = SHAPES["agent"].audit_dict()
    assert set(doc) == {
        "acting",
        "delegating_user",
        "chain",
        "agent_id_asserted_by",
        "schema_version",
        "metadata",
    }
    assert set(doc["acting"]) == {
        "kind",
        "id",
        "org_id",
        "label",
        "credential",
        "schema_version",
        "metadata",
    }


def test_pat_owner_link_has_no_credential_in_the_record() -> None:
    doc = SHAPES["pat"].audit_dict()
    assert doc["chain"][0]["credential"] is None
    assert doc["chain"][1]["credential"] == "pat"
