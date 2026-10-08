"""Tests for `alkera login`, `alkera logout`, and the supporting auth file.

`alkera login` uses the RFC 8628 device flow: it asks the backend for a device
code, prints the code-carrying URL + user_code, opens the browser best-effort,
and polls until approval. These tests stub `device_flow.request_device_code` / `poll_for_token`
(and `webbrowser.open`) so the command runs with no network, browser, or sockets.
Identity is fetched via `session.resolve_user`, also stubbed.
"""

from __future__ import annotations

import base64
import json
import os
import stat
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alkera_cli import main as cli_main
from alkera_cli.account import auth_file, device_flow
from alkera_cli.account import session as session_module
from alkera_cli.account.auth_file import StoredAuth
from alkera_cli.account.session import CurrentUser
from alkera_cli.commands import account as account_command
from alkera_cli.gateway import client as gateway_client
from alkera_cli.gateway.client import GatewayAuthError, GatewayUnavailableError
from alkera_cli.host import paths
from typer.testing import CliRunner

runner = CliRunner()


# ---------- helpers ----------


def _build_jwt(*, sub: str = "user-1", email: str = "user@example.com", exp: int) -> str:
    """Construct an unsigned JWT for tests — _jwt_expires_at only reads `exp`."""
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
    payload = (
        base64.urlsafe_b64encode(json.dumps({"sub": sub, "email": email, "exp": exp}).encode())
        .rstrip(b"=")
        .decode()
    )
    return f"{header}.{payload}.sig"


def _isolate_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    home = tmp_path / "alkera-home"
    monkeypatch.setattr(paths, "ALKERA_HOME", home)
    monkeypatch.setattr(paths, "AUTH_FILE_PATH", home / "auth.yml")
    monkeypatch.setattr(auth_file, "AUTH_FILE_PATH", home / "auth.yml")
    return home


def _stub_resolve_user(monkeypatch: pytest.MonkeyPatch, *, user: CurrentUser | None) -> None:
    monkeypatch.setattr(session_module, "resolve_user", lambda _api, _token, **_kw: user)


def _stub_gateway_auth(
    monkeypatch: pytest.MonkeyPatch,
    *,
    accepted: bool | None = True,
    detail: str | None = None,
) -> list[tuple[str, str, float]]:
    """Stand in for the gateway catalog fetch the login check makes: it serves
    the token (``accepted=True``), rejects it with ``detail`` (False), or cannot
    be reached (None).

    Returns recorded (gateway_url, token, timeout_seconds) calls for assertions.
    """
    calls: list[tuple[str, str, float]] = []

    async def _fake_fetch(*, gateway_url: str, token: str, timeout_seconds: float) -> list[object]:
        calls.append((gateway_url, token, timeout_seconds))
        if accepted is False:
            raise GatewayAuthError(detail or "rejected")
        if accepted is None:
            raise GatewayUnavailableError(detail or "gateway unavailable")
        return []

    monkeypatch.setattr(gateway_client, "fetch_models", _fake_fetch)
    return calls


def _stub_device_flow(
    monkeypatch: pytest.MonkeyPatch,
    *,
    token: str | None = None,
    poll_error: Exception | None = None,
) -> dict[str, int]:
    """Stub the device flow so `alkera login` runs without network/browser.

    `request_device_code` returns a fixed code; `poll_for_token` returns `token`
    (or raises `poll_error`). Returns a counter of request_device_code calls.
    """
    calls = {"request": 0, "poll": 0}

    def fake_request(*_args: object, **_kwargs: object) -> device_flow.DeviceCodeResponse:
        calls["request"] += 1
        return device_flow.DeviceCodeResponse(
            device_code="dev-code-xyz",
            user_code="WXYZ-1234",
            verification_uri="http://localhost:5173/device",
            verification_uri_complete="http://localhost:5173/device?user_code=WXYZ-1234",
            expires_in=600,
            interval=1,
        )

    def fake_poll(*_args: object, **_kwargs: object) -> str:
        calls["poll"] += 1
        if poll_error is not None:
            raise poll_error
        assert token is not None
        return token

    monkeypatch.setattr(device_flow, "request_device_code", fake_request)
    monkeypatch.setattr(device_flow, "poll_for_token", fake_poll)
    monkeypatch.setattr(account_command.webbrowser, "open", lambda _url: True)
    return calls


