"""`api_credentials`: what a caller sends to the API on the stored login's behalf."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from alkera_cli.account import auth_file
from alkera_cli.account.auth_file import StoredAuth, api_credentials, save_auth
from alkera_cli.host import paths


@pytest.fixture(autouse=True)
def _isolate_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    home = tmp_path / "alkera-home"
    monkeypatch.setattr(paths, "ALKERA_HOME", home)
    monkeypatch.setattr(paths, "AUTH_FILE_PATH", home / "auth.yml")
    monkeypatch.setattr(auth_file, "AUTH_FILE_PATH", home / "auth.yml")


def _save(token: str) -> None:
    save_auth(
        StoredAuth(
            api_url="https://api.alkera.test",
            token=token,
            expires_at=datetime(2099, 1, 1, tzinfo=UTC),
        )
    )


def test_a_stored_login_gives_its_url_and_token() -> None:
    _save("tok-1")

    assert api_credentials() == ("https://api.alkera.test", "tok-1")


def test_no_stored_login_gives_nothing() -> None:
    assert api_credentials() is None


def test_a_login_with_no_token_gives_nothing() -> None:
    _save("")

    assert api_credentials() is None
