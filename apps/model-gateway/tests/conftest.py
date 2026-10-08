"""Fixtures for gateway tests (real local Postgres).

The gateway's *outbound* provider traffic is mocked with `httpx.MockTransport`
installed on `app.state.http_client` — explicit, no global patching, and the
test's own calls to the gateway (ASGITransport) stay separate. Seed data is
committed so the gateway's short-lived sessions see it.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import secrets
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import httpx
import pytest
import pytest_asyncio
from _helpers.live_server import bound_port, wait_until_serving
from alkera_core.auth import encode_cli_token, register_token
from alkera_core.config import settings
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.egress import EgressPolicy, Resolver
from alkera_core.http import GuardedAsyncClient
from alkera_core.llm_provider import Provider
from alkera_core.model_catalog import ModelTier, RateKind
from alkera_core.models import Team, TokenType, User
from alkera_core.models.model_catalog import Model, ModelRoute
from httpx import ASGITransport, AsyncClient
from model_gateway.app_factory import process_app
from sqlalchemy.ext.asyncio import AsyncSession

#: The process's gateway app, built from whatever extensions are installed.
gateway_app = process_app()


@pytest.fixture(autouse=True)
def _reset_state() -> object:
    from alkera_core.auth import revocation
    from model_gateway.pipeline import end_drain

    revocation._cache.reset()
    end_drain()
    gateway_app.state.transport_factory_override = None
    yield
    revocation._cache.reset()
    # A test that told the process to stop must not leave the next one refused.
    end_drain()
    gateway_app.state.transport_factory_override = None


@pytest.fixture(autouse=True)
def _fast_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    # Make retries instant — no test depends on real backoff timing.
    monkeypatch.setattr("model_gateway.pipeline._backoff", lambda _a: 0.0)


@pytest.fixture
def sleep_recorder(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Capture every retry sleep the pipeline performs (returning instantly).
    Patches the pipeline's own ``_sleep`` seam — NOT ``asyncio.sleep``, which is
    the shared module object and would swallow unrelated awaits process-wide.
    The autouse ``_fast_backoff`` zeroes ``_backoff``, so any nonzero recorded
    duration is attributable ONLY to the Retry-After floor."""
    sleeps: list[float] = []

    async def _instant(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr("model_gateway.pipeline._sleep", _instant)
    return sleeps


@dataclass
class Seeded:
    user_id: UUID
    org_id: UUID
    model_id: str
    token: str
    #: The seat's billing account and its balance, where a layer that bills
    #: seeded them; ``None`` in the open composition.
    account_id: UUID | None = None
    balance_id: UUID | None = None


@dataclass
class GatewaySeed:
    """One ``seed`` call, handed to ``pytest_alkera_gateway_seed`` so a layer
    can add its own rows beside the person, the model and the route."""

    session: AsyncSession
    user: User
    model_id: str
    upstream_model_id: str
    provider: Provider
    effective_at: datetime
    #: The pricing and funding arguments the test passed, by name.
    pricing: dict[str, Any]
    account_id: UUID | None = None
    balance_id: UUID | None = None


def anthropic_sse(
    *,
    input_tokens: int,
    output_tokens: int,
    cache_read: int = 0,
    cache_write_5m: int = 0,
    text: str = "ok",
) -> bytes:
    start_usage = {
        "input_tokens": input_tokens,
        "cache_read_input_tokens": cache_read,
        "cache_creation_input_tokens": cache_write_5m,
        "output_tokens": 1,
    }
    events = [
        (
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": "msg_1",
                    "type": "message",
                    "role": "assistant",
                    "content": [],
                    "model": "x",
                    "usage": start_usage,
                },
            },
        ),
        (
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
        ),
        (
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": text},
            },
        ),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        (
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn"},
                "usage": {"output_tokens": output_tokens},
            },
        ),
        ("message_stop", {"type": "message_stop"}),
    ]
    out = b""
    for name, data in events:
        out += f"event: {name}\ndata: {json.dumps(data)}\n\n".encode()
    return out


def anthropic_response(**kw: int | str) -> httpx.Response:
    return httpx.Response(
        200,
        content=anthropic_sse(**kw),  # type: ignore[arg-type]
        headers={"content-type": "text/event-stream"},
    )


