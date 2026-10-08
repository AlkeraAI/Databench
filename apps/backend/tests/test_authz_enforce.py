"""enforce(): the decision becomes the right HTTP error and lands on record.

A recording sink pins what enforce() hands the sink; the real outbox pins
WHERE the row is written — an allow inside the request transaction (gone with
a rollback), a deny in its own committed transaction (survives the request's
rollback) — and what the row looks like.
"""

from __future__ import annotations

import importlib
import secrets
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import pytest
from alkera_core.authz import (
    AUTHZ_EVENT_TYPE,
    ActingContext,
    Action,
    CredentialKind,
    Decision,
    DecisionEvent,
    Policy,
    Resource,
    ResourceType,
    Role,
    allow,
    deny,
    temporarily_registered,
)
from alkera_core.authz.policies import connector as connector_policy
from alkera_core.config import settings
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.events import VISIBILITY_PLATFORM
from alkera_core.events.outbox import MAX_ENTITY_ID_LENGTH
from alkera_core.models import EventOutbox, Team
from backend.authz import (
    OutboxDecisionSink,
    decide_many,
    decide_on_record,
    enforce,
    http_error_for,
    role_resolver,
)
from backend.services.org import teams as team_service
from fastapi import HTTPException, Request
from prometheus_client import REGISTRY
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from structlog.testing import capture_logs

pytestmark = pytest.mark.asyncio

# The package re-exports the function `enforce`, which shadows the submodule of
# the same name; the module is what the outbox seam is patched on.
enforce_module = importlib.import_module("backend.authz.enforce")

ORG = uuid4()
OTHER_ORG = uuid4()
USER_ID = uuid4()
CONNECTION_ID = uuid4()
TEAM_ID = uuid4()

ENTITLED: dict[str, object] = {
    "member_entitled": True,
    "enabled": True,
    "auth_mode": "shared",
    "has_shared_secret": True,
    "team_id": str(TEAM_ID),
    # Empty is a TEAM row: no one person owns it, so entitlement decides.
    "owner_user_id": "",
}


def _request(method: str = "GET", path: str = "/api/v1/things/1", query: bytes = b"") -> Request:
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": query,
        "headers": [(b"host", b"test")],
        "server": ("test", 80),
        "client": ("127.0.0.1", 12345),
    }
    return Request(scope)


def _user_ctx(org_id: UUID = ORG, user_id: UUID = USER_ID) -> ActingContext:
    return ActingContext.for_user(user_id=user_id, org_id=org_id, email="user@alkera.dev")


def _agent_ctx(org_id: UUID = ORG) -> ActingContext:
    return ActingContext.for_agent(
        user_id=USER_ID, org_id=org_id, email="user@alkera.dev", session_id="sess-1"
    )


def _connector(org_id: UUID | None = ORG, team_id: UUID | None = TEAM_ID) -> Resource:
    return Resource(ResourceType.CONNECTOR, id=str(CONNECTION_ID), org_id=org_id, team_id=team_id)


@dataclass
class RecordingSink:
    allows: list[tuple[AsyncSession | None, DecisionEvent]] = field(default_factory=list)
    denies: list[DecisionEvent] = field(default_factory=list)

    async def record_allow(self, db: AsyncSession, event: DecisionEvent) -> None:
        self.allows.append((db, event))

    async def record_deny(self, db: AsyncSession, event: DecisionEvent) -> None:
        self.denies.append(event)


class ExplodingSink(RecordingSink):
    async def record_deny(self, db: AsyncSession, event: DecisionEvent) -> None:
        raise RuntimeError("sink down")


# --------------------------------------------------------------------------- #
# what the sink is handed
# --------------------------------------------------------------------------- #


async def test_an_allow_is_returned_and_recorded_with_the_request_session() -> None:
    sink = RecordingSink()
    request = _request("GET", "/api/v1/me/team-connections/x/credential", b"debug=1")
    session_marker: Any = object()
    decision = await enforce(
        request,
        session_marker,
        _user_ctx(),
        Action.FETCH_CREDENTIAL,
        _connector(),
        ENTITLED,
        sink=sink,
    )
    assert decision.allowed is True
    assert (decision.policy, decision.reason) == (connector_policy.POLICY, "member_entitled")
    assert sink.denies == []
    ((db, event),) = sink.allows
    assert db is session_marker
    assert event.decision is decision
    assert event.action is Action.FETCH_CREDENTIAL
    assert event.resource == _connector()
    assert event.org_id == ORG
    assert event.method == "GET"
    assert event.path == "/api/v1/me/team-connections/x/credential"  # no query string
    assert event.actor == _user_ctx().audit_dict()
    assert [link["kind"] for link in event.actor["chain"]] == ["user"]


