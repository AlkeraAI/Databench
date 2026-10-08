"""Tests for the daemon's auth methods + notifications.

We drive the daemon in-process via piped streams (same pattern as
``test_daemon.py``), and isolate ~/.alkera/ to a tmp dir via the
``_isolate_home`` helper so the real user's auth file is never touched.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import http.server
import json
import os
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from alkera_cli.account import auth_file, device_flow
from alkera_cli.account.auth_file import StoredAuth
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
from alkera_cli.daemon.server import (
    AUTH_REQUIRED,
    AuthRequiredError,
)
from alkera_cli.host import paths

#: How long a test waits for one daemon reply or for shutdown. Well above the
#: preferences / instructions writers' own patience: a 2 s lock retry plus the
#: NTFS sharing-violation backoff behind every atomic write, which a loaded
#: Windows shard does spend. The suite-wide 90 s timeout still catches a hang.
_REPLY_BUDGET_S = 20.0


def _isolate_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    home = tmp_path / "alkera-home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(paths, "ALKERA_HOME", home)
    monkeypatch.setattr(paths, "AUTH_FILE_PATH", home / "auth.yml")
    monkeypatch.setattr(auth_file, "AUTH_FILE_PATH", home / "auth.yml")
    return home


def _make_jwt(*, exp: datetime | None = None) -> str:
    """Manufacture a minimally-shaped JWT so jwt_expires_at can decode `exp`."""
    if exp is None:
        exp = datetime.now(UTC) + timedelta(days=30)
    header = base64.urlsafe_b64encode(b'{"alg":"HS256"}').rstrip(b"=").decode()
    payload = (
        base64.urlsafe_b64encode(json.dumps({"exp": int(exp.timestamp()), "sub": "u1"}).encode())
        .rstrip(b"=")
        .decode()
    )
    sig = base64.urlsafe_b64encode(b"sig").rstrip(b"=").decode()
    return f"{header}.{payload}.{sig}"


# ---------------------------------------------------------------------------
# Daemon stream plumbing — mirrors test_daemon.py exactly.
# ---------------------------------------------------------------------------


async def _connected_streams() -> tuple[
    asyncio.StreamReader,
    PipeWriter,
    asyncio.StreamReader,
    PipeWriter,
]:
    # Through ``daemon.pipes`` so each platform runs its production transport
    # (raw connect_read_pipe never delivers on the Windows ProactorEventLoop).
    c2s_r, c2s_w = os.pipe()
    server_reader = await connect_pipe_reader(os.fdopen(c2s_r, "rb", buffering=0))
    client_writer = await connect_pipe_writer(os.fdopen(c2s_w, "wb", buffering=0))
    s2c_r, s2c_w = os.pipe()
    client_reader = await connect_pipe_reader(os.fdopen(s2c_r, "rb", buffering=0))
    server_writer = await connect_pipe_writer(os.fdopen(s2c_w, "wb", buffering=0))
    return server_reader, server_writer, client_reader, client_writer


async def _send(writer: PipeWriter, envelope: dict[str, Any]) -> None:
    await write_frame_async(writer, json.dumps(envelope).encode("utf-8"))


async def _recv(reader: asyncio.StreamReader) -> dict[str, Any]:
    return json.loads((await read_frame_async(reader)).decode("utf-8"))


async def _recv_until(
    reader: asyncio.StreamReader,
    predicate,
    *,
    timeout: float = _REPLY_BUDGET_S,  # noqa: ASYNC109 — test helper; explicit timeout reads clearer than asyncio.timeout()
) -> dict[str, Any]:
    """Pull frames until one matches; drops the rest. Useful for tests
    that fire both a method response AND a notification but only care
    about one of them in a given assertion."""
    deadline = time.monotonic() + timeout
    while True:
        remaining = max(0.01, deadline - time.monotonic())
        msg = await asyncio.wait_for(_recv(reader), timeout=remaining)
        if predicate(msg):
            return msg


@pytest.fixture
async def daemon(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    _isolate_home(monkeypatch, tmp_path)
    # Speed up the auth watcher so cross-process tests don't take forever.
    from alkera_cli.account import auth_watcher

    monkeypatch.setattr(
        auth_watcher.AuthFileWatcher.__init__,
        "__defaults__",
        (0.05,),  # poll_interval_seconds default
    )

    sr, sw, cr, cw = await _connected_streams()
    server = JsonRpcServer(reader=sr, writer=sw)
    task = asyncio.create_task(server.serve())

    async def stop() -> None:
        server.request_shutdown()
        cw.close()
        await asyncio.wait_for(task, timeout=_REPLY_BUDGET_S)

    try:
        yield server, cw, cr, stop
    finally:
        if not task.done():
            await stop()


# ---------------------------------------------------------------------------
# auth.status
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auth_status_missing(daemon):
    _server, cw, cr, stop = daemon
    await _send(cw, {"jsonrpc": "2.0", "id": 1, "method": "auth.status", "params": {}})
    resp = await _recv_until(cr, lambda m: m.get("id") == 1)
    assert resp["result"]["authenticated"] is False
    assert resp["result"]["reason"] == "missing"
    assert resp["result"]["email"] is None
    await stop()


@pytest.mark.asyncio
async def test_auth_status_carries_the_resolved_frontend_url(daemon):
    """Every auth.status response stamps the config-resolved web-app URL (used by
    the editor's "open web app" button) — present even when unauthenticated."""
    from alkera_cli.host.config import get_settings

    _server, cw, cr, stop = daemon
    await _send(cw, {"jsonrpc": "2.0", "id": 1, "method": "auth.status", "params": {}})
    resp = await _recv_until(cr, lambda m: m.get("id") == 1)
    assert resp["result"]["frontend_url"] == get_settings().alkera_frontend_url.rstrip("/")
    await stop()


@pytest.mark.asyncio
async def test_auth_status_authenticated(daemon, monkeypatch: pytest.MonkeyPatch):
    """When auth.yml exists AND the cloud accepts the token, we report
    authenticated + the email from /auth/me."""
    _server, cw, cr, stop = daemon
    auth_file.save_auth(
        StoredAuth(
            api_url="http://localhost:8000",
            token=_make_jwt(),
            expires_at=datetime.now(UTC) + timedelta(days=30),
        )
    )
    from alkera_cli.daemon.methods import auth as auth_methods

    monkeypatch.setattr(
        auth_methods,
        "_compute_status",
        lambda *_args: auth_methods.AuthStatusResponse(
            authenticated=True,
            email="user@example.com",
            api_url="http://localhost:8000",
            expires_at=None,
            reason=None,
        ),
    )

    await _send(cw, {"jsonrpc": "2.0", "id": 2, "method": "auth.status", "params": {}})
    resp = await _recv_until(cr, lambda m: m.get("id") == 2)
    assert resp["result"]["authenticated"] is True
    assert resp["result"]["email"] == "user@example.com"
    assert resp["result"]["reason"] is None
    await stop()


@pytest.mark.asyncio
async def test_auth_status_unverified_email_reads_as_signed_out(
    daemon, monkeypatch: pytest.MonkeyPatch
):
    """A stored token whose account never verified its email reports NOT
    authenticated with the `email_verification_required` reason — the editor
    shows the login panel (which links the verification page) instead of
    opening chat surfaces the gateway would refuse anyway. The email rides
    along so the panel can name the account."""
    _server, cw, cr, stop = daemon
    auth_file.save_auth(
        StoredAuth(
            api_url="http://localhost:8000",
            token=_make_jwt(),
            expires_at=datetime.now(UTC) + timedelta(days=30),
        )
    )
    from alkera_cli.account import session

    monkeypatch.setattr(
        session,
        "resolve_user",
        lambda _api, _token, **_kw: session.CurrentUser(
            id="u1",
            email="unverified@alkera.dev",
            display_name="U User",
            email_verification_required=True,
        ),
    )

    await _send(cw, {"jsonrpc": "2.0", "id": 3, "method": "auth.status", "params": {}})
    resp = await _recv_until(cr, lambda m: m.get("id") == 3)
    assert resp["result"]["authenticated"] is False
    assert resp["result"]["reason"] == "email_verification_required"
    assert resp["result"]["email"] == "unverified@alkera.dev"
    # The stored token is left in place — once verified, the next status
    # flips clean without another login.
    assert auth_file.load_auth() is not None
    await stop()


# ---------------------------------------------------------------------------
# auth.logout
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auth_logout_revokes_backend_deletes_file_and_emits_notification(
    daemon, monkeypatch: pytest.MonkeyPatch
):
    _server, cw, cr, stop = daemon
    token = _make_jwt()
    auth_file.save_auth(
        StoredAuth(
            api_url="http://localhost:8000",
            token=token,
            expires_at=datetime.now(UTC) + timedelta(days=30),
        )
    )

    # Stub the backend revoke so logout makes no real network call.
    revoke_calls: list[tuple[str, str]] = []

    def _fake_revoke(api_url: str, tok: str, **_kw: object) -> bool:
        revoke_calls.append((api_url, tok))
        return True

    monkeypatch.setattr("alkera_cli.account.session.revoke_session", _fake_revoke)

    await _send(cw, {"jsonrpc": "2.0", "id": 3, "method": "auth.logout", "params": {}})

    # Expect both an auth.changed notification AND the response.
    saw_response = False
    saw_notification = False
    deadline = time.monotonic() + _REPLY_BUDGET_S
    while not (saw_response and saw_notification) and time.monotonic() < deadline:
        msg = await asyncio.wait_for(_recv(cr), timeout=_REPLY_BUDGET_S)
        if msg.get("id") == 3:
            assert msg["result"]["status"]["authenticated"] is False
            assert msg["result"]["status"]["reason"] == "missing"
            saw_response = True
        elif msg.get("method") == "auth.changed":
            assert msg["params"]["authenticated"] is False
            saw_notification = True
    assert saw_response and saw_notification
    assert not paths.AUTH_FILE_PATH.exists()
    # Best-effort server-side revoke was attempted (off-thread) with the
    # stored credentials before the local file was forgotten.
    assert revoke_calls == [("http://localhost:8000", token)]
    await stop()


# ---------------------------------------------------------------------------
# auth.startDeviceLogin / auth.cancelDeviceLogin (RFC 8628 device flow)
# ---------------------------------------------------------------------------


def _device(device_code: str = "dev-code", user_code: str = "WXYZ-1234") -> Any:
    return device_flow.DeviceCodeResponse(
        device_code=device_code,
        user_code=user_code,
        verification_uri="https://app.alkera.test/device",
        verification_uri_complete=f"https://app.alkera.test/device?user_code={user_code}",
        expires_in=600,
        interval=1,
    )


def _stub_gateway(monkeypatch: pytest.MonkeyPatch, *, error: Exception | None = None) -> list[str]:
    """Stand in for the gateway catalog fetch the login check makes. Records the
    tokens it was shown; raises ``error`` when given, else accepts the token."""
    from alkera_cli.gateway import client as gateway_client

    seen: list[str] = []

    async def _fetch(*, gateway_url: str, token: str, **_kw: object) -> list[object]:
        seen.append(token)
        if error is not None:
            raise error
        return []

    monkeypatch.setattr(gateway_client, "fetch_models", _fetch)
    return seen


def _blocking_poll(*_args: object, **kwargs: object):
    """Stand-in for poll_for_token that never resolves until cancelled, so the
    login stays 'pending'. Honors the cooperative should_stop flag."""
    should_stop = kwargs.get("should_stop")
    while not (callable(should_stop) and should_stop()):
        time.sleep(0.01)
    raise device_flow.DeviceLoginCancelledError("cancelled")


@pytest.mark.asyncio
async def test_auth_start_device_login_returns_code_and_urls(
    daemon, monkeypatch: pytest.MonkeyPatch
):
    server, cw, cr, stop = daemon
    monkeypatch.setattr(device_flow, "request_device_code", lambda *a, **k: _device())
    monkeypatch.setattr(device_flow, "poll_for_token", _blocking_poll)

    await _send(cw, {"jsonrpc": "2.0", "id": 4, "method": "auth.startDeviceLogin", "params": {}})
    resp = await _recv_until(cr, lambda m: m.get("id") == 4)
    body = resp["result"]
    assert body["user_code"] == "WXYZ-1234"
    assert body["verification_uri"] == "https://app.alkera.test/device"
    assert body["verification_uri_complete"].endswith("user_code=WXYZ-1234")
    assert body["interval"] == 1
    assert body["expires_in"] == 600

    # Cancel tears the pending poll down.
    await _send(cw, {"jsonrpc": "2.0", "id": 5, "method": "auth.cancelDeviceLogin", "params": {}})
    resp2 = await _recv_until(cr, lambda m: m.get("id") == 5)
    assert resp2["result"]["cancelled"] is True
    assert server.pending_login is None
    await stop()


@pytest.mark.asyncio
async def test_auth_start_device_login_single_flight(daemon, monkeypatch: pytest.MonkeyPatch):
    server, cw, cr, stop = daemon
    codes = iter(["dev-code-1", "dev-code-2"])
    monkeypatch.setattr(
        device_flow, "request_device_code", lambda *a, **k: _device(device_code=next(codes))
    )
    monkeypatch.setattr(device_flow, "poll_for_token", _blocking_poll)

    await _send(cw, {"jsonrpc": "2.0", "id": 4, "method": "auth.startDeviceLogin", "params": {}})
    await _recv_until(cr, lambda m: m.get("id") == 4)
    # Second start supersedes the first.
    await _send(cw, {"jsonrpc": "2.0", "id": 5, "method": "auth.startDeviceLogin", "params": {}})
    await _recv_until(cr, lambda m: m.get("id") == 5)
    assert server.pending_login.device_code == "dev-code-2"

    await _send(cw, {"jsonrpc": "2.0", "id": 6, "method": "auth.cancelDeviceLogin", "params": {}})
    await _recv_until(cr, lambda m: m.get("id") == 6)
    await stop()


@pytest.mark.asyncio
async def test_auth_device_login_success_persists_and_emits(
    daemon, monkeypatch: pytest.MonkeyPatch
):
    _server, cw, cr, stop = daemon
    from alkera_cli.account import session
    from alkera_cli.daemon.methods import auth as auth_methods

    token = _make_jwt()
    user = CurrentUser(id="u1", email="user@example.com", display_name="User")
    monkeypatch.setattr(device_flow, "request_device_code", lambda *a, **k: _device())
    monkeypatch.setattr(device_flow, "poll_for_token", lambda *a, **k: token)
    monkeypatch.setattr(session, "resolve_user", lambda _api, _token, **_kw: user)
    gateway_seen = _stub_gateway(monkeypatch)

    real = auth_methods._compute_status

    def fake_compute(*args: object) -> auth_methods.AuthStatusResponse:
        if paths.AUTH_FILE_PATH.exists():
            return auth_methods.AuthStatusResponse(
                authenticated=True,
                email="user@example.com",
                api_url="http://localhost:8000",
                expires_at=None,
                reason=None,
                frontend_url=auth_methods._frontend_url(),
            )
        return real(*args)

    monkeypatch.setattr(auth_methods, "_compute_status", fake_compute)

    await _send(cw, {"jsonrpc": "2.0", "id": 7, "method": "auth.startDeviceLogin", "params": {}})
    await _recv_until(cr, lambda m: m.get("id") == 7)

    msg = await _recv_until(cr, lambda m: m.get("method") == "auth.changed", timeout=5.0)
    assert msg["params"]["authenticated"] is True
    assert msg["params"]["email"] == "user@example.com"
    # The web-app URL rides the success notification so the editor's "open web
    # app" button gets the right per-env URL the moment auth lands.
    from alkera_cli.host.config import get_settings

    assert msg["params"]["frontend_url"] == get_settings().alkera_frontend_url.rstrip("/")
    stored = auth_file.load_auth()
    assert stored is not None and stored.token == token
    assert gateway_seen == [token]
    await stop()


@pytest.mark.asyncio
async def test_auth_device_login_gateway_rejection_refuses_without_saving(
    daemon, monkeypatch: pytest.MonkeyPatch
):
    """A token the API accepts but the model gateway rejects is not saved: the
    editor would otherwise sign in to chat surfaces whose every request fails.
    ``alkera login`` refuses the same token.

    The editor learns the gateway is the problem from ``failure`` and the gateway's
    own words from ``detail``. ``reason`` stays ``invalid``, the value an editor
    built before ``failure`` existed settles on: it keeps waiting for approval on a
    reason it does not know."""
    _server, cw, cr, stop = daemon
    from alkera_cli.account import session
    from alkera_cli.gateway.client import GatewayAuthError

    token = _make_jwt()
    monkeypatch.setattr(device_flow, "request_device_code", lambda *a, **k: _device())
    monkeypatch.setattr(device_flow, "poll_for_token", lambda *a, **k: token)
    monkeypatch.setattr(
        session,
        "resolve_user",
        lambda _api, _token, **_kw: CurrentUser(
            id="u1", email="user@example.com", display_name="U"
        ),
    )
    gateway_seen = _stub_gateway(monkeypatch, error=GatewayAuthError("user no longer exists"))

    await _send(cw, {"jsonrpc": "2.0", "id": 9, "method": "auth.startDeviceLogin", "params": {}})
    await _recv_until(cr, lambda m: m.get("id") == 9)

    msg = await _recv_until(cr, lambda m: m.get("method") == "auth.changed", timeout=5.0)
    assert msg["params"]["authenticated"] is False
    assert msg["params"]["reason"] == "invalid"
    assert msg["params"]["failure"] == "gateway_rejected"
    assert msg["params"]["detail"] == "user no longer exists"
    assert auth_file.load_auth() is None
    assert gateway_seen == [token]
    await stop()


@contextlib.contextmanager
def _html_gateway() -> Iterator[str]:
    """A real gateway URL that answers every request 200 with an HTML page, the
    way a captive portal or a URL pointed at the web app does."""

    class _Page(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            body = b"<html>sign in to the wifi</html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args: object) -> None:
            return

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Page)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()


@pytest.mark.asyncio
async def test_auth_device_login_settles_when_the_gateway_answers_a_page(
    daemon, monkeypatch: pytest.MonkeyPatch
):
    """A gateway answering 200 with a body that is not the catalog is a gateway
    that could not be checked, as for `alkera login`: the token is saved and the
    editor hears the result. The login panel waits for this notification and has
    no timeout of its own, so a probe that raised instead left it on "waiting for
    approval" for good."""
    _server, cw, cr, stop = daemon
    from alkera_cli.account import login, session
    from alkera_cli.host.config import get_settings

    token = _make_jwt()
    monkeypatch.setattr(device_flow, "request_device_code", lambda *a, **k: _device())
    monkeypatch.setattr(device_flow, "poll_for_token", lambda *a, **k: token)
    monkeypatch.setattr(
        session,
        "resolve_user",
        lambda _api, _token, **_kw: CurrentUser(
            id="u1", email="user@example.com", display_name="U"
        ),
    )
    with _html_gateway() as gateway_url:
        settings = get_settings().model_copy(update={"alkera_gateway_url": gateway_url})
        monkeypatch.setattr(login, "get_settings", lambda: settings)

        await _send(
            cw, {"jsonrpc": "2.0", "id": 11, "method": "auth.startDeviceLogin", "params": {}}
        )
        await _recv_until(cr, lambda m: m.get("id") == 11)
        msg = await _recv_until(cr, lambda m: m.get("method") == "auth.changed", timeout=10.0)

    assert msg["params"]["authenticated"] is True
    assert msg["params"]["email"] == "user@example.com"
    stored = auth_file.load_auth()
    assert stored is not None and stored.token == token
    await stop()


@pytest.mark.asyncio
async def test_auth_device_login_unverified_email_refuses_without_saving(
    daemon, monkeypatch: pytest.MonkeyPatch
):
    """An approved device login for an UNVERIFIED account must not become a
    session: no auth.yml write, and the failure notification carries the
    `email_verification_required` reason (+ frontend_url) so the login panel
    can link the verification page."""
    _server, cw, cr, stop = daemon
    from alkera_cli.account import session

    monkeypatch.setattr(device_flow, "request_device_code", lambda *a, **k: _device())
    monkeypatch.setattr(device_flow, "poll_for_token", lambda *a, **k: _make_jwt())
    monkeypatch.setattr(
        session,
        "resolve_user",
        lambda _api, _token, **_kw: CurrentUser(
            id="u1",
            email="unverified@alkera.dev",
            display_name="U User",
            email_verification_required=True,
        ),
    )

    await _send(cw, {"jsonrpc": "2.0", "id": 8, "method": "auth.startDeviceLogin", "params": {}})
    await _recv_until(cr, lambda m: m.get("id") == 8)

    msg = await _recv_until(cr, lambda m: m.get("method") == "auth.changed", timeout=5.0)
    assert msg["params"]["authenticated"] is False
    assert msg["params"]["reason"] == "email_verification_required"
    assert msg["params"]["email"] == "unverified@alkera.dev"
    from alkera_cli.host.config import get_settings

    assert msg["params"]["frontend_url"] == get_settings().alkera_frontend_url.rstrip("/")
    assert auth_file.load_auth() is None  # the token was NOT persisted
    await stop()


@pytest.mark.asyncio
async def test_auth_device_login_denied_emits_invalid(daemon, monkeypatch: pytest.MonkeyPatch):
    _server, cw, cr, stop = daemon

    def _deny(*_a: object, **_k: object):
        raise device_flow.AuthorizationDeniedError("denied")

    monkeypatch.setattr(device_flow, "request_device_code", lambda *a, **k: _device())
    monkeypatch.setattr(device_flow, "poll_for_token", _deny)

    await _send(cw, {"jsonrpc": "2.0", "id": 8, "method": "auth.startDeviceLogin", "params": {}})
    await _recv_until(cr, lambda m: m.get("id") == 8)

    msg = await _recv_until(cr, lambda m: m.get("method") == "auth.changed", timeout=5.0)
    assert msg["params"]["authenticated"] is False
    assert msg["params"]["reason"] == "invalid"
    assert not paths.AUTH_FILE_PATH.exists()
    await stop()


@pytest.mark.parametrize(
    "stop_method",
    [
        pytest.param("auth.cancelDeviceLogin", id="cancel"),
        # A logout that saw the token saved after it would leave the user signed in.
        pytest.param("auth.logout", id="logout"),
    ],
)
@pytest.mark.asyncio
async def test_a_stop_during_the_gateway_check_wins_over_the_save(
    daemon, monkeypatch: pytest.MonkeyPatch, stop_method: str
):
    """The browser approved, and the daemon is still checking the token with the
    gateway (up to its request timeout, twice). A cancel or a logout that arrives
    then must stop the save: the user said no before anything was written."""
    _server, cw, cr, stop = daemon
    from alkera_cli.account import login, session
    from alkera_cli.gateway import client as gateway_client

    token = _make_jwt()
    monkeypatch.setattr(device_flow, "request_device_code", lambda *a, **k: _device())
    monkeypatch.setattr(device_flow, "poll_for_token", lambda *a, **k: token)
    monkeypatch.setattr(
        session,
        "resolve_user",
        lambda _api, _token, **_kw: CurrentUser(
            id="u1", email="user@example.com", display_name="U"
        ),
    )
    monkeypatch.setattr(session, "revoke_session", lambda *_a, **_k: True)
    checking = threading.Event()
    release = threading.Event()

    async def _slow_gateway(**_kw: object) -> list[object]:
        checking.set()
        release.wait(_REPLY_BUDGET_S)
        return []

    monkeypatch.setattr(gateway_client, "fetch_models", _slow_gateway)
    finished = threading.Event()
    real_complete = login.complete_login

    def _watched(*args: Any, **kwargs: Any) -> login.LoginOutcome:
        try:
            return real_complete(*args, **kwargs)
        finally:
            finished.set()

    monkeypatch.setattr(login, "complete_login", _watched)

    await _send(cw, {"jsonrpc": "2.0", "id": 12, "method": "auth.startDeviceLogin", "params": {}})
    await _recv_until(cr, lambda m: m.get("id") == 12)
    assert await asyncio.to_thread(checking.wait, _REPLY_BUDGET_S)

    await _send(cw, {"jsonrpc": "2.0", "id": 13, "method": stop_method, "params": {}})
    resp = await _recv_until(cr, lambda m: m.get("id") == 13)
    release.set()
    assert await asyncio.to_thread(finished.wait, _REPLY_BUDGET_S)

    assert auth_file.load_auth() is None
    if stop_method == "auth.cancelDeviceLogin":
        assert resp["result"]["cancelled"] is True
    await stop()


def _save_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    def _refuse(_profile: object, **_kw: object) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(auth_file, "save_profile", _refuse)


def _poll_breaks(monkeypatch: pytest.MonkeyPatch) -> None:
    def _break(*_a: object, **_k: object) -> str:
        raise RuntimeError("poll loop broke")

    monkeypatch.setattr(device_flow, "poll_for_token", _break)


@pytest.mark.parametrize(
    ("breaks", "detail"),
    [
        pytest.param(_save_fails, "[Errno 28] No space left on device", id="auth-yml-write-fails"),
        pytest.param(_poll_breaks, "poll loop broke", id="poll-raises-unexpectedly"),
    ],
)
@pytest.mark.asyncio
async def test_an_unexpected_error_ends_the_sign_in_as_a_failure(
    daemon, monkeypatch: pytest.MonkeyPatch, breaks, detail: str
):
    """The sign-in panel waits for auth.changed with no timeout of its own, so a
    login task that died silently left it on "waiting for approval" until the code
    expired. The failure keeps ``reason: network``, a value every editor build
    settles on, and names itself in ``failure`` with the error in ``detail``."""
    server, cw, cr, stop = daemon
    from alkera_cli.account import session

    monkeypatch.setattr(device_flow, "request_device_code", lambda *a, **k: _device())
    monkeypatch.setattr(device_flow, "poll_for_token", lambda *a, **k: _make_jwt())
    monkeypatch.setattr(
        session,
        "resolve_user",
        lambda _api, _token, **_kw: CurrentUser(
            id="u1", email="user@example.com", display_name="U"
        ),
    )
    _stub_gateway(monkeypatch)
    breaks(monkeypatch)

    await _send(cw, {"jsonrpc": "2.0", "id": 14, "method": "auth.startDeviceLogin", "params": {}})
    await _recv_until(cr, lambda m: m.get("id") == 14)
    msg = await _recv_until(cr, lambda m: m.get("method") == "auth.changed", timeout=5.0)

    assert msg["params"]["authenticated"] is False
    assert msg["params"]["reason"] == "network"
    assert msg["params"]["failure"] == "login_failed"
    assert msg["params"]["detail"] == detail
    assert server.pending_login is None
    assert not paths.AUTH_FILE_PATH.exists()
    await stop()


#: Many times the cap, the size an exception carrying a response body reaches. Kept
#: modest because the test logging config renders the traceback through rich,
#: whose wrapping slows sharply past tens of kilobytes (the daemon's own log
#: formats tracebacks as plain text).
_HUGE_MESSAGE = "disk refused the write " + "x" * 8_000


def _huge_error_breaks(monkeypatch: pytest.MonkeyPatch) -> None:
    def _break(*_a: object, **_k: object) -> str:
        raise RuntimeError(_HUGE_MESSAGE)

    monkeypatch.setattr(device_flow, "poll_for_token", _break)


def _huge_gateway_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    from alkera_cli.gateway.client import GatewayAuthError

    _stub_gateway(monkeypatch, error=GatewayAuthError(_HUGE_MESSAGE))


@pytest.mark.parametrize(
    ("breaks", "failure"),
    [
        pytest.param(_huge_error_breaks, "login_failed", id="unexpected-error"),
        pytest.param(_huge_gateway_refusal, "gateway_rejected", id="gateway-refusal"),
    ],
)
@pytest.mark.asyncio
async def test_a_huge_failure_message_reaches_the_editor_capped(
    daemon, monkeypatch: pytest.MonkeyPatch, breaks, failure: str
):
    """``detail`` comes from an exception message or a gateway's refusal body, so
    its size is not this daemon's to choose. Whichever path builds it, the editor
    gets at most ``AUTH_DETAIL_MAX_CHARS`` characters, the cut marked by an ellipsis."""
    _server, cw, cr, stop = daemon
    from alkera_cli.account import session
    from alkera_cli.daemon.methods.auth import AUTH_DETAIL_MAX_CHARS

    monkeypatch.setattr(device_flow, "request_device_code", lambda *a, **k: _device())
    monkeypatch.setattr(device_flow, "poll_for_token", lambda *a, **k: _make_jwt())
    monkeypatch.setattr(
        session,
        "resolve_user",
        lambda _api, _token, **_kw: CurrentUser(
            id="u1", email="user@example.com", display_name="U"
        ),
    )
    _stub_gateway(monkeypatch)
    breaks(monkeypatch)

    await _send(cw, {"jsonrpc": "2.0", "id": 15, "method": "auth.startDeviceLogin", "params": {}})
    await _recv_until(cr, lambda m: m.get("id") == 15)
    msg = await _recv_until(cr, lambda m: m.get("method") == "auth.changed", timeout=5.0)

    detail = msg["params"]["detail"]
    assert msg["params"]["failure"] == failure
    assert len(detail) == AUTH_DETAIL_MAX_CHARS
    assert detail == _HUGE_MESSAGE[: AUTH_DETAIL_MAX_CHARS - 1] + "\u2026"
    await stop()


@pytest.mark.parametrize(
    ("extra", "capped"),
    [
        pytest.param(-1, False, id="under-the-cap"),
        pytest.param(0, False, id="exactly-the-cap"),
        pytest.param(1, True, id="one-past-the-cap"),
    ],
)
@pytest.mark.parametrize("model_name", ["AuthStatusResponse", "AuthChangedNotification"])
def test_every_auth_payload_caps_detail_at_the_boundary(model_name: str, extra: int, capped: bool):
    """The cap lives on the field, so a payload built anywhere is bounded, and a
    detail that fits is sent untouched. Multi-byte characters count as one each."""
    from alkera_cli.daemon.methods import auth as auth_methods

    limit = auth_methods.AUTH_DETAIL_MAX_CHARS
    text = "\u00e9" * (limit + extra)
    model = getattr(auth_methods, model_name)
    payload = model(authenticated=False, email=None, api_url=None, expires_at=None, detail=text)
    if capped:
        assert payload.detail == "\u00e9" * (limit - 1) + "\u2026"
    else:
        assert payload.detail == text


def test_the_protocol_schema_states_the_detail_cap():
    """The extension's generated types come from this schema, so the bound is
    part of the published contract, not only of this daemon's behaviour."""
    from alkera_cli.daemon.methods import auth as auth_methods

    for model in (auth_methods.AuthStatusResponse, auth_methods.AuthChangedNotification):
        branches = model.model_json_schema()["properties"]["detail"]["anyOf"]
        strings = [b for b in branches if b.get("type") == "string"]
        assert strings == [{"type": "string", "maxLength": auth_methods.AUTH_DETAIL_MAX_CHARS}]


# ---------------------------------------------------------------------------
# AUTH_REQUIRED error code
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auth_required_serializes_with_data_reason(daemon):
    """A handler that raises AuthRequiredError should get serialized as
    JSON-RPC error code -32001 with ``data: {reason}``."""
    _server, cw, cr, stop = daemon

    # Register a one-off test method dynamically. It bypasses the @method
    # decorator's import-time registration to avoid polluting METHODS for
    # other tests; we just patch the registry directly.
    from alkera_cli.daemon.methods.auth import AuthStatusRequest, AuthStatusResponse
    from alkera_cli.daemon.protocol import METHODS, MethodSpec

    async def raises(_server, _params):
        raise AuthRequiredError("expired")

    METHODS["__test.auth_required"] = MethodSpec(
        name="__test.auth_required",
        request_type=AuthStatusRequest,
        response_type=AuthStatusResponse,
        handler=raises,
    )
    try:
        await _send(
            cw,
            {
                "jsonrpc": "2.0",
                "id": 8,
                "method": "__test.auth_required",
                "params": {},
            },
        )
        resp = await _recv_until(cr, lambda m: m.get("id") == 8)
        assert "error" in resp
        assert resp["error"]["code"] == AUTH_REQUIRED
        assert resp["error"]["data"] == {"reason": "expired"}
    finally:
        METHODS.pop("__test.auth_required", None)
    await stop()


# ---------------------------------------------------------------------------
# Cross-process watcher → auth.changed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auth_changed_fires_on_external_login(daemon, monkeypatch: pytest.MonkeyPatch):
    """Simulate another VS Code window writing auth.yml: the watcher
    should fire and the daemon should emit auth.changed."""
    _server, _cw, cr, stop = daemon

    from alkera_cli.account import session

    user = CurrentUser(id="u2", email="b@example.com", display_name="B")
    monkeypatch.setattr(session, "resolve_user", lambda _api, _token, **_kw: user)

    # Sleep a touch so the watcher has time to seed its baseline.
    await asyncio.sleep(0.15)

    # Pretend window B logged in.
    auth_file.save_auth(
        StoredAuth(
            api_url="http://localhost:8000",
            token=_make_jwt(),
            expires_at=datetime.now(UTC) + timedelta(days=30),
        )
    )

    msg = await _recv_until(cr, lambda m: m.get("method") == "auth.changed", timeout=5.0)
    assert msg["params"]["authenticated"] is True
    assert msg["params"]["email"] == "b@example.com"
    await stop()


# ---------------------------------------------------------------------------
# One sign-in rule for both surfaces
# ---------------------------------------------------------------------------

_VERIFIED = CurrentUser(id="u1", email="user@example.com", display_name="U")
_UNVERIFIED = CurrentUser(
    id="u1", email="user@example.com", display_name="U", email_verification_required=True
)


@pytest.mark.parametrize(
    ("user", "gateway", "saved", "editor_reason", "editor_failure", "cli_exit"),
    [
        pytest.param(_VERIFIED, True, True, None, None, 0, id="verified"),
        pytest.param(
            _UNVERIFIED, True, False, "email_verification_required", None, 1, id="unverified"
        ),
        pytest.param(None, True, False, "invalid", None, 1, id="api-rejected"),
        pytest.param(
            _VERIFIED, False, False, "invalid", "gateway_rejected", 1, id="gateway-rejected"
        ),
        # An outage is not an auth failure: both save, checked against the API only.
        pytest.param(_VERIFIED, None, True, None, None, 0, id="gateway-unreachable"),
    ],
)
@pytest.mark.asyncio
async def test_the_editor_and_alkera_login_reach_the_same_outcome(
    daemon,
    monkeypatch: pytest.MonkeyPatch,
    user: CurrentUser | None,
    gateway: bool | None,
    saved: bool,
    editor_reason: str | None,
    editor_failure: str | None,
    cli_exit: int,
) -> None:
    from alkera_cli import main as cli_main
    from alkera_cli.account import session
    from alkera_cli.commands import account as account_command
    from alkera_cli.gateway.client import GatewayAuthError, GatewayUnavailableError
    from typer.testing import CliRunner

    _server, cw, cr, stop = daemon
    token = _make_jwt()
    monkeypatch.setattr(device_flow, "request_device_code", lambda *a, **k: _device())
    monkeypatch.setattr(device_flow, "poll_for_token", lambda *a, **k: token)
    monkeypatch.setattr(account_command.webbrowser, "open", lambda _url: True)
    monkeypatch.setattr(session, "resolve_user", lambda _api, _token, **_kw: user)
    error: Exception | None = None
    if gateway is False:
        error = GatewayAuthError("user no longer exists")
    elif gateway is None:
        error = GatewayUnavailableError("gateway unreachable")
    _stub_gateway(monkeypatch, error=error)

    await _send(cw, {"jsonrpc": "2.0", "id": 10, "method": "auth.startDeviceLogin", "params": {}})
    await _recv_until(cr, lambda m: m.get("id") == 10)
    msg = await _recv_until(cr, lambda m: m.get("method") == "auth.changed", timeout=5.0)
    editor_saved = auth_file.load_auth()
    auth_file.delete_auth()
    await stop()

    result = await asyncio.to_thread(CliRunner().invoke, cli_main.app, ["login", "--force"])
    cli_saved = auth_file.load_auth()

    assert msg["params"]["authenticated"] is saved
    assert msg["params"]["reason"] == editor_reason
    assert msg["params"]["failure"] == editor_failure
    assert (editor_saved is not None and editor_saved.token == token) is saved
    assert result.exit_code == cli_exit, result.stdout
    assert (cli_saved is not None and cli_saved.token == token) is saved