def _resp_event(data: dict) -> bytes:
    # A Responses SSE frame: `event: <type>` + `data: <json>` (the data JSON also
    # self-describes via its own `type`, which the usage parser keys off).
    return f"event: {data['type']}\ndata: {json.dumps(data)}\n\n".encode()


def openai_sse(
    *,
    prompt_tokens: int,
    completion_tokens: int,
    cached_tokens: int = 0,
    cache_write_tokens: int = 0,
    reasoning_tokens: int = 0,
    text: str = "ok",
) -> bytes:
    """An OpenAI **Responses API** stream: `response.*` events ending in
    `response.completed`, which carries `response.usage` (the only place usage
    appears). Param names mirror the old chat builder (so billing assertions are
    unchanged); they're emitted as Responses `input_tokens`/`output_tokens`. When
    `reasoning_tokens` > 0 a `reasoning` output item with `encrypted_content` is
    included (the continuity anchor)."""
    rid = "resp_1"
    output: list[dict] = []
    events: list[bytes] = [
        _resp_event(
            {
                "type": "response.created",
                "response": {
                    "id": rid,
                    "object": "response",
                    "status": "in_progress",
                    "output": [],
                },
            }
        )
    ]
    idx = 0
    if reasoning_tokens:
        reasoning_item = {
            "id": "rs_1",
            "type": "reasoning",
            "encrypted_content": "ENC_REASONING",
            "summary": [{"type": "summary_text", "text": "thinking…"}],
        }
        events += [
            _resp_event(
                {
                    "type": "response.output_item.added",
                    "output_index": idx,
                    "item": {"id": "rs_1", "type": "reasoning", "summary": []},
                }
            ),
            _resp_event(
                {
                    "type": "response.reasoning_summary_text.delta",
                    "output_index": idx,
                    "delta": "thinking…",
                }
            ),
            _resp_event(
                {"type": "response.output_item.done", "output_index": idx, "item": reasoning_item}
            ),
        ]
        output.append(reasoning_item)
        idx += 1
    msg_item = {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": text}],
    }
    events += [
        _resp_event(
            {
                "type": "response.output_item.added",
                "output_index": idx,
                "item": {"id": "msg_1", "type": "message", "role": "assistant", "content": []},
            }
        ),
        _resp_event({"type": "response.output_text.delta", "output_index": idx, "delta": text}),
        _resp_event({"type": "response.output_text.done", "output_index": idx, "text": text}),
        _resp_event({"type": "response.output_item.done", "output_index": idx, "item": msg_item}),
    ]
    output.append(msg_item)
    usage = {
        "input_tokens": prompt_tokens,
        # The shape the live API returns (GPT-6 / GPT-5.6 report cache writes).
        "input_tokens_details": {
            "cached_tokens": cached_tokens,
            "cache_write_tokens": cache_write_tokens,
        },
        "output_tokens": completion_tokens,
        "output_tokens_details": {"reasoning_tokens": reasoning_tokens},
        "total_tokens": prompt_tokens + completion_tokens,
    }
    events.append(
        _resp_event(
            {
                "type": "response.completed",
                "response": {
                    "id": rid,
                    "object": "response",
                    "status": "completed",
                    "output": output,
                    "usage": usage,
                },
            }
        )
    )
    return b"".join(events)


def openai_response(**kw: int | str) -> httpx.Response:
    return httpx.Response(
        200,
        content=openai_sse(**kw),  # type: ignore[arg-type]
        headers={"content-type": "text/event-stream"},
    )


