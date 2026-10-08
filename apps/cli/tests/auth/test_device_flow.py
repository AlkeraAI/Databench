"""Unit tests for the RFC 8628 device-flow client state machine.

Driven with an `httpx.MockTransport` + injected `sleep`/`now`, so there's no real
server, no sockets, and no real time. Each scripted response is `(status, body)`,
consumed in order as the client polls.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest
from alkera_cli.account import device_flow

_FULL_CODE = {
    "device_code": "dev-code-abc",
    "user_code": "WXYZ-1234",
    "verification_uri": "http://localhost:5173/device",
    "verification_uri_complete": "http://localhost:5173/device?user_code=WXYZ-1234",
    "expires_in": 600,
    "interval": 1,
}


def _transport(responses: list[tuple[int, dict]]) -> httpx.MockTransport:
    it = iter(responses)

    def handler(_request: httpx.Request) -> httpx.Response:
        status, body = next(it)
        return httpx.Response(status, json=body)

    return httpx.MockTransport(handler)


def _recording_sleep() -> tuple[Callable[[float], None], list[float]]:
    slept: list[float] = []
    return (lambda secs: slept.append(secs)), slept


# ---------- request_device_code ----------


def test_request_device_code_parses_fields() -> None:
    resp = device_flow.request_device_code("http://api", transport=_transport([(200, _FULL_CODE)]))
    assert resp.device_code == "dev-code-abc"
    assert resp.user_code == "WXYZ-1234"
    assert resp.verification_uri_complete.endswith("user_code=WXYZ-1234")
    assert resp.expires_in == 600
    assert resp.interval == 1


def test_request_device_code_defaults_interval_to_one() -> None:
    body = {k: v for k, v in _FULL_CODE.items() if k != "interval"}
    resp = device_flow.request_device_code("http://api", transport=_transport([(200, body)]))
    assert resp.interval == 1


def test_request_device_code_non_2xx_raises() -> None:
    with pytest.raises(device_flow.DeviceCodeRequestError):
        device_flow.request_device_code("http://api", transport=_transport([(500, {})]))


def test_request_device_code_missing_field_raises() -> None:
    with pytest.raises(device_flow.DeviceCodeRequestError):
        device_flow.request_device_code(
            "http://api", transport=_transport([(200, {"device_code": "x"})])
        )


# ---------- poll_for_token ----------


def test_poll_pending_then_success() -> None:
    sleep, slept = _recording_sleep()
    token = device_flow.poll_for_token(
        "http://api",
        "dev-code",
        interval=1,
        sleep=sleep,
        transport=_transport(
            [
                (400, {"error": "authorization_pending"}),
                (200, {"access_token": "the.jwt.value", "token_type": "Bearer"}),
            ]
        ),
    )
    assert token == "the.jwt.value"
    assert slept == [1]  # slept once, for the interval, after the pending poll


def test_poll_slow_down_bumps_interval() -> None:
    sleep, slept = _recording_sleep()
    bumps: list[int] = []
    token = device_flow.poll_for_token(
        "http://api",
        "dev-code",
        interval=1,
        on_slow_down=bumps.append,
        sleep=sleep,
        transport=_transport(
            [
                (400, {"error": "slow_down"}),
                (400, {"error": "authorization_pending"}),
                (200, {"access_token": "jwt"}),
            ]
        ),
    )
    assert token == "jwt"
    assert bumps == [6]  # 1 + 5
    assert slept == [6, 6]  # slow_down bumped the interval used for both sleeps


def test_poll_access_denied_raises() -> None:
    sleep, _ = _recording_sleep()
    with pytest.raises(device_flow.AuthorizationDeniedError):
        device_flow.poll_for_token(
            "http://api",
            "dev-code",
            sleep=sleep,
            transport=_transport([(400, {"error": "access_denied"})]),
        )


def test_poll_expired_token_raises() -> None:
    sleep, _ = _recording_sleep()
    with pytest.raises(device_flow.DeviceCodeExpiredError):
        device_flow.poll_for_token(
            "http://api",
            "dev-code",
            sleep=sleep,
            transport=_transport([(400, {"error": "expired_token"})]),
        )


def test_poll_unknown_error_raises_token_error() -> None:
    sleep, _ = _recording_sleep()
    with pytest.raises(device_flow.DeviceTokenError):
        device_flow.poll_for_token(
            "http://api",
            "dev-code",
            sleep=sleep,
            transport=_transport([(400, {"error": "teapot"})]),
        )


def test_poll_local_deadline_times_out_even_if_server_keeps_pending() -> None:
    """A server that never reports expired_token must not hang a headless client —
    the local deadline (expires_in via `now`) fires instead."""
    sleep, _ = _recording_sleep()
    clock = {"t": 0.0}

    def fake_now() -> float:
        clock["t"] += 100.0  # each check jumps 100s; deadline is expires_in=10
        return clock["t"]

    with pytest.raises(device_flow.DeviceCodeExpiredError):
        device_flow.poll_for_token(
            "http://api",
            "dev-code",
            expires_in=10,
            sleep=sleep,
            now=fake_now,
            transport=_transport([(400, {"error": "authorization_pending"})]),
        )


def test_poll_transport_error_raises_token_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    sleep, _ = _recording_sleep()
    with pytest.raises(device_flow.DeviceTokenError):
        device_flow.poll_for_token(
            "http://api", "dev-code", sleep=sleep, transport=httpx.MockTransport(handler)
        )


def test_poll_recovers_from_a_transient_blip() -> None:
    """A single failed poll must not abort a multi-minute login — it retries."""
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("transient")
        return httpx.Response(200, json={"access_token": "jwt"})

    sleep, slept = _recording_sleep()
    token = device_flow.poll_for_token(
        "http://api", "dev-code", sleep=sleep, transport=httpx.MockTransport(handler)
    )
    assert token == "jwt"
    assert slept == [1]  # slept once after the blip, then succeeded


def test_poll_should_stop_cancels_cooperatively() -> None:
    """A driver (the daemon) can cancel the poll via should_stop within an iteration."""
    sleep, _ = _recording_sleep()
    with pytest.raises(device_flow.DeviceLoginCancelledError):
        device_flow.poll_for_token(
            "http://api",
            "dev-code",
            should_stop=lambda: True,
            sleep=sleep,
            transport=_transport([(400, {"error": "authorization_pending"})]),
        )


# ---------- poll_for_token: a throttled poll is not a failed login ----------


def _throttled_then(
    throttles: list[httpx.Response], final: httpx.Response
) -> tuple[httpx.MockTransport, list[int]]:
    """Answer each scripted 429 in turn, then ``final`` forever; count the calls."""
    calls: list[int] = []
    queue = list(throttles)

    def handler(_request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return queue.pop(0) if queue else final

    return httpx.MockTransport(handler), calls


@pytest.mark.parametrize(
    ("throttle", "expected_sleeps"),
    [
        pytest.param(
            httpx.Response(
                429, json={"error": {"code": "rate_limited"}}, headers={"Retry-After": "7"}
            ),
            [2, 2, 2, 1],
            id="retry-after-is-waited-in-interval-slices",
        ),
        pytest.param(
            httpx.Response(429, json={"error": {"code": "rate_limited"}}),
            [2],
            id="no-retry-after-waits-one-interval",
        ),
        pytest.param(
            httpx.Response(429, json={}, headers={"Retry-After": "0"}),
            [2],
            id="retry-after-below-the-interval-still-waits-the-interval",
        ),
        pytest.param(
            httpx.Response(429, json={}, headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}),
            [2],
            id="http-date-retry-after-falls-back-to-the-interval",
        ),
        pytest.param(
            httpx.Response(429, text="<html>slow down</html>", headers={"Retry-After": "3"}),
            [2, 1],
            id="a-proxy-429-with-no-json-body",
        ),
    ],
)
def test_poll_waits_out_a_429_and_keeps_polling(
    throttle: httpx.Response, expected_sleeps: list[float]
) -> None:
    sleep, slept = _recording_sleep()
    transport, calls = _throttled_then(
        [throttle], httpx.Response(200, json={"access_token": "jwt"})
    )
    token = device_flow.poll_for_token(
        "http://api", "dev-code", interval=2, sleep=sleep, transport=transport
    )
    assert token == "jwt"
    assert slept == expected_sleeps
    assert len(calls) == 2  # the wait is a wait, not a hot retry


#: What the backend answers when a row lock or the pool is busy for a moment.
LOCK_TIMEOUT_BODY = {
    "error": {
        "code": "db_lock_timeout",
        "message": "Another change to the same data is still in progress. Please retry shortly.",
        "details": {"retryable": True},
    }
}


@pytest.mark.parametrize(
    ("answer", "expected_sleeps"),
    [
        pytest.param(
            httpx.Response(503, json=LOCK_TIMEOUT_BODY, headers={"Retry-After": "3"}),
            [2, 1],
            id="a-busy-503-with-retry-after",
        ),
        pytest.param(
            httpx.Response(503, text="<html>unavailable</html>"),
            [2],
            id="a-proxy-503-with-no-json-body",
        ),
        pytest.param(
            httpx.Response(409, json=LOCK_TIMEOUT_BODY),
            [2],
            id="any-status-whose-envelope-says-retryable",
        ),
    ],
)
def test_poll_waits_out_a_retryable_answer_and_keeps_polling(
    answer: httpx.Response, expected_sleeps: list[float]
) -> None:
    """A server that was briefly busy asked to be tried again; giving up there
    left the login half done (the org switch kept the old org)."""
    sleep, slept = _recording_sleep()
    transport, calls = _throttled_then([answer], httpx.Response(200, json={"access_token": "jwt"}))
    token = device_flow.poll_for_token(
        "http://api", "dev-code", interval=2, sleep=sleep, transport=transport
    )
    assert token == "jwt"
    assert slept == expected_sleeps
    assert len(calls) == 2


def test_a_run_of_retryable_answers_waits_longer_each_time() -> None:
    """With no Retry-After the wait doubles from the poll interval while the
    server keeps saying not now, and starts over once it answers."""
    sleep, slept = _recording_sleep()
    busy = [httpx.Response(503, json=LOCK_TIMEOUT_BODY) for _ in range(3)]
    transport, _ = _throttled_then(busy, httpx.Response(200, json={"access_token": "jwt"}))
    device_flow.poll_for_token(
        "http://api", "dev-code", interval=2, sleep=sleep, transport=transport
    )
    assert slept == [2, 2, 2, 2, 2, 2, 2]  # 2, then 4, then 8, in interval slices


def test_a_refusal_that_is_not_retryable_still_ends_the_login() -> None:
    """A server error is waited out (the person may still be approving); an
    answer that refuses the request itself, and does not ask to be tried
    again, ends the login at once."""
    with pytest.raises(device_flow.DeviceTokenError):
        device_flow.poll_for_token(
            "http://api",
            "dev-code",
            sleep=_recording_sleep()[0],
            transport=_transport([(400, {"error": {"code": "invalid_request"}})]),
        )


def test_poll_429s_do_not_count_as_transport_errors() -> None:
    """More throttles than the consecutive-error allowance: still not fatal."""
    sleep, _ = _recording_sleep()
    throttles = [httpx.Response(429, json={}) for _ in range(20)]
    transport, _ = _throttled_then(throttles, httpx.Response(200, json={"access_token": "jwt"}))
    assert (
        device_flow.poll_for_token("http://api", "dev-code", sleep=sleep, transport=transport)
        == "jwt"
    )


def test_poll_cancel_lands_inside_a_long_throttle_wait() -> None:
    """A 15-minute Retry-After must not hold a superseded login's thread."""
    slept: list[float] = []
    transport, calls = _throttled_then(
        [httpx.Response(429, json={}, headers={"Retry-After": "900"})],
        httpx.Response(200, json={"access_token": "jwt"}),
    )
    with pytest.raises(device_flow.DeviceLoginCancelledError):
        device_flow.poll_for_token(
            "http://api",
            "dev-code",
            interval=1,
            should_stop=lambda: len(slept) >= 3,
            sleep=slept.append,
            transport=transport,
        )
    assert slept == [1, 1, 1]
    assert len(calls) == 1


