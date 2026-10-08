"""The gateway's retry, fairness and error-surface bounds come from settings.

Each of these was a module literal. What matters is not that the field exists but
that moving it moves the gateway: a different number of attempts reaches the
provider, a different number of streams is admitted for one principal, a
different amount of a provider's error body reaches the user.
"""

from __future__ import annotations

import asyncio
import contextlib

import httpx
import pytest
from alkera_core.config import settings
from httpx import AsyncClient
from model_gateway.adapters import UpstreamError
from model_gateway.app_factory import create_app

# Bound at import, before the autouse `_fast_backoff` replaces the module
# attribute per test — this is the real curve, reading settings as it runs.
from model_gateway.pipeline import _backoff, _gated, _StreamGate, begin_drain


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _body(model_id: str) -> dict[str, object]:
    return {
        "model": model_id,
        "max_tokens": 16,
        "messages": [{"role": "user", "content": "hi"}],
        "stream": True,
    }


def _retryable() -> httpx.Response:
    return httpx.Response(
        503,
        json={"type": "error", "error": {"type": "overloaded_error", "message": "busy"}},
        headers={"content-type": "application/json"},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("attempts", "expected_hits"),
    [
        pytest.param(1, 1, id="no-retry-at-all"),
        pytest.param(2, 2, id="one-retry"),
        pytest.param(3, 3, id="the-shipped-three"),
        pytest.param(5, 5, id="a-patient-deployment"),
    ],
)
async def test_attempts_on_one_route_follow_the_setting(
    gateway_client: AsyncClient,
    upstream,
    seed,
    sleep_recorder: list[float],
    monkeypatch: pytest.MonkeyPatch,
    attempts: int,
    expected_hits: int,
) -> None:
    """A single-route model has no next candidate, so this setting is the whole
    tolerance for a provider blip. Counted on the mock upstream, which sees one
    request per attempt."""
    monkeypatch.setattr(settings, "gateway_upstream_max_attempts", attempts)
    s = await seed(granted_nanos=10**12)
    upstream.script(*[_retryable() for _ in range(attempts + 2)])

    resp = await gateway_client.post(
        "/anthropic/v1/messages", headers=_auth(s.token), json=_body(s.model_id)
    )

    assert resp.status_code == 200  # the failure is delivered in-band on the SSE
    assert b"busy" in resp.content
    assert len(upstream.requests) == expected_hits
    assert len(sleep_recorder) == expected_hits - 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("cap", "hint", "honored"),
    [
        pytest.param(30.0, 10, True, id="hint-under-the-shipped-cap"),
        pytest.param(30.0, 60, False, id="anthropics-common-sixty-over-the-cap"),
        pytest.param(120.0, 60, True, id="raised-cap-honors-that-same-sixty"),
    ],
)
async def test_the_retry_after_cap_decides_whether_a_hint_is_waited_out(
    gateway_client: AsyncClient,
    upstream,
    seed,
    make_response,
    sleep_recorder: list[float],
    monkeypatch: pytest.MonkeyPatch,
    cap: float,
    hint: int,
    honored: bool,
) -> None:
    """A hint above the cap is not waited on at all — the route fails over rather
    than pinning a gate slot, a credit reservation and a DB row for the wait."""
    monkeypatch.setattr(settings, "gateway_retry_after_cap_seconds", cap)
    s = await seed(granted_nanos=10**12)
    upstream.script(
        httpx.Response(
            429,
            json={"type": "error", "error": {"type": "rate_limit_error", "message": "slow down"}},
            headers={"content-type": "application/json", "retry-after": str(hint)},
        ),
        make_response(input_tokens=100, output_tokens=50),
    )

    resp = await gateway_client.post(
        "/anthropic/v1/messages", headers=_auth(s.token), json=_body(s.model_id)
    )

    assert resp.status_code == 200
    if honored:
        assert len(upstream.requests) == 2
        assert float(hint) in sleep_recorder
    else:
        assert len(upstream.requests) == 1
        assert sleep_recorder == []


