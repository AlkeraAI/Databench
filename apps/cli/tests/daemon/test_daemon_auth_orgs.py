"""The daemon's view of profiles and orgs: ``auth.status`` per project,
``auth.listOrgs``, ``auth.switchOrg``, and the pinned-project refusal.

Driven in-process over piped streams like the rest of the daemon suite. The
backend is faked at its edges: ``/auth/me`` per token (the org it answers for is
the token's own) and the memberships route behind the real client.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from _profiles import API, ORG_A, ORG_B, USER, make_jwt, store
from alkera_cli.account import auth_file, device_flow, memberships, session
from alkera_cli.account.session import CurrentUser
from alkera_cli.daemon import (
    JsonRpcServer,
    PipeWriter,
    connect_pipe_reader,
    connect_pipe_writer,
    read_frame_async,
    write_frame_async,
)
from alkera_cli.daemon import methods as _register_methods  # noqa: F401
from alkera_cli.daemon.server import AUTH_REQUIRED, INTERNAL_ERROR, PROFILE_REFUSED
from alkera_core.project.cloud_binding import CloudBinding
from alkera_core.project.directory import ProjectDirectory

TOKEN_A = make_jwt(org=ORG_A, exp=4102444800)
TOKEN_B = make_jwt(org=ORG_B, exp=4102444800)
ORG_C = "33333333-3333-4333-8333-333333333333"
_BUDGET_S = 20.0


async def _streams() -> tuple[asyncio.StreamReader, PipeWriter, asyncio.StreamReader, PipeWriter]:
    c2s_r, c2s_w = os.pipe()
    server_reader = await connect_pipe_reader(os.fdopen(c2s_r, "rb", buffering=0))
    client_writer = await connect_pipe_writer(os.fdopen(c2s_w, "wb", buffering=0))
    s2c_r, s2c_w = os.pipe()
    client_reader = await connect_pipe_reader(os.fdopen(s2c_r, "rb", buffering=0))
    server_writer = await connect_pipe_writer(os.fdopen(s2c_w, "wb", buffering=0))
    return server_reader, server_writer, client_reader, client_writer


class _Client:
    def __init__(self, reader: asyncio.StreamReader, writer: PipeWriter) -> None:
        self._reader = reader
        self._writer = writer
        self._next = 0
        self.notifications: list[dict[str, Any]] = []

    async def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._next += 1
        rid = self._next
        envelope = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}}
        await write_frame_async(self._writer, json.dumps(envelope).encode())
        return await self.until(lambda m: m.get("id") == rid)

    async def until(self, predicate: Callable[[dict[str, Any]], bool]) -> dict[str, Any]:
        deadline = time.monotonic() + _BUDGET_S
        while True:
            remaining = max(0.01, deadline - time.monotonic())
            frame = await asyncio.wait_for(read_frame_async(self._reader), timeout=remaining)
            msg = json.loads(frame.decode())
            if "id" not in msg:
                self.notifications.append(msg)
            if predicate(msg):
                return msg


@pytest.fixture
async def daemon() -> AsyncIterator[_Client]:
    sr, sw, cr, cw = await _streams()
    server = JsonRpcServer(reader=sr, writer=sw)
    task = asyncio.create_task(server.serve())
    try:
        yield _Client(cr, cw)
    finally:
        server.request_shutdown()
        cw.close()
        await asyncio.wait_for(task, timeout=_BUDGET_S)


@pytest.fixture
def backend(monkeypatch: pytest.MonkeyPatch) -> list[httpx.Request]:
    users = {
        TOKEN_A: CurrentUser(
            id=USER, email="a@x.com", display_name="A", org_team_id=ORG_A, org_name="Acme"
        ),
        TOKEN_B: CurrentUser(
            id=USER, email="a@x.com", display_name="A", org_team_id=ORG_B, org_name="Bravo"
        ),
    }
    monkeypatch.setattr(session, "resolve_user", lambda _api, token, **_kw: users.get(token))
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "active_org_team_id": request.headers.get("x-alkera-org"),
                "memberships": [
                    {"org_team_id": ORG_A, "org_name": "Acme", "role": "admin"},
                    {"org_team_id": ORG_B, "org_name": "Bravo", "role": "member"},
                    {
                        "org_team_id": ORG_C,
                        "org_name": "Coda",
                        "role": "member",
                        "sso_required": True,
                    },
                ],
            },
        )

    real = memberships.fetch_memberships
    monkeypatch.setattr(
        memberships,
        "fetch_memberships",
        lambda profile, **_kw: real(profile, transport=httpx.MockTransport(handler)),
    )
    return seen


def _pinned(tmp_path: Path, name: str, org: str, org_name: str) -> str:
    root = tmp_path / name
    ProjectDirectory(root / ".alkera").cloud_binding().write(
        CloudBinding(api_url=API, org_team_id=org, org_name=org_name)
    )
    return str(root)


def _other_sign_in() -> auth_file.Profile:
    """A stored sign-in the editor is not acting as, with an email and key of
    its own, so any trace of it on the wire is unambiguous."""
    email = "someone-else@other.test"
    return store(ORG_C, org_name="Coda", email=email, token=make_jwt(org=ORG_C, email=email))


def _carries_no_sign_in_list(payload: dict[str, Any], other: auth_file.Profile) -> None:
    wire = json.dumps(payload)
    assert "profiles" not in payload
    assert other.email not in wire and other.key not in wire
    assert "token" not in wire, "no token crosses the wire"


async def test_status_carries_the_org_it_acts_in_and_no_list_of_sign_ins(
    daemon: _Client, backend: list[httpx.Request]
) -> None:
    """The editor learns which sign-in it acts as; it is never handed every
    stored sign-in's email and key, which it would only post on to a webview."""
    store(ORG_A, org_name="Acme", current=True, token=TOKEN_A)
    other = _other_sign_in()

    result = (await daemon.call("auth.status"))["result"]

    assert result["authenticated"] is True
    assert (result["org_team_id"], result["org_name"], result["email"]) == (
        ORG_A,
        "Acme",
        "a@x.com",
    )
    _carries_no_sign_in_list(result, other)


