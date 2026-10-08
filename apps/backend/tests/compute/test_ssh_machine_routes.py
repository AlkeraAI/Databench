"""Adding a machine the org runs by its SSH details, through the real app and
real Postgres, with the SSH transport replaced by a scripted host.

Who may test and add (an org admin, where the deployment allows it), what an
add writes (an org machine that is ``added``, free and starting, and an
endpoint with the credential sealed), what it refuses and leaves behind
(nothing), what a reader gets back (never the credential), and that removing
a machine that never started forgets the credential at once.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from alkera_core.compute.node_reconcile import reconcile_nodes
from alkera_core.compute.org_reconcile import reconcile_org_machines
from alkera_core.compute.personal import PersonalProvider
from alkera_core.compute.ssh import (
    CommandResult,
    HostKey,
    HostKeyMismatchError,
    SshAuth,
    SshTarget,
    open_credential,
)
from alkera_core.compute.ssh.provider import SshProvider
from alkera_core.compute.ssh.transport import auth_refused, unreachable
from alkera_core.config import settings
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import EventOutbox, OrgAuditEvent
from alkera_core.models.compute import ComputeAllocation
from alkera_core.models.org_machines import OrgMachine
from alkera_core.models.ssh_machines import SshMachineEndpoint
from backend.api.routes.compute import org_machines as org_machine_routes
from backend.services.compute import ssh_machines
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from tests.conftest import OrgWithAdmin, app_client, login, make_member

pytestmark = [pytest.mark.asyncio, pytest.mark.compute_rows]

MACHINES = "/api/v1/org/machines"
PASSWORD = "the-hosts-root-password"
KEY = HostKey(public_key="ssh-ed25519 AAAAC3NzaFAKEHOSTKEY", fingerprint="SHA256:fakehostkey")
LINUX = "Linux\nx86_64\nyes\nyes\n8\n31\n200\n0\nyes\n"


class ScriptedHost:
    """Answers the facts script, presents :data:`KEY`, and accepts only
    :data:`PASSWORD`."""

    def __init__(self, facts: str = LINUX) -> None:
        self.facts = facts
        self.sessions = 0
        #: What the host prints when asked to fetch this deployment back.
        self.callback_answer = "ok\n"
        #: Probe URL -> what the host prints for it, ahead of ``callback_answer``.
        self.answers_for: dict[str, str] = {}
        #: Every script the host was asked to run.
        self.scripts: list[str] = []

    async def host_key(self, target: SshTarget) -> HostKey:
        return KEY

    def session(
        self, target: SshTarget, *, username: str, auth: SshAuth, pinned_key: str
    ) -> AbstractAsyncContextManager[Any]:
        host = self

        class Session:
            async def run(
                self, command: str, *, stdin: str = "", limit_seconds: float = 60.0
            ) -> CommandResult:
                host.scripts.append(stdin)
                if "/health/live" in stdin:
                    for url, answer in host.answers_for.items():
                        if stdin.startswith(f"set -- {url} "):
                            return CommandResult(0, answer, "")
                    return CommandResult(0, host.callback_answer, "")
                return CommandResult(0, host.facts, "")

        @asynccontextmanager
        async def opened() -> AsyncIterator[Session]:
            if pinned_key != KEY.public_key:
                raise HostKeyMismatchError(KEY.fingerprint)
            if auth.secret != PASSWORD:
                raise auth_refused()
            host.sessions += 1
            yield Session()

        return opened()


#: The address this deployment tells nodes to call it back on, in these tests.
NODE_API_URL = "https://databench.example.com"


@pytest.fixture(autouse=True)
def _self_hosted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "ssh_machines_enabled", True)
    monkeypatch.setattr(settings, "ssh_machines_allow_private_addresses", True)
    monkeypatch.setattr(settings, "alkera_node_api_url", NODE_API_URL)
    monkeypatch.setattr(settings, "alkera_node_gateway_url", "")


@pytest.fixture
def host(monkeypatch: pytest.MonkeyPatch) -> ScriptedHost:
    """The host every add and test reaches, in place of a real SSH connection."""
    scripted = ScriptedHost()
    monkeypatch.setattr(org_machine_routes, "default_ssh_transport", lambda: scripted)
    return scripted


@pytest.fixture(autouse=True)
def _no_nudges(monkeypatch: pytest.MonkeyPatch) -> None:
    from backend.services.infra import task_queue

    async def record() -> bool:
        return True

    monkeypatch.setattr(task_queue, "nudge_org_machine_reconcile", record)


@asynccontextmanager
async def _as(email: str, password: str) -> AsyncIterator[AsyncClient]:
    async with app_client() as browser:
        await login(browser, email, password)
        yield browser


def _target(**over: Any) -> dict[str, Any]:
    return {
        "host": "127.0.0.1",
        "port": 2222,
        "username": "root",
        "auth_kind": "password",
        "password": PASSWORD,
        **over,
    }


def _add(**over: Any) -> dict[str, Any]:
    return {
        **_target(),
        "name": f"Rack {uuid4().hex[:6]}",
        "host_key_fingerprint": KEY.fingerprint,
        "audience": [{"kind": "org"}],
        **over,
    }


def _code(body: dict[str, Any]) -> str | None:
    error = body.get("error")
    return error.get("code") if isinstance(error, dict) else None


async def _endpoints(org_id: UUID) -> list[SshMachineEndpoint]:
    async with AsyncSessionLocal() as session:
        rows = await session.execute(
            select(SshMachineEndpoint).where(SshMachineEndpoint.org_team_id == org_id)
        )
        return list(rows.scalars().all())


async def _machines(org_id: UUID) -> list[OrgMachine]:
    async with AsyncSessionLocal() as session:
        rows = await session.execute(select(OrgMachine).where(OrgMachine.org_team_id == org_id))
        return list(rows.scalars().all())


async def _decisions(org_id: UUID) -> list[dict[str, Any]]:
    async with AsyncSessionLocal() as session:
        rows = await session.execute(
            select(EventOutbox.payload)
            .where(
                EventOutbox.org_id == org_id,
                EventOutbox.type == "authz.decision",
                EventOutbox.entity == "org_machine",
            )
            .order_by(EventOutbox.id)
        )
        return [dict(payload or {}) for payload in rows.scalars().all()]


async def test_a_test_reports_the_fingerprint_and_what_the_host_has(
    org_admin: OrgWithAdmin, host: ScriptedHost
) -> None:
    async with _as(org_admin.admin_email, org_admin.admin_password) as browser:
        resp = await browser.post(f"{MACHINES}/ssh/test", json=_target())
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "reachable": True,
        "host_key_fingerprint": KEY.fingerprint,
        "host_key_type": "ED25519",
        "os": "Linux",
        "arch": "x86_64",
        "vcpu": 8,
        "memory_gb": 31,
        "disk_gb": 200,
        "gpu_count": 0,
        "prerequisites_met": True,
        "missing": [],
        "error_code": None,
        "message": None,
    }
    assert await _machines(org_admin.org_id) == []


async def test_a_refused_credential_is_unreachable_and_never_echoed(
    org_admin: OrgWithAdmin, host: ScriptedHost
) -> None:
    async with _as(org_admin.admin_email, org_admin.admin_password) as browser:
        resp = await browser.post(f"{MACHINES}/ssh/test", json=_target(password="wrong-one-xyz"))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (body["reachable"], body["error_code"]) == (False, "auth_failed")
    assert "wrong-one-xyz" not in resp.text


async def test_an_org_admin_adds_a_machine_that_starts_free_with_the_credential_sealed(
    org_admin: OrgWithAdmin, host: ScriptedHost
) -> None:
    async with _as(org_admin.admin_email, org_admin.admin_password) as browser:
        resp = await browser.post(f"{MACHINES}/ssh", json=_add(name="Basement rack"))
        assert resp.status_code == 202, resp.text
        read = resp.json()
        detail = await browser.get(f"{MACHINES}/{read['id']}")
    assert (read["name"], read["acquisition"], read["card"]["state"]) == (
        "Basement rack",
        "added",
        "starting",
    )
    assert PASSWORD not in resp.text and PASSWORD not in detail.text
    assert detail.json()["ssh"] == {
        "host": "127.0.0.1",
        "port": 2222,
        "username": "root",
        "auth_kind": "password",
        "host_key_fingerprint": KEY.fingerprint,
        "host_key_type": "ED25519",
    }
    [endpoint] = await _endpoints(org_admin.org_id)
    assert PASSWORD not in endpoint.secret_sealed
    assert open_credential(endpoint.secret_sealed) == SshAuth(kind="password", secret=PASSWORD)
    assert endpoint.host_key == KEY.public_key
    async with AsyncSessionLocal() as session:
        machine = await session.get(OrgMachine, UUID(read["id"]))
        assert machine is not None
        alloc = await session.get(ComputeAllocation, machine.current_allocation_id)
        assert alloc is not None
        assert (alloc.price_per_minute_nanos, alloc.storage_price_per_minute_nanos) == (0, 0)
        assert alloc.true_cost_per_minute_nanos == 0
        audit = (
            await session.execute(
                select(OrgAuditEvent).where(
                    OrgAuditEvent.org_team_id == org_admin.org_id,
                    OrgAuditEvent.action == "machine.added",
                )
            )
        ).scalar_one()
        assert PASSWORD not in str(audit.detail)
    decisions = await _decisions(org_admin.org_id)
    assert ("allow", "org_admin_adds") in {(d["effect"], d["reason"]) for d in decisions}


async def test_a_name_taken_during_the_add_is_the_same_409_as_a_taken_one(
    org_admin: OrgWithAdmin, host: ScriptedHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The name check and the insert are not one step: a second add that passes
    the check before the first commits is refused by the database, and its
    caller hears what a sequential duplicate hears, with nothing of it kept."""
    async with _as(org_admin.admin_email, org_admin.admin_password) as browser:
        first = await browser.post(f"{MACHINES}/ssh", json=_add(name="Basement rack"))
        assert first.status_code == 202, first.text
        taken = await browser.post(f"{MACHINES}/ssh", json=_add(name="basement RACK"))

        async def not_yet_taken(*_: object, **__: object) -> bool:
            return False

        monkeypatch.setattr(ssh_machines, "name_is_taken", not_yet_taken)
        raced = await browser.post(
            f"{MACHINES}/ssh", json=_add(name="basement RACK", host="127.0.0.2")
        )
    assert (taken.status_code, _code(taken.json())) == (409, "name_taken"), taken.text
    assert (raced.status_code, _code(raced.json())) == (409, "name_taken"), raced.text
    assert raced.json()["error"]["message"] == taken.json()["error"]["message"]
    assert [m.name for m in await _machines(org_admin.org_id)] == ["Basement rack"]
    assert [e.host for e in await _endpoints(org_admin.org_id)] == ["127.0.0.1"]