def _stub_revoke_session(monkeypatch: pytest.MonkeyPatch, *, ok: bool) -> list[tuple[str, str]]:
    """Stub `session.revoke_session` so logout never makes a real network call."""
    calls: list[tuple[str, str]] = []

    def _fake(api_url: str, token: str, **_kw: object) -> bool:
        calls.append((api_url, token))
        return ok

    monkeypatch.setattr(session_module, "revoke_session", _fake)
    return calls


# ---------- auth_file unit ----------


def test_auth_file_round_trip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _isolate_home(monkeypatch, tmp_path)
    stored = StoredAuth(
        api_url="http://localhost:8000",
        token="tok",
        expires_at=datetime.now(UTC) + timedelta(days=90),
    )
    auth_file.save_auth(stored)
    loaded = auth_file.load_auth()
    assert loaded is not None
    assert loaded.token == "tok"
    assert loaded.api_url == "http://localhost:8000"
    if sys.platform != "win32":
        mode = stat.S_IMODE(os.stat(paths.AUTH_FILE_PATH).st_mode)
        assert mode == 0o600
    assert "display_name" not in auth_file.AUTH_FILE_PATH.read_text(encoding="utf-8")
    assert "frontend_url" not in auth_file.AUTH_FILE_PATH.read_text(encoding="utf-8")


def test_auth_file_load_missing_returns_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _isolate_home(monkeypatch, tmp_path)
    assert auth_file.load_auth() is None


def test_auth_file_delete_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _isolate_home(monkeypatch, tmp_path)
    assert auth_file.delete_auth() is False  # absent — no-op
    auth_file.save_auth(StoredAuth(api_url="http://x", token="t", expires_at=datetime.now(UTC)))
    assert auth_file.delete_auth() is True
    assert auth_file.load_auth() is None


# ---------- alkera login command (device flow) ----------


