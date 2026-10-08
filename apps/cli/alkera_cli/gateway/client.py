"""Client for the model gateway's ``GET /v1/models`` catalog.

Shared by the CLI chat picker and the daemon's ``harness.list_models`` method:
both need the list of models (+ their reasoning-effort variants) the gateway can
serve for the current user. Authenticated with the user's Alkera JWT.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx

from alkera_cli.contracts.gateway_model import GatewayModel, WireProtocol
from alkera_cli.host.http_error import response_error_detail

TEST_MODEL_FAMILIES = frozenset({"test", "t"})
"""Gateway families that are e2e fixtures, never real models. A dev gateway
DB accumulates hundreds of these from test runs; they must never appear in
the human model picker. (Tests that exercise one pin it by id via
``--model``, which bypasses the picker, so excluding here is safe.)"""


def selectable_models(models: list[GatewayModel]) -> list[GatewayModel]:
    """The models a human may pick — the catalog with e2e test-fixture
    families removed. ``fetch_models`` still returns the FULL catalog (the
    safety judge / subagent router / daemon need it); this is the
    picker-presentation filter only."""
    return [m for m in models if m.family not in TEST_MODEL_FAMILIES]


def resolve_model_selection(
    models: list[GatewayModel], model_id: str, effort: str | None
) -> GatewayModel:
    """The catalog entry for ``model_id``, validating that ``effort`` (when
    given) is actually offered — a silently-dropped effort would pin the chat
    at the wrong reasoning level. Raises ``ValueError`` with a user-facing
    message; callers (CLI flags, headless) wrap it in their own error surface."""
    model = next((m for m in models if m.id == model_id), None)
    if model is None:
        available = ", ".join(m.id for m in models) or "(none)"
        raise ValueError(f"Unknown model '{model_id}'. Available: {available}")
    if effort is not None and effort not in model.efforts:
        offered = ", ".join(model.efforts) or "(none)"
        raise ValueError(f"Model '{model_id}' doesn't offer effort '{effort}'. Offers: {offered}")
    return model


class GatewayUnavailableError(Exception):
    """The gateway could not be reached or returned an unexpected status."""


class GatewayAuthError(GatewayUnavailableError):
    """The gateway rejected the token (401/403) with a caller-visible reason."""

    def __init__(self, detail: str, *, status_code: int = 401) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


def _response_detail(resp: httpx.Response) -> str:
    """The backend's sentence, with a status-shaped fallback."""
    return response_error_detail(resp) or f"the gateway returned HTTP {resp.status_code}"


def _non_negative_int(value: Any) -> int:
    """A non-negative int from a gateway payload field, else 0. Missing / non-int /
    negative all degrade to 0 (= unknown limit) — never an exception or a bogus
    ``context: 0`` that would disable opencode overflow detection silently."""
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _parse_model(item: dict[str, Any]) -> GatewayModel | None:
    model_id = item.get("id")
    wire = item.get("wire")
    if not isinstance(model_id, str) or not model_id:
        return None
    if wire not in ("anthropic", "openai"):
        return None
    efforts = tuple(e for e in (item.get("efforts") or []) if isinstance(e, str))
    default_effort = item.get("default_effort")
    tier = item.get("tier")
    family = item.get("family")
    return GatewayModel(
        id=model_id,
        display_name=item.get("display_name") or model_id,
        wire=wire,  # narrowed to the Literal by the membership check above
        efforts=efforts,
        default_effort=default_effort if isinstance(default_effort, str) else None,
        tier=tier if isinstance(tier, str) and tier else "standard",
        family=family if isinstance(family, str) else "",
        # The context/output limits arm opencode's native auto-compaction.
        context_window=_non_negative_int(item.get("context_window")),
        max_output_tokens=_non_negative_int(item.get("max_output_tokens")),
        # An older gateway names neither: the model then reads only itself.
        reasoning_format=fmt
        if isinstance(fmt := item.get("reasoning_format"), str) and fmt
        else None,
        reads_reasoning_formats=tuple(
            f for f in item.get("reads_reasoning_formats") or () if isinstance(f, str) and f
        ),
    )


