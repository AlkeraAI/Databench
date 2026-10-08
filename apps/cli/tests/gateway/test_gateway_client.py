"""`alkera_cli.gateway.client.fetch_models` parses /v1/models + maps error paths."""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest
from alkera_cli.gateway.client import (
    GatewayAuthError,
    GatewayUnavailableError,
    fetch_catalog,
    fetch_models,
)


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://gw")


@pytest.mark.asyncio
async def test_fetch_models_parses_data_and_efforts() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.headers["authorization"] == "Bearer tok"
        assert req.url.path == "/v1/models"
        return httpx.Response(
            200,
            json={
                "object": "list",
                "data": [
                    {
                        "id": "claude-x",
                        "display_name": "Claude X",
                        "wire": "anthropic",
                        "efforts": ["low", "high"],
                        "default_effort": "low",
                        "tier": "frontier",
                        "context_window": 1_000_000,
                        "max_output_tokens": 64_000,
                    },
                    {"id": "gpt-x", "display_name": "GPT X", "wire": "openai"},
                    {"id": "bad", "wire": "weird"},  # unknown wire → skipped
                ],
            },
        )

    async with _client(handler) as c:
        models = await fetch_models(gateway_url="http://gw", token="tok", client=c)

    assert [m.id for m in models] == ["claude-x", "gpt-x"]
    claude, gpt = models
    assert claude.wire == "anthropic"
    assert claude.efforts == ("low", "high")
    assert claude.default_effort == "low"
    assert claude.tier == "frontier"  # parsed for subagent routing
    assert claude.context_window == 1_000_000  # carried for opencode's limit
    assert claude.max_output_tokens == 64_000
    assert gpt.efforts == ()  # no efforts key → empty
    assert gpt.default_effort is None
    assert gpt.tier == "standard"  # default when the gateway omits tier
    assert gpt.context_window == 0  # omitted → unknown (no limit emitted downstream)
    assert gpt.max_output_tokens == 0


@pytest.mark.asyncio
async def test_fetch_models_coerces_bad_limit_values_to_zero() -> None:
    # Defensive: a missing / non-int / negative / boolean limit must degrade to 0
    # (unknown), never raise or produce a bogus `context: 0` that disables overflow.
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "object": "list",
                "data": [
                    {
                        "id": "m",
                        "display_name": "M",
                        "wire": "anthropic",
                        "context_window": -5,
                        "max_output_tokens": "lots",
                    }
                ],
            },
        )

    async with _client(handler) as c:
        models = await fetch_models(gateway_url="http://gw", token="tok", client=c)
    assert models[0].context_window == 0
    assert models[0].max_output_tokens == 0


@pytest.mark.asyncio
async def test_fetch_models_401_raises_auth_with_detail() -> None:
    async with _client(
        lambda _r: httpx.Response(401, json={"detail": "invalid token: expired"})
    ) as c:
        with pytest.raises(GatewayAuthError) as exc_info:
            await fetch_models(gateway_url="http://gw", token="tok", client=c)
    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "invalid token: expired"
    assert str(exc_info.value) == "invalid token: expired"


@pytest.mark.asyncio
async def test_fetch_models_403_raises_auth_with_detail() -> None:
    async with _client(
        lambda _r: httpx.Response(403, json={"detail": "email_verification_required"})
    ) as c:
        with pytest.raises(GatewayAuthError) as exc_info:
            await fetch_models(gateway_url="http://gw", token="tok", client=c)
    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == "email_verification_required"


@pytest.mark.asyncio
async def test_fetch_models_auth_error_prefers_canonical_message_over_detail() -> None:
    """Catches a legacy-detail-first parser that hides the canonical backend
    error envelope when both shapes are present."""
    async with _client(
        lambda _r: httpx.Response(
            401,
            json={
                "error": {"message": "canonical session expired"},
                "detail": "legacy session expired",
            },
        )
    ) as c:
        with pytest.raises(GatewayAuthError) as exc_info:
            await fetch_models(gateway_url="http://gw", token="tok", client=c)

    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "canonical session expired"
    assert "legacy session expired" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_fetch_models_auth_error_falls_back_when_detail_missing() -> None:
    async with _client(lambda _r: httpx.Response(401, json={"error": "no detail"})) as c:
        with pytest.raises(GatewayAuthError) as exc_info:
            await fetch_models(gateway_url="http://gw", token="tok", client=c)
    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "the gateway returned HTTP 401"


@pytest.mark.asyncio
async def test_fetch_models_auth_error_falls_back_when_body_is_not_json() -> None:
    async with _client(lambda _r: httpx.Response(401, content=b"not json")) as c:
        with pytest.raises(GatewayAuthError) as exc_info:
            await fetch_models(gateway_url="http://gw", token="tok", client=c)
    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "the gateway returned HTTP 401"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        pytest.param(httpx.Response(401, json=None), id="json-null"),
        pytest.param(httpx.Response(401, json=[]), id="json-array"),
        pytest.param(httpx.Response(401, json="denied"), id="json-string"),
        pytest.param(httpx.Response(401, json={}), id="empty-object"),
        pytest.param(httpx.Response(401, content=b""), id="empty-body"),
    ],
)
async def test_fetch_models_auth_error_does_not_treat_non_objects_as_messages(
    response: httpx.Response,
) -> None:
    """Catches a parser that stringifies arbitrary JSON or an empty body instead
    of using the gateway's status-specific fallback."""
    async with _client(lambda _r: response) as c:
        with pytest.raises(GatewayAuthError) as exc_info:
            await fetch_models(gateway_url="http://gw", token="tok", client=c)

    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "the gateway returned HTTP 401"