def test_poll_throttle_wait_ends_at_the_local_deadline() -> None:
    clock = {"t": 0.0}

    def sleep(secs: float) -> None:
        clock["t"] += secs

    transport, _ = _throttled_then(
        [httpx.Response(429, json={}, headers={"Retry-After": "900"})],
        httpx.Response(429, json={}, headers={"Retry-After": "900"}),
    )
    with pytest.raises(device_flow.DeviceCodeExpiredError):
        device_flow.poll_for_token(
            "http://api",
            "dev-code",
            interval=5,
            expires_in=20,
            sleep=sleep,
            now=lambda: clock["t"],
            transport=transport,
        )
    assert clock["t"] == 20.0


# ---------- poll_for_token: an unattended login eases off ----------


def _pending_forever_until(total_calls: int) -> tuple[httpx.MockTransport, list[int]]:
    calls: list[int] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) >= total_calls:
            return httpx.Response(200, json={"access_token": "jwt"})
        return httpx.Response(400, json={"error": "authorization_pending"})

    return httpx.MockTransport(handler), calls


def test_poll_is_fast_while_someone_is_likely_there_then_eases_off() -> None:
    clock = {"t": 0.0}
    slept: list[float] = []

    def sleep(secs: float) -> None:
        slept.append(secs)
        clock["t"] += secs

    transport, _ = _pending_forever_until(36)
    device_flow.poll_for_token(
        "http://api",
        "dev-code",
        interval=1,
        sleep=sleep,
        now=lambda: clock["t"],
        transport=transport,
    )
    # Thirty one-second polls, and from the 30 s mark every wait is five.
    assert slept == [1] * 30 + [5] * 5