async def test_the_recorded_attrs_are_the_policy_allowlist_only() -> None:
    sink = RecordingSink()
    attrs: dict[str, object] = {
        **ENTITLED,
        "shared_secret": "hunter2",
        "unlisted": 1,
        "nested": {"a": 1},
    }
    await enforce(
        _request(), object(), _user_ctx(), Action.FETCH_CREDENTIAL, _connector(), attrs, sink=sink
    )
    ((_db, event),) = sink.allows
    assert event.attrs == {
        "auth_mode": "shared",
        "enabled": True,
        "has_shared_secret": True,
        "member_entitled": True,
        "owner_user_id": "",
        "team_id": str(TEAM_ID),
    }
    assert "shared_secret" not in event.outbox_payload()["attrs"]
    assert "unlisted" not in event.outbox_payload()["attrs"]


async def test_an_agent_allow_records_the_two_link_chain() -> None:
    sink = RecordingSink()
    await enforce(
        _request(),
        object(),
        _agent_ctx(),
        Action.FETCH_CREDENTIAL,
        _connector(),
        ENTITLED,
        sink=sink,
    )
    ((_db, event),) = sink.allows
    assert [(link["kind"], link["id"]) for link in event.actor["chain"]] == [
        ("user", str(USER_ID)),
        ("agent", "sess-1"),
    ]
    assert event.actor["agent_id_asserted_by"] == "client"


async def test_an_opaque_deny_is_a_404_with_the_policy_message_and_is_recorded() -> None:
    sink = RecordingSink()
    with pytest.raises(HTTPException) as excinfo:
        await enforce(
            _request(),
            object(),
            _user_ctx(),
            Action.FETCH_CREDENTIAL,
            _connector(),
            {**ENTITLED, "member_entitled": False},
            sink=sink,
        )
    assert excinfo.value.status_code == 404
    assert excinfo.value.detail == connector_policy.NOT_FOUND
    assert sink.allows == []
    (event,) = sink.denies
    assert event.decision.reason == "not_entitled"
    assert event.decision.as_not_found is True
    assert event.attrs["member_entitled"] is False


def _refusing_policy(error_code: str | None = None) -> Policy:
    """A policy that refuses every request, with ``error_code`` when given."""

    def _decide(
        ctx: ActingContext, action: Action, resource: Resource, attrs: Mapping[str, object]
    ) -> Decision | None:
        return deny(
            "artifact.test",
            "admin_required",
            message="admin role required",
            error_code=error_code,
        )

    return Policy(
        name="artifact.test",
        resource_type=ResourceType.ARTIFACT,
        decide=_decide,
        audited_attrs=frozenset({"roles"}),
    )


async def test_a_plain_deny_is_a_403_with_a_string_detail() -> None:
    sink = RecordingSink()
    attrs: dict[str, object] = {"roles": frozenset({Role.MEMBER, Role.VIEWER})}
    resource = Resource(ResourceType.ARTIFACT, id="c-9", org_id=ORG)
    with temporarily_registered(_refusing_policy()), pytest.raises(HTTPException) as excinfo:
        await enforce(_request(), object(), _user_ctx(), Action.READ, resource, attrs, sink=sink)
    assert excinfo.value.status_code == 403
    assert excinfo.value.detail == "admin role required"
    (event,) = sink.denies
    assert event.decision.reason == "admin_required"
    assert event.attrs["roles"] == ["member", "viewer"]