async def test_a_host_presenting_another_key_is_409_and_writes_nothing(
    org_admin: OrgWithAdmin, host: ScriptedHost
) -> None:
    async with _as(org_admin.admin_email, org_admin.admin_password) as browser:
        resp = await browser.post(
            f"{MACHINES}/ssh", json=_add(host_key_fingerprint="SHA256:the-one-i-confirmed")
        )
    assert resp.status_code == 409, resp.text
    assert _code(resp.json()) == "host_key_mismatch"
    assert host.sessions == 0
    assert await _machines(org_admin.org_id) == []
    assert await _endpoints(org_admin.org_id) == []


async def test_a_host_missing_a_prerequisite_is_422_and_names_it(
    org_admin: OrgWithAdmin, host: ScriptedHost
) -> None:
    host.facts = "Linux\nx86_64\nno\nno\n2\n4\n20\n0\n"
    async with _as(org_admin.admin_email, org_admin.admin_password) as browser:
        resp = await browser.post(f"{MACHINES}/ssh", json=_add())
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert _code(body) == "host_not_ready"
    assert "systemd" in body["error"]["message"]
    assert await _machines(org_admin.org_id) == []


# --------------------------------------------------------------------------- #
# the address the node calls back on
# --------------------------------------------------------------------------- #

