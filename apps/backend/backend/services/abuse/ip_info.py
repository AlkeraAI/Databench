"""Best-effort IP geo/network lookups for the admin console.

Uses ipapi.co's key-less JSON endpoint (HTTPS, generous free tier) — enough
for the "is this a hosting-provider IP in a country we have no users in?"
read an abuse investigation needs. Deliberately forgiving: a deployment with
no egress (self-hosted/VPC) or a rate-limited hour gets an ``error`` field,
never a failed admin page. Results are cached per IP for the process
lifetime — geo data for a fixed IP doesn't move on admin-console timescales.
"""

from __future__ import annotations

from urllib.parse import quote

import httpx
from alkera_core.http import async_client
from alkera_core.logging import get_logger
from alkera_core.schemas.identity.admin_users import IpInfo

log = get_logger(__name__)

_LOOKUP_TIMEOUT_SECONDS = 5.0
_CACHE_MAX_ENTRIES = 4096

_cache: dict[str, IpInfo] = {}


async def lookup(ip: str | None) -> IpInfo | None:
    """Geo/network context for ``ip``; None when there is no IP recorded."""
    if not ip:
        return None
    cached = _cache.get(ip)
    if cached is not None:
        return cached
    info = await _fetch(ip)
    if info.error is None and len(_cache) < _CACHE_MAX_ENTRIES:
        _cache[ip] = info
    return info


async def _fetch(ip: str) -> IpInfo:
    try:
        # Through the shared factory: this is the one outbound call the admin
        # console makes, and a VPC that routes everything through a proxy with an
        # internal CA has to be able to route this too.
        async with async_client(timeout=_LOOKUP_TIMEOUT_SECONDS) as client:
            resp = await client.get(f"https://ipapi.co/{quote(ip, safe='')}/json/")
    except httpx.HTTPError as exc:
        log.info("admin.ip_info.lookup_failed", ip=ip, error=str(exc))
        return IpInfo(ip=ip, error="lookup unavailable")
    if resp.status_code != 200:
        return IpInfo(ip=ip, error=f"lookup refused (HTTP {resp.status_code})")
    try:
        data = resp.json()
    except ValueError:
        return IpInfo(ip=ip, error="lookup returned a malformed response")
    if data.get("error"):
        # ipapi returns {"error": true, "reason": "..."} for bogons/reserved IPs.
        return IpInfo(ip=ip, error=str(data.get("reason") or "unknown address"))
    return IpInfo(
        ip=ip,
        city=data.get("city") or None,
        region=data.get("region") or None,
        country=data.get("country_name") or None,
        network=data.get("org") or None,
    )


def reset_ip_info_cache() -> None:
    """Test hook."""
    _cache.clear()
