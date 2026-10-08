"""The timeouts every ``alkera files`` transfer runs on.

A real socket, a real thread and a real stall — an ``httpx`` transport stub
cannot show a timeout, because the timeout is the transport's own behaviour.
The server here answers one route and takes its time about it, which is what a
server hashing, storing and committing a 100 MB part looks like from the
client's side.
"""

from __future__ import annotations

import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest
from _profiles import ORG_A, store
from alkera_cli.commands import files as files_cli
from alkera_cli.files.push import PushSummary, TransferTimeoutError, push
from typer.testing import CliRunner

DRIVE = "11111111-1111-1111-1111-111111111111"
NODE = "22222222-2222-2222-2222-222222222222"


class _Stalling(BaseHTTPRequestHandler):
    """Answers a content PUT after ``server.stall`` seconds."""

    protocol_version = "HTTP/1.1"

    def do_PUT(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        time.sleep(self.server.stall)  # type: ignore[attr-defined]
        body = b'{"id": "%s", "etag": "9"}' % NODE.encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: Any) -> None:
        return


@pytest.fixture
def stalling_server() -> Any:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Stalling)
    server.stall = 0.0  # type: ignore[attr-defined]
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _client_for(server: Any, monkeypatch: pytest.MonkeyPatch) -> httpx.Client:
    """The very client every Files verb gets, pointed at the stalling server."""
    host, port = server.server_address[0], server.server_address[1]
    store(ORG_A, current=True, api_url=f"http://{host}:{port}")
    api = files_cli._signed_in_client()
    return api.raw_client.get_httpx_client()


def test_a_transfer_client_waits_out_a_stall_the_sdk_default_would_abort(
    stalling_server: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Six seconds is nothing for a part; the SDK's 5 s default kills it.

    Driven through ``_Gap.put_content`` — the raw-``httpx`` helper a push does
    its content writes with — because inheriting the client's timeout is the
    whole point: the helper never sets one of its own.
    """
    from alkera_cli.files.push import _Gap

    stalling_server.stall = 6.0
    source = tmp_path / "big.bin"
    source.write_bytes(b"payload")

    http = _client_for(stalling_server, monkeypatch)
    with http:
        landed = _Gap(http).put_content(DRIVE, NODE, "7", source)

    assert landed["etag"] == "9"


def test_a_stall_past_the_read_timeout_names_the_file_it_stalled_on(
    stalling_server: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The transport exception says only "ReadTimeout"; the operator needs to
    know which of ten thousand entries stalled, so the push raises its own."""
    monkeypatch.setattr(
        files_cli,
        "FILES_TRANSFER_TIMEOUT",
        httpx.Timeout(connect=10.0, read=0.25, write=10.0, pool=10.0),
    )
    stalling_server.stall = 2.0

    root = tmp_path / "tree"
    root.mkdir()
    (root / "ledger.csv").write_bytes(b"payload")

    http = _client_for(stalling_server, monkeypatch)
    with http, pytest.raises(TransferTimeoutError) as stalled:
        push(files=_FilesStub(), http=http, root=root, dest="")

    assert "ledger.csv" in str(stalled.value)
    assert "resumes" in str(stalled.value)


def test_the_push_command_reports_a_stalled_transfer_without_a_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A read timeout is an operator message, not a stack trace."""
    store(ORG_A, current=True, api_url="http://127.0.0.1:1")

    def _stall(**_kwargs: Any) -> PushSummary:
        raise TransferTimeoutError("ledger.csv: the server did not answer within the timeout")

    monkeypatch.setattr(files_cli, "push", _stall)
    root = tmp_path / "tree"
    root.mkdir()
    result = CliRunner().invoke(files_cli.files_app, ["push", str(root), "/Shared/proj"])

    assert result.exit_code == 1
    assert "ledger.csv" in result.output
    assert "Traceback" not in result.output


class _FilesStub:
    """The ``client.files`` calls a one-file push makes before the content PUT.

    The file is reported as already present with a stale hash, so the push
    takes the single-call content branch — the one the stalling server answers.
    """

    def drive(self) -> dict[str, Any]:
        return {"id": DRIVE, "rootId": NODE}

    def item_by_path(self, drive_id: str, item_path: str) -> dict[str, Any]:
        return {
            "id": NODE,
            "etag": "7",
            "kind": "file",
            "name": item_path,
            "file": {"content_hash": "stale", "size": 0},
        }
