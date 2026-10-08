"""What an extension registered into the backend's extension points gets.

Runs in a fresh interpreter: the points freeze once the app is built, and the
suite's own app has built them already. The probe installs
an extension with one router in each slot and two lifespans, builds the app
through the open factory, and reports what an anonymous caller gets and the
order the lifespans ran in.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest
from backend.api.extension_points import MountPoint, RouterMount, RouterSlot, SignedBody
from fastapi import APIRouter

pytestmark = [pytest.mark.xdist_group("backend_extension_slots")]

PROBE = """
import asyncio, json
from contextlib import asynccontextmanager

from alkera_core.extensions import Extension, install_extensions
from fastapi import APIRouter

from backend.api.admin._audit import AuditedRoute
from backend.api.extension_points import (
    HTTP_ROUTERS,
    LIFESPANS,
    MountPoint,
    RouterMount,
    RouterSlot,
)

public = APIRouter(prefix="/api/v1/probe-public")
gated = APIRouter(prefix="/api/v1/probe-gated")
admin = APIRouter(prefix="/probe-admin", route_class=AuditedRoute)
webhook = APIRouter(prefix="/api/v1/probe-webhook")
ci = APIRouter(prefix="/api/v1/probe-ci")
machine = APIRouter(prefix="/api/v1/probe-machine")
for router in (public, gated, admin, webhook, ci, machine):
    router.add_api_route("", lambda: {"ok": True}, methods=["GET"])

events = []


def tracked(name):
    @asynccontextmanager
    async def lifespan(application):
        events.append(f"enter {name}")
        try:
            yield
        finally:
            events.append(f"exit {name}")

    return lifespan


def install():
    HTTP_ROUTERS.register(RouterMount(public, RouterSlot.PUBLIC))
    HTTP_ROUTERS.register(RouterMount(gated, RouterSlot.GATED))
    HTTP_ROUTERS.register(RouterMount(admin, RouterSlot.ADMIN))
    HTTP_ROUTERS.register(RouterMount(webhook, RouterSlot.WEBHOOK))
    HTTP_ROUTERS.register(RouterMount(ci, RouterSlot.GATED, MountPoint.CI))
    HTTP_ROUTERS.register(RouterMount(machine, RouterSlot.MACHINE))
    LIFESPANS.register(tracked("first"))
    LIFESPANS.register(tracked("second"))


install_extensions([Extension(name="probe", install=install)])

# The realtime runtime opens the outbox listener; record it instead.
from backend.services.realtime import runtime as realtime_runtime


async def start(application, *, decide):
    events.append("realtime start")
    return object()


async def stop(application, runtime):
    events.append("realtime stop")


realtime_runtime.start = start
realtime_runtime.stop = stop

from backend.api import rate_limit
from backend.app_factory import create_app, lifespan
from httpx import ASGITransport, AsyncClient

app = create_app()


def first_index(prefix):
    return next(
        i for i, route in enumerate(app.routes) if getattr(route, "path", "").startswith(prefix)
    )


order = {
    prefix: first_index(prefix)
    for prefix in (
        "/api/v1/probe-webhook",
        "/api/v1/compute",
        "/api/v1/scim/v2",
        "/api/v1/probe-ci",
        "/api/v1/files",
        "/api/v1/workspaces",
        "/api/v1/probe-machine",
        "/api/v1/objects",
    )
}
webhook_class = rate_limit.declared_class(app.routes[order["/api/v1/probe-webhook"]])
machine_class = rate_limit.declared_class(app.routes[order["/api/v1/probe-machine"]])
team_connection_paths = sorted(
    {route.path for route in app.routes if "/connections" in getattr(route, "path", "")}
)


async def main():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        paths = (
            "/api/v1/probe-public",
            "/api/v1/probe-gated",
            "/admin/v1/probe-admin",
            "/probe-admin",
            "/api/v1/probe-webhook",
            "/api/v1/probe-ci",
            "/api/v1/probe-machine",
        )
        status = {path: (await client.get(path)).status_code for path in paths}
    async with lifespan(app):
        events.append("serving")
    return status


