"""The sign-in library both `alkera login` and the editor's device login call.

The API identity (`session.resolve_user`) and the gateway catalog fetch are the
two network boundaries, stubbed here; the auth file is real, under a tmp home.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alkera_cli.account import auth_file, login, session
from alkera_cli.account.auth_file import StoredAuth
from alkera_cli.account.session import CurrentUser
from alkera_cli.gateway import client as gateway_client
from alkera_cli.gateway.client import GatewayAuthError, GatewayUnavailableError
from alkera_cli.host import paths

# exp = 2_000_000_000 (2033-05-18T03:33:20Z), unsigned: only `exp` is read.
_TOKEN = "eyJhbGciOiJub25lIn0.eyJzdWIiOiJ1MSIsImV4cCI6MjAwMDAwMDAwMH0.sig"
_TOKEN_EXPIRES = datetime(2033, 5, 18, 3, 33, 20, tzinfo=UTC)
_API = "http://api.example"

_VERIFIED = CurrentUser(id="u1", email="user@example.com", display_name="U")
_UNVERIFIED = CurrentUser(
    id="u1", email="user@example.com", display_name="U", email_verification_required=True
)


@pytest.fixture(autouse=True)
def _home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    home = tmp_path / "alkera-home"
    monkeypatch.setattr(paths, "ALKERA_HOME", home)
    monkeypatch.setattr(paths, "AUTH_FILE_PATH", home / "auth.yml")
    monkeypatch.setattr(auth_file, "AUTH_FILE_PATH", home / "auth.yml")


def _gateway(monkeypatch: pytest.MonkeyPatch, error: Exception | None) -> list[str]:
    seen: list[str] = []

    async def _fetch(*, gateway_url: str, token: str, timeout_seconds: float) -> list[object]:
        seen.append(token)
        if error is not None:
            raise error
        return []

    monkeypatch.setattr(gateway_client, "fetch_models", _fetch)
    return seen


def _identity(monkeypatch: pytest.MonkeyPatch, user: CurrentUser | None) -> None:
    monkeypatch.setattr(session, "resolve_user", lambda _api, _token, **_kw: user)


@pytest.mark.parametrize(
    ("user", "error", "refusal", "gateway", "asked_gateway"),
    [
        pytest.param(_VERIFIED, None, None, login.GatewayCheck(True), True, id="verified"),
        pytest.param(None, None, "api_rejected", None, False, id="api-rejected"),
        pytest.param(
            _UNVERIFIED, None, "email_verification_required", None, False, id="unverified"
        ),
        pytest.param(
            _VERIFIED,
            GatewayAuthError("user no longer exists"),
            "gateway_rejected",
            login.GatewayCheck(False, "user no longer exists"),
            True,
            id="gateway-rejected",
        ),
        pytest.param(
            _VERIFIED,
            GatewayUnavailableError("connection refused"),
            None,
            login.GatewayCheck(None, "connection refused"),
            True,
            id="gateway-unreachable-still-saves",
        ),
    ],
)
def test_complete_login_saves_only_what_every_check_admits(
    monkeypatch: pytest.MonkeyPatch,
    user: CurrentUser | None,
    error: Exception | None,
    refusal: str | None,
    gateway: login.GatewayCheck | None,
    asked_gateway: bool,
) -> None:
    _identity(monkeypatch, user)
    seen = _gateway(monkeypatch, error)

    outcome = login.complete_login(_API, _TOKEN)

    assert outcome.refusal == refusal
    assert outcome.gateway == gateway
    assert seen == ([_TOKEN] if asked_gateway else [])
    stored = auth_file.load_auth()
    if refusal is None:
        assert outcome.saved is True
        assert stored == StoredAuth(api_url=_API, token=_TOKEN, expires_at=_TOKEN_EXPIRES)
        assert outcome.expires_at == _TOKEN_EXPIRES
    else:
        assert outcome.saved is False
        assert stored is None


def test_a_refused_login_leaves_the_previous_sign_in_in_place(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    previous = StoredAuth(api_url=_API, token="old", expires_at=datetime.now(UTC))
    auth_file.save_auth(previous)
    _identity(monkeypatch, _VERIFIED)
    _gateway(monkeypatch, GatewayAuthError("rejected"))

    assert login.complete_login(_API, _TOKEN).refusal == "gateway_rejected"
    loaded = auth_file.load_auth()
    assert loaded is not None and loaded.token == "old"


def test_a_stop_asked_for_during_the_checks_saves_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``should_stop`` is read after the gateway answered, so a stop that arrived
    while the checks ran keeps the previous sign-in."""
    previous = StoredAuth(api_url=_API, token="old", expires_at=datetime.now(UTC))
    auth_file.save_auth(previous)
    _identity(monkeypatch, _VERIFIED)
    stopped: list[bool] = []

    async def _fetch(**_kw: object) -> list[object]:
        stopped.append(True)  # the stop lands while the gateway is being asked
        return []

    monkeypatch.setattr(gateway_client, "fetch_models", _fetch)

    outcome = login.complete_login(_API, _TOKEN, should_stop=lambda: bool(stopped))

    assert (outcome.refusal, outcome.saved) == ("cancelled", False)
    loaded = auth_file.load_auth()
    assert loaded is not None and loaded.token == "old"


def test_a_stop_never_asked_for_lets_the_save_through(monkeypatch: pytest.MonkeyPatch) -> None:
    _identity(monkeypatch, _VERIFIED)
    _gateway(monkeypatch, None)

    outcome = login.complete_login(_API, _TOKEN, should_stop=lambda: False)

    assert outcome.refusal is None
    assert auth_file.load_auth() == StoredAuth(
        api_url=_API, token=_TOKEN, expires_at=_TOKEN_EXPIRES
    )


@pytest.mark.parametrize(
    ("stored", "user", "reason", "email"),
    [
        pytest.param(False, _VERIFIED, "missing", None, id="nothing-saved"),
        pytest.param(True, None, "invalid", None, id="api-rejects-the-saved-token"),
        pytest.param(
            True, _UNVERIFIED, "email_verification_required", "user@example.com", id="unverified"
        ),
        pytest.param(True, _VERIFIED, None, "user@example.com", id="holds"),
    ],
)
def test_auth_status_reads_the_saved_sign_in(
    monkeypatch: pytest.MonkeyPatch,
    stored: bool,
    user: CurrentUser | None,
    reason: str | None,
    email: str | None,
) -> None:
    saved = StoredAuth(api_url=_API, token=_TOKEN, expires_at=datetime.now(UTC) + timedelta(1))
    if stored:
        auth_file.save_auth(saved)
    _identity(monkeypatch, user)

    status = login.auth_status()

    assert status.reason == reason
    assert status.authenticated is (reason is None)
    assert status.email == email
    assert (status.stored is not None and status.stored.token == _TOKEN) is stored
    # Reading never deletes: an unverified account's token holds once verified.
    assert (auth_file.load_auth() is not None) is stored