@pytest.mark.asyncio
async def test_fetch_models_5xx_raises_unavailable() -> None:
    async with _client(lambda _r: httpx.Response(503)) as c:
        with pytest.raises(GatewayUnavailableError):
            await fetch_models(gateway_url="http://gw", token="tok", client=c)


@pytest.mark.asyncio
async def test_fetch_models_5xx_keeps_unavailable_type_and_canonical_message() -> None:
    """Catches a refactor that either changes the public 5xx exception type or
    ignores the canonical backend message."""
    async with _client(
        lambda _r: httpx.Response(
            503,
            json={
                "error": {"message": "catalog temporarily unavailable"},
                "detail": "legacy outage",
            },
        )
    ) as c:
        with pytest.raises(GatewayUnavailableError) as exc_info:
            await fetch_models(gateway_url="http://gw", token="tok", client=c)

    assert "catalog temporarily unavailable" in str(exc_info.value)
    assert "legacy outage" not in str(exc_info.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        pytest.param(b"<html>sign in to the wifi</html>", id="html-page"),
        pytest.param(b"", id="empty-body"),
        pytest.param(b"\xff\xfe\x00{", id="not-utf8"),
    ],
)
async def test_fetch_models_200_that_is_not_json_raises_unavailable(body: bytes) -> None:
    """A 200 whose body is not JSON (a captive portal, a proxy page, a gateway
    URL pointed at the web app) is a gateway that could not be read, never a
    crash: every caller handles GatewayUnavailableError and nothing else."""
    async with _client(
        lambda _r: httpx.Response(200, content=body, headers={"content-type": "text/html"})
    ) as c:
        with pytest.raises(GatewayUnavailableError) as exc_info:
            await fetch_models(gateway_url="http://gw", token="tok", client=c)

    assert not isinstance(exc_info.value, GatewayAuthError)
    assert "http://gw" in str(exc_info.value)


@pytest.mark.asyncio
async def test_fetch_models_connection_error_raises_unavailable() -> None:
    def boom(_r: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    async with _client(boom) as c:
        with pytest.raises(GatewayUnavailableError):
            await fetch_models(gateway_url="http://gw", token="tok", client=c)


# ---------------------------------------------------------------------------
# fetch_catalog — the org_flags carrier
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload_extra", "expected"),
    [
        pytest.param({"org_flags": {"web_search_enabled": True}}, True, id="flag-true"),
        pytest.param({"org_flags": {"web_search_enabled": False}}, False, id="flag-false"),
        pytest.param({}, False, id="absent-fails-closed"),
        pytest.param({"org_flags": "corrupt"}, False, id="malformed-fails-closed"),
        pytest.param(
            {"org_flags": {"web_search_enabled": "yes"}}, False, id="non-bool-fails-closed"
        ),
    ],
)
async def test_fetch_catalog_parses_org_flags(payload_extra: dict, expected: bool) -> None:
    """The org web-tools flag rides the models fetch. Anything other than an
    explicit `true` — an older gateway that omits org_flags, or a malformed
    value — reads as disabled (fail-closed)."""

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "object": "list",
                "data": [{"id": "m1", "display_name": "M1", "wire": "anthropic"}],
                **payload_extra,
            },
        )

    async with _client(handler) as c:
        catalog = await fetch_catalog(gateway_url="http://gw", token="tok", client=c)

    assert catalog.web_search_enabled is expected
    assert [m.id for m in catalog.models] == ["m1"]  # models parse regardless


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("flags", "search", "fetch"),
    [
        pytest.param(
            {"web_search_enabled": True, "web_fetch_enabled": True}, True, True, id="both-on"
        ),
        pytest.param(
            {"web_search_enabled": True, "web_fetch_enabled": False},
            True,
            False,
            id="deployment-killed-fetch",
        ),
        pytest.param(
            {"web_search_enabled": False, "web_fetch_enabled": True},
            False,
            False,
            id="fetch-cannot-grant-past-the-org-toggle",
        ),
        pytest.param(
            {"web_search_enabled": True},
            True,
            True,
            id="older-gateway-fetch-follows-search",
        ),
        pytest.param(
            {"web_search_enabled": True, "web_fetch_enabled": "yes"},
            True,
            False,
            id="non-bool-fails-closed",
        ),
    ],
)
async def test_fetch_catalog_parses_the_web_fetch_kill_switch(
    flags: dict, search: bool, fetch: bool
) -> None:
    """`web.fetch` has its own flag so a deployment can refuse the tool that
    pulls an arbitrary URL while keeping the metasearch. A gateway that predates
    the switch sends only `web_search_enabled`, and on it fetch must keep
    following search — anything else silently drops a tool on a mixed-version
    fleet."""

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"object": "list", "data": [], "org_flags": flags})

    async with _client(handler) as c:
        catalog = await fetch_catalog(gateway_url="http://gw", token="tok", client=c)

    assert catalog.web_search_enabled is search
    assert catalog.web_fetch_enabled is fetch


@pytest.mark.asyncio
async def test_fetch_models_still_returns_the_plain_list() -> None:
    # The narrow wrapper keeps its list contract for callers that don't need flags.
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "object": "list",
                "data": [{"id": "m1", "display_name": "M1", "wire": "openai"}],
                "org_flags": {"web_search_enabled": True},
            },
        )

    async with _client(handler) as c:
        models = await fetch_models(gateway_url="http://gw", token="tok", client=c)
    assert [m.id for m in models] == ["m1"]