def test_an_abandoned_login_stays_a_small_share_of_a_five_minute_budget() -> None:
    """The whole ten-minute life of a code nobody approves, in requests."""
    clock = {"t": 0.0}

    def sleep(secs: float) -> None:
        clock["t"] += secs

    transport, calls = _pending_forever_until(10_000)
    with pytest.raises(device_flow.DeviceCodeExpiredError):
        device_flow.poll_for_token(
            "http://api",
            "dev-code",
            interval=1,
            expires_in=600,
            sleep=sleep,
            now=lambda: clock["t"],
            transport=transport,
        )
    assert len(calls) == 30 + (600 - 30) // 5


def test_easing_off_never_polls_faster_than_a_slowed_interval() -> None:
    """`slow_down` pushed the interval past the patient one: the larger wins."""
    clock = {"t": 100.0}
    slept: list[float] = []

    def sleep(secs: float) -> None:
        slept.append(secs)
        clock["t"] += secs

    responses = [(400, {"error": "slow_down"})] * 2 + [(200, {"access_token": "jwt"})]
    device_flow.poll_for_token(
        "http://api",
        "dev-code",
        interval=1,
        sleep=sleep,
        now=lambda: clock["t"],
        transport=_transport(responses),
    )
    assert slept == [6, 11]


# ---------- a server error while the person is still approving ----------