status = asyncio.run(main())
print(
    "REPORT"
    + json.dumps(
        {
            "status": status,
            "events": events,
            "order": order,
            "webhook_class": webhook_class,
            "machine_class": machine_class,
            "team_connection_paths": team_connection_paths,
        }
    )
)
"""


@pytest.fixture(scope="module")
def report(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    home = tmp_path_factory.mktemp("home")
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(PROBE)],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
        env={**os.environ, "ALKERA_HOME": str(home)},
        cwd=Path(__file__).parent,
    )
    assert result.returncode == 0, result.stderr[-4000:]
    line = next(line for line in result.stdout.splitlines() if line.startswith("REPORT"))
    parsed: dict[str, Any] = json.loads(line.removeprefix("REPORT"))
    return parsed


def test_a_public_router_mounts_with_no_gate(report: dict[str, Any]) -> None:
    assert report["status"]["/api/v1/probe-public"] == 200


def test_a_gated_router_mounts_behind_the_verification_gate(report: dict[str, Any]) -> None:
    # The probe route checks nothing itself, so the refusal is the gate's.
    assert report["status"]["/api/v1/probe-gated"] == 401


def test_an_admin_router_mounts_under_the_admin_prefix_behind_the_staff_floor(
    report: dict[str, Any],
) -> None:
    assert report["status"]["/admin/v1/probe-admin"] == 401
    assert report["status"]["/probe-admin"] == 404


def test_extension_lifespans_run_inside_the_realtime_runtime_and_unwind_in_reverse(
    report: dict[str, Any],
) -> None:
    assert report["events"] == [
        "realtime start",
        "enter first",
        "enter second",
        "serving",
        "exit second",
        "exit first",
        "realtime stop",
    ]


def test_a_webhook_router_mounts_with_no_gate_under_the_webhook_class(
    report: dict[str, Any],
) -> None:
    assert report["status"]["/api/v1/probe-webhook"] == 200
    assert report["webhook_class"] == "webhook"


def test_a_router_mounts_at_the_point_it_names(report: dict[str, Any]) -> None:
    order = report["order"]
    # The webhooks point sits ahead of a provider's shutdown notice.
    assert order["/api/v1/probe-webhook"] < order["/api/v1/compute"]
    # The CI point sits after SCIM and ahead of Files, behind the gate its slot names.
    assert order["/api/v1/scim/v2"] < order["/api/v1/probe-ci"] < order["/api/v1/files"]
    assert report["status"]["/api/v1/probe-ci"] == 401


def test_a_machine_router_mounts_where_a_box_calls_behind_the_machine_gate(
    report: dict[str, Any],
) -> None:
    order = report["order"]
    # After the workspace routes, ahead of the objects a box fills.
    assert order["/api/v1/workspaces"] < order["/api/v1/probe-machine"] < order["/api/v1/objects"]
    # The gate that admits a machine still refuses a caller with no credential.
    assert report["status"]["/api/v1/probe-machine"] == 401
    assert report["machine_class"] == "chat"


def test_the_open_app_serves_team_connections_without_the_products_extras(
    report: dict[str, Any],
) -> None:
    """Team and personal connections, their leases and a chat's connections are
    the open platform's; the pre-save check routes and the OAuth relay are not."""
    paths = report["team_connection_paths"]
    assert "/api/v1/teams/{team_id}/connections" in paths
    assert "/api/v1/me/connections" in paths
    assert "/api/v1/chats/{chat_id}/connections" in paths
    assert "/api/v1/workspaces/{workspace_id}/connections" in paths
    assert not [p for p in paths if "verification" in p or "oauth" in p]


@pytest.mark.parametrize(
    ("slot", "at", "point"),
    [
        pytest.param(RouterSlot.PUBLIC, None, MountPoint.PUBLIC, id="public-default"),
        pytest.param(RouterSlot.GATED, None, MountPoint.ORG, id="gated-default"),
        pytest.param(RouterSlot.WEBHOOK, None, MountPoint.WEBHOOKS, id="webhook-default"),
        pytest.param(RouterSlot.ADMIN, None, MountPoint.ADMIN, id="admin-default"),
        pytest.param(RouterSlot.MACHINE, None, MountPoint.HELD, id="machine-default"),
        pytest.param(RouterSlot.GATED, MountPoint.CI, MountPoint.CI, id="named-point-wins"),
        pytest.param(RouterSlot.PUBLIC, MountPoint.CI, MountPoint.CI, id="ungated-at-a-point"),
    ],
)
def test_a_mount_lands_at_its_named_point_or_its_slots_own(
    slot: RouterSlot, at: MountPoint | None, point: MountPoint
) -> None:
    assert RouterMount(APIRouter(), slot, at).point is point


@pytest.mark.parametrize(
    ("slot", "at"),
    [
        pytest.param(RouterSlot.GATED, MountPoint.ADMIN, id="gated-at-admin"),
        pytest.param(RouterSlot.PUBLIC, MountPoint.ADMIN, id="public-at-admin"),
        pytest.param(RouterSlot.ADMIN, MountPoint.CI, id="admin-elsewhere"),
    ],
)
def test_only_the_admin_slot_mounts_at_the_admin_point(slot: RouterSlot, at: MountPoint) -> None:
    with pytest.raises(ValueError, match="cannot mount"):
        RouterMount(APIRouter(), slot, at)


def _served(path: str) -> APIRouter:
    router = APIRouter()
    router.add_api_route(path, lambda: {"ok": True}, methods=["POST"])
    return router


def test_a_router_declares_a_signed_body_on_a_path_it_serves() -> None:
    body = SignedBody("/api/v1/signed", 1024, ("x-signature",))
    mount = RouterMount(_served("/api/v1/signed"), RouterSlot.PUBLIC, signed_bodies=(body,))
    assert mount.signed_bodies == (body,)


@pytest.mark.parametrize(
    ("served", "path", "size", "headers", "match"),
    [
        pytest.param("/api/v1/a", "/api/v1/b", 1024, ("x-sig",), "not a path", id="unserved"),
        pytest.param("/api/v1/a", "/api/v1/a", 1024, (), "no signature header", id="no-header"),
        pytest.param("/api/v1/a", "/api/v1/a", 1024, ("X-Sig",), "lowercase", id="uppercase"),
        pytest.param("/api/v1/a", "/api/v1/a", 0, ("x-sig",), "positive", id="no-cap"),
    ],
)
def test_a_signed_body_the_guard_could_not_bound_is_refused(
    served: str, path: str, size: int, headers: tuple[str, ...], match: str
) -> None:
    with pytest.raises(ValueError, match=match):
        RouterMount(
            _served(served), RouterSlot.PUBLIC, signed_bodies=(SignedBody(path, size, headers),)
        )