async def test_a_deny_with_an_error_code_is_a_403_with_a_structured_detail() -> None:
    sink = RecordingSink()
    attrs: dict[str, object] = {"roles": frozenset({Role.ADMIN})}
    resource = Resource(ResourceType.ARTIFACT, id="c-9", org_id=ORG)
    policy = _refusing_policy(error_code="verify_email")
    with temporarily_registered(policy), pytest.raises(HTTPException) as excinfo:
        await enforce(_request(), object(), _user_ctx(), Action.READ, resource, attrs, sink=sink)
    assert excinfo.value.status_code == 403
    assert excinfo.value.detail == {"code": "verify_email", "message": "admin role required"}
    (event,) = sink.denies
    assert event.decision.error_code == "verify_email"


async def test_a_resource_with_no_policy_is_denied_and_recorded_under_the_callers_org() -> None:
    sink = RecordingSink()
    resource = Resource(ResourceType.ARTIFACT, id="c-1", org_id=None)
    with pytest.raises(HTTPException) as excinfo:
        await enforce(_request(), object(), _user_ctx(), Action.READ, resource, {}, sink=sink)
    assert excinfo.value.status_code == 403
    assert excinfo.value.detail == "Not allowed"
    (event,) = sink.denies
    assert (event.decision.policy, event.decision.reason) == ("none", "no_policy")
    assert event.attrs == {}
    assert event.org_id == ORG  # no resource org → the caller's


async def test_a_cross_org_resource_is_an_opaque_404_recorded_against_the_probed_org() -> None:
    sink = RecordingSink()
    with pytest.raises(HTTPException) as excinfo:
        await enforce(
            _request(),
            object(),
            _user_ctx(),
            Action.FETCH_CREDENTIAL,
            _connector(org_id=OTHER_ORG),
            ENTITLED,
            sink=sink,
        )
    assert excinfo.value.status_code == 404
    assert excinfo.value.detail == "Not found"
    (event,) = sink.denies
    assert event.decision.reason == "cross_org"
    assert event.org_id == OTHER_ORG
    assert event.actor["acting"]["org_id"] == str(ORG)


async def test_a_missing_attribute_is_a_403_not_an_exception() -> None:
    sink = RecordingSink()
    with pytest.raises(HTTPException) as excinfo:
        await enforce(
            _request(), object(), _user_ctx(), Action.FETCH_CREDENTIAL, _connector(), {}, sink=sink
        )
    assert excinfo.value.status_code == 403
    (event,) = sink.denies
    assert event.decision.reason == "missing_attribute:member_entitled"


async def test_a_sink_that_fails_on_a_deny_does_not_change_the_refusal() -> None:
    """A custom sink's failure on deny propagates (it is not the production
    sink); the production sink's own guard is tested against the real outbox
    below. Pinned so the contract of the seam is explicit."""
    sink = ExplodingSink()
    with pytest.raises(RuntimeError, match="sink down"):
        await enforce(
            _request(),
            object(),
            _user_ctx(),
            Action.FETCH_CREDENTIAL,
            _connector(),
            {**ENTITLED, "enabled": False},
            sink=sink,
        )


@pytest.mark.parametrize(
    ("decision", "status_code", "detail"),
    [
        pytest.param(
            deny("p", "r", message="Team not found", as_not_found=True),
            404,
            "Team not found",
            id="opaque-404",
        ),
        pytest.param(
            deny("p", "r", message="Not found", as_not_found=True, error_code="ignored_on_404"),
            404,
            "Not found",
            id="opaque-404-wins-over-a-code",
        ),
        pytest.param(
            deny("p", "r", message="Verify", error_code="email_verification_required"),
            403,
            {"code": "email_verification_required", "message": "Verify"},
            id="coded-403",
        ),
        pytest.param(deny("p", "r", message="nope"), 403, "nope", id="plain-403"),
        pytest.param(
            deny("p", "r", message="Refused", error_code="c", unauthenticated=True),
            401,
            {"code": "c", "message": "Refused"},
            id="coded-401",
        ),
        pytest.param(
            deny("p", "r", message="Refused", unauthenticated=True), 401, "Refused", id="plain-401"
        ),
    ],
)
def test_http_error_for(decision: Decision, status_code: int, detail: object) -> None:
    error = http_error_for(decision)
    assert (error.status_code, error.detail) == (status_code, detail)
    # A 401 tells the client which scheme to come back with; nothing else does.
    assert (error.headers or {}).get("WWW-Authenticate") == (
        "Bearer" if status_code == 401 else None
    )


# --------------------------------------------------------------------------- #
# the real outbox: where the row lands
# --------------------------------------------------------------------------- #


