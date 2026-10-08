"""The concrete ``CredentialManager``.

Resolves a ``CredentialRef`` → a live ``Credential`` at the I/O boundary, from
the local machine: an environment variable, a chmod-600 file, or the OS keychain
(via ``keyring`` when it's installed). Every other scheme (an OAuth token bundle,
a team connection's brokered lease, a dbt profile) is registered by the code that
owns it (:func:`register_credential_resolver`). It NEVER
returns plaintext into agent context — a caller gets a ``SecretStr``-bearing
``Credential`` and reads it only when it actually opens a connection.

Driver-native schemes (e.g. a Snowflake ``connections.toml`` profile resolving
keypair / external-browser SSO / OAuth) are NOT this manager's job — the driver
resolves those itself from the profile; ``resolve`` raises for an unknown scheme
so a caller can't silently mishandle one.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from pydantic import SecretStr

from alkera_cli.contracts.tool_types import CredentialRef
from alkera_cli.plugins.plugin_base.connection import (
    Connection,
    Credential,
    CredentialManager,
    TokenCredential,
    credential_secret_value,
    named_credential_ref,
    named_credential_refs,
    with_named_credential_refs,
)


class CredentialResolutionError(RuntimeError):
    """A ``CredentialRef`` couldn't be resolved (unset env / missing file /
    absent keychain entry / unresolvable scheme)."""


#: Plugin-registered resolvers for schemes the base manager doesn't know
#: (``dbt_profile``, ``airflow_conn`` — re-read the source at connect time so a
#: suggested connection is usable without duplicating a plaintext secret). A
#: resolver takes ``(locator, env)`` and returns a ``Credential`` or raises
#: ``CredentialResolutionError``. Kept out of the base manager to avoid coupling
#: ``plugin_base`` to the dbt/airflow parsers — the owning plugin registers on
#: import.
_EXTRA_RESOLVERS: dict[str, Any] = {}


def register_credential_resolver(scheme: str, resolver: Any) -> None:
    """Register a ``(locator, env) -> Credential`` resolver for ``scheme``. The
    owning plugin calls this on import; ``LocalCredentialManager`` consults it
    before failing an unknown scheme."""
    _EXTRA_RESOLVERS[scheme] = resolver


#: ``(locator, credential) -> None`` writers for registered schemes that persist
#: a credential themselves (an OAuth token bundle keeps its refresh token and
#: expiry, which no single string carries).
CredentialStorer = Callable[[str, Credential], None]
_EXTRA_STORERS: dict[str, CredentialStorer] = {}


def register_credential_storer(scheme: str, storer: CredentialStorer) -> None:
    """Register how ``scheme`` persists a credential. ``LocalCredentialManager``
    consults it before refusing to store an unknown scheme."""
    _EXTRA_STORERS[scheme] = storer


def _import_keyring() -> Any:
    try:
        import keyring  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - exercised only without keyring
        raise CredentialResolutionError(
            "keychain credentials need the 'keyring' package (not installed)"
        ) from exc
    return keyring


def _keychain_parts(locator: str) -> tuple[str, str]:
    """``"service:account"`` → ``(service, account)``; bare ``"service"`` reuses
    the service as the account."""
    service, _, account = locator.partition(":")
    return service, account or service


#: Credential-ref schemes whose locator names a single environment variable. The single
#: source of truth — reused by the SDK-child env builder (``integration_sdk_tool``) so the
#: two can never drift on which schemes a child must forward to authenticate.
ENV_CREDENTIAL_SCHEMES = frozenset({"env", "env_pat", "env_password"})


class LocalCredentialManager(CredentialManager):
    """Resolve/store a ``CredentialRef`` against env vars, files, or the keychain.

    ``env`` is injectable so a test can resolve without touching the real
    environment; ``resolve_sync`` exists for the connector's sync connect path
    (which already runs inside ``asyncio.to_thread``)."""

    #: Schemes whose locator names a single environment variable.
    _ENV_SCHEMES = ENV_CREDENTIAL_SCHEMES

    def __init__(self, *, env: Mapping[str, str] | None = None) -> None:
        self._env = dict(os.environ if env is None else env)

    def resolve_sync(self, ref: CredentialRef) -> Credential:
        """Resolve ``ref`` → ``Credential`` synchronously (the connect boundary).
        Built-in and plugin-registered schemes dispatch off the same table.

        Every resolver here blocks: a keychain prompt, a file read, a token
        refresh that dials a provider, a relay lease over the network. On the
        daemon's event-loop thread that freezes every other connection, every
        notification and the heartbeat for as long as the slowest of them takes,
        so an async caller must reach this through ``asyncio.to_thread``."""
        builtin = self._builtin_resolvers().get(ref.scheme)
        if builtin is not None:
            return builtin(ref.locator)
        resolver = _EXTRA_RESOLVERS.get(ref.scheme)
        if resolver is not None:
            cred = resolver(ref.locator, self._env)
            if not isinstance(cred, Credential):  # pragma: no cover - resolver contract
                raise CredentialResolutionError(
                    f"resolver for {ref.scheme!r} returned a non-Credential"
                )
            return cred
        raise CredentialResolutionError(
            f"scheme {ref.scheme!r} is not resolvable by LocalCredentialManager "
            "(driver-native schemes resolve inside the connector)"
        )

    def _builtin_resolvers(self) -> dict[str, Callable[[str], Credential]]:
        table: dict[str, Callable[[str], Credential]] = dict.fromkeys(
            self._ENV_SCHEMES, self._resolve_env
        )
        table["file"] = self._resolve_file
        table["keychain"] = self._resolve_keychain
        return table

    def _resolve_env(self, locator: str) -> Credential:
        value = self._env.get(locator)
        if not value:
            raise CredentialResolutionError(f"env credential {locator!r} is unset")
        return TokenCredential(token=SecretStr(value))

    def _resolve_file(self, locator: str) -> Credential:
        try:
            value = Path(locator).read_text().strip()
        except OSError as exc:
            # Plain str, no repr: a Windows path's backslashes double under repr
            # and the message stops naming a path the user can open.
            raise CredentialResolutionError(f"file credential {locator}: {exc}") from exc
        return TokenCredential(token=SecretStr(value))

    def _resolve_keychain(self, locator: str) -> Credential:
        service, account = _keychain_parts(locator)
        value = _import_keyring().get_password(service, account)
        if not value:
            raise CredentialResolutionError(f"keychain entry {locator!r} not found")
        return TokenCredential(token=SecretStr(value))

    async def resolve(self, ref: CredentialRef) -> Credential:
        return self.resolve_sync(ref)

    async def store(self, ref: CredentialRef, cred: Credential) -> None:
        # The actual write is blocking I/O (file / keychain) — off the loop.
        await asyncio.to_thread(self.store_sync, ref, cred)

    def store_sync(self, ref: CredentialRef, cred: Credential) -> None:
        storer = _EXTRA_STORERS.get(ref.scheme)
        if storer is not None:
            storer(ref.locator, cred)
            return
        secret = credential_secret_value(cred)
        if secret is None:
            raise CredentialResolutionError(f"cannot persist a {cred.kind!r} credential")
        if ref.scheme == "file":
            path = Path(ref.locator)
            path.parent.mkdir(parents=True, exist_ok=True)
            # Create the file owner-only ATOMICALLY (O_CREAT|0o600) so the secret never
            # exists with a wider umask-default mode in the window before a chmod — no
            # TOCTOU. Truncate an existing ref-file to overwrite cleanly.
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                os.write(fd, secret.encode("utf-8"))
            finally:
                os.close(fd)
            with contextlib.suppress(OSError):
                path.chmod(0o600)  # tighten if the file pre-existed with a wider mode
            return
        if ref.scheme == "keychain":
            service, account = _keychain_parts(ref.locator)
            _import_keyring().set_password(service, account, secret)
            return
        raise CredentialResolutionError(f"cannot store a {ref.scheme!r} credential")


def resolve_token_credential(conn: Connection, *, env_var: str) -> str:
    """The token/secret for ``conn``, resolved through the CredentialManager from
    its ``credential_ref`` (a stored form secret, or an env locator from
    discovery), falling back to ``env_var`` when no resolvable ref is set. The
    one resolver every token-authed plugin client shares."""
    ref = conn.credential_ref
    if ref is not None and ref.locator:
        with contextlib.suppress(CredentialResolutionError):
            cred = LocalCredentialManager().resolve_sync(ref)
            if isinstance(cred, TokenCredential):
                return cred.token.get_secret_value()
    return os.environ.get(env_var, "")


def credential_plaintexts(conn: Connection) -> tuple[str, ...]:
    """Every plaintext ``conn`` would have handed a driver, for REDACTION only.

    A service that refuses a credential is free to quote it back, and that
    sentence ends up in a durable state record and in the row a person reads, so
    the scrubber needs the same values the connector resolved. Nothing here puts
    a secret into anything a caller returns — the values only ever come back out
    of text through ``redact_secrets``.

    Best-effort and never raises: a reference that will not resolve is simply not
    matched, which is why every caller can hand the result straight to a
    scrubber on a path whose real job is something else.
    """
    refs = [
        ref
        for ref in (conn.credential_ref, *named_credential_refs(conn).values())
        if ref is not None
    ]
    manager = LocalCredentialManager()
    values: list[str] = []
    for ref in refs:
        try:
            credential = manager.resolve_sync(ref)
        except Exception:  # noqa: S112 - an unresolvable ref is simply not matched
            continue
        for name in ("token", "password", "access_token", "refresh_token", "private_key"):
            value = getattr(credential, name, None)
            if isinstance(value, SecretStr) and value.get_secret_value():
                values.append(value.get_secret_value())
    return tuple(values)


__all__ = [
    "CredentialResolutionError",
    "LocalCredentialManager",
    "credential_plaintexts",
    "named_credential_ref",
    "named_credential_refs",
    "register_credential_resolver",
    "register_credential_storer",
    "resolve_token_credential",
    "with_named_credential_refs",
]