@pytest.mark.parametrize(
    ("base", "ceiling", "attempt", "floor"),
    [
        pytest.param(0.5, 8.0, 1, 0.5, id="shipped-first-wait"),
        pytest.param(0.5, 8.0, 3, 2.0, id="shipped-third-wait-doubles-twice"),
        pytest.param(4.0, 60.0, 1, 4.0, id="a-slower-base-waits-longer"),
        pytest.param(4.0, 5.0, 4, 5.0, id="the-ceiling-clamps-the-curve"),
    ],
)
def test_the_backoff_curve_is_the_configured_one(
    monkeypatch: pytest.MonkeyPatch,
    base: float,
    ceiling: float,
    attempt: int,
    floor: float,
) -> None:
    """`base * 2^(n-1)`, clamped to the ceiling, plus the jitter spread."""
    monkeypatch.setattr(settings, "gateway_retry_backoff_base_seconds", base)
    monkeypatch.setattr(settings, "gateway_retry_backoff_max_seconds", ceiling)
    jitter = settings.gateway_retry_backoff_jitter_seconds
    assert floor <= _backoff(attempt) <= floor + jitter


@pytest.mark.parametrize(
    "jitter",
    [pytest.param(0.0, id="off"), pytest.param(0.25, id="the-shipped-quarter-second")],
)
def test_the_jitter_spread_is_the_configured_one(
    monkeypatch: pytest.MonkeyPatch, jitter: float
) -> None:
    """Observed over many draws, because one draw proves nothing about a spread:
    zero must give the same wait every time (a deterministic curve a test or a
    single-tenant deployment can want), and a nonzero spread must actually
    scatter the waits across its whole range so a provider blip that refused a
    thousand requests at once does not return them all together."""
    monkeypatch.setattr(settings, "gateway_retry_backoff_base_seconds", 1.0)
    monkeypatch.setattr(settings, "gateway_retry_backoff_max_seconds", 8.0)
    monkeypatch.setattr(settings, "gateway_retry_backoff_jitter_seconds", jitter)

    draws = [_backoff(1) for _ in range(200)]
    spread = max(draws) - min(draws)

    assert all(1.0 <= d <= 1.0 + jitter for d in draws)
    if jitter == 0.0:
        assert spread == 0.0
    else:
        assert spread == pytest.approx(jitter, abs=0.05)


@pytest.mark.parametrize(
    ("share", "cap", "admitted"),
    [
        pytest.param(4, 80, 20, id="the-shipped-quarter"),
        pytest.param(1, 80, 80, id="one-principal-may-take-the-process"),
        pytest.param(8, 80, 10, id="a-stricter-share"),
        pytest.param(4, 2, 1, id="the-floor-keeps-one-slot-reachable"),
    ],
)
def test_one_principals_share_of_the_stream_slots_follows_the_setting(
    monkeypatch: pytest.MonkeyPatch, share: int, cap: int, admitted: int
) -> None:
    """Observed through admission: the gate takes exactly this many streams for
    one principal and refuses the next."""
    monkeypatch.setattr(settings, "gateway_per_principal_stream_share", share)
    monkeypatch.setattr(settings, "gateway_max_concurrent_streams", cap)
    gate = _StreamGate()

    taken = 0
    while gate.try_acquire("user-1"):
        taken += 1
        assert taken <= cap, "the gate admitted past the process cap"

    assert taken == admitted
    # A shared principal (a self-hosted deployment's machine account) is bounded
    # by the process cap alone, so it is still admitted here.
    assert gate.try_acquire("machine", shared=True) is (taken < cap)


