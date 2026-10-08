"""Every route, aimed at org A with org B's credential, finds nothing of A's.

One person is a member of org A (their home) and org B. Every operation the app
documents is called with that person's org-B credential and org A's ids in its
path, query and body (see ``route_matrix`` for how each piece is built). Three
properties hold for every operation:

* a route whose path names one of A's objects refuses: 401, 403, 404 or 422,
  never a 2xx (unless the area declares the id a key in the caller's own org,
  with its reason);
* no answer carries an id of A's the request did not itself send, and no 2xx
  carries any;
* a call that is not a ``GET`` leaves the number of rows filed under org A
  exactly as it was, table by table.

An id parameter the parameter table does not map fails its operation by name.
No operation is allowed to fail: a route that crosses orgs fails here outright.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from alkera_core.config import settings
from httpx import ASGITransport, AsyncClient, Response
from route_matrix import (
    NAMES_THE_CALLERS_ORGS,
    NOT_CALLABLE,
    OPERATIONS,
    OUTSIDE_THE_DOCUMENT,
    PARAMS,
    REFUSALS,
    Operation,
    Request,
    TwoOrgWorld,
    build_request,
    extension_hidden_operations,
    hidden_operations,
    org_row_counts,
)
from sqlalchemy.ext.asyncio import AsyncSession
from tests._suite_app import app as fastapi_app

pytestmark = [pytest.mark.spread]

CALLABLE = [op for op in OPERATIONS if op.name not in NOT_CALLABLE]


@pytest_asyncio.fixture
async def attacker(world: TwoOrgWorld) -> AsyncIterator[AsyncClient]:
    """The two-org person, holding their org-B credential."""
    client = AsyncClient(
        # A crash is answered as the 500 a real server sends, so the matrix
        # reports which status the caller saw instead of a test error.
        transport=ASGITransport(app=fastapi_app, raise_app_exceptions=False),
        base_url="http://test",
        headers={
            "Origin": settings.frontend_base_url,
            "Authorization": f"Bearer {world.token_b}",
        },
    )
    try:
        yield client
    finally:
        await client.aclose()


async def _send(client: AsyncClient, request: Request) -> Response:
    headers = dict(request.headers)
    if request.body_kind == "json" or (request.body_kind == "none" and request.body is not None):
        return await client.request(
            request.method, request.url, params=request.query, headers=headers, json=request.body
        )
    if request.body_kind in ("form", "multipart"):
        fields = request.body if isinstance(request.body, dict) else {}
        data = {k: v for k, v in fields.items() if not isinstance(v, (bytes, list, dict))}
        files = {"file": ("x.bin", b"x")} if request.body_kind == "multipart" else None
        return await client.request(
            request.method,
            request.url,
            params=request.query,
            headers=headers,
            data=data,
            files=files,
        )
    if request.body_kind == "bytes":
        return await client.request(
            request.method, request.url, params=request.query, headers=headers, content=b"x"
        )
    return await client.request(request.method, request.url, params=request.query, headers=headers)


def _leaked(text: str, needles: set[str]) -> str | None:
    return next((needle for needle in sorted(needles) if needle in text), None)


@pytest.mark.parametrize("op", [pytest.param(op, id=op.name) for op in CALLABLE])
async def test_org_b_reaches_nothing_of_org_a(
    op: Operation,
    world: TwoOrgWorld,
    attacker: AsyncClient,
    real_session: AsyncSession,
) -> None:
    request = build_request(op, world)
    if request.unmapped:
        pytest.fail(
            f"{op.name}: path parameter(s) {', '.join(request.unmapped)} name no tenant "
            "object in the cross-tenant parameter table; map each in params_<area>.py "
            "(or list it in NOT_TENANT_PARAMS with its reason)"
        )
    needles = world.needles(names_callers_orgs=op.name in NAMES_THE_CALLERS_ORGS)
    before = await org_row_counts(real_session, world.org_a) if op.method != "GET" else None
    await real_session.commit()

    response = await _send(attacker, request)
    text = response.text
    problems: list[str] = []
    status = response.status_code
    if status >= 500:
        problems.append(f"answered {status}: {text[:300]}")
    if request.aimed and status not in REFUSALS:
        if not (200 <= status < 300 and op.name in PARAMS.caller_scoped):
            problems.append(f"answered {status} for org A's object: {text[:300]}")
    foreign = needles - request.carried
    leak = _leaked(text, needles if 200 <= status < 300 else foreign)
    if leak is not None:
        problems.append(f"answered with org A's id {leak!r} (status {status}): {text[:300]}")
    if before is not None:
        real_session.expire_all()
        after = await org_row_counts(real_session, world.org_a)
        await real_session.commit()
        moved = {t: (before.get(t, 0), n) for t, n in after.items() if n != before.get(t, 0)}
        if moved:
            problems.append(f"moved org A's rows (before, after): {moved}")
    if problems:
        pytest.fail(f"{op.name} with org B's credential: " + "; ".join(problems))


# --------------------------------------------------------------------------
# the matrix's own contract
# --------------------------------------------------------------------------


def test_the_matrix_drives_the_documented_surface() -> None:
    """The routes most likely to leak are the ones the matrix forgot: every
    area's routes are in it, both reads and writes."""
    assert len(OPERATIONS) > 150
    areas = {op.area for op in OPERATIONS}
    assert {"kb", "objects_files", "chats_compute_gate", "org_connections"} <= areas
    assert any(op.method == "GET" for op in OPERATIONS)
    assert any(op.method != "GET" for op in OPERATIONS)
    assert not any(op.path.startswith(("/api/v1/admin/", "/admin/v1/")) for op in OPERATIONS)


def test_every_hidden_route_is_accounted_for() -> None:
    """A route kept out of the document is out of the matrix too, so each one is
    named with what authenticates it instead of a member's credential."""
    assert hidden_operations() - extension_hidden_operations() == set(OUTSIDE_THE_DOCUMENT)


def test_no_two_areas_claim_the_same_parameter() -> None:
    assert PARAMS.conflicts == ()


def test_every_caller_scoped_and_uncallable_operation_exists() -> None:
    served = {op.name for op in OPERATIONS}
    assert set(PARAMS.caller_scoped) <= served
    assert set(NOT_CALLABLE) <= served
    assert set(NAMES_THE_CALLERS_ORGS) <= served
