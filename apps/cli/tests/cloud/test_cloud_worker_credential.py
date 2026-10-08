"""An org worker's one credential: replaced in place, and every client that
talks to the backend for the worker speaks the new one from then on."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, ClassVar, cast

import httpx
import pytest
from alkera_cli.cloud.org_worker import worker_settings
from alkera_cli.cloud.worker_credential import (
    WorkerConnectionsClient,
    WorkerCredential,
    WorkerFolders,
    worker_rest_client,
)
from alkera_cli.cloud_sync import client as client_module
from alkera_cli.cloud_sync import shared_lease
from alkera_cli.cloud_sync.client import resolve_connections_client
from alkera_cli.cloud_sync.lease_scope import LEASE_CHAT
from alkera_cli.cloud_sync.shared_lease import lease_shared_secret_sync
from alkera_cli.commands.box import build_worker_service
from alkera_cli.host import paths
from alkera_cli.org_worker_protocol import Hello
from alkera_core.auth.machine_token import MACHINE_WORKER_TOKEN_PREFIX
from freezegun import freeze_time

ORG = "8ef423bc-531e-4d2a-baa5-8bfb6e9e8863"
API = "https://api.alkera.test"
FIRST = f"{MACHINE_WORKER_TOKEN_PREFIX}first"
SECOND = f"{MACHINE_WORKER_TOKEN_PREFIX}second"
HELD = "chat-held"
#: One shared connection of the org, leased rather than kept, as the chat
#: route answers it.
RECORD: dict[str, Any] = {
    "id": "r-shared",
    "team_id": "t1",
    "team_name": "Data Platform",
    "plugin": "postgres",
    "handle": "wh",
    "shared_values": {"host": "db.internal", "dbname": "analytics", "user": "svc"},
    "auth_mode": "shared",
    "auth_method": "password",
    "auto_add": False,
    "enabled": True,
    "has_shared_secret": True,
    "shared_custody": "lease",
    "credential_version": 1,
    "updated_at": "2026-07-01T00:00:00+00:00",
}
#: The locator a connector leases the record's primary credential by.
LOCATOR = f"{RECORD['id']}@{RECORD['credential_version']}#primary"


def test_a_replacement_is_kept_and_an_empty_one_is_ignored() -> None:
    credential = WorkerCredential("alkm_org.one")
    credential.replace("alkm_org.two")
    credential.replace("")
    assert credential.token == "alkm_org.two"


def test_the_rest_client_speaks_the_replacement_from_the_moment_it_is_made() -> None:
    """Not only after a 401: the box's notebooks sign every request with the
    REST client's headers, and a request signed with the expired bearer is
    refused with nothing to send it back for a fresh one. Its source stays
    this holder, never a file on disk."""
    credential = WorkerCredential("alkm_org.one")
    rest = worker_rest_client(credential, "https://api.example")
    clone = rest.for_agent("machine-1")
    assert rest.headers()["Authorization"] == "Bearer alkm_org.one"
    assert rest.credential.refresh() is False
    credential.replace("alkm_org.two")
    assert rest.headers()["Authorization"] == "Bearer alkm_org.two"
    assert clone.headers()["Authorization"] == "Bearer alkm_org.two"
    assert rest.credential.refresh() is False  # nothing newer to read


async def test_the_connections_read_speaks_the_replacement() -> None:
    sent: list[str] = []

    def answer(request: httpx.Request) -> httpx.Response:
        sent.append(request.headers["Authorization"])
        return httpx.Response(200, json={"items": []})

    credential = WorkerCredential("alkm_org.one")
    connections = WorkerConnectionsClient(
        api_url="https://api.example",
        credential=credential,
        chats=lambda: [],
        transport=httpx.MockTransport(answer),
    )
    await connections.records_for_chat("c1")
    credential.replace("alkm_org.two")
    await connections.records_for_chat("c1")
    assert sent == ["Bearer alkm_org.one", "Bearer alkm_org.two"]


def test_the_files_clients_speak_the_replacement(tmp_path: Path) -> None:
    """The transfers and the lease beats alike: a beat is what keeps a
    folder's lease, and one signed with an expired bearer lets it lapse."""
    sent: list[str] = []

    def answer(request: httpx.Request) -> httpx.Response:
        sent.append(request.headers["Authorization"])
        return httpx.Response(200, json={})

    credential = WorkerCredential("alkm_org.one")
    folders = WorkerFolders.for_worker(
        api_url="https://api.example", credential=credential, chats_root=tmp_path
    )
    for client in (folders._require_http(), folders._beats()):
        client._transport = httpx.MockTransport(answer)
    credential.replace("alkm_org.two")
    folders._require_http().get("/api/v1/files/item")
    folders._beats().post("/api/v1/files/leases/heartbeat")
    assert sent == ["Bearer alkm_org.two", "Bearer alkm_org.two"]