class MockUpstream:
    """Records outbound requests + returns scripted responses (cycling the last).

    `script_for(upstream_model_id, ...)` keys responses on the request body's
    `model` field (= the route's upstream_model_id), so multi-route failover
    tests can make one route fail while another serves.
    """

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self._responses: list[httpx.Response] = [
            anthropic_response(input_tokens=10, output_tokens=5)
        ]
        self._by_model: dict[str, list[httpx.Response]] = {}
        self._open_delay = 0.0
        self._open_delay_by_model: dict[str, float] = {}

    def script(self, *responses: httpx.Response) -> None:
        self._responses = list(responses)

    def script_for(self, upstream_model_id: str, *responses: httpx.Response) -> None:
        self._by_model[upstream_model_id] = list(responses)

    def requests_for(self, upstream_model_id: str) -> list[httpx.Request]:
        out = []
        for req in self.requests:
            try:
                if json.loads(req.content).get("model") == upstream_model_id:
                    out.append(req)
            except (json.JSONDecodeError, ValueError):
                continue
        return out

    @staticmethod
    def _pop(queue: list[httpx.Response]) -> httpx.Response:
        return queue.pop(0) if len(queue) > 1 else queue[0]

    def delay_open(self, seconds: float, *, upstream_model_id: str | None = None) -> None:
        """Withhold the response HEADERS for `seconds` — the shape of a provider
        that buffers a long prelude, or a cold invoke. Distinct from a silent
        response BODY (`_silent_stream`): this delays `client.stream().__aenter__`
        itself, so the gateway is still opening the route, not yet reading it."""
        if upstream_model_id is None:
            self._open_delay = seconds
        else:
            self._open_delay_by_model[upstream_model_id] = seconds

    def _handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        try:
            model = json.loads(request.content).get("model")
        except (json.JSONDecodeError, ValueError):
            model = None
        if model in self._by_model:
            return self._pop(self._by_model[model])
        return self._pop(self._responses)

    async def _async_handler(self, request: httpx.Request) -> httpx.Response:
        try:
            model = json.loads(request.content).get("model")
        except (json.JSONDecodeError, ValueError):
            model = None
        delay = (
            self._open_delay_by_model.get(model, self._open_delay) if model else self._open_delay
        )
        if delay > 0:
            await asyncio.sleep(delay)
        return self._handler(request)

    def install(self, *, byok_resolver: Resolver | None = None) -> None:
        """Serve both pooled clients from this mock.

        The BYOK one keeps its egress guard — the mock is its INNER transport —
        so a BYOK request still gets canonicalised, resolved and vetted on the
        way out; only the socket is faked. ``byok_resolver`` scripts the lookup
        (by default every host answers with one public address), so a test can
        point a BYOK endpoint at a metadata address and see the guard refuse.
        """
        transport = httpx.MockTransport(self._async_handler)
        gateway_app.state.http_client = httpx.AsyncClient(transport=transport)
        gateway_app.state.byok_http_client = GuardedAsyncClient(
            egress_policy=EgressPolicy.from_settings(allow_private=settings.is_self_hosted),
            egress_resolver=byok_resolver or (lambda host, port: ["93.184.216.34"]),
            transport=transport,
        )


@pytest_asyncio.fixture
async def upstream() -> AsyncIterator[MockUpstream]:
    mock = MockUpstream()
    mock.install()
    yield mock
    await gateway_app.state.http_client.aclose()
    await gateway_app.state.byok_http_client.aclose()


@pytest_asyncio.fixture
async def gateway_client() -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=gateway_app), base_url="http://gw") as c:
        yield c


@pytest_asyncio.fixture
async def live_gateway() -> AsyncIterator[str]:
    """Run the gateway as a REAL uvicorn server on an ephemeral port, in the
    test's own event loop (so it shares the session-scoped asyncpg engine).

    This makes the client→gateway hop real HTTP/TCP — real chunked SSE framing
    and real client-disconnect (TCP close) — while the upstream stays mocked via
    `app.state.http_client`. Yields the base URL (`http://127.0.0.1:<port>`)."""
    import asyncio

    import uvicorn

    config = uvicorn.Config(
        gateway_app, host="127.0.0.1", port=0, log_level="warning", lifespan="on"
    )
    server = uvicorn.Server(config)
    serve_task = asyncio.create_task(server.serve())
    try:
        # The lifespan warms the token encodings off the loop for up to its own
        # budget before it binds, so the wait is on the wall clock, not a poll count.
        await wait_until_serving(server, serve_task, what="live gateway")
        yield f"http://127.0.0.1:{bound_port(server)}"
    finally:
        server.should_exit = True
        with contextlib.suppress(TimeoutError, asyncio.TimeoutError):
            await asyncio.wait_for(serve_task, timeout=5.0)
        if not serve_task.done():
            serve_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await serve_task


@pytest.fixture
def make_sse() -> Callable[..., bytes]:
    return anthropic_sse


@pytest.fixture
def make_response() -> Callable[..., httpx.Response]:
    return anthropic_response


