"""Tenancy gates: every request's org comes from its credential.

One identity will belong to several orgs, so code that reads "the org" off a
user row, asks for "every team this person is in", lets a client choose an org,
or mints a credential outside the mint helper is a cross-tenant bug waiting for
the day a second membership exists. These gates keep new code from adding any
of it, with zero tolerance:

* **user-org reads** (``.org_team_id`` off a user) and **unscoped membership
  queries** (``TeamMembership.user_id`` without ``TeamMembership.org_team_id``)
  must find nothing anywhere in the scanned trees. There is no allowlist: one
  finding fails, which is what keeps ``multi_org_enabled`` safe to turn on.
* **no client-supplied org**: no route outside the platform-admin surfaces takes
  a parameter or a body field naming an org, except the exact set below, each
  with its reason.
* **one mint helper**: only ``backend.auth.membership_tokens`` (and the token
  module that defines them) calls the session, CLI and gateway token encoders.

Each gate has a decoy that plants a violation and proves the scan sees it.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from _tenancy_architecture_scan import (
    OPEN_AREAS,
    SCOPES,
    area_of,
    encoder_callers,
    home_org_reads,
    iter_modules,
    layer_areas,
    merge_areas,
    unscoped_membership_queries,
    user_org_reads,
)
from fastapi import APIRouter, Body, Depends, FastAPI, Header, Query
from pydantic import BaseModel

pytestmark = [pytest.mark.spread]

#: The modules that may call a token encoder: the mint helper, which binds every
#: credential to a membership, and the module that defines the encoders.
ENCODER_CALLERS = frozenset({"backend.auth.membership_tokens", "alkera_core.auth.tokens"})

#: Route surfaces that are platform-staff only and name orgs by design.
ADMIN_PREFIXES = ("/api/v1/admin", "/admin/v1")

#: The names a client-supplied org would travel under.
ORG_FIELD_NAMES = frozenset({"org_id", "org_team_id", "organization_id"})

_SSO_SIGN_IN = (
    "chooses which org's single sign-on to start; a selector before any credential exists"
)

#: Every route that takes an org from the client, with why it may. A method,
#: the route's path, and where the org travels (``path:``, ``query:``,
#: ``header:``, ``body:`` + the field).
ALLOWED_CLIENT_ORG: Mapping[tuple[str, str, str], str] = {
    ("GET", "/api/v1/auth/sso/{org_id}/login", "path:org_id"): _SSO_SIGN_IN,
    ("GET", "/api/v1/auth/sso/{org_id}/login/callback", "path:org_id"): _SSO_SIGN_IN,
    ("GET", "/api/v1/auth/sso/{org_id}/saml/login", "path:org_id"): _SSO_SIGN_IN,
    ("POST", "/api/v1/auth/sso/{org_id}/saml/acs", "path:org_id"): _SSO_SIGN_IN,
    ("GET", "/api/v1/auth/sso/{org_id}/saml/metadata", "path:org_id"): (
        "the org's public SAML service-provider metadata, read by its IdP; no credential"
    ),
    ("POST", "/api/v1/users", "body:UserCreate.org_team_id"): (
        "deprecated create kept for compatibility; equality-checked against the request's "
        "org and never selects one"
    ),
    ("POST", "/api/v1/auth/refresh/org", "body:SwitchOrgRequest.org_team_id"): (
        "the switch selector: checked once against the caller's active memberships at mint time"
    ),
    ("POST", "/api/v1/auth/device/approve", "body:DeviceApproveRequest.org_team_id"): (
        "the device-approval selector: checked once against the caller's memberships"
    ),
    ("POST", "/api/v1/auth/memberships/join", "body:JoinMembershipRequest.org_team_id"): (
        "the join selector: checked once against the caller's own pending memberships"
    ),
    (
        "POST",
        "/api/v1/machines/me/worker-credentials",
        "body:MachineWorkerCredentialRequest.org_id",
    ): (
        "a box names the org whose worker process it is minting for; decided against the "
        "orgs its machine credential may serve (decide_machine_standing) and minted for "
        "that org alone"
    ),
}


# --- the route table ----------------------------------------------------------


def _norm(name: str | None) -> str:
    return (name or "").lower().replace("-", "_")


def _walk_dependant(dependant: Any, found: set[str]) -> None:
    for kind, params in (
        ("path", dependant.path_params),
        ("query", dependant.query_params),
        ("header", dependant.header_params),
        ("cookie", dependant.cookie_params),
    ):
        for field in params:
            if _norm(field.name) in ORG_FIELD_NAMES or _norm(field.alias) in ORG_FIELD_NAMES:
                found.add(f"{kind}:{field.name}")
    for field in dependant.body_params:
        if _norm(field.name) in ORG_FIELD_NAMES or _norm(field.alias) in ORG_FIELD_NAMES:
            found.add(f"body:{field.name}")
        model = field.field_info.annotation
        if isinstance(model, type) and issubclass(model, BaseModel):
            for name, info in model.model_fields.items():
                if _norm(name) in ORG_FIELD_NAMES or _norm(info.alias) in ORG_FIELD_NAMES:
                    found.add(f"body:{model.__name__}.{name}")
    for sub in dependant.dependencies:
        _walk_dependant(sub, found)


def client_org_fields(app: FastAPI) -> set[tuple[str, str, str]]:
    """Every (method, path, where) a route outside the admin surfaces takes an
    org from the client: a path, query, header or cookie parameter, a body
    parameter, or a field of a body model one level deep, on the route or any
    dependency it resolves."""
    hits: set[tuple[str, str, str]] = set()
    for route in app.routes:
        dependant = getattr(route, "dependant", None)
        path = getattr(route, "path", "")
        if dependant is None or path.startswith(ADMIN_PREFIXES):
            continue
        found: set[str] = set()
        _walk_dependant(dependant, found)
        methods = sorted(getattr(route, "methods", None) or {"WS"})
        hits |= {(method, path, where) for method in methods for where in found}
    return hits


# --- the gates ----------------------------------------------------------------


def test_no_user_org_read_anywhere() -> None:
    """Every org comes from the credential or the resource, never off a person."""
    assert user_org_reads() == {}


def test_no_unscoped_membership_query_anywhere() -> None:
    """No function asks for every team a person is in across all their orgs."""
    assert unscoped_membership_queries() == {}


def test_the_user_row_has_no_org_attribute() -> None:
    """The column is the identity's home org and is mapped under that name, so
    ``user.org_team_id`` cannot be written by habit and read as "the org"."""
    from alkera_core.models import User

    assert not hasattr(User, "org_team_id")
    assert User.__mapper__.c.home_org_team_id is User.__table__.c.org_team_id


def test_the_home_org_is_read_only_through_the_tenancy_module() -> None:
    assert home_org_reads() == {}


def test_no_route_takes_an_org_from_the_client() -> None:
    from _suite_app import app

    found = client_org_fields(app)
    new = sorted(found - set(ALLOWED_CLIENT_ORG))
    gone = sorted(set(ALLOWED_CLIENT_ORG) - found)
    assert not new, f"routes taking an org from the client: {new}"
    assert not gone, f"allowlisted client-org routes no longer exist; remove them: {gone}"


def test_only_the_mint_helper_calls_the_token_encoders() -> None:
    callers = encoder_callers()
    assert callers <= ENCODER_CALLERS, (
        f"token encoders called outside the mint helper: {sorted(callers - ENCODER_CALLERS)}"
    )
    assert "backend.auth.membership_tokens" in callers


def test_the_scans_read_the_real_tree() -> None:
    names = {name for name, _ in iter_modules()}
    for module in (
        "backend.app_factory",
        "worker",
        "model_gateway.auth",
        "alkera_core.auth.tokens",
    ):
        assert module in names, f"{module} not scanned; SCOPES = {SCOPES}"


# --- decoys -------------------------------------------------------------------


def _plant(root: Path, package: str, files: Mapping[str, str]) -> list[tuple[Path, str]]:
    base = root / package
    for rel, body in files.items():
        path = base / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    return [(base, package)]


def test_a_user_org_read_is_counted_and_a_resource_org_is_not(tmp_path: Path) -> None:
    scopes = _plant(
        tmp_path,
        "backend",
        {
            "services/gadgets/items.py": (
                "def f(user, chat, self_):\n"
                "    a = user.org_team_id\n"
                "    b = chat.org_team_id\n"
                "    c = User.org_team_id\n"
                "    return a, b, c\n"
                "class S:\n"
                "    def g(self):\n"
                "        return self.user.org_team_id, self.org_team_id\n"
            ),
            "services/org/teams.py": (
                "def h(caller, ctx):\n    return caller.org_team_id, ctx.org_id\n"
            ),
        },
    )
    assert user_org_reads(scopes) == {
        "backend.services.gadgets.items": 3,
        "backend.services.org.teams": 1,
    }


def test_the_excluded_modules_are_not_counted(tmp_path: Path) -> None:
    scopes = _plant(
        tmp_path,
        "alkera_core",
        {
            "models/user.py": "x = User.org_team_id\n",
            "auth/tenancy.py": "def f(user):\n    return user.org_team_id\n",
            "auth/refresh.py": "def f(user):\n    return user.org_team_id\n",
        },
    )
    assert user_org_reads(scopes) == {"alkera_core.auth.refresh": 1}


def test_a_home_org_read_outside_the_tenancy_module_is_counted(tmp_path: Path) -> None:
    scopes = _plant(
        tmp_path,
        "alkera_core",
        {
            "auth/tenancy.py": "def f(user):\n    return user.home_org_team_id\n",
            "models/user.py": "x = User.home_org_team_id\n",
            "auth/refresh.py": (
                "def f(row, User):\n"
                "    a = row.home_org_team_id\n"
                "    b = User.home_org_team_id\n"
                "    return a, b, User(home_org_team_id=1)\n"
            ),
        },
    )
    assert home_org_reads(scopes) == {"alkera_core.auth.refresh": 2}


def test_an_unscoped_membership_query_is_counted_per_function(tmp_path: Path) -> None:
    scopes = _plant(
        tmp_path,
        "model_gateway",
        {
            "resolution.py": (
                "def funding_chain(db, uid):\n"
                "    return select(TeamMembership.team_id).where(TeamMembership.user_id == uid)\n"
                "def scoped(db, uid, org):\n"
                "    return select(TeamMembership).where(\n"
                "        TeamMembership.user_id == uid, TeamMembership.org_team_id == org\n"
                "    )\n"
                "def outer(uid, org):\n"
                "    q = TeamMembership.org_team_id == org\n"
                "    def inner():\n"
                "        return TeamMembership.user_id == uid\n"
                "    return q, inner\n"
                "def by_team(team):\n"
                "    return TeamMembership.team_id == team\n"
            ),
        },
    )
    # funding_chain and the nested `inner` (its own scope) are unscoped.
    assert unscoped_membership_queries(scopes) == {"model_gateway.resolution": 2}


def test_an_encoder_called_outside_the_helper_is_caught(tmp_path: Path) -> None:
    scopes = _plant(
        tmp_path,
        "backend",
        {
            "auth/membership_tokens.py": "encode_session_token(user_id=1)\n",
            "services/chats/catalog.py": "tokens.encode_cli_token(user_id=1)\n",
            "api/routes/chats/chats.py": "t = mint_gateway_token\nmint_machine_gateway_token()\n",
            "services/gadgets/items.py": "encode_ws_ticket()\n",
        },
    )
    assert encoder_callers(scopes) == {
        "backend.auth.membership_tokens",
        "backend.services.chats.catalog",
        "backend.api.routes.chats.chats",
    }


def test_areas_are_assigned_by_prefix() -> None:
    assert area_of("backend.services.files.items") == "objects_files"
    assert area_of("backend.services.chats.catalog") == "chats_compute_gate"
    assert area_of("alkera_core.files.store") == "objects_files"
    assert area_of("alkera_core.auth.refresh") == "auth"
    assert area_of("alkera_core.models.team") == "api_core"
    assert area_of("model_gateway.resolution") == "gateway"
    assert area_of("worker.tasks.sweep") == "worker"
    assert area_of("backend.services.infra.now") == "other"
    # A prefix matches whole package names only.
    assert area_of("backend.services.filesx") == "other"


def test_a_layer_adds_areas_and_prefixes(tmp_path: Path) -> None:
    (tmp_path / "tenancy_architecture.json").write_text(
        '{"areas": {"widgets": ["backend.services.widgets", "alkera_core.widgets"],'
        ' "identity": ["backend.services.people"]}}',
        encoding="utf-8",
    )
    areas = merge_areas(OPEN_AREAS, layer_areas(tmp_path))
    assert area_of("alkera_core.widgets.rates", areas) == "widgets"
    assert area_of("backend.services.people.x", areas) == "identity"
    assert area_of("backend.services.identity.users", areas) == "identity"
    assert area_of("alkera_core.widgets.rates") == "api_core"
    assert layer_areas(tmp_path / "absent") == {}


class _DecoyPayload(BaseModel):
    name: str
    org_team_id: str | None = None


def _decoy_org_dependency(organization_id: str = Query("")) -> str:
    return organization_id


def _decoy_app() -> FastAPI:
    app = FastAPI()
    router = APIRouter()

    @router.get("/api/v1/things/{org_id}")
    def by_path(org_id: str) -> None: ...

    @router.get("/api/v1/things")
    def by_header(x_org_id: str = Header("", alias="org-id")) -> None: ...

    @router.post("/api/v1/things")
    def by_body(payload: _DecoyPayload) -> None: ...

    @router.post("/api/v1/scalars")
    def by_scalar_body(org_id: str = Body(...), other: str = Body(...)) -> None: ...

    @router.get("/api/v1/deps", dependencies=[Depends(_decoy_org_dependency)])
    def by_dependency() -> None: ...

    @router.get("/api/v1/admin/orgs/{org_id}")
    def admin(org_id: str) -> None: ...

    @router.get("/api/v1/clean/{chat_id}")
    def clean(chat_id: str, q: str = "") -> None: ...

    app.include_router(router)
    return app


def test_a_client_supplied_org_is_caught_wherever_it_travels() -> None:
    assert client_org_fields(_decoy_app()) == {
        ("GET", "/api/v1/things/{org_id}", "path:org_id"),
        ("GET", "/api/v1/things", "header:x_org_id"),
        ("POST", "/api/v1/things", "body:_DecoyPayload.org_team_id"),
        ("POST", "/api/v1/scalars", "body:org_id"),
        ("GET", "/api/v1/deps", "query:organization_id"),
    }