@pytest.mark.parametrize(
    ("chars", "expected"),
    [
        pytest.param(300, 300, id="the-shipped-three-hundred"),
        pytest.param(2000, 2000, id="a-deployment-that-wants-the-whole-page"),
        pytest.param(20, 20, id="a-terse-surface"),
    ],
)
def test_how_much_of_an_upstream_error_body_reaches_the_caller(
    monkeypatch: pytest.MonkeyPatch, chars: int, expected: int
) -> None:
    """A provider that answers with HTML instead of JSON: the text is what the
    user's terminal shows, so the deployment decides how much of it."""
    monkeypatch.setattr(settings, "gateway_upstream_error_excerpt_chars", chars)
    err = UpstreamError(502, b"<html>" + b"x" * 5000)
    assert len(err.detail) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "leg", "value"),
    [
        pytest.param("gateway_upstream_connect_timeout_seconds", "connect", 3.5, id="connect"),
        pytest.param("gateway_upstream_write_timeout_seconds", "write", 45.0, id="write"),
        pytest.param("gateway_upstream_pool_timeout_seconds", "pool", 2.0, id="pool"),
    ],
)
async def test_the_other_upstream_timeout_legs_are_settings_too(
    monkeypatch: pytest.MonkeyPatch, field: str, leg: str, value: float
) -> None:
    """None of these three is about how long a model may think (that is the read
    leg) — they are the connection's own health, and a locked-down network may
    need them widened."""
    monkeypatch.setattr(settings, field, value)
    app = create_app()
    try:
        assert getattr(app.state.http_client.timeout, leg) == value
    finally:
        await app.state.http_client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "hint", [pytest.param(1, id="the-shipped-second"), pytest.param(12, id="a-slower-pacing-hint")]
)
async def test_a_shed_request_is_paced_by_the_configured_retry_after(
    gateway_client: AsyncClient,
    upstream,
    seed,
    monkeypatch: pytest.MonkeyPatch,
    hint: int,
) -> None:
    """Both refusals the gateway makes without reaching a provider carry it: the
    capacity shed and the draining 503. A proxy-mode caller honors this header
    exactly as it honors a provider's, so it is the only pacing it gets."""
    monkeypatch.setattr(settings, "gateway_shed_retry_after_seconds", hint)
    s = await seed(granted_nanos=10**12)

    # Capacity: a process cap of zero sheds the very first request.
    monkeypatch.setattr(settings, "gateway_max_concurrent_streams", 0)
    shed = await gateway_client.post(
        "/anthropic/v1/messages", headers=_auth(s.token), json=_body(s.model_id)
    )
    assert shed.status_code == 429
    assert shed.headers["retry-after"] == str(hint)

    # Draining: refused before any slot is taken, so it answers with the cap
    # restored too. (`_reset_state` ends the drain after the test.)
    monkeypatch.setattr(settings, "gateway_max_concurrent_streams", 80)
    begin_drain()
    draining = await gateway_client.post(
        "/anthropic/v1/messages", headers=_auth(s.token), json=_body(s.model_id)
    )
    assert draining.status_code == 503
    assert draining.headers["retry-after"] == str(hint)
    assert len(upstream.requests) == 0, "neither refusal may reach a provider"


class _WedgedRelay:
    """A relay that cannot be closed from outside — the shape the expiry watchdog
    exists for. A real relay parked mid-step raises exactly this from `aclose`,
    and parks again within a chunk, which is why the watchdog retries instead of
    giving up on the first refusal."""

    def __init__(self) -> None:
        self.close_attempts = 0

    def __aiter__(self) -> _WedgedRelay:
        return self

    async def __anext__(self) -> bytes:
        await asyncio.sleep(3600)
        raise StopAsyncIteration

    async def aclose(self) -> None:
        self.close_attempts += 1
        raise RuntimeError("aclose(): asynchronous generator is already running")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("budget", "gap", "fewest", "most"),
    [
        # Same budget, different gap: the two windows do not overlap, so neither
        # setting can be read off the other.
        pytest.param(0.6, 0.1, 5, 9, id="a-tight-gap-asks-many-times"),
        pytest.param(0.6, 0.3, 2, 4, id="a-wide-gap-asks-a-few"),
    ],
)
async def test_the_expiry_watchdog_retries_the_close_for_its_configured_budget(
    monkeypatch: pytest.MonkeyPatch, budget: float, gap: float, fewest: int, most: int
) -> None:
    """A stream past the step cap is torn down by closing the upstream and THEN
    freeing its capacity slot. A relay wedged inside an await refuses the close,
    so the budget decides how long we keep asking and the gap decides how often —
    and the slot is freed at the end either way, because pinning capacity on a
    wedged relay forever is worse than one leaked provider stream."""
    monkeypatch.setattr(settings, "gateway_max_stream_seconds", 1)
    monkeypatch.setattr(settings, "gateway_stream_expiry_close_seconds", budget)
    monkeypatch.setattr(settings, "gateway_stream_expiry_retry_seconds", gap)
    gate = _StreamGate()
    assert gate.try_acquire("user-1")
    relay = _WedgedRelay()

    async def drain() -> None:
        async for _ in _gated(relay, gate, "user-1"):  # type: ignore[arg-type]
            pass

    task = asyncio.create_task(drain())
    # The watchdog is armed at the step cap (floored at one second by the
    # pipeline), then spends its budget refusing to give up.
    await asyncio.sleep(1.0 + budget + 3 * gap)

    assert fewest <= relay.close_attempts <= most
    assert gate.active == 0, "the slot must be freed once the budget is spent"

    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
