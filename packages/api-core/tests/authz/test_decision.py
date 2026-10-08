"""Decisions, the redaction rules, and the record a decision leaves behind."""

from __future__ import annotations

import json
import math
from dataclasses import FrozenInstanceError
from typing import Any
from uuid import UUID

import pytest
from alkera_core.authz import (
    AUTHZ_EVENT_TYPE,
    NEVER_AUDITED_KEYS,
    SERVER_ONLY_EVENT_PREFIXES,
    Action,
    Decision,
    DecisionEvent,
    Effect,
    Resource,
    ResourceType,
    Role,
    allow,
    audited_attrs,
    deny,
    is_server_only_event_type,
)
from alkera_core.authz.decision import MAX_AUDITED_STRING

ORG = UUID("00000000-0000-4000-8000-00000000000a")
TEAM = UUID("00000000-0000-4000-8000-00000000000b")


# ---------------------------------------------------------------------------
# Decision / allow / deny
# ---------------------------------------------------------------------------


def test_allow_is_a_bare_allow() -> None:
    decision = allow("kb.promote_scope", "share_subtree")
    assert decision.effect is Effect.ALLOW
    assert decision.allowed is True
    assert decision.policy == "kb.promote_scope"
    assert decision.reason == "share_subtree"
    assert decision.as_not_found is False
    assert decision.message == ""
    assert decision.error_code is None


def test_deny_carries_the_http_hints() -> None:
    decision = deny(
        "billing.team_pool_cap",
        "email_verification_required",
        message="Verify your email address to perform this action.",
        error_code="email_verification_required",
    )
    assert decision.effect is Effect.DENY
    assert decision.allowed is False
    assert decision.message == "Verify your email address to perform this action."
    assert decision.error_code == "email_verification_required"
    assert decision.as_not_found is False


def test_deny_as_not_found_defaults() -> None:
    decision = deny("connector.credential", "not_entitled", message="Connection not found")
    assert decision.as_not_found is False
    opaque = deny(
        "connector.credential", "not_entitled", message="Connection not found", as_not_found=True
    )
    assert opaque.as_not_found is True
    assert opaque.error_code is None