async def test_auth_changed_carries_no_list_of_sign_ins(
    daemon: _Client, backend: list[httpx.Request]
) -> None:
    store(ORG_A, org_name="Acme", current=True, token=TOKEN_A)
    store(ORG_B, org_name="Bravo", token=TOKEN_B)
    other = _other_sign_in()

    await daemon.call("auth.switchOrg", {"org_team_id": ORG_B})

    changed = [n for n in daemon.notifications if n.get("method") == "auth.changed"]
    assert changed and changed[-1]["params"]["org_team_id"] == ORG_B
    _carries_no_sign_in_list(changed[-1]["params"], other)


async def test_two_projects_pinned_to_two_orgs_report_their_own_org(
    daemon: _Client, backend: list[httpx.Request], tmp_path: Path
) -> None:
    store(ORG_A, org_name="Acme", current=True, token=TOKEN_A)
    store(ORG_B, org_name="Bravo", token=TOKEN_B)
    on_a = _pinned(tmp_path, "proj-a", ORG_A, "Acme")
    on_b = _pinned(tmp_path, "proj-b", ORG_B, "Bravo")

    first = (await daemon.call("auth.status", {"project_path": on_a}))["result"]
    second = (await daemon.call("auth.status", {"project_path": on_b}))["result"]

    assert first["org_team_id"] == ORG_A
    assert second["org_team_id"] == ORG_B


async def test_a_project_pinned_to_an_org_without_a_sign_in_reads_as_refused(
    daemon: _Client, backend: list[httpx.Request], tmp_path: Path
) -> None:
    store(ORG_A, org_name="Acme", current=True, token=TOKEN_A)
    on_b = _pinned(tmp_path, "proj-b", ORG_B, "Bravo")

    result = (await daemon.call("auth.status", {"project_path": on_b}))["result"]

    assert result["authenticated"] is False
    assert result["reason"] == "invalid", "a reason every editor build already handles"
    assert result["failure"] == "org_refused"
    assert result["detail"] == "This project belongs to Bravo. Run `alkera org switch Bravo`."


async def test_a_project_method_on_a_refused_project_answers_the_refusal_code(
    daemon: _Client, backend: list[httpx.Request], tmp_path: Path
) -> None:
    store(ORG_A, org_name="Acme", current=True, token=TOKEN_A)
    on_b = _pinned(tmp_path, "proj-b", ORG_B, "Bravo")

    reply = await daemon.call("usage.get", {"project_path": on_b})

    assert reply["error"]["code"] == PROFILE_REFUSED
    assert (
        reply["error"]["message"] == "This project belongs to Bravo. Run `alkera org switch Bravo`."
    )