async def _decision_rows(org_id: UUID) -> list[EventOutbox]:
    async with AsyncSessionLocal() as session:
        rows = await session.execute(
            select(EventOutbox)
            .where(EventOutbox.org_id == org_id, EventOutbox.type == AUTHZ_EVENT_TYPE)
            .order_by(EventOutbox.id)
        )
        return list(rows.scalars().all())


def _throwaway_team() -> Team:
    return Team(name=f"throwaway {secrets.token_hex(4)}", is_root=True, parent_team_id=None)


async def test_a_deny_row_survives_the_request_rollback() -> None:
    org = uuid4()
    async with AsyncSessionLocal() as session:
        session.add(_throwaway_team())
        await session.flush()
        with pytest.raises(HTTPException) as excinfo:
            await enforce(
                _request("GET", "/api/v1/probe"),
                session,
                _user_ctx(org_id=org),
                Action.FETCH_CREDENTIAL,
                _connector(org_id=org),
                {**ENTITLED, "auth_mode": "per_user"},
            )
        assert excinfo.value.status_code == 404
        await session.rollback()

    rows = await _decision_rows(org)
    assert len(rows) == 1
    (row,) = rows
    assert row.payload["effect"] == "deny"
    assert row.payload["reason"] == "auth_mode_not_shared"
    assert row.payload["path"] == "/api/v1/probe"


async def test_an_allow_row_rolls_back_with_the_request_and_commits_with_it() -> None:
    org = uuid4()
    async with AsyncSessionLocal() as session:
        session.add(_throwaway_team())
        await session.flush()
        decision = await enforce(
            _request(),
            session,
            _user_ctx(org_id=org),
            Action.FETCH_CREDENTIAL,
            _connector(org_id=org),
            ENTITLED,
        )
        assert decision.allowed
        await session.rollback()
    assert await _decision_rows(org) == []

    async with AsyncSessionLocal() as session:
        await enforce(
            _request(),
            session,
            _user_ctx(org_id=org),
            Action.FETCH_CREDENTIAL,
            _connector(org_id=org),
            ENTITLED,
        )
        # Not visible to anyone else until the request commits.
        assert await _decision_rows(org) == []
        await session.commit()
    rows = await _decision_rows(org)
    assert len(rows) == 1
    assert rows[0].payload["effect"] == "allow"


@pytest.mark.parametrize(
    ("attrs", "effect"),
    [
        pytest.param(ENTITLED, "allow", id="allow-rides-the-callers-session"),
        pytest.param({**ENTITLED, "auth_mode": "per_user"}, "deny", id="deny-is-committed-alone"),
    ],
)
async def test_a_decision_without_a_request_is_returned_and_recorded_like_enforce(
    attrs: Mapping[str, object], effect: str
) -> None:
    """The request-less door (a socket's message): a deny is returned, not
    raised, and each lands where enforce() puts it -- an allow commits with
    the caller's session, a deny survives its rollback -- under the method
    and path the caller names."""
    org = uuid4()
    async with AsyncSessionLocal() as session:
        session.add(_throwaway_team())
        await session.flush()
        decision = await decide_on_record(
            session,
            _user_ctx(org_id=org),
            Action.FETCH_CREDENTIAL,
            _connector(org_id=org),
            attrs,
            method="NB_CHANNEL_COMM",
            path="nb:probe",
        )
        assert decision.effect.value == effect
        if effect == "allow":
            assert await _decision_rows(org) == []
            await session.commit()
        else:
            await session.rollback()
    (row,) = await _decision_rows(org)
    assert (row.payload["effect"], row.payload["method"], row.payload["path"]) == (
        effect,
        "NB_CHANNEL_COMM",
        "nb:probe",
    )