@dataclass(frozen=True, slots=True)
class GatewayCatalog:
    """One ``GET /v1/models`` response: the model list plus the org-wide feature
    flags the gateway resolves per caller (this call is already the CLI's
    per-org, per-session bootstrap fetch)."""

    models: list[GatewayModel] = field(default_factory=list)
    web_search_enabled: bool = False
    """The org's resolved web-tools toggle. Fail-closed: an older gateway that
    doesn't send ``org_flags`` reads as disabled."""
    web_fetch_enabled: bool = False
    """Whether ``web.fetch`` in particular may be registered: the org toggle
    above AND the deployment's own ``AGENT_WEB_FETCH_ENABLED`` kill switch. The
    two are separate because fetching an arbitrary URL is the half an install
    that keeps its agents off the public internet wants to refuse without also
    losing the key-less metasearch."""


async def fetch_catalog(
    *,
    gateway_url: str,
    token: str,
    timeout_seconds: float = 10.0,
    client: httpx.AsyncClient | None = None,
) -> GatewayCatalog:
    """The catalog (+ org flags) the gateway serves for the holder of ``token``.

    Raises ``GatewayAuthError`` on 401/403, ``GatewayUnavailableError`` on any
    other non-200, a body that is not JSON, or a connection failure. ``client``
    is injectable for tests (a caller-owned client is not closed here)."""
    url = f"{gateway_url.rstrip('/')}/v1/models"
    headers = {"Authorization": f"Bearer {token}"}
    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=timeout_seconds)
    try:
        resp = await client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        raise GatewayUnavailableError(
            f"could not reach the gateway at {gateway_url}: {exc}"
        ) from exc
    finally:
        if owns_client:
            await client.aclose()

    if resp.status_code in (401, 403):
        raise GatewayAuthError(_response_detail(resp), status_code=resp.status_code)
    if resp.status_code != 200:
        raise GatewayUnavailableError(_response_detail(resp))

    try:
        payload: Any = resp.json()
    except ValueError as exc:
        # A captive portal, a proxy page, or a URL pointed at the web app: the
        # gateway could not be read, which every caller already handles.
        raise GatewayUnavailableError(
            f"the gateway at {gateway_url} answered with a body that is not JSON"
        ) from exc
    if not isinstance(payload, dict):
        return GatewayCatalog()
    data = payload.get("data")
    out: list[GatewayModel] = []
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                parsed = _parse_model(item)
                if parsed is not None:
                    out.append(parsed)
    raw_flags = payload.get("org_flags")
    flags: dict[str, Any] = raw_flags if isinstance(raw_flags, dict) else {}
    web_search = flags.get("web_search_enabled") is True
    # A gateway that predates the fetch kill switch sends only the one flag, and
    # on it `web.fetch` follows `web.search` — which is exactly what it did
    # before the switch existed. Anything else would silently disable a tool on
    # a mixed-version fleet.
    fetch_flag = flags.get("web_fetch_enabled")
    web_fetch = web_search if fetch_flag is None else fetch_flag is True
    if out:
        REMEMBERED_CATALOG.remember(out)
    return GatewayCatalog(
        models=out, web_search_enabled=web_search, web_fetch_enabled=web_search and web_fetch
    )


class RememberedCatalog:
    """The catalog this process last read from the gateway. A session spawned
    while it is known declares every model in it, so the chat can switch models
    without the agent being spawned again (the editor's daemon reads the catalog
    for its picker before it opens a chat)."""

    def __init__(self) -> None:
        self._models: tuple[GatewayModel, ...] = ()

    def remember(self, models: list[GatewayModel]) -> None:
        self._models = tuple(models)

    @property
    def models(self) -> tuple[GatewayModel, ...]:
        return self._models


REMEMBERED_CATALOG = RememberedCatalog()


async def fetch_models(
    *,
    gateway_url: str,
    token: str,
    timeout_seconds: float = 10.0,
    client: httpx.AsyncClient | None = None,
) -> list[GatewayModel]:
    """Models the gateway can serve for the holder of ``token`` — the catalog
    fetch for callers that don't need the org flags (see ``fetch_catalog``)."""
    catalog = await fetch_catalog(
        gateway_url=gateway_url, token=token, timeout_seconds=timeout_seconds, client=client
    )
    return catalog.models


__all__ = [
    "GatewayAuthError",
    "GatewayCatalog",
    "GatewayUnavailableError",
    "WireProtocol",
    "fetch_catalog",
    "fetch_models",
    "resolve_model_selection",
    "selectable_models",
]
