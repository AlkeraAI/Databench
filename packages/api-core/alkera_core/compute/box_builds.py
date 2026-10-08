"""Reading box builds from the release host: the I/O half of the box contract.

:mod:`alkera_core.compute.box_contract` decides, from plain documents, which
release a machine installs and which command it starts; this module fetches
those documents. The fetcher is a seam: a deployment reads its configured
release base over HTTP, and a test installs a fetcher that serves documents
from memory (:func:`use_release_host`).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any

import httpx

from alkera_core.compute.bootstrap import NodeBootProfile
from alkera_core.compute.box_contract import (
    SOURCE_VERSION,
    BootstrapPlan,
    FetchJson,
    ReleaseHostError,
    bootstrap_plan,
    resolve_release,
    source_build,
)
from alkera_core.compute.daemon_source import NodeDaemonSource, daemon_source
from alkera_core.compute.provider import TRANSIENT_FAILURE, ComputeProviderError
from alkera_core.config import Settings
from alkera_core.http import async_client

#: How long one read of the release host may take.
FETCH_TIMEOUT_SECONDS = 15.0
#: The release host sits behind a CDN whose firewall refuses some library user
#: agents; the release scripts read it as curl does, and so does this.
USER_AGENT = "curl/alkera-bootstrap"

ReleaseHost = Callable[[str], FetchJson]


def http_release_host(base_url: str) -> FetchJson:
    """Read JSON documents under ``base_url`` over HTTPS.

    A path the host has no object at is ``None``: a 404, or the 403 the public
    bucket behind the CDN answers for a key it does not hold (it grants no
    listing, so it cannot say 404). Any other failure is a
    :class:`ReleaseHostError`."""
    base = base_url.rstrip("/")

    async def fetch(path: str) -> Mapping[str, Any] | None:
        try:
            async with async_client(
                timeout=FETCH_TIMEOUT_SECONDS, headers={"User-Agent": USER_AGENT}
            ) as client:
                answer = await client.get(f"{base}/{path}")
        except httpx.HTTPError as exc:
            raise ReleaseHostError(f"the release host did not answer for {path}: {exc}") from exc
        if answer.status_code == 404 or (
            answer.status_code == 403 and "<Code>AccessDenied</Code>" in answer.text
        ):
            return None
        if answer.status_code != 200:
            raise ReleaseHostError(f"the release host answered {answer.status_code} for {path}")
        try:
            doc = json.loads(answer.text)
        except ValueError as exc:
            raise ReleaseHostError(f"the release host's {path} is not JSON") from exc
        if not isinstance(doc, dict):
            raise ReleaseHostError(f"the release host's {path} is not a JSON object")
        return doc

    return fetch


_release_host: ReleaseHost = http_release_host


def use_release_host(host: ReleaseHost) -> ReleaseHost:
    """Read releases through ``host`` from now on; returns the one it replaced.
    A test installs an in-memory host this way and puts the old one back."""
    global _release_host
    previous = _release_host
    _release_host = host
    return previous


async def plan_for(
    profile: NodeBootProfile, config: Settings, *, source: NodeDaemonSource | None = None
) -> BootstrapPlan:
    """What a machine of ``profile`` installs and runs, its daemon coming from
    ``source`` (by default the one this process registered, :func:`daemon_source`).

    A box that runs the developer's source tree, or the node bundle this
    deployment built from it (:data:`~alkera_core.compute.daemon_source.SERVED_BUNDLE`),
    has every command this checkout has. Every other box installs the pinned
    release, else the one the environment's node channel names, else the
    stable release, and runs it in the profile's start mode when that build's
    manifest has it. A release host that cannot be read is a transient
    provider failure (the launch can be tried again); a build the backend
    cannot start is a ``ValueError``."""
    fetch_from = source or daemon_source()
    if profile.daemon == "source" or not fetch_from.needs_release_base:
        return bootstrap_plan(SOURCE_VERSION, source_build(), wanted=profile.start_mode)
    fetch = _release_host(fetch_from.release_base(config.alkera_node_release_base_url))
    try:
        return await resolve_release(
            fetch,
            pinned=config.alkera_node_daemon_version,
            channel=config.alkera_node_release_channel,
            wanted=profile.start_mode,
        )
    except ReleaseHostError as exc:
        raise ComputeProviderError(str(exc), kind=TRANSIENT_FAILURE) from exc


__all__ = [
    "FETCH_TIMEOUT_SECONDS",
    "USER_AGENT",
    "ReleaseHost",
    "http_release_host",
    "plan_for",
    "use_release_host",
]