@pytest.fixture
def make_openai_sse() -> Callable[..., bytes]:
    return openai_sse


@pytest.fixture
def make_openai_response() -> Callable[..., httpx.Response]:
    return openai_response


# --------------------------------------------------------------------------- #
# Fake Bedrock (aioboto3) — events as native Anthropic JSON objects, wrapped in
# the AWS event-stream chunk envelope. Used via the transport_factory_override.
# --------------------------------------------------------------------------- #


def bedrock_native_events(
    *,
    input_tokens: int,
    output_tokens: int,
    cache_read: int = 0,
    cache_write_5m: int = 0,
    text: str = "ok",
) -> list[dict]:
    return [
        {
            "type": "message_start",
            "message": {
                "usage": {
                    "input_tokens": input_tokens,
                    "cache_read_input_tokens": cache_read,
                    "cache_creation_input_tokens": cache_write_5m,
                    "output_tokens": 1,
                }
            },
        },
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn"},
            "usage": {"output_tokens": output_tokens},
        },
        {"type": "message_stop"},
    ]


class FakeBedrockClient:
    """Stand-in for an aioboto3 bedrock-runtime client (also its own async cm)."""

    def __init__(self, events: list[dict] | None = None, *, error: Exception | None = None) -> None:
        self._events = events or []
        self._error = error
        self.calls: list[dict] = []

    async def __aenter__(self) -> FakeBedrockClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def _stream(self):
        for ev in self._events:
            yield {"chunk": {"bytes": json.dumps(ev).encode()}}

    async def invoke_model_with_response_stream(self, **kwargs: object) -> dict:
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        return {"body": self._stream()}


def fake_bedrock_factory(client: FakeBedrockClient) -> Callable[..., object]:
    """A `transport_factory_override` that fulfils any route via a Bedrock
    transport backed by `client`."""
    from model_gateway.adapters import BedrockInvokeTransport

    def _client_factory() -> FakeBedrockClient:
        return client

    def transport_factory(_route: object) -> object:
        return BedrockInvokeTransport(client_factory=_client_factory)

    return transport_factory


@pytest.fixture
def install_bedrock() -> Callable[[FakeBedrockClient], None]:
    def _install(client: FakeBedrockClient) -> None:
        gateway_app.state.transport_factory_override = fake_bedrock_factory(client)

    return _install


@pytest.fixture
def make_bedrock_events() -> Callable[..., list[dict]]:
    return bedrock_native_events


@pytest.fixture
def bedrock_client_cls() -> type[FakeBedrockClient]:
    return FakeBedrockClient


