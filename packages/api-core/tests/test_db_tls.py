"""DB-TLS settings → per-driver connect_args translation.

Pins the in-transit-encryption contract: the ``prefer`` default is a true no-op (so
existing non-TLS deployments are unchanged), and each stricter mode produces the right
asyncpg / psycopg connect args.
"""

from __future__ import annotations

import ssl
from pathlib import Path

import certifi
import pytest
from alkera_core.config import Settings
from alkera_core.db import tls

# A real, loadable CA bundle on disk — create_default_context(cafile=...) reads it
# eagerly, so verify-* cases need a path that actually exists.
_REAL_CA = certifi.where()

# Every case here builds an EXPLICIT Settings and asserts what the validator makes of
# exactly those values; an ambient DB/Files/OAuth env var reaches the same constructor
# and answers with a refusal about something else entirely.
pytestmark = pytest.mark.usefixtures("sealed_settings_env")


def _settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, **overrides)  # type: ignore[arg-type, call-arg]


def test_prefer_is_a_noop_for_both_drivers() -> None:
    s = _settings(database_sslmode="prefer")
    assert tls.asyncpg_connect_args(s) == {}
    assert tls.psycopg_connect_args(s) == {}


def test_disable_for_both_drivers() -> None:
    s = _settings(database_sslmode="disable")
    assert tls.asyncpg_connect_args(s) == {"ssl": False}
    assert tls.psycopg_connect_args(s) == {"sslmode": "disable"}


def test_require_encrypts_without_verifying() -> None:
    s = _settings(database_sslmode="require")
    ctx = tls.asyncpg_connect_args(s)["ssl"]
    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.verify_mode == ssl.CERT_NONE
    assert ctx.check_hostname is False
    assert tls.psycopg_connect_args(s) == {"sslmode": "require"}


def test_verify_ca_checks_chain_not_hostname() -> None:
    s = _settings(database_sslmode="verify-ca", database_sslrootcert=_REAL_CA)
    ctx = tls.asyncpg_connect_args(s)["ssl"]
    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname is False
    assert tls.psycopg_connect_args(s) == {"sslmode": "verify-ca", "sslrootcert": _REAL_CA}


def test_verify_full_checks_chain_and_hostname() -> None:
    s = _settings(database_sslmode="verify-full", database_sslrootcert=_REAL_CA)
    ctx = tls.asyncpg_connect_args(s)["ssl"]
    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname is True


def test_prod_rejects_unreadable_rootcert(tmp_path: Path) -> None:
    missing = tmp_path / "nope-ca.pem"
    with pytest.raises(ValueError, match="is not a readable file"):
        Settings(  # type: ignore[call-arg]
            _env_file=None,
            app_env="production",
            auth_jwt_secret="real-prod-secret-at-least-32-bytes-long",
            token_hash_pepper="real-prod-pepper-at-least-32-bytes-long",
            auth_cookie_secure=True,
            smtp_host="smtp.example.com",
            smtp_username="u",
            smtp_password="p",
            database_url="postgresql+asyncpg://prod:prod@db.internal:5432/alkera",
            api_cors_origins="https://app.example",
            oauth_mock_enabled=False,
            turnstile_secret_key="t",
            temporal_address="temporal.example.internal:7233",
            database_sslmode="verify-full",
            database_sslrootcert=str(missing),
        )


@pytest.mark.parametrize("mode", ["verify-ca", "verify-full"])
def test_prod_requires_rootcert_for_verify_modes(mode: str) -> None:
    with pytest.raises(ValueError, match="DATABASE_SSLROOTCERT must be set"):
        Settings(  # type: ignore[call-arg]
            _env_file=None,
            app_env="production",
            auth_jwt_secret="real-prod-secret-at-least-32-bytes-long",
            token_hash_pepper="real-prod-pepper-at-least-32-bytes-long",
            auth_cookie_secure=True,
            smtp_host="smtp.example.com",
            smtp_username="u",
            smtp_password="p",
            database_url="postgresql+asyncpg://prod:prod@db.internal:5432/alkera",
            api_cors_origins="https://app.example",
            oauth_mock_enabled=False,
            turnstile_secret_key="t",
            temporal_address="temporal.example.internal:7233",
            database_sslmode=mode,
        )