@pytest.mark.parametrize(
    "kwargs",
    [
        pytest.param({"effect": "allow", "reason": "r", "policy": "p"}, id="effect-not-an-enum"),
        pytest.param({"effect": Effect.DENY, "reason": "", "policy": "p"}, id="empty-reason"),
        pytest.param({"effect": Effect.DENY, "reason": "r", "policy": ""}, id="empty-policy"),
        pytest.param(
            {"effect": Effect.ALLOW, "reason": "r", "policy": "p", "as_not_found": True},
            id="allow-with-not-found",
        ),
        pytest.param(
            {"effect": Effect.ALLOW, "reason": "r", "policy": "p", "error_code": "x"},
            id="allow-with-error-code",
        ),
        pytest.param(
            {"effect": Effect.ALLOW, "reason": "r", "policy": "p", "unauthenticated": True},
            id="allow-with-401",
        ),
        pytest.param(
            {
                "effect": Effect.DENY,
                "reason": "r",
                "policy": "p",
                "as_not_found": True,
                "unauthenticated": True,
            },
            id="both-404-and-401",
        ),
    ],
)
def test_decision_refuses_inconsistent_construction(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        Decision(**kwargs)


def test_decision_is_immutable() -> None:
    decision = allow("p", "r")
    with pytest.raises(FrozenInstanceError):
        decision.effect = Effect.DENY  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Server-only event types
# ---------------------------------------------------------------------------


def test_authz_event_type_is_the_decision_row_type() -> None:
    assert AUTHZ_EVENT_TYPE == "authz.decision"
    assert SERVER_ONLY_EVENT_PREFIXES == ("authz.",)
    assert is_server_only_event_type(AUTHZ_EVENT_TYPE) is True


@pytest.mark.parametrize(
    ("event_type", "expected"),
    [
        pytest.param("authz.decision", True, id="decision"),
        pytest.param("authz.", True, id="bare-prefix"),
        pytest.param("authz.anything", True, id="future-authz-type"),
        pytest.param("agent.x", False, id="agent"),
        pytest.param("doc.op", False, id="doc-op"),
        pytest.param("", False, id="empty"),
        pytest.param("authz", False, id="no-dot"),
        pytest.param("xauthz.decision", False, id="not-a-prefix"),
        pytest.param("AUTHZ.decision", False, id="case-differs"),
    ],
)
def test_is_server_only_event_type(event_type: str, expected: bool) -> None:
    assert is_server_only_event_type(event_type) is expected


# ---------------------------------------------------------------------------
# audited_attrs — the allowlist + coercion + redaction rules
# ---------------------------------------------------------------------------


def test_never_audited_keys_are_pinned() -> None:
    """A security contract: the names that can never reach a decision row."""
    assert NEVER_AUDITED_KEYS == frozenset(
        {
            "secret",
            "password",
            "token",
            "api_key",
            "access_token",
            "refresh_token",
            "shared_secret",
            "client_secret",
            "body",
            "payload",
            "content",
            "sql",
            "query",
        }
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        pytest.param(True, True, id="bool-true"),
        pytest.param(False, False, id="bool-false"),
        pytest.param(7, 7, id="int"),
        pytest.param(0, 0, id="int-zero"),
        pytest.param(1.5, 1.5, id="float"),
        pytest.param("shared", "shared", id="str"),
        pytest.param("", "", id="empty-str"),
        pytest.param("x" * 300, "x" * MAX_AUDITED_STRING, id="str-cut-at-128"),
        pytest.param("x" * MAX_AUDITED_STRING, "x" * MAX_AUDITED_STRING, id="str-exactly-128"),
        pytest.param(Role.ADMIN, "admin", id="strenum-to-value"),
        pytest.param(Action.PROMOTE, "promote", id="strenum-action"),
        pytest.param(TEAM, str(TEAM), id="uuid-to-str"),
        pytest.param(
            frozenset({Role.OWNER, Role.ADMIN, Role.MEMBER}),
            ["admin", "member", "owner"],
            id="frozenset-of-roles-sorted",
        ),
        pytest.param({Role.VIEWER, Role.AGENT}, ["agent", "viewer"], id="set-of-roles"),
        pytest.param([Role.MEMBER, Role.ADMIN], ["admin", "member"], id="list-of-roles"),
        pytest.param(("b", "a"), ["a", "b"], id="tuple-of-str"),
        pytest.param([TEAM, ORG], sorted([str(TEAM), str(ORG)]), id="list-of-uuids"),
        pytest.param(frozenset(), [], id="empty-collection"),
    ],
)
def test_audited_attrs_coerces_to_audit_safe_scalars(value: object, expected: object) -> None:
    assert audited_attrs({"k": value}, frozenset({"k"})) == {"k": expected}


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(None, id="none"),
        pytest.param({"nested": 1}, id="dict"),
        pytest.param(b"bytes", id="bytes"),
        pytest.param(object(), id="arbitrary-object"),
        pytest.param(math.nan, id="nan"),
        pytest.param(math.inf, id="inf"),
        pytest.param(["a", 1], id="mixed-collection"),
        pytest.param([1, 2], id="list-of-ints"),
        pytest.param([["a"]], id="nested-list"),
        pytest.param([None], id="list-with-none"),
    ],
)
def test_audited_attrs_drops_what_it_cannot_coerce(value: object) -> None:
    assert audited_attrs({"k": value}, frozenset({"k"})) == {}


def test_audited_attrs_keeps_only_allowlisted_keys_that_are_present() -> None:
    attrs = {"enabled": True, "auth_mode": "shared", "plugin": "snowflake"}
    out = audited_attrs(attrs, frozenset({"enabled", "auth_mode", "team_id"}))
    assert out == {"enabled": True, "auth_mode": "shared"}
    assert "plugin" not in out
    assert "team_id" not in out


