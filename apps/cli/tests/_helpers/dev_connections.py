"""Live connections sourced from the local dev control-plane database.

Most live suites read their warehouse config from ``.env.local``. Two of them cannot: the
Tinybird workspace and the PlanetScale database this repo develops against are *team*
connections, configured through the product's own admin form and stored in the dev
database's ``team_connections`` — non-secret fields in ``shared_values``, the credential
Fernet-encrypted in ``shared_secret_encrypted`` under the dev app's key. There is no
``.env`` spelling of them to copy, and inventing one would be a second source of truth for
a credential.

So this builds the connector's ``Connection`` the way the background worker does when it
verifies one: ``built_attributes`` over the row's non-secret values through the connector
catalog, and ``decrypt_secret`` for the credential. The plaintext is put in a
PROCESS-LOCAL environment variable that the connection points at with a ``CredentialRef``,
which is the same boundary every connector resolves its secret at — so the secret is never
on the ``Connection``, never in a log, and never in an assertion message.

**Read-only against the dev database**, and every entry point SKIPS rather than fails: no
database, no key, no row, or a row this build cannot open is a live suite that cannot run,
not a red.
"""

from __future__ import annotations

import os
import time
from typing import Any

import pytest
from alkera_cli.plugins.plugin_base import Connection, CredentialRef, Environment

#: The team whose connections the dev environment is configured with.
DEV_TEAM_ID = "9ec514c9-e20e-449e-930b-a6197bcc3299"

#: How long one dial of the dev control-plane database may take, and how many times it is
#: tried. Generous on purpose: this database also serves a running dev stack, and a skip
#: here costs the entire live tier its coverage.
_CONNECT_TIMEOUT = 30
_CONNECT_ATTEMPTS = 3
_CONNECT_BACKOFF = 2.0

_ROW_SQL = (
    "select auth_method, shared_values, shared_secret_encrypted"
    " from team_connections where team_id = %s and plugin = %s and handle = %s"
)


#: Names the dev control-plane database outright, for a worktree whose own
#: ``.env.workspace`` points ``DATABASE_URL_SYNC`` at its private test Postgres — which is
#: every worktree ``make`` has ever run in, and where the connections simply are not. Without
#: it the whole live tier skips on a healthy box, which reads as "no credentials" rather
#: than "asked the wrong server".
DEV_DSN_ENV = "ALKERA_DEV_CONNECTIONS_DSN"


def _dsn() -> str:
    """The dev control-plane DSN, in the form psycopg dials."""
    from _helpers.live_env import load_env

    override = os.environ.get(DEV_DSN_ENV) or load_env(DEV_DSN_ENV).get(DEV_DSN_ENV) or ""
    if override:
        return override.replace("postgresql+psycopg://", "postgresql://")
    dotenv = load_env("DATABASE_URL")
    raw = dotenv.get("DATABASE_URL_SYNC") or os.environ.get("DATABASE_URL_SYNC") or ""
    if not raw:
        pytest.skip("no DATABASE_URL_SYNC — the dev control-plane database is not configured")
    return raw.replace("postgresql+psycopg://", "postgresql://")


def _load_dev_settings() -> None:
    """Put the dev app's secret-box key in the environment before the box is built.

    ``decrypt_secret`` reads the process settings, and a live run started from a plain
    shell has neither ``.env`` nor ``.env.local`` loaded — which would look like a
    corrupted ciphertext rather than a missing key."""
    from _helpers.live_env import load_env

    for key in ("SECRET_BOX_KEY", "AUTH_JWT_SECRET"):
        value = load_env(key).get(key)
        if value and not os.environ.get(key):
            os.environ[key] = value


def team_connection(
    plugin: str,
    handle: str,
    *,
    env_var: str,
    team_id: str = DEV_TEAM_ID,
) -> Connection:
    """The dev environment's ``plugin``/``handle`` connection, ready to dial.

    ``env_var`` names the process-local variable the decrypted credential is placed in and
    the returned connection's ``CredentialRef`` points at."""
    try:
        import psycopg
        from alkera_core.auth.secret_box import InvalidToken, decrypt_secret
        from alkera_core.connectors.catalog import built_attributes, get_descriptor
    except ImportError as exc:  # pragma: no cover - a build without the dev extras
        pytest.skip(f"dev-database connections need {exc.name}")

    _load_dev_settings()
    row = None
    failure: psycopg.Error | None = None
    # The dev box serves a running stack as well as this read, and a five-second dial
    # against a loaded Postgres times out -- which silently skipped the WHOLE live tier
    # while the database was perfectly healthy. A skip has to mean "no database", so
    # give the dial room and try again before believing it.
    for attempt in range(_CONNECT_ATTEMPTS):
        try:
            with (
                psycopg.connect(_dsn(), connect_timeout=_CONNECT_TIMEOUT) as db,
                db.cursor() as cur,
            ):
                cur.execute(_ROW_SQL, (team_id, plugin, handle))
                row = cur.fetchone()
            break
        except psycopg.Error as exc:
            failure = exc
            if attempt + 1 < _CONNECT_ATTEMPTS:
                time.sleep(_CONNECT_BACKOFF)
    if failure is not None and row is None:
        kind = type(failure).__name__
        pytest.skip(f"dev control-plane database unreachable ({kind}); run `make infra-up`")
    if row is None:
        pytest.skip(f"no {plugin}/{handle} connection configured on team {team_id}")

    auth_method, values, ciphertext = row
    try:
        attributes: dict[str, Any] = dict(
            built_attributes(
                get_descriptor(plugin), handle, str(auth_method or ""), dict(values or {})
            )
        )
        secret = decrypt_secret(ciphertext) if ciphertext else ""
    except (KeyError, InvalidToken, ValueError) as exc:
        pytest.skip(
            f"the {plugin}/{handle} credential cannot be opened here ({type(exc).__name__})"
        )
    if not secret:
        pytest.skip(f"the {plugin}/{handle} connection carries no shared credential")

    os.environ[env_var] = secret
    return Connection(
        handle=f"{handle}_live",
        plugin=plugin,
        dialect=plugin,
        environment=_environment(attributes),
        urn_namespace=f"{plugin}://{attributes.get('host', '')}",
        credential_ref=CredentialRef(scheme="env", locator=env_var),
        attributes=attributes,
    )


def _environment(attributes: dict[str, Any]) -> Environment:
    """The row's own deployment-tier label, or ``dev`` when it names one this build does
    not know — a descriptive field must never be the thing that fails a live run."""
    try:
        return Environment(str(attributes.get("environment") or "dev"))
    except ValueError:
        return Environment.DEV


__all__ = ["DEV_DSN_ENV", "DEV_TEAM_ID", "team_connection"]
