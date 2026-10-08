"""RFC 8628 OAuth 2.0 Device Authorization Grant — client side.

Pure ``httpx`` + injectable ``sleep`` / ``now`` / ``transport`` seams so the
polling state machine is unit-testable against an ``httpx.MockTransport`` with
zero real time and zero sockets. Shared by ``alkera login`` (synchronous) and the
daemon's ``auth.startDeviceLogin`` (driven from a thread executor).

Replaces the loopback ``AuthCallbackServer`` — there is no port to bind and no
assumption that the host running the CLI has a browser next to it, so login works
identically from a laptop, a container, or an SSH session.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

import httpx

from alkera_cli.host.backoff import exponential_delay, parse_retry_after
from alkera_cli.host.http_error import response_is_retryable

# Public client identifiers (RFC 8628 §3.1 — no secret). Distinct per surface so
# the browser consent screen can name what's signing in.
CLIENT_ID_CLI = "alkera-cli"
CLIENT_ID_VSCODE = "alkera-vscode"
#: A person's own box. Its approval mints a machine credential bound to the
#: approver's org and to them, never a session.
CLIENT_ID_BOX = "alkera-box"
DEFAULT_SCOPE = "cli"

_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:device_code"
# RFC 8628 §3.5: on `slow_down`, increase the interval by at least 5 seconds.
_SLOW_DOWN_BUMP_SECONDS = 5
# The issued interval is a minimum, and it is tuned for someone sitting at the
# browser: approval lands within a second. Past this long with no answer nobody
# is, so ease off — a login tab left open otherwise polls every second for the
# code's whole life, which from one address is half the edge's five-minute
# request budget for everyone behind it.
_PATIENT_AFTER_SECONDS = 30.0
_PATIENT_INTERVAL_SECONDS = 5
# Tolerate brief connectivity blips mid-poll before giving up — the local
# deadline still bounds a long outage into DeviceCodeExpiredError.
_MAX_CONSECUTIVE_POLL_ERRORS = 6

#: The longest a poll waits out a server that keeps saying "not now" without
#: naming a time: it grows from the poll interval up to this.
_RETRY_CAP_SECONDS = 30.0

_CODE_PATH = "/api/v1/auth/device/code"
_TOKEN_PATH = "/api/v1/auth/device/token"  # noqa: S105 — URL path, not a secret


@dataclass(frozen=True, slots=True)
class DeviceCodeResponse:
    device_code: str
    user_code: str
    verification_uri: str
    verification_uri_complete: str
    expires_in: int
    interval: int


class DeviceFlowError(Exception):
    """Base for every device-flow failure."""


class DeviceCodeRequestError(DeviceFlowError):
    """The initial device/code request failed (network / non-2xx / bad body)."""


class AuthorizationDeniedError(DeviceFlowError):
    """The user denied the request — token endpoint returned access_denied."""


class DeviceCodeExpiredError(DeviceFlowError):
    """The device_code expired before approval (expired_token, or our own
    local deadline elapsed first)."""


class DeviceTokenError(DeviceFlowError):
    """An unexpected token-endpoint outcome (unknown error code or transport)."""


class DeviceLoginCancelledError(DeviceFlowError):
    """Polling was cancelled cooperatively via ``should_stop`` (e.g. the daemon
    superseded this login with a new one, or the user closed the panel)."""


def request_device_code(
    api_url: str,
    *,
    client_id: str = CLIENT_ID_CLI,
    scope: str = DEFAULT_SCOPE,
    timeout: float = 10.0,
    transport: httpx.BaseTransport | None = None,
) -> DeviceCodeResponse:
    """POST the device authorization endpoint. ``transport`` is the test seam."""
    url = f"{api_url.rstrip('/')}{_CODE_PATH}"
    try:
        with httpx.Client(timeout=timeout, transport=transport) as client:
            resp = client.post(url, data={"client_id": client_id, "scope": scope})
            resp.raise_for_status()
            body = resp.json()
        return DeviceCodeResponse(
            device_code=body["device_code"],
            user_code=body["user_code"],
            verification_uri=body["verification_uri"],
            verification_uri_complete=body["verification_uri_complete"],
            expires_in=int(body["expires_in"]),
            interval=int(body.get("interval", 1)),
        )
    except (httpx.HTTPError, KeyError, ValueError, TypeError) as exc:
        raise DeviceCodeRequestError(f"could not start device login: {exc}") from exc


def _retry_after_seconds(resp: httpx.Response) -> float:
    """The ``Retry-After`` delay in seconds; 0 when absent or unreadable. The
    HTTP-date form is not read (no wall clock is trusted here): the caller's
    floor applies."""
    return parse_retry_after(resp.headers.get("retry-after")) or 0.0


def poll_for_token(
    api_url: str,
    device_code: str,
    *,
    client_id: str = CLIENT_ID_CLI,
    interval: int = 1,
    expires_in: int = 600,
    on_slow_down: Callable[[int], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
    timeout: float = 10.0,
    transport: httpx.BaseTransport | None = None,
) -> str:
    """Poll the token endpoint until terminal. Returns the access token (JWT).

    State machine (RFC 8628 §3.5):
      authorization_pending → sleep(interval; 5s once 30s have passed), keep polling
      slow_down             → interval += 5 (+ on_slow_down), sleep, keep polling
      retryable (429, any   → sleep(Retry-After, at least a wait that grows from the
      5xx, or details.        interval), keep polling: the server is busy, not
      retryable)              answering, whatever its body says
      access_denied         → AuthorizationDeniedError
      expired_token         → DeviceCodeExpiredError
      access_token present  → return it

    A local deadline (``expires_in`` measured via ``now``) guards a headless / SSH
    caller from hanging forever if the server never reports ``expired_token``.
    ``should_stop`` is polled once per iteration so a background driver (the
    daemon) can cancel cooperatively within one interval.
    """
    url = f"{api_url.rstrip('/')}{_TOKEN_PATH}"
    started = now()
    deadline = started + expires_in
    poll_interval = max(1, interval)

    def pause() -> int:
        if now() - started < _PATIENT_AFTER_SECONDS:
            return poll_interval
        return max(poll_interval, _PATIENT_INTERVAL_SECONDS)

    data = {"grant_type": _GRANT_TYPE, "device_code": device_code, "client_id": client_id}
    consecutive_errors = 0
    retries = 0

    with httpx.Client(timeout=timeout, transport=transport) as client:
        while True:
            if should_stop is not None and should_stop():
                raise DeviceLoginCancelledError("device login cancelled")
            if now() >= deadline:
                raise DeviceCodeExpiredError("device login timed out before approval")
            try:
                resp = client.post(url, data=data)
                # A throttle or a server error may come from a proxy with no
                # JSON body, and neither body is an answer to read: both are
                # waited out below.
                retryable = response_is_retryable(resp) or resp.is_server_error
                body = {} if retryable else resp.json()
            except (httpx.HTTPError, ValueError) as exc:
                # A transient blip shouldn't abort a multi-minute login. Retry
                # (re-polling the same device_code is safe — the server only
                # advances its throttle on polls that reach it) until either the
                # deadline elapses or we hit a sustained outage.
                consecutive_errors += 1
                if consecutive_errors >= _MAX_CONSECUTIVE_POLL_ERRORS:
                    raise DeviceTokenError(f"device token poll failed: {exc}") from exc
                sleep(poll_interval)
                continue
            consecutive_errors = 0

            if retryable:
                # Throttled, busy or restarting (an exhausted pool, a lock
                # timeout) while the person may still be approving: the code
                # is still good and only the deadline, or a real answer, ends
                # the wait. Wait as long as the server asks, and longer each
                # time it asks again in a row, in poll-interval slices so a
                # cancel or the deadline still lands within one interval.
                retries += 1
                waited = 0.0
                wait = max(
                    exponential_delay(retries - 1, first=poll_interval, cap=_RETRY_CAP_SECONDS),
                    _retry_after_seconds(resp),
                )
                while waited < wait and now() < deadline:
                    if should_stop is not None and should_stop():
                        raise DeviceLoginCancelledError("device login cancelled")
                    step = min(float(poll_interval), wait - waited)
                    sleep(step)
                    waited += step
                continue
            retries = 0

            if resp.status_code == 200 and "access_token" in body:
                return str(body["access_token"])

            error = body.get("error") if isinstance(body, dict) else None
            if error == "authorization_pending":
                sleep(pause())
                continue
            if error == "slow_down":
                poll_interval += _SLOW_DOWN_BUMP_SECONDS
                if on_slow_down is not None:
                    on_slow_down(poll_interval)
                sleep(pause())
                continue
            if error == "access_denied":
                raise AuthorizationDeniedError("login was denied in the browser")
            if error == "expired_token":
                raise DeviceCodeExpiredError("device code expired before approval")
            raise DeviceTokenError(f"unexpected device token response: {error or resp.status_code}")


def approval_url_for_org(verification_uri_complete: str, org: str | None) -> str:
    """The approval page with ``org`` pre-selected (``&org=<id>``). The approver
    can still pick another of their orgs there; the token says which they did."""
    if not org:
        return verification_uri_complete
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

    parts = urlsplit(verification_uri_complete)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != "org"]
    query.append(("org", org))
    return urlunsplit(parts._replace(query=urlencode(query)))
