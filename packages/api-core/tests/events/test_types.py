"""The event vocabulary, the visibility grammar and the actor document builders.

Pure: no database. The one cross-module contract pinned here is that the
authorization layer's decision event type is a member of this registry, and
that every actor builder returns exactly the shape ``ActingContext.audit_dict``
persists.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from alkera_core.authz import ActingContext, ActorChainRecord, PrincipalRecord
from alkera_core.authz.decision import AUTHZ_EVENT_TYPE, is_server_only_event_type
from alkera_core.events import (
    REALTIME_EVENT_TYPE_VALUES,
    SERVER_ONLY_EVENT_TYPES,
    USER_VISIBILITY_PATTERN,
    VISIBILITY_ORG,
    VISIBILITY_PLATFORM,
    Entity,
    EventType,
    RealtimeEventType,
    actor_for_ci_token,
    actor_for_user,
    actor_system,
    coerce_event_type,
    is_realtime_event_type,
    user_id_of_visibility,
    user_visibility,
    validate_visibility,
)
from alkera_core.models import CiToken, User

_UUID = UUID("0f2b6c1e-6d1a-4a6d-9f1e-2c3b4a5d6e7f")

# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

EXPECTED_EVENT_TYPES = {
    "gate_run.ingested",
    "gate_lease.changed",
    "team_connection.updated",
    "team_connection.probed",
    "connection.verification_changed",
    "connection.credential_changed",
    "billing.summary_changed",
    "org_billing.changed",
    "kb_item.changed",
    "user.email_verified",
    "membership.changed",
    "invitation.changed",
    "chat.updated",
    "artifact.updated",
    "compute_machine.changed",
    "org_machine.changed",
    "workspace.machine_move",
    "workspace_object.changed",
    "doc.op",
    "authz.decision",
    "file_node.changed",
    "file_operation.changed",
    "file_lease.changed",
    "notebook.event",
}

EXPECTED_ENTITIES = {
    "gate_run",
    "gate_lease",
    "team_connection",
    "connection_verification",
    "billing_account",
    "org_billing",
    "kb_item",
    "user",
    "membership",
    "invitation",
    "chat",
    "artifact",
    "compute_machine",
    "org_machine",
    "workspace_object",
    "doc",
    "authz",
    "notebook",
}


def test_event_type_vocabulary_is_exactly_the_expected_wire_names() -> None:
    # The count is derived from the set above rather than written twice, so
    # adding a type is one edit and cannot leave the two disagreeing.
    assert {member.value for member in EventType} == EXPECTED_EVENT_TYPES
    assert len(EventType) == len(EXPECTED_EVENT_TYPES)


def test_entity_vocabulary_is_pinned() -> None:
    assert {member.value for member in Entity} == EXPECTED_ENTITIES


def test_the_authz_decision_type_is_a_registry_member() -> None:
    """The authorization layer spells its event type as a literal; the registry
    must carry the same value or a decision row would name an unknown type."""
    assert AUTHZ_EVENT_TYPE == EventType.AUTHZ_DECISION.value
    assert EventType(AUTHZ_EVENT_TYPE) is EventType.AUTHZ_DECISION


@pytest.mark.parametrize("member", list(EventType), ids=lambda m: m.value)
def test_only_the_authz_decision_is_server_only(member: EventType) -> None:
    assert is_server_only_event_type(member.value) is (member is EventType.AUTHZ_DECISION)


# ---------------------------------------------------------------------------
# The client-deliverable subset
# ---------------------------------------------------------------------------


def test_realtime_event_types_are_the_registry_minus_the_server_only_members() -> None:
    """The event stream's vocabulary is derived from the registry by exclusion,
    so a new registry member reaches clients unless it is deliberately named
    server-only — and this test then fails until the enum is updated too."""
    assert SERVER_ONLY_EVENT_TYPES == {
        EventType.DOC_OP,
        EventType.AUTHZ_DECISION,
        EventType.NOTEBOOK_EVENT,
    }
    expected = {m.value for m in EventType} - {m.value for m in SERVER_ONLY_EVENT_TYPES}
    assert {m.value for m in RealtimeEventType} == expected
    assert REALTIME_EVENT_TYPE_VALUES == expected
    assert len(RealtimeEventType) == len(expected)


@pytest.mark.parametrize("member", list(RealtimeEventType), ids=lambda m: m.value)
def test_every_realtime_member_spells_its_registry_twin_exactly(
    member: RealtimeEventType,
) -> None:
    assert EventType(member.value).name == member.name
    assert is_realtime_event_type(member.value) is True


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("doc.op", id="doc-op"),
        pytest.param("authz.decision", id="authz-decision"),
        pytest.param("kb_item.deleted", id="unknown"),
        pytest.param("", id="empty"),
        pytest.param("KB_ITEM.CHANGED", id="wrong-case"),
    ],
)
def test_is_realtime_event_type_refuses_server_only_and_unknown_values(value: str) -> None:
    assert is_realtime_event_type(value) is False
    with pytest.raises(ValueError):
        RealtimeEventType(value)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        pytest.param(EventType.KB_ITEM_CHANGED, EventType.KB_ITEM_CHANGED, id="member"),
        pytest.param("kb_item.changed", EventType.KB_ITEM_CHANGED, id="wire-value"),
        pytest.param("authz.decision", EventType.AUTHZ_DECISION, id="authz-wire-value"),
    ],
)
def test_coerce_event_type_accepts_members_and_wire_values(
    value: EventType | str, expected: EventType
) -> None:
    assert coerce_event_type(value) is expected


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("", id="empty"),
        pytest.param("kb_item.deleted", id="unknown"),
        pytest.param("KB_ITEM_CHANGED", id="python-name-not-value"),
        pytest.param("KB_ITEM.CHANGED", id="wrong-case"),
        pytest.param("kb_item.changed ", id="trailing-space"),
        pytest.param("authz.", id="prefix-only"),
    ],
)
def test_coerce_event_type_rejects_anything_outside_the_registry(value: str) -> None:
    with pytest.raises(ValueError, match="unknown event type"):
        coerce_event_type(value)


# ---------------------------------------------------------------------------
# Visibility grammar
# ---------------------------------------------------------------------------


def test_user_visibility_round_trips() -> None:
    user_id = uuid4()
    value = user_visibility(user_id)
    assert value == f"user:{user_id}"
    assert validate_visibility(value) == value
    assert user_id_of_visibility(value) == user_id


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(VISIBILITY_ORG, id="org"),
        pytest.param(VISIBILITY_PLATFORM, id="platform"),
        pytest.param(f"user:{_UUID}", id="user"),
    ],
)
def test_validate_visibility_accepts_the_three_audiences(value: str) -> None:
    assert validate_visibility(value) == value


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("", id="empty"),
        pytest.param("team:x", id="team-is-not-an-audience"),
        pytest.param("user:not-a-uuid", id="user-non-uuid"),
        pytest.param(f"USER:{_UUID}", id="user-prefix-uppercase"),
        pytest.param(f"user:{str(_UUID).upper()}", id="user-uuid-uppercase"),
        pytest.param(f"user:{_UUID}x", id="user-trailing-junk"),
        pytest.param(f"users:{_UUID}", id="user-plural-prefix"),
        pytest.param("user:", id="user-empty-id"),
        pytest.param(f"user:{str(_UUID).replace('-', '')}", id="user-uuid-without-hyphens"),
        pytest.param(" org", id="org-leading-space"),
        pytest.param("Org", id="org-capitalised"),
        pytest.param("platform ", id="platform-trailing-space"),
        pytest.param("all", id="all-is-not-an-audience"),
    ],
)
def test_validate_visibility_rejects_everything_else(value: str) -> None:
    with pytest.raises(ValueError, match="invalid visibility"):
        validate_visibility(value)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        pytest.param(VISIBILITY_ORG, None, id="org"),
        pytest.param(VISIBILITY_PLATFORM, None, id="platform"),
        pytest.param(f"USER:{_UUID}", None, id="wrong-case-is-not-a-user"),
        pytest.param(f"user:{_UUID}", _UUID, id="user"),
    ],
)
def test_user_id_of_visibility(value: str, expected: UUID | None) -> None:
    assert user_id_of_visibility(value) == expected


def test_the_sql_pattern_is_the_python_pattern() -> None:
    """The table's CHECK constraint repeats the grammar; the model module cannot
    import this package (cycle), so the equality is pinned here instead."""
    from alkera_core.models.event_outbox import EventOutbox

    check = next(
        c for c in EventOutbox.__table__.constraints if c.name == "ck_event_outbox_visibility"
    )
    assert USER_VISIBILITY_PATTERN in str(check.sqltext)


# ---------------------------------------------------------------------------
# Actor documents
# ---------------------------------------------------------------------------

_TOP_LEVEL_KEYS = {
    "schema_version",
    "metadata",
    "acting",
    "delegating_user",
    "chain",
    "agent_id_asserted_by",
}
_LINK_KEYS = {"schema_version", "metadata", "kind", "id", "org_id", "label", "credential"}


def _user() -> User:
    return User(
        id=uuid4(),
        home_org_team_id=uuid4(),
        email="someone@example.com",
        first_name="Some",
        last_name="One",
    )


def _ci_token(label: str | None = "deploy-bot") -> CiToken:
    return CiToken(id=uuid4(), org_team_id=uuid4(), token_hash="a" * 64, label=label)


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(lambda: actor_for_user(_user(), org_id=uuid4()), id="user"),
        pytest.param(lambda: actor_for_ci_token(_ci_token()), id="ci-token"),
        pytest.param(lambda: actor_system("worker:connections.verification"), id="system"),
    ],
)
def test_every_builder_returns_an_actor_chain_record_document(build) -> None:
    document = build()
    assert set(document) == _TOP_LEVEL_KEYS
    assert set(document["acting"]) == _LINK_KEYS
    assert document["delegating_user"] is None
    assert document["agent_id_asserted_by"] is None
    # A principal acting alone is its own one-link chain.
    assert document["chain"] == [document["acting"]]
    # The document is exactly what the record re-emits: no lossy field, no extra.
    assert ActorChainRecord.model_validate(document).model_dump(mode="json") == document


def test_actor_for_user_carries_ids_only() -> None:
    user = _user()
    document = actor_for_user(user, org_id=user.home_org_team_id)
    acting = document["acting"]
    assert acting == {
        "schema_version": PrincipalRecord.SCHEMA_VERSION,
        "metadata": {},
        "kind": "user",
        "id": str(user.id),
        "org_id": str(user.home_org_team_id),
        "label": "",
        "credential": None,
    }
    assert "@" not in str(document)


def test_actor_for_user_names_the_org_it_is_given_never_the_home_org() -> None:
    """A person in two orgs acting in their second org is recorded in that
    org: the row lands in that org's stream, and the id of the other org the
    person belongs to must not ride along. There is no home-org default to
    fall back on."""
    user = _user()
    other_org = uuid4()
    acting = actor_for_user(user, org_id=other_org)["acting"]
    assert acting["org_id"] == str(other_org)
    assert str(user.home_org_team_id) not in str(actor_for_user(user, org_id=other_org))
    with pytest.raises(TypeError):
        actor_for_user(user)


def test_actor_for_ci_token_is_a_service_with_its_operator_label() -> None:
    token = _ci_token("deploy-bot")
    acting = actor_for_ci_token(token)["acting"]
    assert acting["kind"] == "service"
    assert acting["id"] == str(token.id)
    assert acting["org_id"] == str(token.org_team_id)
    assert acting["label"] == "deploy-bot"
    assert acting["credential"] == "ci_token"


def test_actor_for_ci_token_without_a_label_records_an_empty_label() -> None:
    assert actor_for_ci_token(_ci_token(None))["acting"]["label"] == ""


def test_actor_system_names_the_component_as_id_and_label() -> None:
    acting = actor_system("stripe:invoice.paid")["acting"]
    assert acting == {
        "schema_version": PrincipalRecord.SCHEMA_VERSION,
        "metadata": {},
        "kind": "service",
        "id": "stripe:invoice.paid",
        "org_id": "",
        "label": "stripe:invoice.paid",
        "credential": None,
    }


@pytest.mark.parametrize("label", ["", "   "], ids=["empty", "blank"])
def test_actor_system_refuses_a_nameless_component(label: str) -> None:
    with pytest.raises(ValueError, match="non-empty label"):
        actor_system(label)


def test_actor_for_user_has_the_shape_of_an_acting_context_audit_dict() -> None:
    """A producer that later swaps the builder for ``ctx.audit_dict()`` must not
    change the persisted shape: same keys at the top level and on every link,
    same one-link chain. Only the label and credential differ (the context
    knows how the user authenticated; the builder deliberately records neither)."""
    user = _user()
    from_builder = actor_for_user(user, org_id=user.home_org_team_id)
    from_context = ActingContext.for_user(
        user_id=user.id, org_id=user.home_org_team_id, email=user.email
    ).audit_dict()
    assert set(from_builder) == set(from_context)
    assert set(from_builder["acting"]) == set(from_context["acting"])
    assert len(from_builder["chain"]) == len(from_context["chain"]) == 1
    for key in ("schema_version", "kind", "id", "org_id"):
        assert from_builder["acting"][key] == from_context["acting"][key]