@pytest_asyncio.fixture
def seed(request: pytest.FixtureRequest) -> Callable[..., object]:
    async def _seed(
        *,
        granted_nanos: int = 10**12,
        input_sell: str = "3.00",
        input_cost: str = "0.30",
        output_sell: str = "15.00",
        output_cost: str = "1.50",
        provider: Provider = Provider.ANTHROPIC,
        cache_prices: bool = True,
        output_priced: bool = True,
        reasoning_efforts: list[str] | None = None,
        default_effort: str | None = None,
        thinking_mode: str | None = None,
        supports_thinking: bool = False,
        upstream_model_id: str | None = None,
        tiers: dict[RateKind, list[tuple[int, str, str]]] | None = None,
        model_tier: ModelTier = ModelTier.STANDARD,
        max_output_tokens: int = 0,
        cache_min_tokens: int | None = None,
        flags: list[str] | None = None,
    ) -> Seeded:
        async with AsyncSessionLocal() as s:
            team = Team(name=f"org-{secrets.token_hex(4)}", is_root=True)
            s.add(team)
            await s.flush()
            user = User(
                home_org_team_id=team.id,
                email=f"u-{secrets.token_hex(6)}@alkera.dev",
                first_name="U",
                last_name="User",
                # The gateway refuses unverified accounts outright; tests exercising
                # that gate null this out explicitly.
                email_verified_at=datetime.now(UTC),
            )
            s.add(user)
            await s.flush()

            model_id = f"m-{secrets.token_hex(4)}"
            upstream_id = upstream_model_id or f"up-{model_id}"
            eff = datetime.now(UTC) - timedelta(days=1)
            s.add(
                Model(
                    id=model_id,
                    display_name="M",
                    family="test",
                    enabled=True,
                    context_window=200_000,
                    supports_thinking=supports_thinking,
                    reasoning_efforts=reasoning_efforts or [],
                    default_effort=default_effort,
                    thinking_mode=thinking_mode,
                    tier=model_tier,
                    max_output_tokens=max_output_tokens,
                    cache_min_tokens=cache_min_tokens,
                    flags=flags or [],
                )
            )
            await s.flush()
            s.add(
                ModelRoute(
                    model_id=model_id,
                    provider=provider,
                    upstream_model_id=upstream_id,
                    enabled=True,
                    priority=0,
                )
            )
            # What a layer that bills adds for the same seed: the seat's
            # account, its balance, and the model's prices and costs.
            seeding = GatewaySeed(
                session=s,
                user=user,
                model_id=model_id,
                upstream_model_id=upstream_id,
                provider=provider,
                effective_at=eff,
                pricing={
                    "granted_nanos": granted_nanos,
                    "input_sell": input_sell,
                    "input_cost": input_cost,
                    "output_sell": output_sell,
                    "output_cost": output_cost,
                    "cache_prices": cache_prices,
                    "output_priced": output_priced,
                    "tiers": tiers,
                },
            )
            for step in request.config.hook.pytest_alkera_gateway_seed(seed=seeding):
                await step
            token, claims = encode_cli_token(
                user_id=user.id, email=user.email, org_team_id=team.id, platform_role=None
            )
            # Register the token (as the device-grant token endpoint would), so
            # revocation is durable across a cache reload.
            await register_token(s, claims=claims, token_type=TokenType.CLI)
            await s.commit()
            return Seeded(
                user_id=user.id,
                org_id=team.id,
                model_id=model_id,
                token=token,
                account_id=seeding.account_id,
                balance_id=seeding.balance_id,
            )

    return _seed


# --------------------------------------------------------------------------- #
# The two paths the input-token estimate can take.
# --------------------------------------------------------------------------- #
#
# `model_gateway.estimate` counts text with a real tokenizer when one loads and
# with the family's measured characters-per-token ratio when none does. The two
# disagree by a lot on some bodies — a run of 300 identical characters is 46
# tokens to tiktoken and 86 to the ratio — so ANY test that asserts a number
# about an estimate is asserting about whichever path that machine happened to
# have. That is how a green suite turns red the day the optional dependency
# lands. A test that cares about a number pins the path and derives the number
# from the documented calibration; a test that cares only about "was this field
# counted at all" runs on both.

#: The fake tokenizer's rate. Deliberately NOT any ratio in the calibration
#: table, so a test that claims to be on the tokenizer path cannot pass by
#: silently falling through to the ratio one.
FAKE_CHARS_PER_TOKEN = 7


class _FakeEncoding:
    def encode_ordinary(self, text: str) -> list[int]:
        return [0] * max(1, len(text) // FAKE_CHARS_PER_TOKEN)


@pytest.fixture(params=["tokenizer", "ratio"])
def estimator_path(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch):
    """Pin the estimator to one path and hand back what a run of text is worth on it.

    Both are forced with deterministic stand-ins, so the case means the same
    thing whether or not tiktoken is installed on the machine running it. The
    REAL tokenizer's accuracy is not this fixture's job — the recorded-corpus
    cases and the `live_provider` tier score that against actual provider bills.

    The returned callable is the FLOOR a run of `chars` characters contributes
    for `family`, derived from the calibration table (or the fake's rate). A
    family with no encodings is on the ratio path either way — the tokenizer
    parameter cannot invent one for it.
    """
    from model_gateway.estimate import CALIBRATIONS

    forced = request.param
    if forced == "tokenizer":
        monkeypatch.setattr("model_gateway.estimate._encoding", lambda _name: _FakeEncoding())
    else:
        monkeypatch.setattr("model_gateway.estimate._encoding", lambda _name: None)

    def floor_for(chars: int, family: str) -> int:
        calibration = CALIBRATIONS[family]
        if forced == "tokenizer" and calibration.encodings:
            return chars // FAKE_CHARS_PER_TOKEN
        # int() floors, and the estimator rounds UP — so this stays a floor.
        return int(chars / calibration.chars_per_token)

    return floor_for
