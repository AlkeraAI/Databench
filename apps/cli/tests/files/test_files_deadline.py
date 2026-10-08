"""Every Files request from the box ends inside a deadline fixed when it was sent.

``httpx`` bounds one socket operation at a time, so a server that dribbles a
byte inside every read timeout is never cut off. These run the transport over
a real socket against a server that does exactly that, one that never answers,
and one that sends a large body slowly but steadily.
"""

from __future__ import annotations

import contextlib
import socket
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
import pytest
from alkera_cli.cloud.box_auth import BearerAuth
from alkera_cli.cloud.folder import ChatFolders
from alkera_cli.cloud.limits import ENV_FOLDER_BEAT_TIMEOUT_SECONDS
from alkera_cli.files.deadline import DeadlineTransport
from alkera_cli.files.outage import OutageAwareTransport

Handler = Callable[[socket.socket], None]


@pytest.fixture
def serve() -> Iterator[Callable[[Handler], str]]:
    """A one-connection-at-a-time TCP server running ``handler`` per client."""
    sockets: list[socket.socket] = []
    stop = threading.Event()

    def start(handler: Handler) -> str:
        listener = socket.socket()
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        listener.settimeout(0.1)
        sockets.append(listener)

        def accept() -> None:
            while not stop.is_set():
                try:
                    client, _ = listener.accept()
                except (TimeoutError, OSError):
                    continue
                sockets.append(client)
                threading.Thread(target=handler, args=(client,), daemon=True).start()

        threading.Thread(target=accept, daemon=True).start()
        return f"http://127.0.0.1:{listener.getsockname()[1]}"

    yield start
    stop.set()
    for opened in sockets:
        opened.close()


def _read_request(client: socket.socket) -> None:
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = client.recv(4096)
        if not chunk:
            return
        data += chunk


def _dribbler(every: float) -> Handler:
    """Headers at once, then a byte of an endless body every ``every`` s."""

    def handle(client: socket.socket) -> None:
        _read_request(client)
        try:
            client.sendall(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n")
            while True:
                client.sendall(b"1\r\nx\r\n")
                time.sleep(every)
        except OSError:
            return

    return handle


def _silent(client: socket.socket) -> None:
    _read_request(client)
    time.sleep(30)


def _steady(size: int, *, seconds: float) -> Handler:
    """A body of ``size`` bytes, announced up front, spread over ``seconds``."""

    def handle(client: socket.socket) -> None:
        _read_request(client)
        try:
            client.sendall(f"HTTP/1.1 200 OK\r\nContent-Length: {size}\r\n\r\n".encode())
            steps = 10
            for _ in range(steps):
                client.sendall(b"y" * (size // steps))
                time.sleep(seconds / steps)
        except OSError:
            return

    return handle


def _client(total: float, *, read: float = 5.0, min_rate: float = 64 * 1024) -> httpx.Client:
    return httpx.Client(
        transport=DeadlineTransport(httpx.HTTPTransport(), total=total, min_rate=min_rate),
        timeout=httpx.Timeout(read),
    )


def test_a_body_dribbled_inside_every_read_timeout_is_cut_off_at_the_deadline(
    serve: Callable[[Handler], str],
) -> None:
    url = serve(_dribbler(every=0.1))
    started = time.monotonic()
    with _client(total=1.0, read=5.0) as client, pytest.raises(httpx.TimeoutException):
        client.get(url)
    # Budget plus at most one capped read: well short of the read timeout.
    assert time.monotonic() - started < 2.0


def test_the_same_dribble_with_only_per_operation_timeouts_never_ends() -> None:
    """The premise: without the deadline the dribble outlives every bound
    ``httpx`` has (a read timeout of a second, three seconds of dribble)."""
    received = bytearray()
    done = threading.Event()

    def run(url: str) -> None:
        with contextlib.suppress(httpx.HTTPError), httpx.Client(timeout=1.0) as plain:
            with plain.stream("GET", url) as got:
                for chunk in got.iter_bytes():
                    received.extend(chunk)
                    if done.is_set():
                        return

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    url = f"http://127.0.0.1:{listener.getsockname()[1]}"
    worker = threading.Thread(target=run, args=(url,), daemon=True)
    worker.start()
    peer, _ = listener.accept()
    serving = threading.Thread(target=_dribbler(0.1), args=(peer,), daemon=True)
    serving.start()
    time.sleep(3.0)
    assert worker.is_alive() and len(received) > 10
    done.set()
    peer.close()
    listener.close()


def test_a_server_that_never_answers_is_given_up_on_within_the_deadline(
    serve: Callable[[Handler], str],
) -> None:
    url = serve(_silent)
    started = time.monotonic()
    with _client(total=0.9, read=30.0) as client, pytest.raises(httpx.TimeoutException):
        client.get(url)
    assert time.monotonic() - started < 1.5


def test_a_large_body_moving_steadily_is_paced_not_cut_off(
    serve: Callable[[Handler], str],
) -> None:
    """3 000 bytes over 1.5 s at a floor of 1 000 B/s earns 3 s on top of a
    half-second budget: a slow transfer that is moving finishes."""
    url = serve(_steady(3000, seconds=1.5))
    with _client(total=0.5, read=1.0, min_rate=1000) as client:
        answer = client.get(url)
    assert answer.status_code == 200
    assert len(answer.content) == 3000


def test_each_operation_is_capped_at_its_share_and_a_tighter_timeout_is_kept() -> None:
    seen: list[dict[str, float]] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(dict(request.extensions["timeout"]))
        return httpx.Response(200)

    transport = DeadlineTransport(httpx.MockTransport(record), total=30.0)
    with httpx.Client(transport=transport, timeout=httpx.Timeout(4.0, connect=1.0)) as client:
        client.get("http://api.test/x")
    # A 30 s budget caps the pool wait at 3 s; everything else asked for less.
    assert seen == [{"connect": 1.0, "read": 4.0, "write": 4.0, "pool": 3.0}]


def test_a_box_beats_its_leases_on_a_client_apart_from_its_transfers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lease beat must never queue behind a folder transfer for a pooled
    connection, nor wait out a transfer's budget: the beat client has its own
    pool (its own transport) and the beat's own short deadline, and every
    transfer still goes through the outage memory and a deadline of its own."""
    monkeypatch.setenv(ENV_FOLDER_BEAT_TIMEOUT_SECONDS, "4")
    folders = ChatFolders.for_box(
        api_url="http://box.test", auth=BearerAuth(lambda: "t"), chats_root=tmp_path
    )
    transfers = folders._require_http()
    beats = folders._beats()
    assert beats is not transfers
    assert beats._transport is not transfers._transport
    assert isinstance(beats._transport, DeadlineTransport)
    assert beats.timeout.read == 4.0
    assert isinstance(transfers._transport, OutageAwareTransport)
    folders.bind_machine("machine-1")
    assert beats.headers.get("authorization") == transfers.headers.get("authorization")
    assert {k: v for k, v in transfers.headers.items() if k.startswith("x-alkera")} == {
        k: v for k, v in beats.headers.items() if k.startswith("x-alkera")
    }