async def test_the_recorded_row_has_the_documented_shape() -> None:
    org = uuid4()
    ctx = _agent_ctx(org_id=org)
    resource = _connector(org_id=org)
    async with AsyncSessionLocal() as session:
        await enforce(
            _request("POST", "/api/v1/kb/promote", b"x=1"),
            session,
            ctx,
            Action.FETCH_CREDENTIAL,
            resource,
            ENTITLED,
        )
        await session.commit()
    (row,) = await _decision_rows(org)
    assert row.type == AUTHZ_EVENT_TYPE == "authz.decision"
    assert row.entity == "connector"
    assert row.entity_id == str(CONNECTION_ID)
    assert row.version == 0
    assert row.visibility == VISIBILITY_PLATFORM == "platform"
    assert row.actor == ctx.audit_dict()
    assert row.payload == {
        "effect": "allow",
        "action": "fetch_credential",
        "reason": "member_entitled",
        "policy": connector_policy.POLICY,
        "as_not_found": False,
        "error_code": None,
        "resource": {"type": "connector", "id": str(CONNECTION_ID), "team_id": str(TEAM_ID)},
        "attrs": {
            "auth_mode": "shared",
            "enabled": True,
            "has_shared_secret": True,
            "member_entitled": True,
            "owner_user_id": "",
            "team_id": str(TEAM_ID),
        },
        "method": "POST",
        "path": "/api/v1/kb/promote",
    }


#: What a caller can put in a URL and the database cannot store verbatim: a NUL
#: in the path; a NUL, a lone surrogate and ten kilobytes in the probed id.
HOSTILE_PATH = "/api/v1/files/drives/\x00/items/x/content"
HOSTILE_ID = "a\x00\ud800" + "x" * 10_000


@pytest.mark.parametrize(
    ("attrs", "effect"),
    [
        pytest.param(ENTITLED, "allow", id="allow-rides-the-request-session"),
        pytest.param({**ENTITLED, "auth_mode": "per_user"}, "deny", id="deny-in-its-own-session"),
    ],
)
async def test_a_decision_about_hostile_input_is_still_recorded(
    attrs: Mapping[str, object], effect: str
) -> None:
    """The path and id are the caller's, so the row cannot carry them verbatim.

    An allow that could not be written used to turn the request into a 500, and
    a deny that could not be written vanished from the record; both are
    written, with each unstorable character replaced and the id cut to the
    column's width.
    """
    org = uuid4()
    resource = Resource(ResourceType.CONNECTOR, id=HOSTILE_ID, org_id=org, team_id=TEAM_ID)
    async with AsyncSessionLocal() as session:
        try:
            await enforce(
                _request("GET", HOSTILE_PATH),
                session,
                _user_ctx(org_id=org),
                Action.FETCH_CREDENTIAL,
                resource,
                attrs,
            )
            await session.commit()
        except HTTPException:
            await session.rollback()

    (row,) = await _decision_rows(org)
    assert row.payload["effect"] == effect
    assert row.payload["path"] == "/api/v1/files/drives/\ufffd/items/x/content"
    assert row.entity_id == row.payload["resource"]["id"]
    assert row.entity_id == ("a\ufffd\ufffd" + "x" * 10_000)[:MAX_ENTITY_ID_LENGTH]


async def test_an_outbox_failure_on_a_deny_is_logged_and_the_refusal_stands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("outbox down")

    monkeypatch.setattr(enforce_module, "emit", _boom)
    org = uuid4()
    async with AsyncSessionLocal() as session:
        with capture_logs() as logs, pytest.raises(HTTPException) as excinfo:
            await enforce(
                _request(),
                session,
                _user_ctx(org_id=org),
                Action.FETCH_CREDENTIAL,
                _connector(org_id=org),
                {**ENTITLED, "has_shared_secret": False},
            )
    assert excinfo.value.status_code == 404
    failed = [entry for entry in logs if entry["event"] == "authz.decision.write_failed"]
    assert len(failed) == 1
    assert failed[0]["log_level"] == "error"
    assert failed[0]["reason"] == "no_shared_secret"
    assert await _decision_rows(org) == []


