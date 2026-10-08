"""A worker refused for an expired credential asks the supervisor for a fresh
one once and sends the request once more on it; it never loops."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
from alkera_cli.cloud.box_auth import WorkerCredential
from alkera_cli.cloud.rest import CloudApiError, CloudRestClient
from alkera_cli.cloud.worker_credential import WorkerFolders
from alkera_core.machine_refusals import (
    MACHINE_CREDENTIAL_REFUSED,
    MACHINE_WORKER_CREDENTIAL_EXPIRED,
)

API = "https://api.example"
FIRST = "alkm_org.first"
SECOND = "alkm_org.second"
THIRD = "alkm_org.third"


class _Backend:
    """Answers 200 for a live bearer and 401 with ``code`` for the rest,
    recording every bearer it was sent."""

    def __init__(self, live: set[str], code: str = MACHINE_WORKER_CREDENTIAL_EXPIRED) -> None:
        self.live = live
        self.code = code
        self.sent: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        bearer = request.headers["Authorization"].removeprefix("Bearer ")
        self.sent.append(bearer)
        if bearer in self.live:
            return httpx.Response(200, json={"items": []})
        return httpx.Response(401, json={"detail": {"code": self.code, "message": "refused"}})


class _Supervisor:
    """Answers each ask by replacing the credential with the next of
    ``hands`` (or not at all once they run out)."""

    def __init__(self, credential: WorkerCredential, hands: list[str]) -> None:
        self.credential = credential
        self.hands = hands
        self.asked = 0

    def ask(self) -> None:
        self.asked += 1
        if self.hands:
            token = self.hands.pop(0)
            asyncio.get_running_loop().call_soon(self.credential.replace, token)


def _rest(credential: WorkerCredential, backend: _Backend) -> CloudRestClient:
    return CloudRestClient(
        api_url=API,
        token=credential.box_credential(),
        agent_id=None,
        transport=httpx.MockTransport(backend),
    )


async def test_an_expired_credential_is_renewed_once_and_the_request_sent_once_more() -> None:
    credential = WorkerCredential(FIRST)
    supervisor = _Supervisor(credential, [SECOND])
    credential.bind(supervisor.ask)
    backend = _Backend(live={SECOND})
    await _rest(credential, backend).list_chats()
    assert (backend.sent, supervisor.asked, credential.token) == ([FIRST, SECOND], 1, SECOND)


async def test_requests_refused_together_ask_once() -> None:
    credential = WorkerCredential(FIRST)
    supervisor = _Supervisor(credential, [SECOND, THIRD])
    credential.bind(supervisor.ask)
    backend = _Backend(live={SECOND})
    rest = _rest(credential, backend)
    await asyncio.gather(*(rest.list_chats() for _ in range(5)))
    assert supervisor.asked == 1
    assert backend.sent.count(FIRST) == 5 and backend.sent.count(SECOND) == 5


async def test_a_retry_refused_again_stands_and_is_not_sent_a_third_time() -> None:
    credential = WorkerCredential(FIRST)
    supervisor = _Supervisor(credential, [THIRD, SECOND])
    credential.bind(supervisor.ask)
    backend = _Backend(live={SECOND})
    with pytest.raises(CloudApiError) as refused:
        await _rest(credential, backend).list_chats()
    assert refused.value.status == 401
    assert (backend.sent, supervisor.asked) == ([FIRST, THIRD], 1)


async def test_a_supervisor_that_does_not_answer_leaves_the_refusal_after_its_wait() -> None:
    credential = WorkerCredential(FIRST, renew_wait=0.05)
    supervisor = _Supervisor(credential, [])
    credential.bind(supervisor.ask)
    backend = _Backend(live={SECOND})
    with pytest.raises(CloudApiError):
        await _rest(credential, backend).list_chats()
    assert (backend.sent, supervisor.asked) == ([FIRST], 1)


async def test_a_refusal_that_is_not_expiry_asks_for_nothing() -> None:
    credential = WorkerCredential(FIRST)
    supervisor = _Supervisor(credential, [SECOND])
    credential.bind(supervisor.ask)
    backend = _Backend(live=set(), code=MACHINE_CREDENTIAL_REFUSED)
    with pytest.raises(CloudApiError):
        await _rest(credential, backend).list_chats()
    assert (backend.sent, supervisor.asked) == ([FIRST], 0)


async def test_a_files_beat_off_the_loop_is_renewed_the_same_way(tmp_path: Path) -> None:
    """The Files clients run in threads: the renewal is served on the
    worker's loop and the beat sent once more from the thread."""
    credential = WorkerCredential(FIRST)
    supervisor = _Supervisor(credential, [SECOND])
    credential.bind(supervisor.ask)
    backend = _Backend(live={SECOND})
    folders = WorkerFolders.for_worker(api_url=API, credential=credential, chats_root=tmp_path)
    beats = folders._beats()
    beats._transport = httpx.MockTransport(backend)
    answer = await asyncio.to_thread(beats.post, "/api/v1/files/leases/heartbeat")
    assert answer.status_code == 200
    assert (backend.sent, supervisor.asked) == ([FIRST, SECOND], 1)
