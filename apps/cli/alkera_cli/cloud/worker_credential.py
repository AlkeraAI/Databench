"""The one credential an org worker has, and the clients that follow it.

The supervisor mints it for (this machine, this org) and replaces it before it
expires, over the socketpair; it is never in the worker's environment or on
disk. Every client the worker talks to the backend through asks the holder
(:class:`~alkera_cli.cloud.box_auth.WorkerCredential`) for the bearer at each
request: the mirror's REST client, the Files transfers and lease beats, and
the connections client. None keeps a copy.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from pathlib import Path

import httpx

from alkera_cli.cloud.box_auth import WorkerCredential
from alkera_cli.cloud.folder import ChatFolders
from alkera_cli.cloud.rest import CloudRestClient
from alkera_cli.cloud_sync.client import ChatConnectionsClient


def worker_rest_client(credential: WorkerCredential, api_url: str) -> CloudRestClient:
    """The mirror's REST client: every request, and the headers the box's
    notebooks sign theirs with, carry the bearer the worker holds now."""
    return CloudRestClient(api_url=api_url, token=credential.box_credential(), agent_id=None)


class WorkerFolders(ChatFolders):
    """The box's folder custody on the worker's credential: the transfers,
    the lease beats and every folder's fenced client sign each request with
    the bearer the worker holds at that moment."""

    @classmethod
    def for_worker(
        cls, *, api_url: str, credential: WorkerCredential, chats_root: Path
    ) -> WorkerFolders:
        folders = cls.for_box(api_url=api_url, auth=credential.auth(), chats_root=chats_root)
        assert isinstance(folders, WorkerFolders)
        return folders


class WorkerConnectionsClient(ChatConnectionsClient):
    """The worker's one connections client: the chat-scoped read, the schema
    cards' sync and every connector's credential lease all go through it.

    It is built from the credential holder, never from a token string, and
    reads the holder at each request: the credential lives minutes, the worker
    lives as long as its org is busy, and a client that kept the first bearer
    would be refused every lease once that bearer expired."""

    def __init__(
        self,
        *,
        api_url: str,
        credential: WorkerCredential,
        chats: Callable[[], Iterable[str]],
        workspaces: Callable[[], Iterable[str]] = tuple,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        super().__init__(
            api_url=api_url,
            token=credential.token,
            chats=chats,
            workspaces=workspaces,
            transport=transport,
            token_source=lambda: credential.token,
            auth=credential.auth(),
        )


__all__ = ["WorkerConnectionsClient", "WorkerCredential", "WorkerFolders", "worker_rest_client"]
