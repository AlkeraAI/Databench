"""A request the far side CUT is sent once more — and only when that is safe.

The backend and the gateway are replaced by CUTTING: they refuse new work on
the signal, finish what they had inside a bounded grace, and go. A request that
was open across that moment comes back as a connection reset, or as the 502/503
the load balancer answers in the gap before the new process binds. None of
those is the request's own fault and all of them are gone a moment later, so
the box sends that ONE request again.

Once, never more: a deploy cuts a request once, and a call that fails a second
time is a real failure the caller has to see rather than sit behind. And only
where a second send means the same thing as the first — a read, or a write the
route makes idempotent. Publishing a message is not such a write, and nothing
here may quietly make it one.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import pytest
from alkera_cli.cloud.rest import CUT_STATUSES, CloudApiError, CloudRestClient


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> CloudRestClient:
    return CloudRestClient(
        api_url="http://api.test",
        token="t",
        agent_id="machine:x",
        transport=httpx.MockTransport(handler),
    )


class _Script:
    """Answers the first call with ``first`` and every later one with 200."""

    def __init__(self, first: httpx.Response | BaseException) -> None:
        self._first = first
        self.calls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        if self.calls == 1:
            if isinstance(self._first, BaseException):
                raise self._first
            return self._first
        return httpx.Response(200, json={"ok": True})


def _cut(kind: str) -> httpx.Response | BaseException:
    if kind == "reset":
        return httpx.ReadError("connection reset")
    if kind == "half-closed":
        return httpx.RemoteProtocolError("server disconnected")
    if kind == "refused":
        return httpx.ConnectError("connection refused")
    return httpx.Response(int(kind), json={"detail": "draining"})


# --------------------------------------------------------------------------- #
# what IS retried
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "kind",
    [
        pytest.param("502", id="bad-gateway"),
        pytest.param("503", id="service-unavailable-the-drain-answers-with"),
        pytest.param("504", id="gateway-timeout"),
        pytest.param("reset", id="connection-reset-under-the-request"),
        pytest.param("half-closed", id="server-disconnected-mid-response"),
        pytest.param("refused", id="listener-closed-before-the-new-one-bound"),
    ],
)
async def test_a_read_cut_by_a_deploy_is_sent_once_more(kind: str) -> None:
    script = _Script(_cut(kind))
    client = _client(script)
    assert await client.request("GET", "/api/v1/chats") == {"ok": True}
    assert script.calls == 2


async def test_a_write_the_route_makes_idempotent_is_sent_once_more() -> None:
    """The heartbeat is a stamp: the same beat twice writes the same moment.
    Losing one to a deploy would spend one of the four the box may miss before
    the platform calls it unreachable."""
    script = _Script(_cut("503"))
    client = _client(script)
    await client.heartbeat_machine("m-1", capacity=6, chats_served=0)
    assert script.calls == 2


async def test_a_claim_cut_by_a_deploy_is_sent_once_more() -> None:
    """Idempotent by pod id. A claim lost to a restarting backend would leave
    the box with no machine id, and so with no chats, until its next beat."""
    script = _Script(_cut("reset"))
    client = _client(script)
    await client.claim_machine(
        credential="c", name="box", provider_pod_id="pod-1", capacity=6, daemon_version="0"
    )
    assert script.calls == 2


# --------------------------------------------------------------------------- #
# what is NOT retried
# --------------------------------------------------------------------------- #


async def test_a_plain_write_is_never_sent_twice() -> None:
    """Publishing is the write this rule protects: a message sent twice is a
    message the reader sees twice, which is worse than one they do not see."""
    script = _Script(_cut("503"))
    client = _client(script)
    with pytest.raises(CloudApiError) as raised:
        await client.request("POST", "/api/v1/chats/c-1/messages", json_body={"text": "hi"})
    assert raised.value.status == 503
    assert script.calls == 1


@pytest.mark.parametrize(
    "status",
    [
        pytest.param(400, id="bad-request"),
        # 401 is NOT here: a refused credential has its own rule (the box goes
        # back to its credential file and replays only under a bearer that has
        # actually changed). Pinned in test_cloud_credential_refresh.py.
        pytest.param(403, id="forbidden"),
        pytest.param(404, id="not-found"),
        pytest.param(409, id="conflict"),
        pytest.param(429, id="rate-limited"),
        pytest.param(500, id="the-handler-itself-failed"),
    ],
)
async def test_an_answer_that_is_not_a_cut_is_never_replayed(status: int) -> None:
    """These are answers, not interruptions. A 500 is the handler failing on
    this request, and sending it again only fails again; a 429 has its own
    pacing, and replaying it at once is what turns a busy server into a
    struggling one."""
    assert status not in CUT_STATUSES
    script = _Script(httpx.Response(status, json={"detail": "no"}))
    client = _client(script)
    with pytest.raises(CloudApiError) as raised:
        await client.request("GET", "/api/v1/chats")
    assert raised.value.status == status
    assert script.calls == 1


async def test_a_client_with_no_credential_file_behind_it_never_replays_a_401() -> None:
    """A client handed a bare bearer has nothing to re-read, so a 401 is the
    answer. It must not reach for whatever credential file happens to be on
    the machine — what a call does would then depend on whose home directory
    the process is running in."""
    script = _Script(httpx.Response(401, json={"detail": "Not authenticated"}))
    client = _client(script)
    with pytest.raises(CloudApiError) as raised:
        await client.request("GET", "/api/v1/chats")
    assert raised.value.status == 401
    assert script.calls == 1


async def test_a_request_that_times_out_is_not_replayed() -> None:
    """A read timeout is a request that may still be running on the far side;
    sending it again is how one slow call becomes two."""

    def _always_timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("no answer")

    client = _client(_always_timeout)
    with pytest.raises(httpx.ReadTimeout):
        await client.request("GET", "/api/v1/chats")


async def test_the_retry_is_exactly_one() -> None:
    """A second cut is a real failure the caller has to see. Retrying past it
    would hold the box behind a backend that is genuinely down."""
    calls = 0

    def _always_cut(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503, json={"detail": "draining"})

    client = _client(_always_cut)
    with pytest.raises(CloudApiError) as raised:
        await client.request("GET", "/api/v1/chats")
    assert raised.value.status == 503
    assert calls == 2


async def test_the_second_send_carries_the_same_request() -> None:
    """A replay that dropped the body, the params or the per-call credential
    header would be a different request — and would fail differently, which is
    the confusing kind of failure."""
    seen: list[tuple[bytes, str, str | None]] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.content, str(request.url), request.headers.get("x-alkera-machine")))
        if len(seen) == 1:
            return httpx.Response(503, json={"detail": "draining"})
        return httpx.Response(200, json={"ok": True})

    client = _client(_handler)
    body: dict[str, Any] = {"a": 1}
    await client.request(
        "POST",
        "/api/v1/machines/claim",
        json_body=body,
        params={"q": "x"},
        headers={"x-alkera-machine": "secret"},
        retry_on_cut=True,
    )
    assert len(seen) == 2
    assert seen[0] == seen[1]
