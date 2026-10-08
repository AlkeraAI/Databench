"""A box whose session the server refuses is signed out, not in an outage.

A box whose token had expired beat every 15-30 s for an hour, each beat a 401,
logging "the server has stopped answering" once and nothing after — while it
went on holding its chats, running turns it could no longer publish, and
leasing folders its refused beats could not keep. The web said the workspace
was unreachable and nothing on the box said why.

What the machine loop must do instead, pinned here through the injected sleep,
clock and rng seams (no socket, no wall clock):

* a refused session (401 of any code, 403 ``session_revoked``) is said ONCE at
  ERROR, naming the token file and the command that fixes it;
* the beats back off exponentially to a five-minute ceiling, not the jittered
  heartbeat interval an outage keeps;
* the box takes no new chat, and after the refusal repeats it puts the chats it
  holds to sleep;
* a token replaced on disk is picked up by the next beat without a restart, and
  the box takes chats again;
* a real outage, and a 403 that is not about the session, stay on the outage
  path.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from _mirror_service import SERVICE_LOGGER, Clock, FakeMirror, NoSocket, build_service
from alkera_cli.cloud.rest import BoxCredential, CloudApiError, CloudRestClient
from alkera_cli.cloud.service import CloudMirrorService

HEARTBEAT = 15.0
#: Five minutes: the longest a signed-out box waits between beats.
SIGNED_OUT_CAP = 300.0
MACHINE_ID = "807bf89a-464c-4867-8b08-6e020a9bd8a3"


class _LoopStoppedError(Exception):
    """Ends the loop from inside its own sleep seam."""


def _refusal(status: int, code: str) -> CloudApiError:
    return CloudApiError(
        status,
        {"detail": {"code": code, "message": "no"}},
        method="POST",
        path=f"/api/v1/machines/{MACHINE_ID}/heartbeat",
    )


class _Backend:
    """The box's backend over a mock transport: every route answers 401
    ``token_expired`` unless the request carries ``good`` as its bearer, or
    answers what ``refuse`` says for the heartbeat."""

    def __init__(self, *, refuse: CloudApiError | None = None) -> None:
        self.refuse = refuse
        self.beats: list[str] = []
        self.listings = 0
        self.chats: list[dict[str, Any]] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        bearer = request.headers.get("authorization", "").removeprefix("Bearer ")
        path = request.url.path
        if path.endswith("/heartbeat"):
            self.beats.append(bearer)
            if self.refuse is not None:
                return httpx.Response(self.refuse.status, json=self.refuse.body)
        if bearer != "good":
            return httpx.Response(
                401, json={"detail": {"code": "token_expired", "message": "expired"}}
            )
        if path == "/api/v1/machines/register":
            return httpx.Response(200, json={"id": MACHINE_ID})
        if path == "/api/v1/chats":
            self.listings += 1
            return httpx.Response(200, json={"items": list(self.chats), "next_cursor": None})
        return httpx.Response(200, json={})


def _rig(
    tmp_path: Path,
    backend: _Backend,
    *,
    token_file: list[str],
    stop_after: int,
    on_sleep: Callable[[int], None] | None = None,
    renew_session: Callable[[], Awaitable[bool]] | None = None,
) -> tuple[CloudMirrorService, list[float], dict[str, FakeMirror]]:
    """A service whose REST client reads its bearer from ``token_file[0]`` —
    the credential file, as the box reads it — and whose sleep records each
    wait and stops the loop after ``stop_after`` of them."""
    rest = CloudRestClient(
        api_url="http://127.0.0.1:1",
        token=BoxCredential(token_file[0], read=lambda: token_file[0]),
        agent_id=f"machine:{MACHINE_ID}",
        transport=httpx.MockTransport(backend.handle),
    )
    waits: list[float] = []

    async def sleep(seconds: float) -> None:
        waits.append(seconds)
        if on_sleep is not None:
            on_sleep(len(waits))
        if len(waits) >= stop_after:
            raise _LoopStoppedError

    service, built = build_service(
        tmp_path, clock=Clock(), rest=rest, sleep=sleep, heartbeat_interval=HEARTBEAT
    )
    service._rng = lambda: 0.5  # the backoff's jitter factor is then exactly 1.0
    service._renew_session = renew_session
    service._socket = NoSocket(rest)
    service._machine_id = MACHINE_ID
    return service, waits, built


async def _run(service: CloudMirrorService) -> None:
    with pytest.raises(_LoopStoppedError):
        await service._machine_loop()
    await service.settle_background()


def _serve(service: CloudMirrorService, built: dict[str, FakeMirror], chat_id: str) -> FakeMirror:
    mirror = FakeMirror(chat_id)
    built[chat_id] = mirror
    service._mirrors[chat_id] = mirror  # type: ignore[assignment]
    return mirror


@pytest.mark.parametrize(
    "refusal",
    [
        pytest.param(_refusal(401, "token_expired"), id="401-token-expired"),
        pytest.param(_refusal(401, "invalid_token"), id="401-invalid-token"),
        # What a bearer gets once its browser session's refresh family was
        # ended, by a sign-out or by a refresh token coming back a second time.
        pytest.param(_refusal(401, "session_revoked"), id="401-session-revoked"),
        pytest.param(_refusal(401, "refresh_token_reused"), id="401-refresh-token-reused"),
        pytest.param(_refusal(403, "session_revoked"), id="403-session-revoked"),
    ],
)
async def test_a_refused_session_is_said_once_and_backs_off_to_five_minutes(
    tmp_path: Path, refusal: CloudApiError, caplog: pytest.LogCaptureFixture
) -> None:
    backend = _Backend(refuse=refusal)
    service, waits, _ = _rig(tmp_path, backend, token_file=["good"], stop_after=8)
    with caplog.at_level(logging.DEBUG, logger=SERVICE_LOGGER):
        await _run(service)

    assert waits == [15.0, 30.0, 60.0, 120.0, 240.0, 300.0, 300.0, 300.0]
    assert max(waits) == SIGNED_OUT_CAP
    assert service.signed_out == f"{refusal.status} {refusal.code}"

    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1, errors
    assert "auth.yml" in errors[0] and "alkera login" in errors[0]
    assert refusal.code in errors[0]
    assert not [r for r in caplog.records if "stopped answering" in r.getMessage()], (
        "a refused session is not reported as the server going away"
    )


@pytest.mark.parametrize(
    "refusal",
    [
        pytest.param(_refusal(403, "forbidden"), id="403-not-about-the-session"),
        pytest.param(_refusal(503, ""), id="503-outage"),
    ],
)
async def test_an_outage_or_an_unrelated_403_keeps_the_outage_path(
    tmp_path: Path, refusal: CloudApiError, caplog: pytest.LogCaptureFixture
) -> None:
    backend = _Backend(refuse=refusal)
    service, waits, _ = _rig(tmp_path, backend, token_file=["good"], stop_after=6)
    with caplog.at_level(logging.DEBUG, logger=SERVICE_LOGGER):
        await _run(service)

    assert all(HEARTBEAT <= wait < 2 * HEARTBEAT for wait in waits), waits
    assert service.signed_out is None
    assert not [r for r in caplog.records if r.levelno == logging.ERROR]


async def test_the_box_takes_no_chat_and_sleeps_the_ones_it_holds(tmp_path: Path) -> None:
    backend = _Backend(refuse=_refusal(401, "token_expired"))
    service, waits, built = _rig(tmp_path, backend, token_file=["good"], stop_after=1)
    held = _serve(service, built, "chat-held")

    await _run(service)
    # One refusal is not yet a verdict: the chat keeps its session.
    assert "chat-held" in service.mirrors and not held.stopped
    # A chat bound here meanwhile is not taken while the session is refused.
    new_chat = {"id": "chat-new", "machine_id": MACHINE_ID, "last_seq": 1}
    assert service._serves(new_chat) is False
    assert service._serves({"id": "chat-held", "machine_id": MACHINE_ID}) is True

    service._sleep = _stop_after_one(waits)
    await _run(service)
    assert held.stopped, "the chat the box cannot publish is put to sleep"
    assert service.mirrors == {}


def _stop_after_one(waits: list[float]) -> Callable[[float], Awaitable[None]]:
    async def sleep(seconds: float) -> None:
        waits.append(seconds)
        raise _LoopStoppedError

    return sleep


async def test_a_token_replaced_on_disk_is_picked_up_and_the_box_takes_chats_again(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """No refusal is scripted: the backend refuses the stale bearer, and the
    operator writes a good one into the file after the second refused beat."""
    backend = _Backend()
    backend.chats = [{"id": "chat-a", "machine_id": MACHINE_ID, "last_seq": 1}]
    token_file = ["stale"]

    def operator(slept: int) -> None:
        if slept == 2:
            token_file[0] = "good"

    service, waits, _ = _rig(
        tmp_path, backend, token_file=token_file, stop_after=4, on_sleep=operator
    )
    with caplog.at_level(logging.INFO, logger=SERVICE_LOGGER):
        await _run(service)

    assert backend.beats[:2] == ["stale", "stale"]
    assert "good" in backend.beats, "the beat after the file changed sends the new token"
    assert waits[:2] == [15.0, 30.0]
    assert waits[2:] == [HEARTBEAT, HEARTBEAT], "a landed beat returns to the plain cadence"
    assert service.signed_out is None
    assert any("accepts this box's session again" in r.getMessage() for r in caplog.records)
    assert backend.listings == 1, "the beat that landed re-read the chats at once"
    assert service._serves(backend.chats[0]) is True


async def test_a_renewer_that_puts_a_new_session_in_place_beats_again_at_once(
    tmp_path: Path,
) -> None:
    backend = _Backend()
    token_file = ["stale"]
    renewals: list[str] = []

    async def renew() -> bool:
        renewals.append(token_file[0])
        token_file[0] = "good"
        return True

    service, waits, _ = _rig(
        tmp_path, backend, token_file=token_file, stop_after=2, renew_session=renew
    )
    await _run(service)

    assert renewals == ["stale"]
    assert waits == [0.0, HEARTBEAT], "the renewed session is tried at once, then lands"
    assert service.signed_out is None
