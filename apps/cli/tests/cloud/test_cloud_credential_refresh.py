"""A box outlives the session it booted with.

The box reads its credential file once, at start. A turn may run for days, and
the box's session is re-issued while it does — by a rotation, or by an operator
running `alkera login` on the box. A process holding the string it booted with
is refused from that moment on, for everything: heartbeats (so the platform
calls it unreachable), the socket ticket (so no chat is published), the
transcript, the folder leases. It sits there looking alive with a perfectly
good credential on its own disk.

So a 401 sends the box back to the file. If the file holds something new, the
one refused request goes again under it. If the file holds exactly what was
just refused, the credential really is being refused — a revocation, which is
an answer no retry changes — and it is surfaced as such.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest
from alkera_cli.cloud.rest import BoxCredential, CloudApiError, CloudRestClient


class _Server:
    """A backend that accepts exactly one bearer, and can be made to rotate it."""

    def __init__(self, accepts: str) -> None:
        self.accepts = accepts
        self.seen: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        offered = request.headers.get("authorization", "").removeprefix("Bearer ")
        self.seen.append(offered)
        if offered != self.accepts:
            return httpx.Response(401, json={"detail": "Not authenticated"})
        return httpx.Response(200, json={"ok": True})


def _client(server: _Server, *, token: str, on_disk: Callable[[], str]) -> CloudRestClient:
    return CloudRestClient(
        api_url="http://api.test",
        token=BoxCredential(token, read=on_disk),
        agent_id="machine:x",
        transport=httpx.MockTransport(server),
    )


async def test_a_box_whose_session_was_re_issued_picks_the_new_one_off_its_disk() -> None:
    """The reported failure, end to end: the server has moved on to a new
    bearer and the file already holds it. Without the re-read every call from
    here on is a 401."""
    server = _Server(accepts="fresh")
    client = _client(server, token="stale", on_disk=lambda: "fresh")

    assert await client.request("GET", "/api/v1/chats") == {"ok": True}
    assert server.seen == ["stale", "fresh"]


async def test_the_refresh_reaches_a_write_too() -> None:
    """A 401 means the request was REFUSED, so nothing happened and sending it
    again is safe even for a publish — unlike a cut, where the far side may
    have acted on it."""
    server = _Server(accepts="fresh")
    client = _client(server, token="stale", on_disk=lambda: "fresh")

    assert await client.request("POST", "/api/v1/chats/c-1/messages", json_body={"text": "hi"}) == {
        "ok": True
    }
    assert server.seen == ["stale", "fresh"]


async def test_the_refresh_reaches_every_chat_at_once() -> None:
    """A box serving twenty chats has twenty clients. A credential each of them
    had to notice separately is a box that comes back one chat at a time."""
    server = _Server(accepts="fresh")
    client = _client(server, token="stale", on_disk=lambda: "fresh")
    mirror = client.for_agent("chat:c-1")

    await client.request("GET", "/api/v1/chats")
    assert await mirror.request("GET", "/api/v1/chats/c-1") == {"ok": True}
    # The clone never saw a 401 of its own: it was already speaking with the
    # bearer the first refresh found.
    assert server.seen == ["stale", "fresh", "fresh"]


async def test_a_revoked_credential_is_refused_rather_than_retried() -> None:
    """The file says exactly what was just refused. That is a revocation —
    an answer no retry changes — and retrying it would have the box asking for
    ever while every chat on it hung."""
    server = _Server(accepts="something-else")
    calls = 0

    def _on_disk() -> str:
        nonlocal calls
        calls += 1
        return "stale"

    client = _client(server, token="stale", on_disk=_on_disk)

    with pytest.raises(CloudApiError) as raised:
        await client.request("GET", "/api/v1/chats")
    assert raised.value.status == 401
    assert calls == 1, "the file is read once, to decide — not per attempt"
    assert server.seen == ["stale"]


@pytest.mark.parametrize(
    "on_disk",
    [
        pytest.param(lambda: "", id="no-credential-file-at-all"),
        pytest.param(lambda: "stale", id="the-same-bearer-that-was-refused"),
    ],
)
async def test_nothing_new_on_disk_is_a_refusal(on_disk: Callable[[], str]) -> None:
    server = _Server(accepts="unreachable-bearer")
    client = _client(server, token="stale", on_disk=on_disk)
    with pytest.raises(CloudApiError):
        await client.request("GET", "/api/v1/chats")
    assert server.seen == ["stale"]


async def test_a_second_401_under_the_fresh_bearer_is_not_retried_again() -> None:
    """One refresh, one retry. A bearer the file offers and the server still
    refuses is a refusal, not a rotation to chase."""
    server = _Server(accepts="never-offered")
    tokens = iter(["fresh-1", "fresh-2", "fresh-3"])
    client = _client(server, token="stale", on_disk=lambda: next(tokens))

    with pytest.raises(CloudApiError):
        await client.request("GET", "/api/v1/chats")
    assert server.seen == ["stale", "fresh-1"]


# --------------------------------------------------------------------------- #
# the holder itself
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("held", "on_disk", "changed"),
    [
        pytest.param("a", "b", True, id="rotated"),
        pytest.param("a", "a", False, id="unchanged"),
        pytest.param("a", "", False, id="file-gone-or-unreadable"),
    ],
)
def test_the_holder_only_reports_a_real_rotation(held: str, on_disk: str, changed: bool) -> None:
    """``False`` is the answer that matters: it is what tells a caller the 401
    is the credential being refused rather than a rotation it slept through."""
    credential = BoxCredential(held, read=lambda: on_disk)
    assert credential.refresh() is changed
    assert credential.token == (on_disk if changed else held)
