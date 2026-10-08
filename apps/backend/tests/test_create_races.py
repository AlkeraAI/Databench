"""Two requests that create the same thing at once: one wins, the other is a 409.

Every create checks for an existing row and then inserts, so two requests that
both pass the check reach the unique index together and the second insert is
refused there. That refusal used to escape as an opaque 500 on sign-up, user
creation, invitations, admin org creation and catalog model creation; it is now
the 409 a create of something that already exists earns, from one handler that
every route shares.
"""

from __future__ import annotations

import asyncio
import uuid
from collections import Counter
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.observability.asgi import install_exception_handlers
from backend.services.identity import users as user_service
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from tests.conftest import OrgWithAdmin, login

pytestmark = pytest.mark.asyncio

#: How many identical creates are fired at once.
RACERS = 8

STRONG_PASSWORD = "Tr0ub4dor-&-correct-horse-battery"


def _scratch_app(statements: list[str]) -> FastAPI:
    """An app whose one route runs ``statements`` in a fresh transaction."""
    app = FastAPI()
    install_exception_handlers(app)

    @app.post("/run")
    async def run() -> dict[str, str]:
        async with AsyncSessionLocal() as session:
            for statement in statements:
                await session.execute(text(statement))
            await session.commit()
        return {"ok": "yes"}

    return app


@pytest.mark.parametrize(
    ("statements", "status", "code"),
    [
        pytest.param(
            [
                "CREATE TEMP TABLE race_probe (k int CONSTRAINT uq_race_probe UNIQUE)",
                "INSERT INTO race_probe VALUES (1)",
                "INSERT INTO race_probe VALUES (1)",
            ],
            409,
            "conflict",
            id="unique-violation-is-a-409",
        ),
        pytest.param(
            [
                "CREATE TEMP TABLE race_probe (k int NOT NULL)",
                "INSERT INTO race_probe VALUES (NULL)",
            ],
            500,
            "internal_error",
            id="not-null-violation-stays-a-500",
        ),
        pytest.param(
            [
                "CREATE TEMP TABLE race_probe (k int CHECK (k > 0))",
                "INSERT INTO race_probe VALUES (0)",
            ],
            500,
            "internal_error",
            id="check-violation-stays-a-500",
        ),
    ],
)
async def test_only_a_unique_violation_becomes_a_409(
    disposable_database: str, statements: list[str], status: int, code: str
) -> None:
    """The asymmetry the handler has to keep: a duplicate is the caller's race,
    a missing column or a broken CHECK is a bug that must still page."""
    transport = ASGITransport(app=_scratch_app(statements), raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as raw:
        response = await raw.post("/run")
    assert response.status_code == status, response.text
    error = response.json()["error"]
    assert error["code"] == code
    assert "race_probe" not in response.text, "the constraint leaked to the caller"


async def test_a_signup_that_loses_the_race_gets_the_existing_account_answer(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both requests passed the existence check; the index decides.

    The check is made to miss so the second sign-up reaches the insert, which
    is exactly where a concurrent one lands. It is answered with the sentence
    a sequential duplicate gets, not a generic conflict.
    """
    body = {"email": f"race-{uuid.uuid4().hex[:8]}@acme-race.dev", "password": STRONG_PASSWORD}
    first = await client.post("/api/v1/auth/signup", json=body)
    assert first.status_code == 201, first.text
    client.cookies.clear()

    async def nobody(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(user_service, "get_by_email", nobody)
    second = await client.post("/api/v1/auth/signup", json=body)
    assert second.status_code == 409, second.text
    assert "already exists" in second.json()["error"]["message"]


Send = Callable[[AsyncClient, OrgWithAdmin, str], Awaitable[Any]]


async def _signup(client: AsyncClient, _org: OrgWithAdmin, tag: str) -> Any:
    return await client.post(
        "/api/v1/auth/signup",
        json={"email": f"race-{tag}@acme-race.dev", "password": STRONG_PASSWORD},
    )


async def _invitation(client: AsyncClient, org: OrgWithAdmin, tag: str) -> Any:
    return await client.post(
        f"/api/v1/teams/{org.org_id}/invitations", json={"email": f"race-{tag}@acme-race.dev"}
    )


async def _user(client: AsyncClient, org: OrgWithAdmin, tag: str) -> Any:
    return await client.post(
        "/api/v1/users",
        json={
            "email": f"race-{tag}@acme-race.dev",
            "first_name": "Race",
            "last_name": "Runner",
            "org_team_id": str(org.org_id),
        },
    )


@pytest.mark.parametrize(
    ("send", "needs_admin"),
    [
        pytest.param(_signup, False, id="signup"),
        pytest.param(_invitation, True, id="team-invitation"),
        pytest.param(_user, True, id="org-user"),
    ],
)
async def test_identical_creates_fired_at_once_answer_one_created_and_the_rest_409(
    client: AsyncClient,
    org_admin: OrgWithAdmin,
    monkeypatch_email_send: list[dict[str, Any]],
    monkeypatch_verification_send: list[dict[str, Any]],
    send: Send,
    needs_admin: bool,
) -> None:
    if needs_admin:
        await login(client, org_admin.admin_email, org_admin.admin_password)
    tag = uuid.uuid4().hex[:8]
    responses = await asyncio.gather(*(send(client, org_admin, tag) for _ in range(RACERS)))
    statuses = Counter(r.status_code for r in responses)
    assert not [s for s in statuses if s >= 500], [r.text for r in responses]
    created = sum(n for s, n in statuses.items() if 200 <= s < 300)
    assert created == 1, statuses
    assert statuses[409] == RACERS - 1, statuses


@pytest.mark.parametrize(
    ("path", "body"),
    [
        pytest.param(
            "/admin/v1/orgs",
            lambda tag: {
                "name": f"Race org {tag}",
                "admin_email": f"race-{tag}@acme-race.dev",
                "admin_first_name": "Race",
                "admin_last_name": "Runner",
                "admin_password": STRONG_PASSWORD,
            },
            id="admin-org",
        ),
        pytest.param(
            "/admin/v1/catalog/models",
            lambda tag: {"id": f"race-model-{tag}", "display_name": "Race", "family": "race"},
            id="catalog-model",
        ),
    ],
)
async def test_identical_admin_creates_fired_at_once_answer_one_created_and_the_rest_409(
    client: AsyncClient,
    platform_admin: OrgWithAdmin,
    monkeypatch_email_send: list[dict[str, Any]],
    path: str,
    body: Callable[[str], dict[str, Any]],
) -> None:
    await login(client, platform_admin.admin_email, platform_admin.admin_password)
    payload = body(uuid.uuid4().hex[:8])
    responses = await asyncio.gather(*(client.post(path, json=payload) for _ in range(RACERS)))
    statuses = Counter(r.status_code for r in responses)
    assert not [s for s in statuses if s >= 500], [r.text for r in responses]
    assert sum(n for s, n in statuses.items() if 200 <= s < 300) == 1, statuses
    assert statuses[409] == RACERS - 1, statuses
