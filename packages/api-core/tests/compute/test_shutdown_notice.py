"""The shutdown-notice wire (signature) and the email hook that speaks it.

The hook is driven end to end over real TCP: one RFC 822 message on stdin,
a local HTTP server standing in for the backend, and the request it received
checked with the receiver's own :func:`verify`.
"""

from __future__ import annotations

import io
import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import pytest
from alkera_core.compute import shutdown_notice as sn
from alkera_core.compute.shutdown_notice import (
    NOTICE_PATH,
    SIGNATURE_HEADER,
    ShutdownNotice,
    notices_from_email,
    sign,
    verify,
)

SECRET = "k" * 40
BODY = b'{"provider":"ec2","machine_id":"i-0abc12345678def90"}'
T = 1_790_000_000


@pytest.mark.parametrize(
    ("secret", "body", "header", "now", "ok"),
    [
        pytest.param(SECRET, BODY, sign(SECRET, BODY, timestamp=T), T, True, id="genuine"),
        pytest.param(SECRET, BODY, sign(SECRET, BODY, timestamp=T), T + 300, True, id="edge-in"),
        pytest.param(SECRET, BODY, sign(SECRET, BODY, timestamp=T), T + 301, False, id="stale"),
        pytest.param(SECRET, BODY, sign(SECRET, BODY, timestamp=T), T - 301, False, id="future"),
        pytest.param(
            SECRET, BODY + b" ", sign(SECRET, BODY, timestamp=T), T, False, id="body-changed"
        ),
        pytest.param("x" * 40, BODY, sign(SECRET, BODY, timestamp=T), T, False, id="other-key"),
        pytest.param(
            SECRET,
            BODY,
            sign(SECRET, BODY, timestamp=T).replace(f"t={T}", f"t={T + 1}"),
            T,
            False,
            id="timestamp-moved",
        ),
        pytest.param(SECRET, BODY, "", T, False, id="empty"),
        pytest.param(SECRET, BODY, f"t={T}", T, False, id="no-digest"),
        pytest.param(SECRET, BODY, "v1=00", T, False, id="no-timestamp"),
        pytest.param(SECRET, BODY, "t=-5,v1=00", T, False, id="negative-timestamp"),
    ],
)
def test_a_signature_verifies_only_for_its_body_key_and_window(
    secret: str, body: bytes, header: str, now: int, ok: bool
) -> None:
    assert verify(secret, body, header, now=now) is ok


@pytest.mark.parametrize(
    ("subject", "body", "expected"),
    [
        pytest.param(
            "[Retirement Notification] Amazon EC2 Instance scheduled for retirement",
            "Your instance i-0abc12345678def90 in us-east-1 is scheduled for retirement.\n"
            "Also affected: i-0abc12345678def90 and i-11112222",
            [("ec2", "i-0abc12345678def90"), ("ec2", "i-11112222")],
            id="ec2-ids-deduplicated",
        ),
        pytest.param(
            "RunPod host maintenance",
            "Pod ID: x7k2mq9zpl4a will be stopped for maintenance on host h-42.",
            [("runpod", "x7k2mq9zpl4a")],
            id="runpod-labelled",
        ),
        pytest.param(
            "Maintenance",
            "Host abcdefgh1234 goes down tonight; your workloads may restart.",
            [],
            id="a-bare-token-is-not-a-pod",
        ),
        pytest.param("Hello", "No machines here.", [], id="nothing-named"),
    ],
)
def test_an_email_yields_one_notice_per_machine_it_names_and_nothing_else(
    subject: str, body: str, expected: list[tuple[str, str]]
) -> None:
    found = notices_from_email(subject, body)
    assert [(n.provider, n.machine_id) for n in found] == expected
    for notice in found:
        assert notice.source == "email"
        assert notice.reason == " ".join(subject.split())
        assert notice.auto_terminate is True


class _Backend:
    def __init__(self, status: int) -> None:
        self.status = status
        self.received: list[tuple[str, dict[str, str], bytes]] = []