@pytest.mark.parametrize(
    ("kind", "record"),
    [
        pytest.param("deny", "single", id="a-denial"),
        pytest.param("batch", "batch", id="a-batch-summary"),
    ],
)
async def test_a_decision_row_that_cannot_be_written_is_counted(
    monkeypatch: pytest.MonkeyPatch, kind: str, record: str
) -> None:
    """A refusal stands when its row cannot be written, so the loss has to be
    visible somewhere other than a log line: it is counted, by what was lost."""

    async def _boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("outbox down")

    monkeypatch.setattr(enforce_module, "emit", _boom)
    monkeypatch.setattr(settings, "metrics_enabled", True)
    metric = "authz_decision_write_failed_total"
    before = REGISTRY.get_sample_value(metric, {"kind": kind}) or 0.0
    org = uuid4()
    async with AsyncSessionLocal() as session:
        if record == "single":
            with pytest.raises(HTTPException):
                await enforce(
                    _request(),
                    session,
                    _user_ctx(org_id=org),
                    Action.FETCH_CREDENTIAL,
                    _connector(org_id=org),
                    {**ENTITLED, "has_shared_secret": False},
                )
        else:
            await decide_many(
                _request(),
                session,
                _user_ctx(org_id=org),
                Action.FETCH_CREDENTIAL,
                [_connector(org_id=org)],
                lambda _resource: ENTITLED,
            )
    assert REGISTRY.get_sample_value(metric, {"kind": kind}) == before + 1


async def test_an_outbox_failure_on_an_allow_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    """An allow that cannot be recorded does not proceed."""

    async def _boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("outbox down")

    monkeypatch.setattr(enforce_module, "emit", _boom)
    async with AsyncSessionLocal() as session:
        with pytest.raises(RuntimeError, match="outbox down"):
            await enforce(
                _request(), session, _user_ctx(), Action.FETCH_CREDENTIAL, _connector(), ENTITLED
            )


async def test_the_production_sink_is_the_default() -> None:
    assert isinstance(enforce_module._default_sink, OutboxDecisionSink)


async def test_a_temporarily_registered_policy_flows_through_the_same_path() -> None:
    """The choke point is policy-agnostic: a policy registered for a test
    resource type gets the same recording and the same status mapping."""
    seen: list[Mapping[str, object]] = []

    def _decide(
        ctx: ActingContext, action: Action, resource: Resource, attrs: Mapping[str, object]
    ) -> Decision | None:
        seen.append(dict(attrs))
        return allow("artifact.test", "ok") if attrs.get("open") else None

    policy = Policy(
        name="artifact.test",
        resource_type=ResourceType.ARTIFACT,
        decide=_decide,
        audited_attrs=frozenset({"open"}),
    )
    sink = RecordingSink()
    resource = Resource(ResourceType.ARTIFACT, id="c-9", org_id=ORG)
    with temporarily_registered(policy):
        ok = await enforce(
            _request(), object(), _user_ctx(), Action.READ, resource, {"open": True}, sink=sink
        )
        assert ok.allowed
        with pytest.raises(HTTPException) as excinfo:
            await enforce(
                _request(), object(), _user_ctx(), Action.READ, resource, {"open": False}, sink=sink
            )
    assert excinfo.value.status_code == 403
    assert sink.denies[0].decision.reason == "no_rule_matched"
    assert [e.attrs for _db, e in sink.allows] == [{"open": True}]
    assert len(seen) == 2


# --------------------------------------------------------------------------- #
# the request's role resolver
# --------------------------------------------------------------------------- #


async def test_role_resolver_is_built_once_per_request_and_bound_to_the_real_chain() -> None:
    async with AsyncSessionLocal() as session:
        org, admin = await team_service.create_org_with_admin(
            session,
            org_name=f"Resolver Org {secrets.token_hex(4)}",
            admin_email=f"resolver-{secrets.token_hex(6)}@alkera.dev",
            admin_first_name="R",
            admin_last_name="A",
            admin_password="pw-1234567890",
        )
        await session.commit()
        ctx = ActingContext.for_user(user_id=admin.id, org_id=org.id, email=admin.email)
        request = _request()
        first = role_resolver(request, session, ctx)
        second = role_resolver(request, session, ctx)
        assert first is second
        assert first.ctx is ctx
        answer = await first.for_team(org.id)
        assert answer.in_org is True
        assert Role.OWNER in answer.roles
        # Another request gets its own.
        assert role_resolver(_request(), session, ctx) is not first


def test_service_context_can_be_built_for_the_sink_too() -> None:
    ctx = ActingContext.for_service(
        token_id=uuid4(), org_id=ORG, label="ci", credential=CredentialKind.CI_TOKEN
    )
    assert ctx.audit_dict()["acting"]["kind"] == "service"


# --------------------------------------------------------------------------- #
# a denial is recorded without touching the caller
# --------------------------------------------------------------------------- #


