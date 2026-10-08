"""An org worker's credential is replaced every few minutes. The box's
notebooks keep speaking to the backend on the current one, a batch that
meets a refusal a later try may pass is sent again, and a request the box
cannot serve because the platform refused it is answered with that error
rather than dropped.

The backend's events route is a ``MockTransport`` that accepts only the
credential the worker holds now; the worker's credential and the REST
client the box signs with are the production ones.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
from alkera_cli.cloud.worker_credential import WorkerCredential, worker_rest_client
from alkera_cli.files.mount import fence_headers
from alkera_cli.notebooks.box import BoxFolder, BoxNotebooks
from alkera_cli.notebooks.box_compose import box_http_client
from alkera_cli.notebooks.box_events import HttpKernelEvents
from alkera_cli.notebooks.store_loro import Located
from alkera_core.schemas.realtime.machine import NotebookMachineRequest
from alkera_sdk.client import AlkeraAuthError

LEASE = "6b0e1c4f-0000-4000-8000-00000000aaaa"
DRIVE = "6b0e1c4f-0000-4000-8000-00000000dddd"


@dataclass
class Backend:
    """The events route: it takes a batch signed with a live credential."""

    live: set[str]
    statuses: list[int] = field(default_factory=list)
    batches: list[dict[str, Any]] = field(default_factory=list)
    #: Answers given before the real one, in order (a busy backend).
    first: list[int] = field(default_factory=list)

    def handler(self, request: httpx.Request) -> httpx.Response:
        bearer = request.headers.get("Authorization", "").removeprefix("Bearer ")
        if self.first:
            status = self.first.pop(0)
        elif bearer not in self.live:
            status = 401
        else:
            status = 200
            self.batches.append(httpx.Response(200, content=request.content).json())
        self.statuses.append(status)
        return httpx.Response(status, json={})

    def answers(self) -> list[dict[str, Any]]:
        return [e for b in self.batches for e in b["events"] if e.get("type") == "answer"]


@dataclass
class Folders:
    held: dict[str, BoxFolder] = field(default_factory=dict)

    def by_key(self, key: str) -> BoxFolder | None:
        return self.held.get(key)

    def by_lease(self, lease_node_id: str) -> BoxFolder | None:
        return next((f for f in self.held.values() if f.lease_node_id == lease_node_id), None)


async def _no_wait(_seconds: float) -> None:
    return None


def _box(
    backend: Backend,
    credential: WorkerCredential,
    folders: Folders | None = None,
    engine: Any = None,
) -> BoxNotebooks:
    rest = worker_rest_client(credential, "http://backend")
    http = box_http_client(
        lambda: rest.headers(), "http://backend", transport=httpx.MockTransport(backend.handler)
    )

    def engine_for(_tenancy: Any) -> Any:
        assert engine is not None, "no engine is made for a request the box cannot open"
        return engine

    async def locate(key: str, path: str) -> Located:
        raise AssertionError("not reached")

    return BoxNotebooks(
        folders=folders or Folders(),
        engine_for=engine_for,
        events=HttpKernelEvents(http, sleep=_no_wait),
        locate=locate,
    )


def _request(path: str = "a.alknb.py") -> NotebookMachineRequest:
    return NotebookMachineRequest(
        request_id=uuid.uuid4(),
        drive_id=uuid.UUID(DRIVE),
        item_id=uuid.uuid4(),
        op="envs",
        body={},
        lease_node_id=uuid.UUID(LEASE),
        path=path,
    )


async def test_an_answer_after_the_credential_was_replaced_is_signed_with_the_new_one() -> None:
    credential = WorkerCredential("alkm_org.first")
    backend = Backend(live={"alkm_org.first"})
    box = _box(backend, credential)
    credential.replace("alkm_org.second")
    backend.live = {"alkm_org.second"}  # the first one has expired
    request = _request()
    await box.dispatch(request)
    (answer,) = backend.answers()
    assert answer["request_id"] == str(request.request_id)
    assert backend.statuses == [200]


@pytest.mark.parametrize(
    ("first", "delivered"),
    [
        pytest.param([503], True, id="busy-once"),
        pytest.param([401, 502], True, id="refused-then-busy"),
        pytest.param([503, 503, 503], False, id="busy-every-try"),
        pytest.param([400], False, id="refused-for-good-is-not-sent-again"),
    ],
)
async def test_a_batch_is_sent_again_while_a_later_try_may_land(
    first: list[int], delivered: bool
) -> None:
    credential = WorkerCredential("alkm_org.first")
    backend = Backend(live={"alkm_org.first"}, first=list(first))
    await _box(backend, credential).dispatch(_request())
    assert bool(backend.answers()) is delivered
    expected = [*first, 200] if delivered else first
    assert backend.statuses == expected


async def test_a_request_the_platform_refused_the_box_is_answered_with_that_error(
    tmp_path: Path,
) -> None:
    """Reading where the notebook is filed failed (the box's credential was
    refused): the person is told, not left waiting on a run that never
    started."""

    def node_at(_relative: str) -> str | None:
        raise AlkeraAuthError(
            label="files/item-under",
            status=401,
            code="machine_worker_credential_expired",
            message="expired",
            trace_id=None,
        )

    folder = BoxFolder(
        key="chat-1",
        root=tmp_path,
        drive_id=DRIVE,
        lease_node_id=LEASE,
        step="",
        headers=dict(fence_headers(3, "instance-1")),
        node_at=node_at,
    )

    class Engine:
        """Opening a notebook resolves where it is filed first."""

        async def open(self, path: str) -> Any:
            node_at(path)
            raise AssertionError("not reached")

        async def close(self) -> None:
            return None

    credential = WorkerCredential("alkm_org.first")
    backend = Backend(live={"alkm_org.first"})
    box = _box(backend, credential, Folders({"chat-1": folder}), engine=Engine())
    await box.dispatch(_request())
    (answer,) = backend.answers()
    assert answer["error"]["code"] == "box_refused"
    assert "401" in answer["error"]["message"]