@pytest.fixture
def backend() -> Iterator[tuple[_Backend, str]]:
    state = _Backend(200)

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            state.received.append((self.path, dict(self.headers.items()), self.rfile.read(length)))
            self.send_response(state.status)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *_args: Any) -> None:
            return None

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state, f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()


EMAIL = (
    b"From: no-reply@aws.example\r\n"
    b"Subject: EC2 instance scheduled for retirement\r\n"
    b"Content-Type: text/plain; charset=utf-8\r\n"
    b"\r\n"
    b"Instance i-0abc12345678def90 will be retired.\r\n"
)


def _run(monkeypatch: pytest.MonkeyPatch, raw: bytes, env: dict[str, str]) -> int:
    for key in ("ALKERA_API_URL", "ALKERA_SHUTDOWN_NOTICE_SECRET"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(raw)))
    return sn.main()


def test_the_email_hook_posts_a_notice_the_backend_can_verify(
    monkeypatch: pytest.MonkeyPatch, backend: tuple[_Backend, str]
) -> None:
    state, url = backend
    code = _run(
        monkeypatch, EMAIL, {"ALKERA_API_URL": url, "ALKERA_SHUTDOWN_NOTICE_SECRET": SECRET}
    )
    assert code == 0
    ((path, headers, body),) = state.received
    assert path == NOTICE_PATH
    assert verify(SECRET, body, headers[SIGNATURE_HEADER])
    notice = ShutdownNotice.model_validate(json.loads(body))
    assert (notice.provider, notice.machine_id, notice.source) == (
        "ec2",
        "i-0abc12345678def90",
        "email",
    )


def test_the_email_hook_says_when_the_backend_refused(
    monkeypatch: pytest.MonkeyPatch, backend: tuple[_Backend, str]
) -> None:
    state, url = backend
    state.status = 404
    code = _run(
        monkeypatch, EMAIL, {"ALKERA_API_URL": url, "ALKERA_SHUTDOWN_NOTICE_SECRET": SECRET}
    )
    assert code == 1


@pytest.mark.parametrize(
    "env",
    [
        pytest.param({"ALKERA_SHUTDOWN_NOTICE_SECRET": SECRET}, id="no-url"),
        pytest.param(
            {"ALKERA_API_URL": "http://x", "ALKERA_SHUTDOWN_NOTICE_SECRET": "short"},
            id="weak-secret",
        ),
    ],
)
def test_the_email_hook_refuses_to_run_unconfigured(
    monkeypatch: pytest.MonkeyPatch, backend: tuple[_Backend, str], env: dict[str, str]
) -> None:
    state, _url = backend
    assert _run(monkeypatch, EMAIL, env) == 2
    assert state.received == []


def test_an_email_naming_no_machine_posts_nothing(
    monkeypatch: pytest.MonkeyPatch, backend: tuple[_Backend, str]
) -> None:
    state, url = backend
    raw = b"Subject: newsletter\r\nContent-Type: text/plain\r\n\r\nNothing to see.\r\n"
    code = _run(monkeypatch, raw, {"ALKERA_API_URL": url, "ALKERA_SHUTDOWN_NOTICE_SECRET": SECRET})
    assert code == 0
    assert state.received == []


@pytest.mark.parametrize(
    ("raw", "on"),
    [
        pytest.param("", False, id="empty-is-off"),
        pytest.param("k" * 32, True, id="long-enough"),
    ],
)
def test_the_notice_secret_is_off_or_strong(raw: str, on: bool) -> None:
    from alkera_core.config import Settings

    parsed = Settings(compute_shutdown_notice_secret=raw).compute_shutdown_notice_secret
    assert (parsed is not None) is on


def test_a_short_notice_secret_is_refused() -> None:
    from alkera_core.config import Settings
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="at least 32"):
        Settings(compute_shutdown_notice_secret="k" * 31)