def test_audited_attrs_with_empty_allowlist_is_empty() -> None:
    assert audited_attrs({"enabled": True}, frozenset()) == {}


@pytest.mark.parametrize("key", sorted(NEVER_AUDITED_KEYS))
def test_audited_attrs_never_emits_a_forbidden_key_even_when_allowlisted(key: str) -> None:
    out = audited_attrs({key: "hunter2", "enabled": True}, frozenset({key, "enabled"}))
    assert out == {"enabled": True}


def test_audited_attrs_output_is_json_serialisable() -> None:
    out = audited_attrs(
        {"roles": frozenset({Role.ADMIN}), "team_id": TEAM, "n": 1, "f": 0.5, "s": "x"},
        frozenset({"roles", "team_id", "n", "f", "s"}),
    )
    assert json.loads(json.dumps(out)) == out


# ---------------------------------------------------------------------------
# DecisionEvent.outbox_payload
# ---------------------------------------------------------------------------


def _event(decision: Decision, *, team_id: UUID | None, attrs: dict[str, Any]) -> DecisionEvent:
    return DecisionEvent(
        org_id=ORG,
        actor={"acting": {"kind": "user"}},
        action=Action.FETCH_CREDENTIAL,
        resource=Resource(ResourceType.CONNECTOR, id="conn-1", org_id=ORG, team_id=team_id),
        decision=decision,
        attrs=attrs,
        method="GET",
        path="/api/v1/me/team-connections/conn-1/credential",
    )


def test_outbox_payload_shape_for_a_deny() -> None:
    event = _event(
        deny(
            "connector.credential",
            "not_entitled",
            message="Connection not found",
            as_not_found=True,
        ),
        team_id=TEAM,
        attrs={"member_entitled": False, "auth_mode": "shared"},
    )
    assert event.outbox_payload() == {
        "effect": "deny",
        "action": "fetch_credential",
        "reason": "not_entitled",
        "policy": "connector.credential",
        "as_not_found": True,
        "error_code": None,
        "resource": {"type": "connector", "id": "conn-1", "team_id": str(TEAM)},
        "attrs": {"member_entitled": False, "auth_mode": "shared"},
        "method": "GET",
        "path": "/api/v1/me/team-connections/conn-1/credential",
    }


def test_outbox_payload_shape_for_an_allow_without_a_team() -> None:
    event = _event(allow("connector.credential", "member_entitled"), team_id=None, attrs={})
    payload = event.outbox_payload()
    assert payload["effect"] == "allow"
    assert payload["as_not_found"] is False
    assert payload["error_code"] is None
    assert payload["resource"] == {"type": "connector", "id": "conn-1", "team_id": None}
    assert payload["attrs"] == {}


def test_outbox_payload_carries_the_error_code() -> None:
    event = _event(
        deny(
            "p",
            "email_verification_required",
            message="m",
            error_code="email_verification_required",
        ),
        team_id=None,
        attrs={},
    )
    assert event.outbox_payload()["error_code"] == "email_verification_required"


def test_outbox_payload_redacts_forbidden_keys_that_bypassed_audited_attrs() -> None:
    """Belt and braces: an event built with un-filtered attrs still never
    persists a forbidden key."""
    event = _event(
        allow("p", "r"),
        team_id=None,
        attrs={"secret": "hunter2", "shared_secret": "x", "enabled": True},
    )
    assert event.outbox_payload()["attrs"] == {"enabled": True}


def test_outbox_payload_is_json_serialisable() -> None:
    event = _event(allow("p", "r"), team_id=TEAM, attrs={"roles": ["admin"]})
    payload = event.outbox_payload()
    assert json.loads(json.dumps(payload)) == payload


def test_outbox_payload_never_carries_the_actor_or_org() -> None:
    """The actor document and org id travel in their own outbox columns; the
    payload is only the decision."""
    payload = _event(allow("p", "r"), team_id=None, attrs={}).outbox_payload()
    assert "actor" not in payload
    assert "org_id" not in payload