def test_login_skips_when_existing_token_works(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _isolate_home(monkeypatch, tmp_path)
    gateway_calls = _stub_gateway_auth(monkeypatch)
    existing_token = _build_jwt(exp=int(time.time()) + 86400)
    auth_file.save_auth(
        StoredAuth(
            api_url="http://localhost:8000",
            token=existing_token,
            expires_at=datetime.now(UTC) + timedelta(days=1),
        )
    )
    _stub_resolve_user(
        monkeypatch, user=CurrentUser(id="u-1", email="existing@e", display_name="X")
    )
    flow = _stub_device_flow(monkeypatch, token="unused")

    # Decline the "Re-login?" prompt → command exits 0 without running the flow.
    result = runner.invoke(cli_main.app, ["login"], input="n\n")
    assert result.exit_code == 0, result.stdout
    assert flow["request"] == 0
    assert "Already logged in as existing@e" in result.stdout
    expected_url = cli_main.get_settings().alkera_gateway_url
    assert gateway_calls == [(expected_url, existing_token, 5.0)]


def test_login_with_the_gateway_down_says_so_and_re_logs_in_when_asked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """An unreachable gateway is not an auth failure: the saved sign-in still
    counts as working (with a warning), and accepting "Re-login?" runs the device
    flow and saves the new token checked against the API only."""
    _isolate_home(monkeypatch, tmp_path)
    _stub_gateway_auth(monkeypatch, accepted=None, detail="connection refused")
    old_token = _build_jwt(exp=int(time.time()) + 86400)
    auth_file.save_auth(
        StoredAuth(
            api_url="http://localhost:8000",
            token=old_token,
            expires_at=datetime.now(UTC) + timedelta(days=1),
        )
    )
    _stub_resolve_user(monkeypatch, user=CurrentUser(id="u-1", email="same@e", display_name="S"))
    fresh_token = _build_jwt(sub="u-1", email="same@e", exp=int(time.time()) + 7776000)
    flow = _stub_device_flow(monkeypatch, token=fresh_token)

    result = runner.invoke(cli_main.app, ["login"], input="y\n")

    assert result.exit_code == 0, result.stdout
    out = " ".join(result.stdout.split())
    assert "Already logged in as same@e" in out
    assert "continuing with API authentication only (connection refused)" in out
    assert flow["request"] == 1
    assert "saving API authentication only (connection refused)" in out
    saved = auth_file.load_auth()
    assert saved is not None and saved.token == fresh_token


def test_login_force_skips_prompt_and_runs_flow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _isolate_home(monkeypatch, tmp_path)
    _stub_gateway_auth(monkeypatch)
    auth_file.save_auth(
        StoredAuth(
            api_url="http://localhost:8000",
            token=_build_jwt(exp=int(time.time()) + 86400),
            expires_at=datetime.now(UTC) + timedelta(days=1),
        )
    )
    fresh_user = CurrentUser(id="u-2", email="fresh@e", display_name="Fresh")
    _stub_resolve_user(monkeypatch, user=fresh_user)
    fresh_token = _build_jwt(sub="u-2", email="fresh@e", exp=int(time.time()) + 7776000)
    _stub_device_flow(monkeypatch, token=fresh_token)

    result = runner.invoke(cli_main.app, ["login", "--force"])
    assert result.exit_code == 0, result.stdout
    # The printed manual link is the code-carrying one — following it must land on
    # the pre-filled consent page, never the bare /device entry form.
    assert "http://localhost:5173/device?user_code=WXYZ-1234" in result.stdout
    # The bare URI is a substring of the complete one, so pin the phrasing that
    # only makes sense when the link already carries the code.
    assert "check the code matches" in result.stdout
    assert "WXYZ-1234" in result.stdout  # the user_code is shown
    assert "Logged in as fresh@e" in result.stdout
    saved = auth_file.load_auth()
    assert saved is not None
    assert saved.token == fresh_token


def test_login_falls_through_when_existing_invalid(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _isolate_home(monkeypatch, tmp_path)
    _stub_gateway_auth(monkeypatch)
    auth_file.save_auth(
        StoredAuth(api_url="http://localhost:8000", token="rotten", expires_at=datetime.now(UTC))
    )
    fresh_user = CurrentUser(id="u-3", email="recovered@e", display_name="R")
    calls = {"n": 0}

    def fake_resolve(_api: str, _token: str, **_kw: object) -> CurrentUser | None:
        calls["n"] += 1
        return None if calls["n"] == 1 else fresh_user

    monkeypatch.setattr(session_module, "resolve_user", fake_resolve)
    _stub_device_flow(monkeypatch, token=_build_jwt(exp=int(time.time()) + 7776000))

    result = runner.invoke(cli_main.app, ["login"])
    assert result.exit_code == 0, result.stdout
    assert "Logged in as recovered@e" in result.stdout
    assert auth_file.load_auth() is not None


def test_login_existing_token_gateway_rejection_re_authenticates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A stored token the API still accepts but the GATEWAY rejects must NOT
    hard-exit: login falls through to the device flow. The gateway rejects the
    old token but accepts the freshly-minted one → login succeeds + saves."""
    _isolate_home(monkeypatch, tmp_path)
    old_token = _build_jwt(exp=int(time.time()) + 86400)
    fresh_token = _build_jwt(sub="u-4", email="fresh@e", exp=int(time.time()) + 7776000)
    gw_calls: list[str] = []

    async def _fake_fetch(*, gateway_url: str, token: str, timeout_seconds: float) -> list[object]:
        gw_calls.append(token)
        if token == old_token:
            raise GatewayAuthError("user no longer exists")
        return []

    monkeypatch.setattr(gateway_client, "fetch_models", _fake_fetch)
    auth_file.save_auth(
        StoredAuth(
            api_url="http://localhost:8000",
            token=old_token,
            expires_at=datetime.now(UTC) + timedelta(days=1),
        )
    )
    fresh_user = CurrentUser(id="u-4", email="fresh@e", display_name="Fresh")
    _stub_resolve_user(monkeypatch, user=fresh_user)
    flow = _stub_device_flow(monkeypatch, token=fresh_token)

    result = runner.invoke(cli_main.app, ["login"])

    assert result.exit_code == 0, result.stdout
    assert flow["request"] == 1  # the device flow DID run (no hard-exit)
    assert "re-authenticating" in result.stdout
    assert "Logged in as fresh@e" in result.stdout
    saved = auth_file.load_auth()
    assert saved is not None and saved.token == fresh_token
    assert gw_calls == [old_token, fresh_token]


def test_login_fresh_token_gateway_rejection_surfaces_detail_and_does_not_save(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The refusal names the gateway it asked and the setting that picks it, so a
    self-hosted user who set only the API address sees where the sign-in failed."""
    _isolate_home(monkeypatch, tmp_path)
    monkeypatch.setenv("ALKERA_GATEWAY_URL", "https://gateway.example.test")
    cli_main.get_settings.cache_clear()
    _stub_gateway_auth(monkeypatch, accepted=False, detail="invalid token: bad signature")
    _stub_resolve_user(monkeypatch, user=CurrentUser(id="u-5", email="fresh@e", display_name="F"))
    _stub_device_flow(
        monkeypatch, token=_build_jwt(sub="u-5", email="fresh@e", exp=int(time.time()) + 7776000)
    )

    try:
        result = runner.invoke(cli_main.app, ["login"])
    finally:
        cli_main.get_settings.cache_clear()

    assert result.exit_code == 1, result.stdout
    said = " ".join(result.stdout.split())
    assert (
        "The gateway at https://gateway.example.test refused the new sign-in "
        "(invalid token: bad signature). Check ALKERA_GATEWAY_URL and try again."
    ) in said
    assert auth_file.load_auth() is None


def test_login_fresh_token_unverified_email_refuses_with_link_and_does_not_save(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Login is gated on a proven email: an approved device login for an
    unverified account exits with the verification-page URL and persists
    NOTHING — the account gets no agent session until it verifies."""
    _isolate_home(monkeypatch, tmp_path)
    gw_calls = _stub_gateway_auth(monkeypatch)  # must not even be consulted
    _stub_resolve_user(
        monkeypatch,
        user=CurrentUser(
            id="u-6", email="unverified@e", display_name="U", email_verification_required=True
        ),
    )
    _stub_device_flow(
        monkeypatch, token=_build_jwt(sub="u-6", email="unverified@e", exp=int(time.time()) + 86400)
    )

    result = runner.invoke(cli_main.app, ["login"])

    assert result.exit_code == 1, result.stdout
    assert "hasn't verified its email" in result.stdout
    assert "/verify-email" in result.stdout
    assert auth_file.load_auth() is None
    assert gw_calls == []  # refused on the /auth/me identity, not a gateway probe


def test_login_existing_unverified_token_exits_with_link_without_device_flow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """An unverified existing token short-circuits BEFORE the browser flow —
    re-authenticating the same account can't help, so the command hands over
    the verification URL instead of a device code."""
    _isolate_home(monkeypatch, tmp_path)
    _stub_gateway_auth(monkeypatch)
    auth_file.save_auth(
        StoredAuth(
            api_url="http://localhost:8000",
            token=_build_jwt(exp=int(time.time()) + 86400),
            expires_at=datetime.now(UTC) + timedelta(days=1),
        )
    )
    _stub_resolve_user(
        monkeypatch,
        user=CurrentUser(
            id="u-7", email="unverified@e", display_name="U", email_verification_required=True
        ),
    )
    flow = _stub_device_flow(monkeypatch, token="unused")

    result = runner.invoke(cli_main.app, ["login"])

    assert result.exit_code == 1, result.stdout
    assert flow["request"] == 0  # no device flow was started
    assert "hasn't verified its email" in result.stdout
    assert "/verify-email" in result.stdout


def test_login_denied_exits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _isolate_home(monkeypatch, tmp_path)
    _stub_device_flow(monkeypatch, poll_error=device_flow.AuthorizationDeniedError("nope"))
    result = runner.invoke(cli_main.app, ["login"])
    assert result.exit_code == 1
    assert "denied" in result.stdout.lower()
    assert auth_file.load_auth() is None


def test_login_expired_exits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _isolate_home(monkeypatch, tmp_path)
    _stub_device_flow(monkeypatch, poll_error=device_flow.DeviceCodeExpiredError("gone"))
    result = runner.invoke(cli_main.app, ["login"])
    assert result.exit_code == 1
    assert "expired" in result.stdout.lower()


def test_login_cancelled_exits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _isolate_home(monkeypatch, tmp_path)
    _stub_device_flow(monkeypatch, poll_error=KeyboardInterrupt())
    result = runner.invoke(cli_main.app, ["login"])
    assert result.exit_code == 1
    assert "canceled" in result.stdout.lower()


def test_login_request_failure_exits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _isolate_home(monkeypatch, tmp_path)

    def boom(*_a: object, **_kw: object) -> device_flow.DeviceCodeResponse:
        raise device_flow.DeviceCodeRequestError("backend down")

    monkeypatch.setattr(device_flow, "request_device_code", boom)
    result = runner.invoke(cli_main.app, ["login"])
    assert result.exit_code == 1
    assert "backend down" in result.stdout


# ---------- logout command ----------


def test_logout_revokes_backend_then_deletes_local(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _isolate_home(monkeypatch, tmp_path)
    calls = _stub_revoke_session(monkeypatch, ok=True)
    auth_file.save_auth(
        StoredAuth(api_url="http://api.example", token="tok-123", expires_at=datetime.now(UTC))
    )
    result = runner.invoke(cli_main.app, ["logout"])
    assert result.exit_code == 0
    assert calls == [("http://api.example", "tok-123")]
    assert auth_file.load_auth() is None


def test_logout_offline_still_clears_local(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _isolate_home(monkeypatch, tmp_path)
    _stub_revoke_session(monkeypatch, ok=False)  # backend unreachable
    auth_file.save_auth(
        StoredAuth(api_url="http://api.example", token="t", expires_at=datetime.now(UTC))
    )
    result = runner.invoke(cli_main.app, ["logout"])
    assert result.exit_code == 0
    assert "clearing locally" in result.stdout
    assert auth_file.load_auth() is None


def test_logout_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _isolate_home(monkeypatch, tmp_path)
    result = runner.invoke(cli_main.app, ["logout"])
    assert result.exit_code == 0
    assert "No saved auth" in result.stdout


def test_login_rides_out_a_server_error_while_the_person_approves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real poll, against a backend that answers one poll with a 503 and a
    JSON body while the person is still in the browser: the login waits and
    saves the token the next answers bring."""
    import functools

    import httpx

    _isolate_home(monkeypatch, tmp_path)
    _stub_gateway_auth(monkeypatch)
    user = CurrentUser(id="u-1", email="user@example.com", display_name="User")
    _stub_resolve_user(monkeypatch, user=user)
    token = _build_jwt(exp=int(time.time()) + 7776000)
    answers = iter(
        [
            (503, {"error": "db_pool_exhausted"}),
            (400, {"error": "authorization_pending"}),
            (200, {"access_token": token}),
        ]
    )

    def backend(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/auth/device/token"
        status, body = next(answers)
        return httpx.Response(status, json=body)

    real_poll = device_flow.poll_for_token
    _stub_device_flow(monkeypatch, token=token)
    monkeypatch.setattr(
        device_flow,
        "poll_for_token",
        functools.partial(real_poll, sleep=lambda _s: None, transport=httpx.MockTransport(backend)),
    )

    result = runner.invoke(cli_main.app, ["login", "--force"])
    assert result.exit_code == 0, result.stdout
    saved = auth_file.load_auth()
    assert saved is not None and saved.token == token
