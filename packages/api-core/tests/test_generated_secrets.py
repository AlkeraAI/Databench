"""Server secrets generated on first boot into ``GENERATED_SECRETS_DIR``."""

from __future__ import annotations

import stat
import sys
from pathlib import Path

import pytest
from _settings_env import seal_settings_env
from alkera_core import generated_secrets
from alkera_core.config import Settings

_PRODUCTION: dict[str, object] = {
    "app_env": "production",
    "auth_cookie_secure": True,
    "smtp_host": "smtp.example.com",
    "smtp_username": "smtp-user",
    "smtp_password": "smtp-pass",
    "database_url": "postgresql+asyncpg://prod:prod@db.internal:5432/alkera",
    "api_cors_origins": "https://app.example.com",
    "frontend_base_url": "https://app.example.com",
    "oauth_mock_enabled": False,
    "turnstile_secret_key": "real-prod-turnstile-secret",
    "temporal_address": "temporal.example.internal:7233",
    "files_enabled": True,
    "files_content_base_url": "https://content.example-usercontent.com",
}


@pytest.fixture(autouse=True)
def _sealed(monkeypatch: pytest.MonkeyPatch) -> None:
    seal_settings_env(monkeypatch)


def _production(**overrides: object) -> Settings:
    return Settings(_env_file=None, **{**_PRODUCTION, **overrides})  # type: ignore[arg-type, call-arg]


def test_production_refuses_a_missing_secret_when_no_directory_is_kept() -> None:
    with pytest.raises(ValueError, match="AUTH_JWT_SECRET must be set"):
        _production()


def test_production_boots_on_secrets_generated_into_the_directory(tmp_path: Path) -> None:
    s = _production(generated_secrets_dir=str(tmp_path))
    assert s.auth_jwt_secret == (tmp_path / "AUTH_JWT_SECRET").read_text().strip()
    assert s.token_hash_pepper == (tmp_path / "TOKEN_HASH_PEPPER").read_text().strip()
    assert s.files_content_signing_key is not None
    signing_key = s.files_content_signing_key.get_secret_value()
    assert signing_key == (tmp_path / "FILES_CONTENT_SIGNING_KEY").read_text().strip()
    assert len({s.auth_jwt_secret, s.token_hash_pepper, signing_key}) == 3
    assert all(len(value.encode()) >= 64 for value in (s.auth_jwt_secret, signing_key))


def test_every_later_boot_reads_the_same_secrets(tmp_path: Path) -> None:
    first = _production(generated_secrets_dir=str(tmp_path))
    second = _production(generated_secrets_dir=str(tmp_path))
    assert (first.auth_jwt_secret, first.token_hash_pepper, first.secret_box_key) == (
        second.auth_jwt_secret,
        second.token_hash_pepper,
        second.secret_box_key,
    )


def test_an_operator_value_wins_and_is_never_written(tmp_path: Path) -> None:
    chosen = "an-operator-chosen-secret-of-more-than-32-bytes"
    s = _production(generated_secrets_dir=str(tmp_path), auth_jwt_secret=chosen)
    assert s.auth_jwt_secret == chosen
    assert not (tmp_path / "AUTH_JWT_SECRET").exists()


def test_the_secret_box_key_is_generated_only_beside_a_generated_jwt_secret(
    tmp_path: Path,
) -> None:
    """With an operator's JWT secret, stored credentials may already be keyed off
    it; a new box key would strand them, so none is generated."""
    fresh = _production(generated_secrets_dir=str(tmp_path / "fresh"))
    assert fresh.secret_box_key == (tmp_path / "fresh" / "SECRET_BOX_KEY").read_text().strip()
    upgraded = _production(
        generated_secrets_dir=str(tmp_path / "upgraded"),
        auth_jwt_secret="an-operator-chosen-secret-of-more-than-32-bytes",
    )
    assert upgraded.secret_box_key is None
    assert not (tmp_path / "upgraded" / "SECRET_BOX_KEY").exists()


def test_a_blank_value_counts_as_unset(tmp_path: Path) -> None:
    s = _production(generated_secrets_dir=str(tmp_path), token_hash_pepper="  ")
    assert s.token_hash_pepper == (tmp_path / "TOKEN_HASH_PEPPER").read_text().strip()


@pytest.mark.skipif(sys.platform == "win32", reason="needs POSIX file mode bits")
def test_generated_files_are_readable_by_the_owner_only(tmp_path: Path) -> None:
    _production(generated_secrets_dir=str(tmp_path))
    for name in ("AUTH_JWT_SECRET", "TOKEN_HASH_PEPPER", "FILES_CONTENT_SIGNING_KEY"):
        assert stat.S_IMODE((tmp_path / name).stat().st_mode) == 0o600


def test_a_process_that_loses_the_race_reads_the_winners_value(tmp_path: Path) -> None:
    """Two processes booting at once must agree, or each signs with its own key."""

    def winner_writes_first() -> str:
        (tmp_path / "AUTH_JWT_SECRET").write_text("the-winner\n", encoding="utf-8")
        return "the-loser"

    value = generated_secrets.load_or_create(tmp_path, "AUTH_JWT_SECRET", winner_writes_first)
    assert value == "the-winner"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["AUTH_JWT_SECRET"]
