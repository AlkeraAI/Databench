"""A host the box could not connect to is not asked again for a while.

On a node whose content origin was unreachable, every folder pull that needed
a byte waited the whole connect budget to learn so, one after another, each
holding a take slot — and the chat a reader had just written to waited behind
all of them. The Files client's transport remembers the first connect failure
for a window and refuses the host at once inside it; an answer of any kind
clears the mark, and a host that answered is never marked.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import httpx
import pytest
from alkera_cli.cloud.box_auth import BearerAuth
from alkera_cli.cloud.folder import ChatFolders
from alkera_cli.files.outage import OutageAwareTransport

API = "https://api.test"
ORIGIN = "http://origin.test:8444"
WINDOW = 60.0


class _Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


class _Inner(httpx.BaseTransport):
    """The wire: the API answers; the origin fails as the test says."""

    def __init__(self) -> None:
        self.asked: Counter[str] = Counter()
        self.origin_fails: type[Exception] | None = httpx.ConnectTimeout
        self.origin_status = 200

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        self.asked[host] += 1
        if host == "origin.test" and self.origin_fails is not None:
            raise self.origin_fails("no route", request=request)
        if host == "origin.test":
            return httpx.Response(self.origin_status)
        return httpx.Response(200)


@pytest.fixture
def wire() -> _Inner:
    return _Inner()


@pytest.fixture
def clock() -> _Clock:
    return _Clock()


@pytest.fixture
def client(wire: _Inner, clock: _Clock) -> httpx.Client:
    return httpx.Client(transport=OutageAwareTransport(wire, window=WINDOW, clock=clock))


def test_the_first_connect_failure_answers_the_next_asks_at_once(
    client: httpx.Client, wire: _Inner, clock: _Clock
) -> None:
    with pytest.raises(httpx.ConnectTimeout):
        client.get(f"{ORIGIN}/c/one")
    assert wire.asked["origin.test"] == 1

    clock.now += 12.0
    with pytest.raises(httpx.ConnectError) as refused:
        client.get(f"{ORIGIN}/c/two")
    assert wire.asked["origin.test"] == 1, "the origin was asked again inside the window"
    said = str(refused.value)
    assert "origin.test could not be reached 12 s ago" in said
    assert "ConnectTimeout" in said and "not asked again for another 48 s" in said

    # The API is another host: unaffected.
    assert client.get(f"{API}/api/v1/chats").status_code == 200
    assert wire.asked["api.test"] == 1


def test_the_window_over_the_host_is_asked_again_and_an_answer_clears_the_mark(
    client: httpx.Client, wire: _Inner, clock: _Clock
) -> None:
    with pytest.raises(httpx.ConnectTimeout):
        client.get(f"{ORIGIN}/c/one")
    transport = client._transport
    assert isinstance(transport, OutageAwareTransport)
    assert transport.seconds_left("origin.test") == WINDOW

    clock.now += WINDOW
    wire.origin_fails = None
    assert client.get(f"{ORIGIN}/c/two").status_code == 200
    assert wire.asked["origin.test"] == 2
    assert transport.seconds_left("origin.test") is None
    assert client.get(f"{ORIGIN}/c/three").status_code == 200


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(httpx.ReadTimeout, id="a-read-that-timed-out"),
        pytest.param(httpx.RemoteProtocolError, id="a-broken-answer"),
    ],
)
def test_a_host_that_answered_is_never_marked(
    client: httpx.Client, wire: _Inner, failure: type[Exception]
) -> None:
    wire.origin_fails = failure
    for _ in range(3):
        with pytest.raises(failure):
            client.get(f"{ORIGIN}/c/one")
    assert wire.asked["origin.test"] == 3, "a host that answered was refused as unreachable"


def test_a_refusal_from_the_host_is_an_answer_too(client: httpx.Client, wire: _Inner) -> None:
    wire.origin_fails = None
    wire.origin_status = 503
    assert client.get(f"{ORIGIN}/c/one").status_code == 503
    assert client.get(f"{ORIGIN}/c/two").status_code == 503
    assert wire.asked["origin.test"] == 2


def test_the_box_files_client_bounds_its_connects_and_remembers_an_outage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The wiring: the custody a box runs with connects on the short budget,
    reads on the long one, and speaks through the transport above."""
    monkeypatch.delenv("ALKERA_CLOUD_FILES_CONNECT_TIMEOUT_SECONDS", raising=False)
    folders = ChatFolders.for_box(
        api_url=API, auth=BearerAuth(lambda: "alk_machine_x"), chats_root=tmp_path
    )
    http = folders._http
    assert http is not None
    assert (http.timeout.connect, http.timeout.read) == (10.0, 120.0)
    assert isinstance(http._transport, OutageAwareTransport)

    monkeypatch.setenv("ALKERA_CLOUD_FILES_CONNECT_TIMEOUT_SECONDS", "3")
    shorter = ChatFolders.for_box(
        api_url=API, auth=BearerAuth(lambda: "alk_machine_x"), chats_root=tmp_path
    )
    assert shorter._http is not None and shorter._http.timeout.connect == 3.0

    # A connect budget above the request budget is the request budget.
    tight = ChatFolders.for_box(
        api_url=API, auth=BearerAuth(lambda: "alk_machine_x"), chats_root=tmp_path, timeout=2.0
    )
    assert tight._http is not None
    assert (tight._http.timeout.connect, tight._http.timeout.read) == (2.0, 2.0)
