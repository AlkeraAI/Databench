"""A write on the browser's session cookie must name the org its tab rendered.

The org assertion (``X-Alkera-Org``) keeps a tab left behind after a switch in
another window from writing into the new org. Its coverage is pinned here, in
both modes of ``ORG_ASSERTION_MODE``: a cookie-authed write that names no org
is logged as ``org_assertion.missing`` and let through (``log``) or refused
with a 428 before the route runs (``enforce``). In both modes a read is never
held to it, a header naming another org is a 409, the routes that write before
or across an org choice are exempt, and every credential a browser tab never
holds writes without the header as before. Everything drives the real door
against real Postgres.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from uuid import UUID

import pytest
from alkera_core.auth import COOKIE_NAME, mint_machine_worker_token
from alkera_core.auth.tenancy import ORG_HEADER
from alkera_core.config import settings
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import MachineCredential, UserPreference
from backend.api.session_responses import SWITCH_CSRF_HEADER, SWITCH_CSRF_VALUE
from backend.auth.dependencies import CurrentPrincipal, CurrentUser
from backend.auth.org_assertion import ORG_LESS_WRITES
from backend.services.credentials import ci_tokens as ci_token_service
from backend.services.credentials import pats as pat_service
from backend.services.credentials import proxy_tokens as proxy_token_service
from fastapi import APIRouter, FastAPI
from fastapi.routing import APIRoute
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from structlog.testing import capture_logs
from tests._suite_app import app as fastapi_app
from tests.conftest import OrgWithAdmin, TwoOrg, app_client, login, mint_cli_token
from tests.files._boxes import chat_on_box, credential_box

PREFERENCES = "/api/v1/me/preferences"
MISSING = "org_assertion.missing"
_PROBE = "/api/v1/_test/org-assertion"
_router = APIRouter(prefix=_PROBE)

BOTH_MODES = [pytest.param("log", id="log"), pytest.param("enforce", id="enforce")]


@_router.api_route("/write", methods=["POST", "PUT", "PATCH", "DELETE"])
async def _write(ctx: CurrentPrincipal) -> dict[str, str]:
    return {"org": str(ctx.org_id), "kind": ctx.acting_principal.kind.value}


@pytest.fixture(autouse=True)
def _mounted_router() -> Iterator[None]:
    before = len(fastapi_app.router.routes)
    fastapi_app.include_router(_router)
    try:
        yield
    finally:
        del fastapi_app.router.routes[before:]


@pytest.fixture
def enforce(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "org_assertion_mode", "enforce")


@pytest.fixture
def mode(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> str:
    value = str(request.param)
    monkeypatch.setattr(settings, "org_assertion_mode", value)
    return value


def _code(resp: Response) -> str | None:
    error = resp.json().get("error")
    return error.get("code") if isinstance(error, dict) else None


def _missing(logged: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [entry for entry in logged if entry.get("event") == MISSING]


async def _preference_rows(user_id: UUID) -> int:
    async with AsyncSessionLocal() as db:
        return int(
            await db.scalar(
                select(func.count())
                .select_from(UserPreference)
                .where(UserPreference.user_id == user_id)
            )
            or 0
        )


async def _tab(org: OrgWithAdmin) -> AsyncClient:
    """A browser signed in to ``org`` whose requests name no org."""
    client = app_client(names_org=False)
    await login(client, org.admin_email, org.admin_password)
    return client


# --- log mode (the default) -----------------------------------------------------


def test_log_is_the_default_mode() -> None:
    assert type(settings).model_fields["org_assertion_mode"].default == "log"


async def test_in_log_mode_an_unnamed_cookie_write_goes_through_and_is_logged(
    org_admin: OrgWithAdmin,
) -> None:
    async with await _tab(org_admin) as tab:
        with capture_logs() as logged:
            resp = await tab.patch(PREFERENCES, json={"preferences": {"reduce_motion": True}})
    assert resp.status_code == 200, resp.text
    assert await _preference_rows(org_admin.admin_id) == 1
    assert _missing(logged) == [
        {
            "event": MISSING,
            "log_level": "warning",
            "route": PREFERENCES,
            "method": "PATCH",
            "org_id": str(org_admin.org_id),
            "enforced": False,
        }
    ]


async def test_in_log_mode_a_named_cookie_write_logs_nothing(org_admin: OrgWithAdmin) -> None:
    async with await _tab(org_admin) as tab:
        with capture_logs() as logged:
            resp = await tab.post(f"{_PROBE}/write", headers={ORG_HEADER: str(org_admin.org_id)})
    assert resp.status_code == 200, resp.text
    assert _missing(logged) == []


# --- enforce mode ---------------------------------------------------------------


@pytest.mark.usefixtures("enforce")
@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
async def test_enforced_an_unnamed_cookie_write_is_refused_before_the_route(
    org_admin: OrgWithAdmin, method: str
) -> None:
    async with await _tab(org_admin) as tab:
        with capture_logs() as logged:
            resp = await tab.request(method, f"{_PROBE}/write")
    assert resp.status_code == 428, resp.text
    assert _code(resp) == "org_assertion_required"
    assert resp.json()["error"]["message"] == "Reload this page to continue."
    # The refusal still says which org the session is in, and is on record.
    assert resp.headers[ORG_HEADER] == str(org_admin.org_id)
    assert [entry["enforced"] for entry in _missing(logged)] == [True]


@pytest.mark.usefixtures("enforce")
async def test_enforced_a_refused_cookie_write_changes_nothing(org_admin: OrgWithAdmin) -> None:
    async with await _tab(org_admin) as tab:
        resp = await tab.patch(PREFERENCES, json={"preferences": {"reduce_motion": True}})
    assert resp.status_code == 428, resp.text
    assert await _preference_rows(org_admin.admin_id) == 0


@pytest.mark.usefixtures("enforce")
async def test_enforced_a_bearer_beside_a_cookie_is_held_as_the_cookie(
    org_admin: OrgWithAdmin,
) -> None:
    """The cookie wins over a Bearer header, so the browser's session decides
    whether the write must name its org; a Bearer added by the page cannot
    lift the requirement."""
    raw = await mint_cli_token(
        user_id=org_admin.admin_id, email=org_admin.admin_email, org_team_id=org_admin.org_id
    )
    async with await _tab(org_admin) as tab:
        resp = await tab.post(f"{_PROBE}/write", headers={"Authorization": f"Bearer {raw}"})
    assert resp.status_code == 428, resp.text


# --- both modes -----------------------------------------------------------------


@pytest.mark.parametrize("mode", BOTH_MODES, indirect=True)
async def test_a_cookie_write_that_names_its_org_goes_through(
    org_admin: OrgWithAdmin, mode: str
) -> None:
    async with await _tab(org_admin) as tab:
        resp = await tab.patch(
            PREFERENCES,
            json={"preferences": {"reduce_motion": True}},
            headers={ORG_HEADER: str(org_admin.org_id)},
        )
    assert resp.status_code == 200, resp.text
    assert await _preference_rows(org_admin.admin_id) == 1


@pytest.mark.usefixtures("multi_org")
@pytest.mark.parametrize("mode", BOTH_MODES, indirect=True)
async def test_a_cookie_write_naming_another_org_is_org_changed(
    two_org_identity: TwoOrg, mode: str
) -> None:
    t = two_org_identity
    async with app_client(names_org=False) as tab:
        await login(tab, t.user.email, t.password)
        resp = await tab.patch(
            PREFERENCES,
            json={"preferences": {"reduce_motion": True}},
            headers={ORG_HEADER: str(t.org_b)},
        )
    assert resp.status_code == 409, resp.text
    assert _code(resp) == "org_changed"
    assert await _preference_rows(t.user.id) == 0


@pytest.mark.parametrize("mode", BOTH_MODES, indirect=True)
@pytest.mark.parametrize(
    "path",
    [pytest.param("/api/v1/auth/me", id="me"), pytest.param(PREFERENCES, id="preferences")],
)
async def test_a_cookie_read_that_names_no_org_is_answered_and_not_logged(
    org_admin: OrgWithAdmin, path: str, mode: str
) -> None:
    async with await _tab(org_admin) as tab:
        with capture_logs() as logged:
            resp = await tab.get(path)
    assert resp.status_code == 200, resp.text
    assert resp.headers[ORG_HEADER] == str(org_admin.org_id)
    assert _missing(logged) == []


# --- the org-less writes --------------------------------------------------------


def _app_routes() -> set[tuple[str, str]]:
    return {
        (method, route.path)
        for route in fastapi_app.routes
        if isinstance(route, APIRoute)
        for method in route.methods
    }


@pytest.mark.parametrize("entry", sorted(ORG_LESS_WRITES), ids=lambda e: f"{e[0]} {e[1]}")
def test_every_org_less_write_is_a_route_of_the_app(entry: tuple[str, str]) -> None:
    assert entry in _app_routes()


@pytest.mark.parametrize("mode", BOTH_MODES, indirect=True)
async def test_signing_in_renewing_and_signing_out_need_no_org(
    org_admin: OrgWithAdmin, mode: str
) -> None:
    """A tab holding a live session cookie signs in again, renews and signs out
    without naming an org: the session itself is what those requests are about."""
    async with await _tab(org_admin) as tab:
        with capture_logs() as logged:
            signed_in = await tab.post(
                "/api/v1/auth/login",
                json={"email": org_admin.admin_email, "password": org_admin.admin_password},
            )
            assert signed_in.status_code == 200, signed_in.text
            assert tab.cookies.get(COOKIE_NAME)
            renewed = await tab.post("/api/v1/auth/refresh")
            assert renewed.status_code == 200, renewed.text
            signed_out = await tab.post("/api/v1/auth/logout")
            assert signed_out.status_code == 200, signed_out.text
    assert _missing(logged) == []


@pytest.mark.usefixtures("multi_org")
@pytest.mark.parametrize("mode", BOTH_MODES, indirect=True)
async def test_switching_org_needs_no_org_and_lands_in_the_new_one(
    two_org_identity: TwoOrg, mode: str
) -> None:
    t = two_org_identity
    async with app_client(names_org=False) as tab:
        await login(tab, t.user.email, t.password)
        switched = await tab.post(
            "/api/v1/auth/refresh/org",
            json={"org_team_id": str(t.org_b)},
            headers={SWITCH_CSRF_HEADER: SWITCH_CSRF_VALUE},
        )
        assert switched.status_code == 200, switched.text
        assert switched.json()["user"]["org_team_id"] == str(t.org_b)


def _probe_app() -> FastAPI:
    """The real door on two writes: one at a path the exemption names, one at a
    path it does not."""
    app = FastAPI()
    (logout_path,) = {path for method, path in ORG_LESS_WRITES if path.endswith("/logout")}

    @app.post(logout_path)
    async def exempt(user: CurrentUser) -> dict[str, str]:
        return {"user": str(user.id)}

    @app.post("/probe/write")
    async def held(user: CurrentUser) -> dict[str, str]:
        return {"user": str(user.id)}

    return app


@pytest.mark.parametrize("mode", BOTH_MODES, indirect=True)
async def test_the_exemption_holds_even_for_a_route_that_resolves_the_session(
    org_admin: OrgWithAdmin, mode: str
) -> None:
    """The list is what exempts a route, not the route's happening to skip the
    door: the same cookie write through the door is never logged or refused at a
    listed path, and is at an unlisted one."""
    async with AsyncClient(transport=ASGITransport(app=_probe_app()), base_url="http://test") as c:
        await login(c, org_admin.admin_email, org_admin.admin_password)
        with capture_logs() as exempt_logged:
            exempt = await c.post("/api/v1/auth/logout")
        with capture_logs() as held_logged:
            held = await c.post("/probe/write")
    assert exempt.status_code == 200, exempt.text
    assert _missing(exempt_logged) == []
    assert held.status_code == (428 if mode == "enforce" else 200), held.text
    assert [entry["route"] for entry in _missing(held_logged)] == ["/probe/write"]


# --- the credentials a browser tab never holds ---------------------------------


async def _bearer_jwt(org: OrgWithAdmin, _session: AsyncSession) -> str:
    return await mint_cli_token(user_id=org.admin_id, email=org.admin_email, org_team_id=org.org_id)


async def _pat(org: OrgWithAdmin, session: AsyncSession) -> str:
    _row, raw = await pat_service.mint(
        session, org_id=org.org_id, user_id=org.admin_id, label="pat"
    )
    await session.commit()
    return raw


async def _ci(org: OrgWithAdmin, session: AsyncSession) -> str:
    _row, raw = await ci_token_service.mint(
        session, org_id=org.org_id, created_by_id=org.admin_id, label="ci"
    )
    await session.commit()
    return raw


async def _proxy(org: OrgWithAdmin, session: AsyncSession) -> str:
    _row, raw = await proxy_token_service.mint(
        session, org_id=org.org_id, created_by_id=org.admin_id, label="proxy"
    )
    await session.commit()
    return raw


async def _machine(org: OrgWithAdmin, session: AsyncSession) -> str:
    raw, _machine_id = await credential_box(session, org_id=org.org_id, user_id=org.admin_id)
    return raw


async def _worker(org: OrgWithAdmin, session: AsyncSession) -> str:
    _raw, machine_id = await credential_box(session, org_id=org.org_id, user_id=org.admin_id)
    await chat_on_box(session, org_id=org.org_id, owner_id=org.admin_id, machine=machine_id)
    credential_id = await session.scalar(
        select(MachineCredential.id).where(MachineCredential.machine_id == UUID(machine_id))
    )
    assert credential_id is not None
    token, _claims = mint_machine_worker_token(
        credential_id=credential_id, machine_id=UUID(machine_id), org_id=org.org_id
    )
    return token


CREDENTIALS: list[Any] = [
    pytest.param(_bearer_jwt, "user", id="bearer-jwt"),
    pytest.param(_pat, "pat", id="personal-access-token"),
    pytest.param(_ci, "service", id="ci-token"),
    pytest.param(_proxy, "service", id="proxy-token"),
    pytest.param(_machine, "machine", id="machine-credential"),
    pytest.param(_worker, "machine", id="machine-worker-credential"),
]


@pytest.mark.parametrize("mode", BOTH_MODES, indirect=True)
@pytest.mark.parametrize(("mint", "kind"), CREDENTIALS)
async def test_a_credential_no_tab_holds_writes_without_naming_an_org(
    org_admin: OrgWithAdmin, real_session: AsyncSession, mint: Any, kind: str, mode: str
) -> None:
    raw = await mint(org_admin, real_session)
    async with app_client(names_org=False) as c:
        with capture_logs() as logged:
            resp = await c.post(f"{_PROBE}/write", headers={"Authorization": f"Bearer {raw}"})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"org": str(org_admin.org_id), "kind": kind}
    assert _missing(logged) == []


@pytest.mark.parametrize("mode", BOTH_MODES, indirect=True)
async def test_a_bearer_jwt_writes_a_real_route_without_naming_an_org(
    org_admin: OrgWithAdmin, real_session: AsyncSession, mode: str
) -> None:
    raw = await _bearer_jwt(org_admin, real_session)
    async with app_client(names_org=False) as c:
        resp = await c.patch(
            PREFERENCES,
            json={"preferences": {"reduce_motion": True}},
            headers={"Authorization": f"Bearer {raw}"},
        )
    assert resp.status_code == 200, resp.text
    assert await _preference_rows(org_admin.admin_id) == 1