def _busy(error: str) -> tuple[int, dict]:
    """A 5xx the backend answers with a JSON body while it is under load."""
    return 503, {"error": error, "detail": "try again shortly"}


@pytest.mark.parametrize("error", ["db_pool_exhausted", "db_lock_timeout"])
def test_poll_keeps_polling_through_a_server_error_until_the_token(error: str) -> None:
    sleep, slept = _recording_sleep()
    token = device_flow.poll_for_token(
        "http://api",
        "dev-code",
        interval=2,
        sleep=sleep,
        transport=_transport(
            [
                _busy(error),
                (400, {"error": "authorization_pending"}),
                _busy(error),
                (200, {"access_token": "jwt"}),
            ]
        ),
    )
    assert token == "jwt"
    # Each wait is the server's interval: an error does not hurry the poll.
    assert slept == [2, 2, 2]


def test_server_errors_do_not_add_up_to_a_failed_login() -> None:
    """More server errors in a row than a dropped connection is allowed still
    leave the login waiting for its answer."""
    sleep, _ = _recording_sleep()
    answers = [_busy("db_pool_exhausted")] * 20 + [(200, {"access_token": "jwt"})]
    token = device_flow.poll_for_token(
        "http://api", "dev-code", sleep=sleep, transport=_transport(answers)
    )
    assert token == "jwt"


@pytest.mark.parametrize(
    ("final", "raised"),
    [
        pytest.param(
            (400, {"error": "access_denied"}),
            device_flow.AuthorizationDeniedError,
            id="denied-after-an-error",
        ),
        pytest.param(
            (400, {"error": "expired_token"}),
            device_flow.DeviceCodeExpiredError,
            id="expired-after-an-error",
        ),
        pytest.param(
            (400, {"error": "teapot"}), device_flow.DeviceTokenError, id="unknown-after-an-error"
        ),
    ],
)
def test_a_terminal_answer_after_a_server_error_still_stops(
    final: tuple[int, dict], raised: type[Exception]
) -> None:
    sleep, _ = _recording_sleep()
    with pytest.raises(raised):
        device_flow.poll_for_token(
            "http://api",
            "dev-code",
            sleep=sleep,
            transport=_transport([_busy("db_pool_exhausted"), final]),
        )


def test_a_server_that_keeps_failing_stops_when_the_code_expires() -> None:
    sleep, slept = _recording_sleep()
    clock = {"t": 0.0}

    def asleep(seconds: float) -> None:
        sleep(seconds)
        clock["t"] += seconds

    def busy_forever(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "db_lock_timeout"})

    with pytest.raises(device_flow.DeviceCodeExpiredError):
        device_flow.poll_for_token(
            "http://api",
            "dev-code",
            interval=5,
            expires_in=60,
            sleep=asleep,
            now=lambda: clock["t"],
            transport=httpx.MockTransport(busy_forever),
        )
    # Polled on the interval for the code's whole life, then stopped.
    assert slept == [5] * 12