LOOPBACK_SENTENCE = (
    "Machines are told to reach this server at http://localhost:18880, which on the host "
    "is the host itself. Set ALKERA_NODE_API_URL to an address the host can reach."
)


@pytest.mark.parametrize(
    ("setting", "url"),
    [
        pytest.param("alkera_node_api_url", "http://localhost:18880", id="localhost"),
        pytest.param("alkera_node_api_url", "http://127.0.0.1:18880", id="ipv4-loopback"),
        pytest.param("alkera_node_api_url", "http://[::1]:18880", id="ipv6-loopback"),
        pytest.param("alkera_node_api_url", "http://0.0.0.0:18880", id="unspecified"),
        pytest.param("alkera_node_gateway_url", "http://localhost:18881", id="gateway"),
    ],
)
async def test_a_loopback_callback_fails_the_test_without_asking_the_host(
    org_admin: OrgWithAdmin,
    host: ScriptedHost,
    monkeypatch: pytest.MonkeyPatch,
    setting: str,
    url: str,
) -> None:
    monkeypatch.setattr(settings, setting, url)
    async with _as(org_admin.admin_email, org_admin.admin_password) as browser:
        resp = await browser.post(f"{MACHINES}/ssh/test", json=_target())
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["reachable"] is True
    assert body["prerequisites_met"] is False
    assert body["error_code"] == "callback_unreachable"
    name = (
        "ALKERA_NODE_GATEWAY_URL" if setting == "alkera_node_gateway_url" else "ALKERA_NODE_API_URL"
    )
    assert body["message"] == (
        f"Machines are told to reach this server at {url}, which on the host is the host "
        f"itself. Set {name} to an address the host can reach."
    )
    # Decided from the address alone: the host was never asked to fetch it.
    assert not any("/health/live" in script for script in host.scripts)


