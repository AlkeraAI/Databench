"""Driver connect arguments shared by the host-addressed SQL plugins.

Every secret here is resolved at the I/O boundary, on the way into a driver call, and
is never persisted, logged or put on the connection.
"""

from __future__ import annotations

from typing import Any

from alkera_cli.plugins.plugin_base.connection import Connection, TokenCredential
from alkera_cli.plugins.plugin_base.credential_manager import LocalCredentialManager


def token_secret(conn: Connection) -> str | None:
    """The password or token ``conn``'s credential reference resolves to, or ``None`` when
    it names no secret or the secret is not a token."""
    ref = conn.credential_ref
    if ref is None or not ref.locator:
        return None
    cred = LocalCredentialManager().resolve_sync(ref)
    return cred.token.get_secret_value() if isinstance(cred, TokenCredential) else None


def host_database_connect_kwargs(conn: Connection, *, user_key: str) -> dict[str, Any]:
    """``host``, the user (under the driver's own ``user_key``), ``port`` and ``database``
    from the connection's attributes, plus its resolved ``password`` when it has one."""
    attrs = conn.attributes
    kwargs: dict[str, Any] = {
        "host": str(attrs.get("host", "")),
        user_key: str(attrs.get("user", "")),
    }
    if attrs.get("port"):
        kwargs["port"] = int(attrs["port"])
    if attrs.get("dbname"):
        kwargs["database"] = str(attrs["dbname"])
    secret = token_secret(conn)
    if secret is not None:
        kwargs["password"] = secret
    return kwargs


__all__ = ["host_database_connect_kwargs", "token_secret"]