def test_the_lease_beats_speak_the_replacement(tmp_path: Path, backend_url: str) -> None:
    """The beats go out on a client of their own. Left on the first bearer it
    was refused every beat fifteen minutes in, and every folder the worker held
    lapsed while its agent was still writing into it."""
    credential = WorkerCredential(_Backend.mint(FIRST))
    folders = WorkerFolders.for_worker(
        api_url=backend_url, credential=credential, chats_root=tmp_path
    )
    credential.replace(_Backend.mint(SECOND))
    seen = len(_Backend.seen)

    folders._beats().post("/api/v1/files/drives/d1/leases/heartbeat", json={"leases": []})

    assert _Backend.bearers("POST", since=seen) == {SECOND}


#: How long the backend honours a worker credential after minting it.
TTL_SECONDS = 15 * 60


class _Backend(BaseHTTPRequestHandler):
    """The chat-scoped connection routes on a real socket, as an org worker
    meets them. A worker credential is honoured for fifteen minutes from its
    minting, on the (frozen) wall clock, and refused like an expired one past
    that. Every request's bearer is recorded beside its method."""

    minted: ClassVar[dict[str, float]] = {}
    seen: ClassVar[list[tuple[str, str]]] = []

    def _answer(self, status: int, body: dict[str, Any]) -> None:
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _route(self, method: str) -> None:
        bearer = self.headers.get("Authorization", "").removeprefix("Bearer ")
        type(self).seen.append((method, bearer))
        minted = type(self).minted.get(bearer)
        if minted is None or time.time() >= minted + TTL_SECONDS:
            self._answer(401, {"detail": "token expired"})
            return
        if method == "GET" and self.path == f"/api/v1/chats/{HELD}/connections":
            self._answer(200, {"connections": [RECORD]})
        elif method == "POST" and self.path.endswith(f"/{RECORD['id']}/credential-lease"):
            self._answer(200, {"secret": f"leased-on-{bearer}"})
        else:
            self._answer(404, {})

    def do_GET(self) -> None:
        self._route("GET")

    def do_POST(self) -> None:
        self._route("POST")

    def log_message(self, format: str, *args: Any) -> None:
        return None

    @classmethod
    def mint(cls, token: str) -> str:
        cls.minted[token] = time.time()
        return token

    @classmethod
    def bearers(cls, method: str, since: int = 0) -> set[str]:
        return {bearer for m, bearer in cls.seen[since:] if m == method}


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
def worker_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(paths, "ALKERA_HOME", home)
    monkeypatch.setattr(client_module, "_installed", None)
    monkeypatch.setattr(shared_lease, "_cache", {})
    yield tmp_path
    client_module.install_connections_client(None)


def _lease_as_the_held_chat() -> str:
    token = LEASE_CHAT.set(HELD)
    try:
        return lease_shared_secret_sync(LOCATOR)
    finally:
        LEASE_CHAT.reset(token)


async def test_an_org_worker_leases_and_syncs_on_the_credential_that_replaced_its_first(
    worker_home: Path, backend_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The worker composed exactly as the supervisor starts it, against a
    backend on a real socket. Its first credential is minted at noon and
    lives fifteen minutes; the supervisor replaces it at ten past. At sixteen
    past, a connector's credential lease and the schema cards' connection
    sync both succeed on the replacement; neither is left on the bearer the
    worker started on, which the backend now refuses."""
    monkeypatch.setenv("ALKERA_API_URL", backend_url)
    work = worker_home / "work"
    work.mkdir()
    with freeze_time("2026-10-05T12:00:00Z", real_asyncio=True) as frozen:
        hello = Hello(org_id=ORG, slot=0, machine_id="m-1", credential=_Backend.mint(FIRST))
        settings = worker_settings(hello, project_dir=work)
        credential = WorkerCredential(hello.credential)
        service = build_worker_service(
            settings, credential, org_id=str(hello.org_id), org_root=worker_home
        )
        try:
            service._mirrors[HELD] = cast(Any, object())
            await service.reconcile_connections()
            await service.schema_cards.wait_idle()
            assert await asyncio.to_thread(_lease_as_the_held_chat) == f"leased-on-{FIRST}"

            frozen.move_to("2026-10-05T12:10:00Z")
            credential.replace(_Backend.mint(SECOND))
            replaced = len(_Backend.seen)
            frozen.move_to("2026-10-05T12:16:00Z")

            assert await asyncio.to_thread(_lease_as_the_held_chat) == f"leased-on-{SECOND}"
            await service.reconcile_connections()
            await service.schema_cards.wait_idle()
        finally:
            await service.schema_cards.stop()
    assert _Backend.bearers("GET", since=replaced) == {SECOND}, "the cards' sync kept the old one"
    assert _Backend.bearers("POST", since=replaced) == {SECOND}, "a lease kept the old one"
    assert _Backend.bearers("GET") == _Backend.bearers("POST") == {FIRST, SECOND}
    assert resolve_connections_client() is service.schema_cards._connections