async def test_list_orgs_reads_the_memberships_as_the_projects_profile(
    daemon: _Client, backend: list[httpx.Request]
) -> None:
    store(ORG_A, org_name="Acme", current=True, token=TOKEN_A)
    store(ORG_B, org_name="Bravo", token=TOKEN_B)

    orgs = (await daemon.call("auth.listOrgs"))["result"]["orgs"]

    by_id = {o["org_team_id"]: o for o in orgs}
    assert by_id[ORG_A]["current"] is True and by_id[ORG_A]["stored"] is True
    assert by_id[ORG_B]["current"] is False and by_id[ORG_B]["stored"] is True
    coda = by_id[ORG_C]
    assert coda["stored"] is False and coda["sso_required"] is True
    assert backend[0].headers["authorization"] == f"Bearer {TOKEN_A}"


def _failing_backend(monkeypatch: pytest.MonkeyPatch, answer: int | None) -> None:
    """The memberships route answering ``answer``, or unreachable when None."""
    users = {
        TOKEN_A: CurrentUser(
            id=USER, email="a@x.com", display_name="A", org_team_id=ORG_A, org_name="Acme"
        )
    }
    monkeypatch.setattr(session, "resolve_user", lambda _api, token, **_kw: users.get(token))

    def handler(request: httpx.Request) -> httpx.Response:
        if answer is None:
            raise httpx.ConnectError("connection refused", request=request)
        return httpx.Response(answer, json={"detail": "nope"})

    real = memberships.fetch_memberships
    monkeypatch.setattr(
        memberships,
        "fetch_memberships",
        lambda profile, **_kw: real(profile, transport=httpx.MockTransport(handler)),
    )


@pytest.mark.parametrize(
    ("answer", "signed_out"),
    [
        pytest.param(401, True, id="401-the-sign-in-is-gone"),
        pytest.param(403, False, id="403"),
        pytest.param(404, False, id="404-an-older-backend"),
        pytest.param(409, False, id="409"),
        pytest.param(500, False, id="500"),
        pytest.param(503, False, id="503"),
        pytest.param(None, False, id="offline"),
    ],
)
async def test_list_orgs_signs_the_editor_out_only_on_a_401(
    daemon: _Client, monkeypatch: pytest.MonkeyPatch, answer: int | None, signed_out: bool
) -> None:
    """Only a 401 means the stored sign-in is gone. A blip, an older server or a
    5xx answers an ordinary error, so the editor keeps its sign-in instead of
    showing the login panel, and the stored profile is untouched."""
    store(ORG_A, org_name="Acme", current=True, token=TOKEN_A)
    _failing_backend(monkeypatch, answer)

    reply = await daemon.call("auth.listOrgs")

    assert "result" not in reply
    if signed_out:
        assert reply["error"]["code"] == AUTH_REQUIRED
        assert reply["error"]["data"] == {"reason": "rejected"}
    else:
        assert reply["error"]["code"] == INTERNAL_ERROR
        assert "auth required" not in reply["error"]["message"]
        assert reply["error"]["message"][:1].isupper(), "the sentence starts capitalised"
    stored = auth_file.load_profiles()
    assert stored is not None and stored.current_profile is not None
    assert stored.current_profile.token == TOKEN_A
    assert not [n for n in daemon.notifications if n.get("method") == "auth.changed"]


async def test_switch_org_to_a_stored_profile_makes_it_current_and_announces_it(
    daemon: _Client, backend: list[httpx.Request]
) -> None:
    store(ORG_A, org_name="Acme", current=True, token=TOKEN_A)
    b = store(ORG_B, org_name="Bravo", token=TOKEN_B)

    result = (await daemon.call("auth.switchOrg", {"org_team_id": ORG_B}))["result"]

    assert result["switched"] is True
    assert result["status"]["org_team_id"] == ORG_B
    after = auth_file.load_profiles()
    assert after is not None and after.current == b.key
    changed = [n for n in daemon.notifications if n.get("method") == "auth.changed"]
    assert changed and changed[-1]["params"]["org_team_id"] == ORG_B


def _no_device_login(monkeypatch: pytest.MonkeyPatch) -> None:
    """A device login here would be a wrong answer: make it loud, not a network call."""

    def _refuse(*_a: object, **_k: object) -> device_flow.DeviceCodeResponse:
        raise AssertionError("a stored sign-in needs no device login")

    monkeypatch.setattr(device_flow, "request_device_code", _refuse)


