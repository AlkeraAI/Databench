"""Across a credential rotation, every client of an org worker speaks the new
bearer: the chat-list re-read, the publisher state, the Files transfers a
hand-back goes out on, each held folder's fenced client, the lease beats and
the connections read.

The worker is composed exactly as the supervisor starts it and talks to a
backend on a real socket that refuses a worker credential past its life with
the code the platform uses.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, ClassVar

import pytest
from alkera_cli.cloud.folder import HeldFolder
from alkera_cli.cloud.org_worker import worker_settings
from alkera_cli.cloud.worker_credential import WorkerCredential
from alkera_cli.cloud_sync import client as client_module
from alkera_cli.commands.box import build_worker_service
from alkera_cli.files.mount import MountRecord, fenced_client
from alkera_cli.host import paths
from alkera_cli.org_worker_protocol import Hello
from alkera_core.auth.machine_token import MACHINE_WORKER_TOKEN_PREFIX
from alkera_core.machine_refusals import MACHINE_WORKER_CREDENTIAL_EXPIRED
from freezegun import freeze_time

ORG = "8ef423bc-531e-4d2a-baa5-8bfb6e9e8863"
FIRST = f"{MACHINE_WORKER_TOKEN_PREFIX}first"
SECOND = f"{MACHINE_WORKER_TOKEN_PREFIX}second"
THIRD = f"{MACHINE_WORKER_TOKEN_PREFIX}third"
CHAT = "chat-held"
TTL_SECONDS = 15 * 60


class _Backend(BaseHTTPRequestHandler):
    """Answers every route 200 for a live worker credential and 401
    ``machine_worker_credential_expired`` past its life, recording each
    request's path and bearer."""

    minted: ClassVar[dict[str, float]] = {}
    seen: ClassVar[list[tuple[str, str]]] = []

    def _route(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        bearer = self.headers.get("Authorization", "").removeprefix("Bearer ")
        type(self).seen.append((self.path.split("?")[0], bearer))
        minted = type(self).minted.get(bearer)
        if minted is None or time.time() >= minted + TTL_SECONDS:
            status = 401
            body: dict[str, Any] = {
                "detail": {
                    "code": MACHINE_WORKER_CREDENTIAL_EXPIRED,
                    "message": "Worker credential expired",
                }
            }
        else:
            status, body = 200, {"items": [], "connections": [], "id": "d1"}
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        self._route()

    def do_POST(self) -> None:
        self._route()

    def do_PUT(self) -> None:
        self._route()

    def log_message(self, format: str, *args: Any) -> None:
        return None


@pytest.fixture
def backend_url() -> Iterator[str]:
    _Backend.minted = {}
    _Backend.seen = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Backend)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def worker_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(paths, "ALKERA_HOME", home)
    monkeypatch.setattr(client_module, "_installed", None)
    yield tmp_path
    client_module.install_connections_client(None)


def _record() -> MountRecord:
    return MountRecord(drive_id="d1", node_id="n1", epoch=1, instance_id="i1")


#: Every route a worker client reaches in :func:`_call_every_client`.
EVERY_CLIENT = {
    "/api/v1/chats",
    f"/api/v1/chats/{CHAT}/publisher-state",
    "/api/v1/files/item",
    "/api/v1/files/fenced",
    "/api/v1/files/leases/heartbeat",
    "/api/v1/files/drives/d1/items/n1/lease/heartbeat",
    f"/api/v1/chats/{CHAT}/connections",
}


async def _call_every_client(service: Any, folders: Any, connections: Any) -> bool:
    """One call on each client the worker talks to the backend through, and
    the custody's own lease beat; whether the folder is still held after it."""
    await service._rest.list_chats()
    await service._rest.report_publisher_state(CHAT, state="asleep")
    folders._require_http().get("/api/v1/files/item")
    fenced_client(folders._require_http, _record()).get("/api/v1/files/fenced")
    folders._beats().post("/api/v1/files/leases/heartbeat")
    await connections.records_for_chat(CHAT)
    kept = await asyncio.to_thread(folders.beat, CHAT)
    return bool(kept) and folders.held(CHAT) is not None


async def test_every_worker_client_speaks_each_replaced_credential(
    worker_root: Path, backend_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two rotations, each past the life of the credential it replaced: after
    each, every client speaks the newest one and the folder stays held."""
    monkeypatch.setenv("ALKERA_API_URL", backend_url)
    work = worker_root / "work"
    work.mkdir()
    rounds: list[tuple[str, list[tuple[str, str]], bool]] = []
    with freeze_time("2026-10-05T12:00:00Z", real_asyncio=True) as frozen:
        _Backend.minted[FIRST] = time.time()
        hello = Hello(org_id=ORG, slot=0, machine_id="m-1", credential=FIRST)
        settings = worker_settings(hello, project_dir=work)
        credential = WorkerCredential(hello.credential)
        service = build_worker_service(
            settings, credential, org_id=str(hello.org_id), org_root=worker_root
        )
        folders = service._folders
        folders._held[CHAT] = HeldFolder(
            chat_id=CHAT,
            org_path="/chats/held",
            root=worker_root / "held",
            record=_record(),
            client=fenced_client(folders._require_http, _record()),
        )
        connections = service.schema_cards._connections
        assert connections is not None
        try:
            for minted_at, checked_at, token in (
                ("12:10", "12:16", SECOND),
                ("12:20", "12:31", THIRD),
            ):
                frozen.move_to(f"2026-10-05T{minted_at}:00Z")
                _Backend.minted[token] = time.time()
                credential.replace(token)
                frozen.move_to(f"2026-10-05T{checked_at}:00Z")
                since = len(_Backend.seen)
                kept = await _call_every_client(service, folders, connections)
                rounds.append((token, _Backend.seen[since:], kept))
        finally:
            await service.schema_cards.stop()
    for token, seen, kept in rounds:
        assert {path for path, _bearer in seen} >= EVERY_CLIENT
        stale = sorted({path for path, bearer in seen if bearer != token})
        assert stale == [], f"after the rotation to {token} these kept an expired one: {stale}"
        assert kept, f"the folder was let go after the rotation to {token}"