async def _single_connection_pool() -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    """An engine whose pool holds exactly one connection and gives up on a
    second checkout after a second, so holding two at once is observable."""
    from alkera_core.config import settings
    from alkera_core.db.tls import asyncpg_connect_args

    engine = create_async_engine(
        settings.database_url,
        pool_size=1,
        max_overflow=0,
        pool_timeout=1.0,
        connect_args=asyncpg_connect_args(),
    )
    return engine, async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)


async def test_a_denial_is_recorded_when_the_request_pool_has_no_connection_left() -> None:
    """The request session holds its pool's only connection when enforce()
    runs, and keeps it. The deny row is written through the decision pool, so
    it never waits on the request pool — the two-connections-per-request shape
    that drained it — and the request's connection is still its own after."""
    engine, factory = await _single_connection_pool()
    org = uuid4()
    try:
        async with factory() as request_session:
            await request_session.execute(text("SELECT 1"))
            with capture_logs() as logs, pytest.raises(HTTPException) as excinfo:
                await enforce(
                    _request("GET", "/api/v1/one-connection"),
                    request_session,
                    _user_ctx(org_id=org),
                    Action.FETCH_CREDENTIAL,
                    _connector(org_id=org),
                    {**ENTITLED, "enabled": False},
                )
            assert excinfo.value.status_code == 404
            assert request_session.in_transaction()
            assert (await request_session.execute(text("SELECT 1"))).scalar_one() == 1
    finally:
        await engine.dispose()
    assert [entry for entry in logs if entry["event"] == "authz.decision.write_failed"] == []
    (row,) = await _decision_rows(org)
    assert (row.payload["effect"], row.payload["reason"], row.payload["path"]) == (
        "deny",
        "connection_disabled",
        "/api/v1/one-connection",
    )


async def test_a_denial_leaves_the_callers_session_exactly_as_it_was() -> None:
    """A recorded DENY touches nothing of the caller's: its transaction stays
    open, a write it flushed is still there, and a row it loaded is still
    readable without a reload. Ending the caller's transaction to free its
    connection expired every loaded row, and the next attribute read in an
    async session raised — a listing that refused one entry then 500ed on the
    next (the trash listing, for every member who could not read one entry)."""
    org = uuid4()
    loaded_name = f"loaded {secrets.token_hex(4)}"
    flushed_name = f"flushed {secrets.token_hex(4)}"
    async with AsyncSessionLocal() as session:
        session.add(Team(name=loaded_name, is_root=True, parent_team_id=None))
        await session.commit()
        loaded = (await session.execute(select(Team).where(Team.name == loaded_name))).scalar_one()
        session.add(Team(name=flushed_name, is_root=True, parent_team_id=None))
        await session.flush()
        with pytest.raises(HTTPException) as excinfo:
            await enforce(
                _request(),
                session,
                _user_ctx(org_id=org),
                Action.FETCH_CREDENTIAL,
                _connector(org_id=org),
                {**ENTITLED, "member_entitled": False},
            )
        assert excinfo.value.status_code == 404
        assert loaded.name == loaded_name, "the loaded row was expired by the denial"
        assert session.in_transaction()
        flushed = (await session.execute(select(Team.id).where(Team.name == flushed_name))).first()
        assert flushed is not None, "the caller's flushed write was rolled back"
        await session.rollback()
    (row,) = await _decision_rows(org)
    assert (row.payload["effect"], row.payload["reason"]) == ("deny", "not_entitled")


async def test_an_allow_still_rides_the_request_session_untouched() -> None:
    """An allow is recorded in the request's own transaction, beside a flush
    made before it, and rolls back with it."""
    org = uuid4()
    name = f"throwaway {secrets.token_hex(4)}"
    async with AsyncSessionLocal() as session:
        session.add(Team(name=name, is_root=True, parent_team_id=None))
        await session.flush()
        decision = await enforce(
            _request(),
            session,
            _user_ctx(org_id=org),
            Action.FETCH_CREDENTIAL,
            _connector(org_id=org),
            ENTITLED,
        )
        assert decision.allowed
        assert (await session.execute(select(Team.id).where(Team.name == name))).first() is not None
        await session.rollback()
    assert await _decision_rows(org) == []
