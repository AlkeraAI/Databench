"""A real backend holding real chats, for the working-copy suites.

The backend is the real FastAPI app on a real socket (``files._live_backend``);
the only thing faked is the gateway's model catalog, which a chat create
consults and which these suites have no gateway for. A chat made here is a
real chat, so its folder, its working directory and the facet that names it are
the product's own.

A test module wraps these in its own fixtures (a module-scoped backend, so a
module pays for one server, and a chat per test).
"""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from alkera_sdk import AlkeraClient
from files._live_backend import LiveBackend, live_backend

_CATALOG = {
    "object": "list",
    "data": [
        {
            "id": "claude-opus-4.5",
            "object": "model",
            "display_name": "Claude Opus 4.5",
            "family": "claude",
            "wire": "anthropic",
            "efforts": ["low"],
            "default_effort": "low",
            "tier": "frontier",
            "context_window": 200000,
            "max_output_tokens": 64000,
        }
    ],
    "org_flags": {},
}


@contextlib.contextmanager
def chat_backend(tmp_path: Path) -> Iterator[LiveBackend]:
    """The live backend, with the gateway's model catalog answered locally."""
    from backend.services.chats import catalog as chat_catalog

    def catalog_client(**_: object) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=_CATALOG))
        )

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(chat_catalog, "async_client", catalog_client)
        with live_backend(tmp_path) as served:
            yield served


@dataclass
class Chat:
    """A real chat: its id, its drive, its folder and its working directory."""

    id: str
    drive_id: str
    folder_id: str
    working_id: str


def http_of(api: AlkeraClient) -> httpx.Client:
    return api.raw_client.get_httpx_client()


def make_chat(api: AlkeraClient, title: str = "Quarterly plan") -> Chat:
    """A new real chat, read back the way an editor reads it."""
    made = http_of(api).post("/api/v1/chats", json={"title": title})
    assert made.status_code == 201, made.text
    read = http_of(api).get(f"/api/v1/chats/{made.json()['id']}").json()
    folder = api.files.item(read["files_drive_id"], read["files_node_id"])
    return Chat(
        id=read["id"],
        drive_id=read["files_drive_id"],
        folder_id=read["files_node_id"],
        working_id=folder["object"]["metadata"]["files_node_id"],
    )


def upload(
    api: AlkeraClient, tmp_path: Path, drive: str, parent: str, name: str, data: bytes
) -> str:
    """Put a new file on the drive the way another member would; its node id."""
    source = tmp_path / f"upload-{uuid.uuid4().hex}"
    source.write_bytes(data)
    api.files.upload_file(source, drive_id=drive, parent_id=parent, name=name)
    return str(api.files.item_under(drive, parent, name)["id"])


def remote_bytes(api: AlkeraClient, tmp_path: Path, drive: str, node: str) -> bytes:
    dest = tmp_path / f"download-{uuid.uuid4().hex}"
    api.files.download(drive, node, dest)
    return dest.read_bytes()


def rewrite_remote(api: AlkeraClient, drive: str, node: str, data: bytes) -> None:
    """Somebody else saves a new version of ``node``."""
    api.files.put_content(drive, node, data, if_match=api.files.item(drive, node))
