"""Instantiate a SQLAlchemy Engine from a generic-SQL ``Connection``.

``open_engine`` is the factory behind :class:`IntegrationSdkCapability` for the generic
connector: a ``sqlalchemy.engine.Engine`` on the connection's stored URL, with the
split-out secret re-injected through the ``LocalCredentialManager`` at the I/O boundary —
the same resolution the plugin's own connect path performs — and ``NullPool`` so no pooled
connection holds the secret open. The Engine is the binding because the model can
``engine.connect()`` + ``text(...)``, ``sqlalchemy.inspect(engine)``,
``pd.read_sql(..., engine)``, and still reach the native DBAPI driver via
``engine.raw_connection()``. Called in the ``call_integration_sdk`` CHILD process, so the
secret never crosses a process boundary (the serialized ``Connection`` carries only a
``credential_ref``). FAILS CLOSED: an unresolvable ``credential_ref`` raises rather than
silently connecting without the secret.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from alkera_core.connectors.generic_sql import URL_ATTR

from alkera_cli.plugins.plugin_base.connection import Connection, TokenCredential
from alkera_cli.plugins.plugin_base.credential_manager import LocalCredentialManager

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine


def open_engine(conn: Connection) -> Engine:
    """Build an authenticated Engine for ``conn`` (URL + re-injected secret, ``NullPool``)."""
    from sqlalchemy import create_engine
    from sqlalchemy.engine import make_url
    from sqlalchemy.pool import NullPool

    raw_url = str(conn.attributes.get(URL_ATTR, ""))
    if not raw_url:
        raise ValueError(f"connection {conn.handle!r} has no database URL")
    url = make_url(raw_url)
    ref = conn.credential_ref
    if ref is not None and ref.locator:
        # ``resolve_sync`` raises ``CredentialResolutionError`` when the ref doesn't
        # resolve — propagated (fail closed) rather than connecting without the secret.
        cred = LocalCredentialManager().resolve_sync(ref)
        if isinstance(cred, TokenCredential):
            url = url.set(password=cred.token.get_secret_value())
    return create_engine(url, poolclass=NullPool)


__all__ = ["open_engine"]