async def test_unset_the_public_base_url_is_the_address_checked(
    org_admin: OrgWithAdmin, host: ScriptedHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unset, nodes are given the public base URL, and that is what is checked."""
    monkeypatch.setattr(settings, "alkera_node_api_url", "")
    monkeypatch.setattr(settings, "frontend_base_url", "http://localhost:18880")
    async with _as(org_admin.admin_email, org_admin.admin_password) as browser:
        resp = await browser.post(f"{MACHINES}/ssh/test", json=_target())
    assert resp.json()["message"] == LOOPBACK_SENTENCE


async def test_a_host_that_cannot_fetch_the_callback_fails_the_test_and_says_why(
    org_admin: OrgWithAdmin, host: ScriptedHost
) -> None:
    host.callback_answer = (
        "fail curl: (7) Failed to connect to databench.example.com port 443: Connection refused\n"
    )
    async with _as(org_admin.admin_email, org_admin.admin_password) as browser:
        resp = await browser.post(f"{MACHINES}/ssh/test", json=_target())
    body = resp.json()
    assert body["prerequisites_met"] is False
    assert body["error_code"] == "callback_unreachable"
    assert body["message"] == (
        "The host can't reach this server at https://databench.example.com (curl: (7) Failed "
        "to connect to databench.example.com port 443: Connection refused). Set "
        "ALKERA_NODE_API_URL to an address the host can reach."
    )
    # It asked the host for this deployment's liveness probe at that address.
    assert any("https://databench.example.com/health/live" in script for script in host.scripts)


async def test_this_deployments_own_gateway_is_probed_when_none_is_named(
    org_admin: OrgWithAdmin, host: ScriptedHost
) -> None:
    """Unset, a node is given the gateway at the API's origin plus /gateway, and
    a host that cannot reach it is refused rather than left to fail later."""
    host.answers_for["https://databench.example.com/gateway/health/live"] = (
        "fail curl: (22) The requested URL returned error: 404\n"
    )
    async with _as(org_admin.admin_email, org_admin.admin_password) as browser:
        tested = await browser.post(f"{MACHINES}/ssh/test", json=_target())
        added = await browser.post(f"{MACHINES}/ssh", json=_add())
    message = (
        "The host can't reach this server at https://databench.example.com/gateway (curl: "
        "(22) The requested URL returned error: 404). Set ALKERA_NODE_GATEWAY_URL to an "
        "address the host can reach."
    )
    body = tested.json()
    assert (body["prerequisites_met"], body["error_code"], body["message"]) == (
        False,
        "callback_unreachable",
        message,
    )
    assert added.status_code == 422, added.text
    assert added.json()["error"]["message"] == message
    assert await _machines(org_admin.org_id) == []


@pytest.mark.parametrize(
    "answer",
    [
        pytest.param("ok\n", id="fetched"),
        pytest.param("untested\n", id="no-tool-to-fetch-with"),
    ],
)
async def test_a_host_that_reaches_the_callback_passes(
    org_admin: OrgWithAdmin, host: ScriptedHost, answer: str
) -> None:
    host.callback_answer = answer
    async with _as(org_admin.admin_email, org_admin.admin_password) as browser:
        resp = await browser.post(f"{MACHINES}/ssh/test", json=_target())
    body = resp.json()
    assert (body["prerequisites_met"], body["error_code"], body["message"]) == (True, None, None)


@pytest.mark.parametrize(
    ("node_api_url", "answer", "message"),
    [
        pytest.param("http://localhost:18880", "ok\n", LOOPBACK_SENTENCE, id="loopback"),
        pytest.param(
            NODE_API_URL,
            "fail Could not resolve host: databench.example.com\n",
            "The host can't reach this server at https://databench.example.com (Could not "
            "resolve host: databench.example.com). Set ALKERA_NODE_API_URL to an address the "
            "host can reach.",
            id="probe-failed",
        ),
    ],
)
async def test_an_add_whose_node_could_never_call_back_is_422_and_writes_nothing(
    org_admin: OrgWithAdmin,
    host: ScriptedHost,
    monkeypatch: pytest.MonkeyPatch,
    node_api_url: str,
    answer: str,
    message: str,
) -> None:
    monkeypatch.setattr(settings, "alkera_node_api_url", node_api_url)
    host.callback_answer = answer
    async with _as(org_admin.admin_email, org_admin.admin_password) as browser:
        resp = await browser.post(f"{MACHINES}/ssh", json=_add())
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert _code(body) == "callback_unreachable"
    assert body["error"]["message"] == message
    assert await _machines(org_admin.org_id) == []
    assert await _endpoints(org_admin.org_id) == []


async def test_a_host_without_systemd_run_and_d_bus_is_refused_and_says_so(
    org_admin: OrgWithAdmin, host: ScriptedHost
) -> None:
    """Each org's worker runs as a transient unit (``systemd-run`` over D-Bus):
    a host without them registers and then can never run a chat."""
    host.facts = "Linux\nx86_64\nyes\nyes\n8\n31\n200\n0\nno\n"
    async with _as(org_admin.admin_email, org_admin.admin_password) as browser:
        tested = await browser.post(f"{MACHINES}/ssh/test", json=_target())
        added = await browser.post(f"{MACHINES}/ssh", json=_add())
    assert (tested.json()["prerequisites_met"], tested.json()["missing"]) == (
        False,
        ["systemd-run with a running D-Bus system bus"],
    )
    assert added.status_code == 422, added.text
    assert added.json()["error"]["message"] == (
        "The host needs systemd-run with a running D-Bus system bus."
    )
    assert await _machines(org_admin.org_id) == []


SANDBOX_REFUSAL = (
    "The host reaches this server at {url} through {ip}, inside the block chats on a "
    "machine use (10.200.0.0/14), so no chat there could reach it. Set {setting} to an "
    "address outside 10.200.0.0/14."
)


@pytest.mark.parametrize(
    ("setting", "url", "answer", "ip"),
    [
        pytest.param(
            "alkera_node_api_url",
            "http://web:8080",
            "addr 10.200.1.7\nok\n",
            "10.200.1.7",
            id="api-name-resolves-into-the-block",
        ),
        pytest.param(
            "alkera_node_api_url",
            "http://10.201.4.2:8080",
            "ok\n",
            "10.201.4.2",
            id="api-literal-in-the-block",
        ),
        pytest.param(
            "alkera_node_gateway_url",
            "http://gateway.internal.example:8081",
            "addr 10.203.255.1\nok\n",
            "10.203.255.1",
            id="gateway-resolves-into-the-block",
        ),
    ],
)
async def test_a_server_inside_the_chat_sandbox_block_is_refused(
    org_admin: OrgWithAdmin,
    host: ScriptedHost,
    monkeypatch: pytest.MonkeyPatch,
    setting: str,
    url: str,
    answer: str,
    ip: str,
) -> None:
    """A chat's egress never reaches the block its sandbox addresses come from,
    so a node could register and still never connect a chat."""
    monkeypatch.setattr(settings, setting, url)
    host.answers_for[f"{url}/health/live"] = answer
    async with _as(org_admin.admin_email, org_admin.admin_password) as browser:
        tested = await browser.post(f"{MACHINES}/ssh/test", json=_target())
        added = await browser.post(f"{MACHINES}/ssh", json=_add())
    name = (
        "ALKERA_NODE_GATEWAY_URL" if setting == "alkera_node_gateway_url" else "ALKERA_NODE_API_URL"
    )
    message = SANDBOX_REFUSAL.format(url=url, ip=ip, setting=name)
    body = tested.json()
    assert (body["prerequisites_met"], body["error_code"], body["message"]) == (
        False,
        "callback_unreachable",
        message,
    )
    assert added.status_code == 422, added.text
    assert added.json()["error"]["message"] == message
    assert await _machines(org_admin.org_id) == []


@pytest.mark.parametrize(
    "answer",
    [
        pytest.param("addr 10.199.255.254\nok\n", id="just-below-the-block"),
        pytest.param("addr 10.204.0.1\nok\n", id="just-above-the-block"),
        pytest.param("addr 172.18.0.4\nok\n", id="a-compose-network"),
    ],
)
async def test_a_server_outside_the_block_passes(
    org_admin: OrgWithAdmin, host: ScriptedHost, monkeypatch: pytest.MonkeyPatch, answer: str
) -> None:
    monkeypatch.setattr(settings, "alkera_node_api_url", "http://web:8080")
    host.answers_for["http://web:8080/health/live"] = answer
    async with _as(org_admin.admin_email, org_admin.admin_password) as browser:
        tested = await browser.post(f"{MACHINES}/ssh/test", json=_target())
    body = tested.json()
    assert (body["prerequisites_met"], body["error_code"], body["message"]) == (True, None, None)


async def test_a_member_may_neither_test_nor_add(
    real_session: AsyncSession, org_admin: OrgWithAdmin, host: ScriptedHost
) -> None:
    member, password = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    assert password is not None
    async with _as(member.email, password) as browser:
        tested = await browser.post(f"{MACHINES}/ssh/test", json=_target())
        added = await browser.post(f"{MACHINES}/ssh", json=_add())
        buying = await browser.get(f"{MACHINES}/buying")
    assert (tested.status_code, added.status_code) == (403, 403)
    assert host.sessions == 0
    assert buying.json()["can_add"] is False
    reasons = [d["reason"] for d in await _decisions(org_admin.org_id)]
    assert reasons.count("add_needs_org_admin") == 2


async def test_a_deployment_that_turned_it_off_refuses_and_says_so(
    org_admin: OrgWithAdmin, host: ScriptedHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with _as(org_admin.admin_email, org_admin.admin_password) as browser:
        on = await browser.get(f"{MACHINES}/buying")
        monkeypatch.setattr(settings, "ssh_machines_enabled", False)
        off = await browser.get(f"{MACHINES}/buying")
        refused = await browser.post(f"{MACHINES}/ssh", json=_add())
    assert (on.json()["can_add"], off.json()["can_add"]) == (True, False)
    assert refused.status_code == 403
    assert _code(refused.json()) == "machine_adding_unavailable"


async def test_the_metadata_address_is_refused_even_where_private_ones_are_allowed(
    org_admin: OrgWithAdmin, host: ScriptedHost
) -> None:
    async with _as(org_admin.admin_email, org_admin.admin_password) as browser:
        resp = await browser.post(f"{MACHINES}/ssh/test", json=_target(host="169.254.169.254"))
    assert resp.status_code == 422, resp.text
    assert _code(resp.json()) == "address_not_allowed"
    assert host.sessions == 0


async def test_a_private_address_is_refused_where_the_deployment_says_so(
    org_admin: OrgWithAdmin, host: ScriptedHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "ssh_machines_allow_private_addresses", False)
    async with _as(org_admin.admin_email, org_admin.admin_password) as browser:
        resp = await browser.post(f"{MACHINES}/ssh/test", json=_target(host="10.1.2.3"))
    assert resp.status_code == 422, resp.text
    assert _code(resp.json()) == "address_not_allowed"


async def test_removing_a_machine_that_never_started_forgets_the_credential(
    org_admin: OrgWithAdmin, host: ScriptedHost
) -> None:
    async with _as(org_admin.admin_email, org_admin.admin_password) as browser:
        added = await browser.post(f"{MACHINES}/ssh", json=_add())
        read = added.json()
        removed = await browser.delete(
            f"{MACHINES}/{read['id']}", headers={"If-Match": str(read["version"])}
        )
    assert removed.status_code == 202, removed.text
    [endpoint] = await _endpoints(org_admin.org_id)
    assert endpoint.secret_sealed == ""


async def test_an_added_machine_stops_and_starts_with_no_credit(
    org_admin: OrgWithAdmin, host: ScriptedHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hosted org whose plan buys no machines and holds no credit still
    stops and starts the machine it runs itself."""
    monkeypatch.setattr(settings, "self_hosted", False)
    for tier in ("free", "plus", "pro", "enterprise"):
        monkeypatch.setattr(settings, f"machine_quota_{tier}", 0)
    async with _as(org_admin.admin_email, org_admin.admin_password) as browser:
        read = (await browser.post(f"{MACHINES}/ssh", json=_add())).json()
        stopped = await browser.post(
            f"{MACHINES}/{read['id']}/stop", headers={"If-Match": str(read["version"])}
        )
        assert stopped.status_code == 202, stopped.text
        started = await browser.post(
            f"{MACHINES}/{read['id']}/start",
            headers={"If-Match": str(stopped.json()["version"])},
        )
    assert started.status_code == 202, started.text
    async with AsyncSessionLocal() as session:
        machine = await session.get(OrgMachine, UUID(read["id"]))
        assert machine is not None and machine.desired_power == "on"


async def test_an_added_machine_does_not_count_toward_the_purchase_quota(
    org_admin: OrgWithAdmin, host: ScriptedHost
) -> None:
    async with _as(org_admin.admin_email, org_admin.admin_password) as browser:
        before = (await browser.get(f"{MACHINES}/buying")).json()
        added = await browser.post(f"{MACHINES}/ssh", json=_add())
        assert added.status_code == 202, added.text
        after = (await browser.get(f"{MACHINES}/buying")).json()
    assert after["used"] == before["used"]
    assert after["can_buy"] == before["can_buy"]


async def test_an_added_machine_has_no_new_hardware_to_move_to(
    org_admin: OrgWithAdmin, host: ScriptedHost
) -> None:
    """Stopped, it is in a state a replace applies to, and is still refused."""
    async with _as(org_admin.admin_email, org_admin.admin_password) as browser:
        read = (await browser.post(f"{MACHINES}/ssh", json=_add())).json()
        async with AsyncSessionLocal() as session:
            machine = await session.get(OrgMachine, UUID(read["id"]))
            assert machine is not None
            machine.desired_power = "off"
            machine.stop_reason = "user"
            alloc = await session.get(ComputeAllocation, machine.current_allocation_id)
            assert alloc is not None
            alloc.state = "asleep"
            await session.commit()
        stopped = (await browser.get(f"{MACHINES}/{read['id']}")).json()
        assert stopped["card"]["state"] == "stopped"
        replaced = await browser.post(
            f"{MACHINES}/{read['id']}/replace", headers={"If-Match": str(stopped["version"])}
        )
    assert read["can_manage"] is True and read["can_replace"] is False
    assert replaced.status_code == 409, replaced.text
    assert _code(replaced.json()) == "machine_not_replaceable"
    assert "own host" in replaced.json()["error"]["message"]


async def test_a_removal_whose_host_never_answers_finishes_after_a_day_and_says_so(
    org_admin: OrgWithAdmin, host: ScriptedHost
) -> None:
    """The real reconcile and the real endpoint store: a removed machine whose
    host stays unreachable is released after 24 hours, its credential is
    deleted, and the allocation and the org's audit record say the node may
    still be on the host and how to remove it."""
    async with _as(org_admin.admin_email, org_admin.admin_password) as browser:
        read = (await browser.post(f"{MACHINES}/ssh", json=_add())).json()
    machine_id = UUID(read["id"])
    [endpoint] = await _endpoints(org_admin.org_id)
    removed_at = datetime.now(UTC) - timedelta(hours=25)
    async with AsyncSessionLocal() as session:
        machine = await session.get(OrgMachine, machine_id)
        assert machine is not None
        machine.deleted_at = removed_at
        machine.desired_power = "off"
        alloc = await session.get(ComputeAllocation, machine.current_allocation_id)
        assert alloc is not None
        alloc.state = "releasing"
        alloc.state_changed_at = removed_at
        alloc.provider_machine_id = str(endpoint.id)
        allocation_id = alloc.id
        await session.commit()

    class Unreachable(ScriptedHost):
        async def host_key(self, target: SshTarget) -> HostKey:
            raise unreachable()

        def session(self, target: SshTarget, **_: Any) -> AbstractAsyncContextManager[Any]:
            raise unreachable()

    provider = SshProvider(enabled=True, allow_private=True, transport=Unreachable())
    async with AsyncSessionLocal() as session:
        # Only the ssh rows reach the provider; any other row this worker's
        # database holds is answered by a provider that refuses, and left alone.
        await reconcile_nodes(
            session, providers=lambda kind: provider if kind == "ssh" else PersonalProvider()
        )
    async with AsyncSessionLocal() as session:
        alloc = await session.get(ComputeAllocation, allocation_id)
        assert alloc is not None
        assert alloc.state == "released"
        assert "may still be installed" in alloc.error
        audit = (
            await session.execute(
                select(OrgAuditEvent).where(
                    OrgAuditEvent.org_team_id == org_admin.org_id,
                    OrgAuditEvent.action == "machine.removal_abandoned",
                )
            )
        ).scalar_one()
        assert audit.target == str(machine_id)
        assert "systemctl disable --now alkera-node.service" in str(audit.detail)
        assert PASSWORD not in str(audit.detail)
        row = await session.get(SshMachineEndpoint, endpoint.id)
        assert row is not None and row.secret_sealed == ""


# --------------------------------------------------------------------------- #
# the node bundle for the host's architecture
# --------------------------------------------------------------------------- #

ARM_ONLY_REFUSAL = (
    "This server is x86_64, but this install only has the arm64 machine bundle. "
    "Rebuild the backend and worker images with NODE_BUNDLE_ARCHES=all, or build the "
    "linux-x64 bundle into NODE_BUNDLE_DIR on an amd64 computer."
)


def _serve_bundles(tmp: Path, monkeypatch: pytest.MonkeyPatch, *targets: str) -> None:
    entries = {}
    for target in targets:
        body = f"bundle {target}".encode()
        name = f"alkera-{target}.tar.gz"
        (tmp / name).write_bytes(body)
        entries[target] = {"file": name, "sha256": hashlib.sha256(body).hexdigest(), "size": 1}
    (tmp / "manifest.json").write_text(
        json.dumps({"version": "1.0.0+t", "targets": entries}), encoding="utf-8"
    )
    monkeypatch.setattr(settings, "node_bundle_dir", str(tmp))


X64_ONLY_REFUSAL = (
    "This server is aarch64, but this install only has the amd64 machine bundle. "
    "Rebuild the backend and worker images with NODE_BUNDLE_ARCHES=all, or build the "
    "linux-arm64 bundle into NODE_BUNDLE_DIR on an arm64 computer."
)
NO_BUNDLE_REFUSAL = (
    "This server is x86_64, but this install has no machine bundle. "
    "Rebuild the backend and worker images with NODE_BUNDLE_ARCHES=all, or build the "
    "linux-x64 bundle into NODE_BUNDLE_DIR on an amd64 computer."
)


@pytest.mark.parametrize(
    ("facts_arch", "held", "refusal"),
    [
        pytest.param("x86_64", ("linux-arm64",), ARM_ONLY_REFUSAL, id="x86-host-arm-only"),
        pytest.param("aarch64", ("linux-x64",), X64_ONLY_REFUSAL, id="arm-host-x64-only"),
        pytest.param("x86_64", (), NO_BUNDLE_REFUSAL, id="empty-bundle-dir"),
        pytest.param("x86_64", None, NO_BUNDLE_REFUSAL, id="missing-bundle-dir"),
    ],
)
async def test_a_host_whose_architecture_has_no_bundle_fails_the_test_and_the_add(
    org_admin: OrgWithAdmin,
    host: ScriptedHost,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    facts_arch: str,
    held: tuple[str, ...] | None,
    refusal: str,
) -> None:
    if held is None:
        monkeypatch.setattr(settings, "node_bundle_dir", str(tmp_path / "never-built"))
    else:
        _serve_bundles(tmp_path, monkeypatch, *held)
    host.facts = LINUX.replace("x86_64", facts_arch)
    async with _as(org_admin.admin_email, org_admin.admin_password) as browser:
        tested = await browser.post(f"{MACHINES}/ssh/test", json=_target())
        added = await browser.post(f"{MACHINES}/ssh", json=_add())
    assert tested.status_code == 200, tested.text
    found = tested.json()
    assert (found["reachable"], found["arch"], found["prerequisites_met"]) == (
        True,
        facts_arch,
        False,
    )
    assert (found["error_code"], found["message"]) == ("no_node_bundle", refusal)
    assert added.status_code == 422, added.text
    assert _code(added.json()) == "no_node_bundle"
    assert added.json()["error"]["message"] == refusal
    assert await _machines(org_admin.org_id) == []
    assert await _endpoints(org_admin.org_id) == []


@pytest.mark.parametrize(
    ("facts_arch", "held"),
    [
        pytest.param("x86_64", ("linux-x64",), id="x86-host-x64-bundle"),
        pytest.param("aarch64", ("linux-arm64",), id="arm-host-arm-bundle"),
        pytest.param("x86_64", ("linux-x64", "linux-arm64"), id="x86-host-both"),
        pytest.param("aarch64", ("linux-x64", "linux-arm64"), id="arm-host-both"),
    ],
)
async def test_a_host_whose_architecture_has_a_bundle_passes_and_is_added(
    org_admin: OrgWithAdmin,
    host: ScriptedHost,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    facts_arch: str,
    held: tuple[str, ...],
) -> None:
    _serve_bundles(tmp_path, monkeypatch, *held)
    host.facts = LINUX.replace("x86_64", facts_arch)
    async with _as(org_admin.admin_email, org_admin.admin_password) as browser:
        tested = await browser.post(f"{MACHINES}/ssh/test", json=_target())
        added = await browser.post(f"{MACHINES}/ssh", json=_add())
    found = tested.json()
    assert (found["arch"], found["prerequisites_met"], found["error_code"]) == (
        facts_arch,
        True,
        None,
    )
    assert added.status_code == 202, added.text
    [endpoint] = await _endpoints(org_admin.org_id)
    assert endpoint.arch == facts_arch


async def test_a_start_the_workers_bundles_cannot_serve_says_why_on_the_machine_read(
    org_admin: OrgWithAdmin,
    host: ScriptedHost,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The backend that accepted the host holds its bundle, the worker that
    starts it does not (images built apart). The real org-machine reconcile
    records the provider's refusal, and the machine read the page renders says
    it, not a bare "Couldn't start"."""
    backend_dir, worker_dir = tmp_path / "backend", tmp_path / "worker"
    backend_dir.mkdir()
    worker_dir.mkdir()
    _serve_bundles(backend_dir, monkeypatch, "linux-x64")
    async with _as(org_admin.admin_email, org_admin.admin_password) as browser:
        added = await browser.post(f"{MACHINES}/ssh", json=_add(name="Rack x86"))
    assert added.status_code == 202, added.text
    machine_id = UUID(added.json()["id"])
    _serve_bundles(worker_dir, monkeypatch, "linux-arm64")

    provider = SshProvider(
        enabled=True, allow_private=True, transport=host, bundle_dir=str(worker_dir)
    )

    async def no_sleep(_seconds: float) -> None:
        return None

    async with AsyncSessionLocal() as session:
        await reconcile_org_machines(
            session,
            providers=lambda _kind: provider,
            config=settings,
            sleep=no_sleep,
            only=[machine_id],
        )
    async with AsyncSessionLocal() as session:
        machine = await session.get(OrgMachine, machine_id)
        assert machine is not None
        alloc = await session.get(ComputeAllocation, machine.current_allocation_id)
        assert alloc is not None
        assert (alloc.state, alloc.error) == ("failed", ARM_ONLY_REFUSAL)

    async with _as(org_admin.admin_email, org_admin.admin_password) as browser:
        read = await browser.get(f"{MACHINES}/{machine_id}")
    assert read.status_code == 200, read.text
    body = read.json()
    status = body["card"]["status"]
    assert (status["state"], status["reason_code"]) == ("failed", "start_failed")
    assert (
        status["sentence"] == f'Rack x86 couldn\'t start. The last error was "{ARM_ONLY_REFUSAL}".'
    )
    assert f"Couldn't start: {ARM_ONLY_REFUSAL}" in [entry["words"] for entry in body["timeline"]]
