"""LocalCredentialManager: resolve/store a CredentialRef from
env vars / files / keychain at the I/O boundary, never plaintext into context."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from alkera_cli.plugins.plugin_base import (
    CredentialRef,
    CredentialResolutionError,
    LocalCredentialManager,
    OAuthCredential,
    TokenCredential,
)
from pydantic import SecretStr


def test_resolve_env_returns_a_secret_str() -> None:
    mgr = LocalCredentialManager(env={"SNOWFLAKE_PAT": "the-pat"})
    cred = mgr.resolve_sync(CredentialRef(scheme="env", locator="SNOWFLAKE_PAT"))
    assert isinstance(cred, TokenCredential)
    assert cred.token.get_secret_value() == "the-pat"
    # SecretStr redacts on repr — the secret never leaks into a log line.
    assert "the-pat" not in repr(cred)


def test_resolve_env_unset_raises() -> None:
    mgr = LocalCredentialManager(env={})
    with pytest.raises(CredentialResolutionError, match="unset"):
        mgr.resolve_sync(CredentialRef(scheme="env", locator="SNOWFLAKE_PAT"))


def test_resolve_file(tmp_path: Path) -> None:
    secret = tmp_path / "token.txt"
    secret.write_text("  file-secret\n")  # whitespace is stripped
    mgr = LocalCredentialManager()
    cred = mgr.resolve_sync(CredentialRef(scheme="file", locator=str(secret)))
    assert isinstance(cred, TokenCredential)
    assert cred.token.get_secret_value() == "file-secret"


def test_resolve_missing_file_raises(tmp_path: Path) -> None:
    mgr = LocalCredentialManager()
    with pytest.raises(CredentialResolutionError):
        mgr.resolve_sync(CredentialRef(scheme="file", locator=str(tmp_path / "nope")))


def test_resolve_unknown_scheme_raises() -> None:
    mgr = LocalCredentialManager()
    with pytest.raises(CredentialResolutionError, match="not resolvable"):
        mgr.resolve_sync(CredentialRef(scheme="snowflake_toml", locator="default"))


async def test_async_resolve_matches_sync() -> None:
    mgr = LocalCredentialManager(env={"X": "y"})
    cred = await mgr.resolve(CredentialRef(scheme="env", locator="X"))
    assert isinstance(cred, TokenCredential) and cred.token.get_secret_value() == "y"


async def test_store_to_file_roundtrips(tmp_path: Path) -> None:
    path = tmp_path / "stored.txt"
    ref = CredentialRef(scheme="file", locator=str(path))
    mgr = LocalCredentialManager()
    from pydantic import SecretStr

    await mgr.store(ref, TokenCredential(token=SecretStr("round-trip")))
    assert path.read_text() == "round-trip"
    # owner-only perms (best-effort; chmod modes are POSIX — no-op on Windows).
    if sys.platform != "win32":
        assert (path.stat().st_mode & 0o777) == 0o600
    cred = mgr.resolve_sync(ref)
    assert isinstance(cred, TokenCredential) and cred.token.get_secret_value() == "round-trip"


# --- the oauth scheme (token bundles) ----------------------------------------


async def test_oauth_store_then_resolve_roundtrips(tmp_path: Path) -> None:
    from pydantic import SecretStr

    path = tmp_path / "conn" / "oauth.json"
    ref = CredentialRef(scheme="oauth", locator=str(path))
    mgr = LocalCredentialManager()
    await mgr.store(
        ref,
        OAuthCredential(
            access_token=SecretStr("at-1"), refresh_token=SecretStr("rt-1"), expires_at=123
        ),
    )
    if sys.platform != "win32":
        assert (path.stat().st_mode & 0o777) == 0o600
    cred = mgr.resolve_sync(ref)
    assert isinstance(cred, OAuthCredential)
    assert cred.access_token.get_secret_value() == "at-1"
    assert cred.refresh_token is not None
    assert cred.refresh_token.get_secret_value() == "rt-1"
    assert cred.expires_at == 123
    # The raw token text never appears in the credential's repr (SecretStr).
    assert "at-1" not in repr(cred)


async def test_oauth_roundtrip_without_refresh_token(tmp_path: Path) -> None:
    from pydantic import SecretStr

    ref = CredentialRef(scheme="oauth", locator=str(tmp_path / "oauth.json"))
    mgr = LocalCredentialManager()
    await mgr.store(ref, OAuthCredential(access_token=SecretStr("only-access")))
    cred = mgr.resolve_sync(ref)
    assert isinstance(cred, OAuthCredential)
    assert cred.refresh_token is None


def test_oauth_resolve_missing_file_raises(tmp_path: Path) -> None:
    mgr = LocalCredentialManager()
    with pytest.raises(CredentialResolutionError, match="oauth token bundle"):
        mgr.resolve_sync(CredentialRef(scheme="oauth", locator=str(tmp_path / "nope.json")))


def test_oauth_resolve_corrupt_file_raises(tmp_path: Path) -> None:
    path = tmp_path / "oauth.json"
    path.write_text("{torn write")
    mgr = LocalCredentialManager()
    with pytest.raises(CredentialResolutionError, match="oauth token bundle"):
        mgr.resolve_sync(CredentialRef(scheme="oauth", locator=str(path)))


def test_oauth_resolve_empty_access_token_raises(tmp_path: Path) -> None:
    """A bundle written before the flow completed (no access token yet) must
    surface as re-authorize-needed, not as an empty-string credential."""
    import json

    path = tmp_path / "oauth.json"
    path.write_text(json.dumps({"schema_version": "1.0.0", "access_token": ""}))
    mgr = LocalCredentialManager()
    with pytest.raises(CredentialResolutionError, match="re-authorize"):
        mgr.resolve_sync(CredentialRef(scheme="oauth", locator=str(path)))


async def test_oauth_store_rejects_non_oauth_credential(tmp_path: Path) -> None:
    """Flattening a token credential into the oauth scheme would silently drop
    refresh/expiry — refuse it loudly."""
    from pydantic import SecretStr

    ref = CredentialRef(scheme="oauth", locator=str(tmp_path / "oauth.json"))
    mgr = LocalCredentialManager()
    with pytest.raises(CredentialResolutionError, match="stores OAuthCredential"):
        await mgr.store(ref, TokenCredential(token=SecretStr("t")))
    assert not (tmp_path / "oauth.json").exists()


async def test_oauth_store_overwrites_previous_bundle(tmp_path: Path) -> None:
    from pydantic import SecretStr

    ref = CredentialRef(scheme="oauth", locator=str(tmp_path / "oauth.json"))
    mgr = LocalCredentialManager()
    await mgr.store(ref, OAuthCredential(access_token=SecretStr("old"), expires_at=1))
    await mgr.store(ref, OAuthCredential(access_token=SecretStr("new"), expires_at=2))
    cred = mgr.resolve_sync(ref)
    assert isinstance(cred, OAuthCredential)
    assert cred.access_token.get_secret_value() == "new"
    assert cred.expires_at == 2


# -- schemes the owning code registers -------------------------------------------


def test_an_unregistered_scheme_fails_closed_naming_it() -> None:
    from alkera_cli.contracts.tool_types import CredentialRef
    from alkera_cli.plugins.plugin_base.credential_manager import (
        CredentialResolutionError,
        LocalCredentialManager,
    )

    manager = LocalCredentialManager(env={})
    with pytest.raises(CredentialResolutionError, match="'nobody_registered'"):
        manager.resolve_sync(CredentialRef(scheme="nobody_registered", locator="x"))
    with pytest.raises(CredentialResolutionError, match="'nobody_registered'"):
        manager.store_sync(
            CredentialRef(scheme="nobody_registered", locator="x"),
            TokenCredential(token=SecretStr("s")),
        )


def test_a_registered_storer_persists_its_scheme(monkeypatch: pytest.MonkeyPatch) -> None:
    from alkera_cli.contracts.tool_types import CredentialRef
    from alkera_cli.plugins.plugin_base import credential_manager

    stored: dict[str, str] = {}

    def storer(locator: str, cred: object) -> None:
        assert isinstance(cred, TokenCredential)
        stored[locator] = cred.token.get_secret_value()

    monkeypatch.setitem(credential_manager._EXTRA_STORERS, "vault_test", storer)
    credential_manager.LocalCredentialManager(env={}).store_sync(
        CredentialRef(scheme="vault_test", locator="db/main"),
        TokenCredential(token=SecretStr("hunter2")),
    )
    assert stored == {"db/main": "hunter2"}
