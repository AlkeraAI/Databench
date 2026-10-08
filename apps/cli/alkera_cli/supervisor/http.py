"""The supervisor's few calls to the backend, on the standard library.

Four calls (claim, heartbeat, routing, worker credentials) do not need an HTTP
stack in the box's trusted base: urllib, run off the event loop, with the
deployment's own CA bundle when it names one (``OUTBOUND_CA_BUNDLE``, a file
or inline PEM, as everywhere else in Alkera).
"""

from __future__ import annotations

import asyncio
import json
import os
import ssl
import urllib.error
import urllib.request
from collections.abc import Mapping
from typing import Final

from alkera_core.ca_bundle import ca_bundle_ssl_context

TIMEOUT_SECONDS: Final = 15.0
#: The most a supervisor reads of one answer (a routing page is far smaller).
MAX_BODY_BYTES: Final = 8 << 20


class ApiError(RuntimeError):
    """The backend answered with an error status, or could not be reached
    (``status`` 0)."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"{status}: {message}" if status else message)
        self.status = status


def ssl_context(env: Mapping[str, str] | None = None) -> ssl.SSLContext:
    """The system's trust store, plus the deployment's CA when it names one; a
    bundle that is neither a readable PEM file nor PEM text is refused
    (:class:`~alkera_core.ca_bundle.OutboundTlsConfigError`)."""
    bundle = (os.environ if env is None else env).get("OUTBOUND_CA_BUNDLE", "").strip()
    return ca_bundle_ssl_context(bundle) or ssl.create_default_context()


def _call(
    method: str,
    url: str,
    headers: Mapping[str, str],
    body: object | None,
    context: ssl.SSLContext,
) -> object:
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(url, data=data, method=method)  # noqa: S310 -- the deployment's own API URL
    for name, value in headers.items():
        request.add_header(name, value)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    request.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS, context=context) as answer:  # noqa: S310
            raw = answer.read(MAX_BODY_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raise ApiError(exc.code, exc.reason if isinstance(exc.reason, str) else "refused") from None
    except (urllib.error.URLError, OSError) as exc:
        raise ApiError(0, f"unreachable: {exc}") from None
    if len(raw) > MAX_BODY_BYTES:
        raise ApiError(0, "the answer is past the size a supervisor reads")
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except ValueError:
        raise ApiError(0, "the answer is not JSON") from None


class Api:
    """JSON over HTTP to one base URL with fixed headers."""

    def __init__(self, base: str, headers: Mapping[str, str]) -> None:
        self._base = base.rstrip("/")
        self._headers = dict(headers)
        self._context = ssl_context()

    async def call(self, method: str, path: str, body: object | None = None) -> object:
        return await asyncio.to_thread(
            _call, method, self._base + path, self._headers, body, self._context
        )


__all__ = ["MAX_BODY_BYTES", "TIMEOUT_SECONDS", "Api", "ApiError", "ssl_context"]
