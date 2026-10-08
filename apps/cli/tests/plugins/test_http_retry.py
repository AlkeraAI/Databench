"""The shared bounded 429 retry that backs the Fivetran, Sigma, and Hex REST
clients. ``retry_after_seconds`` reads the server's ``Retry-After`` (capped, with
a default when it is absent or unsane); ``send_with_429_retry`` gives a 429 a
bounded backoff then surfaces the still-429 so the caller's error path runs."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import httpx
import pytest
from alkera_cli.plugins.plugin_base.http_retry import (
    retry_after_seconds,
    send_with_429_retry,
)


def _script(
    responses: list[httpx.Response],
) -> tuple[Callable[[], Awaitable[httpx.Response]], list[int]]:
    """An async ``send`` yielding ``responses`` in order. The returned list gets one
    entry per invocation, so its length is the observed send count."""
    pending = iter(responses)
    sends: list[int] = []

    async def send() -> httpx.Response:
        sends.append(1)
        return next(pending)

    return send, sends


def _sleep_recorder() -> tuple[Callable[[float], Awaitable[None]], list[float]]:
    """An async ``sleep`` that records each backoff instead of waiting."""
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    return sleep, sleeps


# --- retry_after_seconds: parse, cap, default ------------------------------


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        pytest.param("3", 3.0, id="int-seconds-parse"),
        pytest.param("3.5", 3.5, id="float-seconds-parse"),
        pytest.param("0", 0.0, id="zero-is-honored-not-default"),
        pytest.param("30", 30.0, id="cap-boundary-exact"),
        pytest.param("29.5", 29.5, id="just-below-cap-passes-through"),
        pytest.param("31", 30.0, id="just-above-cap-clamped"),
        pytest.param("600", 30.0, id="far-above-cap-clamped"),
        pytest.param("inf", 30.0, id="infinity-clamped-to-cap"),
        pytest.param("-1", 5.0, id="negative-defaults"),
        pytest.param("-0.5", 5.0, id="negative-fraction-defaults"),
        pytest.param("nan", 5.0, id="nan-is-not-ge-zero-defaults"),
        pytest.param("soon", 5.0, id="garbage-defaults"),
        pytest.param("", 5.0, id="empty-header-defaults"),
        pytest.param(None, 5.0, id="absent-header-defaults"),
    ],
)
def test_retry_after_seconds(header: str | None, expected: float) -> None:
    headers = {} if header is None else {"Retry-After": header}
    resp = httpx.Response(429, headers=headers)
    assert retry_after_seconds(resp) == expected


def test_retry_after_seconds_honors_custom_cap_and_default() -> None:
    # The cap + default are injectable so a caller can tune the window; a sane value
    # over the custom cap clamps to it, and an unparseable value falls to the custom default.
    over = httpx.Response(429, headers={"Retry-After": "50"})
    assert retry_after_seconds(over, cap=10.0) == 10.0
    garbage = httpx.Response(429, headers={"Retry-After": "later"})
    assert retry_after_seconds(garbage, default=2.0) == 2.0


# --- send_with_429_retry: the bounded backoff loop -------------------------


async def test_first_non_429_returns_immediately_without_sleeping() -> None:
    # A clean response short-circuits: one send, zero backoff.
    send, sends = _script([httpx.Response(200)])
    sleep, sleeps = _sleep_recorder()
    resp = await send_with_429_retry(send, sleep=sleep)
    assert resp.status_code == 200
    assert len(sends) == 1
    assert sleeps == []


@pytest.mark.parametrize(
    ("headers", "expected_sleep"),
    [
        pytest.param({"Retry-After": "3"}, 3.0, id="honors-retry-after"),
        pytest.param({"Retry-After": "600"}, 30.0, id="caps-a-huge-retry-after"),
        pytest.param({}, 5.0, id="defaults-when-header-absent"),
    ],
)
async def test_retries_a_429_then_returns_the_recovered_response(
    headers: dict[str, str], expected_sleep: float
) -> None:
    # A 429 followed by a 200: exactly one backoff, sized by retry_after_seconds, then
    # the recovered response — proving the sleep routes through the shared parser (cap
    # + default included), not a hardcoded constant.
    send, sends = _script([httpx.Response(429, headers=headers), httpx.Response(200)])
    sleep, sleeps = _sleep_recorder()
    resp = await send_with_429_retry(send, sleep=sleep)
    assert resp.status_code == 200
    assert len(sends) == 2
    assert sleeps == [expected_sleep]


async def test_returns_the_final_429_after_exhausting_the_default_budget() -> None:
    # Unrelenting throttling: three sends total (initial + MAX_429_RETRIES=2 retries),
    # two backoffs, and the caller gets the FINAL 429 back (not an exception) so its own
    # error path runs. The distinguishing header pins it as the third send's response.
    responses = [
        httpx.Response(429, headers={"Retry-After": "3"}),
        httpx.Response(429, headers={"Retry-After": "3"}),
        httpx.Response(429, headers={"Retry-After": "3", "X-Attempt": "final"}),
    ]
    send, sends = _script(responses)
    sleep, sleeps = _sleep_recorder()
    resp = await send_with_429_retry(send, sleep=sleep)
    assert resp.status_code == 429
    assert resp.headers.get("X-Attempt") == "final"
    assert len(sends) == 3
    assert sleeps == [3.0, 3.0]


async def test_stops_at_the_first_non_429_and_does_not_over_consume() -> None:
    # A 429 then a 200 then another 429: the loop must return the 200 and never reach
    # the third response — the recovery ends the retries.
    send, sends = _script(
        [
            httpx.Response(429, headers={"Retry-After": "1"}),
            httpx.Response(200),
            httpx.Response(429),
        ]
    )
    sleep, sleeps = _sleep_recorder()
    resp = await send_with_429_retry(send, sleep=sleep)
    assert resp.status_code == 200
    assert len(sends) == 2
    assert sleeps == [1.0]


@pytest.mark.parametrize(
    ("max_retries", "expected_sends", "expected_sleeps"),
    [
        pytest.param(0, 1, 0, id="zero-retries-sends-once"),
        pytest.param(1, 2, 1, id="one-retry-sends-twice"),
        pytest.param(3, 4, 3, id="three-retries-sends-four-times"),
    ],
)
async def test_custom_max_retries_is_respected(
    max_retries: int, expected_sends: int, expected_sleeps: int
) -> None:
    # An all-429 script exercises the ceiling: sends == max_retries + 1, one backoff
    # between each pair, and the final 429 surfaces.
    responses = [httpx.Response(429, headers={"Retry-After": "1"}) for _ in range(expected_sends)]
    send, sends = _script(responses)
    sleep, sleeps = _sleep_recorder()
    resp = await send_with_429_retry(send, sleep=sleep, max_retries=max_retries)
    assert resp.status_code == 429
    assert len(sends) == expected_sends
    assert len(sleeps) == expected_sleeps
