"""Translate the DB-TLS settings (``DATABASE_SSLMODE`` / ``DATABASE_SSLROOTCERT``)
into per-driver SQLAlchemy ``connect_args``.

The app engine uses **asyncpg** and the Alembic engine uses **psycopg**; the two take
different connect args for the same intent, so each gets its native form here. The
``prefer`` default returns *no* args at all, so a deployment that never sets these
behaves exactly as before (no TLS forced on the local dev Postgres).

Modes mirror libpq:

- ``disable``    — never use TLS.
- ``prefer``     — try TLS, fall back to plaintext (the driver default; emitted as no args).
- ``require``    — encrypt, but do **not** verify the server certificate or hostname.
- ``verify-ca``  — verify the cert chain against ``DATABASE_SSLROOTCERT`` (no hostname check).
- ``verify-full``— verify the chain **and** the hostname.
"""

from __future__ import annotations

import ssl
from typing import Any

from alkera_core.config import Settings, settings


def asyncpg_connect_args(s: Settings | None = None) -> dict[str, Any]:
    """``connect_args`` for ``create_async_engine`` (asyncpg driver)."""
    s = s or settings
    mode = s.database_sslmode
    if mode == "prefer":
        return {}  # asyncpg default — preserves pre-TLS behavior exactly
    if mode == "disable":
        return {"ssl": False}
    ctx = ssl.create_default_context(cafile=s.database_sslrootcert or None)
    if mode == "require":
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    elif mode == "verify-ca":
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_REQUIRED
    else:  # verify-full
        ctx.check_hostname = True
        ctx.verify_mode = ssl.CERT_REQUIRED
    return {"ssl": ctx}


def psycopg_connect_args(s: Settings | None = None) -> dict[str, str]:
    """``connect_args`` for the sync (psycopg / Alembic) engine — libpq keywords."""
    s = s or settings
    if s.database_sslmode == "prefer" and not s.database_sslrootcert:
        return {}  # psycopg default — preserves pre-TLS behavior exactly
    args: dict[str, str] = {"sslmode": s.database_sslmode}
    if s.database_sslrootcert:
        args["sslrootcert"] = s.database_sslrootcert
    return args
