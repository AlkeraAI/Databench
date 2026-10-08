"""The deployment's custom CA (``OUTBOUND_CA_BUNDLE``), on the standard library.

One parser for every outbound TLS client: the backend and gateway build their
httpx clients on it (:mod:`alkera_core.http`), and the box supervisor, which
keeps an HTTP stack out of its trusted base, builds its urllib context on it.
The value is a PEM file path or inline PEM text; either is added to the system
trust store, and a value that is neither is refused with a message that names
the setting instead of surfacing a bare ``ssl`` or ``OSError``.
"""

from __future__ import annotations

import ssl
from pathlib import Path


class OutboundTlsConfigError(ValueError):
    """``OUTBOUND_CA_BUNDLE`` is set but is neither a readable PEM file nor valid PEM text."""


def ca_bundle_ssl_context(bundle: str | None) -> ssl.SSLContext | None:
    """An :class:`ssl.SSLContext` trusting ``bundle`` in addition to the system
    store, or ``None`` when no bundle is configured."""
    if not bundle:
        return None
    ctx = ssl.create_default_context()
    stripped = bundle.strip()
    if "BEGIN CERTIFICATE" in stripped:
        try:
            ctx.load_verify_locations(cadata=stripped)
        except (ssl.SSLError, ValueError) as exc:
            raise OutboundTlsConfigError(f"OUTBOUND_CA_BUNDLE is not valid PEM: {exc}") from exc
        return ctx
    path = Path(bundle)
    if not path.is_file():
        raise OutboundTlsConfigError(
            f"OUTBOUND_CA_BUNDLE={bundle!r} is neither a readable file path nor inline PEM text"
        )
    try:
        ctx.load_verify_locations(cafile=str(path))
    except (ssl.SSLError, OSError) as exc:
        raise OutboundTlsConfigError(
            f"OUTBOUND_CA_BUNDLE file {bundle!r} is not valid: {exc}"
        ) from exc
    return ctx


__all__ = ["OutboundTlsConfigError", "ca_bundle_ssl_context"]