async def test_switch_org_by_a_name_one_stored_sign_in_carries_switches_to_it(
    daemon: _Client, backend: list[httpx.Request], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The daemon names a stored sign-in by the same rule as ``alkera org
    switch``: an unambiguous name is as good as the id."""
    _no_device_login(monkeypatch)
    store(ORG_A, org_name="Acme", current=True, token=TOKEN_A)
    b = store(ORG_B, org_name="Bravo", token=TOKEN_B)

    result = (await daemon.call("auth.switchOrg", {"org_team_id": "bravo"}))["result"]

    assert result["switched"] is True
    after = auth_file.load_profiles()
    assert after is not None and after.current == b.key


async def test_switch_org_by_a_name_two_sign_ins_share_answers_the_refusal(
    daemon: _Client, backend: list[httpx.Request], monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_device_login(monkeypatch)
    a = store(ORG_A, org_name="Acme", current=True, token=TOKEN_A)
    store(ORG_B, org_name="ACME", token=TOKEN_B)

    reply = await daemon.call("auth.switchOrg", {"org_team_id": "acme"})

    assert reply["error"]["code"] == PROFILE_REFUSED
    assert (
        "More than one organization you are signed in to is named acme"
        in (reply["error"]["message"])
    )
    after = auth_file.load_profiles()
    assert after is not None and after.current == a.key


async def test_switch_org_without_a_sign_in_starts_a_device_login_for_it(
    daemon: _Client, backend: list[httpx.Request], monkeypatch: pytest.MonkeyPatch
) -> None:
    store(ORG_A, org_name="Acme", current=True, token=TOKEN_A)
    before = auth_file.AUTH_FILE_PATH.read_bytes()
    monkeypatch.setattr(
        device_flow,
        "request_device_code",
        lambda *_a, **_k: device_flow.DeviceCodeResponse(
            device_code="dc",
            user_code="ABCD-1234",
            verification_uri="https://app.example.test/device",
            verification_uri_complete="https://app.example.test/device?user_code=ABCD-1234",
            expires_in=600,
            interval=1,
        ),
    )
    monkeypatch.setattr(
        device_flow,
        "poll_for_token",
        lambda *_a, should_stop, **_k: _wait_until_stopped(should_stop),
    )

    result = (await daemon.call("auth.switchOrg", {"org_team_id": ORG_B}))["result"]
    await daemon.call("auth.cancelDeviceLogin")

    assert result["switched"] is False
    assert result["login"]["verification_uri_complete"].endswith(f"&org={ORG_B}")
    assert auth_file.AUTH_FILE_PATH.read_bytes() == before, "nothing changes until approval"


def _wait_until_stopped(should_stop: Callable[[], bool]) -> str:
    """A device approval that never comes: the poll waits for its stop flag,
    bounded so a poll the daemon failed to stop cannot outlive the test."""
    deadline = time.monotonic() + 5.0
    while not should_stop():
        if time.monotonic() > deadline:
            raise AssertionError("the daemon never stopped this device login")
        time.sleep(0.01)
    raise device_flow.DeviceLoginCancelledError("cancelled")


async def test_start_device_login_preselects_the_asked_for_org(
    daemon: _Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        device_flow,
        "request_device_code",
        lambda *_a, **_k: device_flow.DeviceCodeResponse(
            device_code="dc",
            user_code="ABCD-1234",
            verification_uri="https://app.example.test/device",
            verification_uri_complete="https://app.example.test/device?user_code=ABCD-1234",
            expires_in=600,
            interval=1,
        ),
    )
    monkeypatch.setattr(
        device_flow,
        "poll_for_token",
        lambda *_a, should_stop, **_k: _wait_until_stopped(should_stop),
    )
    with_org = (await daemon.call("auth.startDeviceLogin", {"org_team_id": ORG_A}))["result"]
    without = (await daemon.call("auth.startDeviceLogin"))["result"]
    await daemon.call("auth.cancelDeviceLogin")

    assert with_org["verification_uri_complete"].endswith(f"&org={ORG_A}")
    assert "org=" not in without["verification_uri_complete"]


async def test_logout_signs_out_of_the_current_org_and_reports_the_next(
    daemon: _Client, backend: list[httpx.Request], monkeypatch: pytest.MonkeyPatch
) -> None:
    revoked: list[str] = []
    monkeypatch.setattr(
        session, "revoke_session", lambda _api, token, **_kw: revoked.append(token) or True
    )
    store(ORG_A, org_name="Acme", current=True, token=TOKEN_A)
    b = store(ORG_B, org_name="Bravo", token=TOKEN_B)

    status = (await daemon.call("auth.logout"))["result"]["status"]

    assert revoked == [TOKEN_A]
    assert status["authenticated"] is True and status["org_team_id"] == ORG_B
    after = auth_file.load_profiles()
    assert after is not None and [p.key for p in after.profiles] == [b.key]
