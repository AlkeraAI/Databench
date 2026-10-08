"""The REST client: every call carries the device JWT AND the agent assertion,
errors carry the route's code, the socket URL derives from the API URL, and
the event stream yields frames — all against ``httpx.MockTransport``."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from alkera_cli.cloud.limits import (
    DEFAULT_HEARTBEAT_TIMEOUT_SECONDS,
    DEFAULT_REST_TIMEOUT_SECONDS,
)
from alkera_cli.cloud.rest import STREAM_OPENED, CloudApiError, CloudRestClient, ws_url_for
from alkera_cli.cloud.service import DEFAULT_HEARTBEAT_INTERVAL_SECONDS
from alkera_core.authz import AgentHeaderError
from alkera_core.authz.headers import ACTOR_HEADER, AGENT_ID_HEADER, parse_agent_assertion
from alkera_core.config import settings
from alkera_core.schemas.compute import MachineRegisterRequest

TOKEN = "device-jwt-xyz"


class _Recorder:
    def __init__(self, reply: Any = None, status: int = 200) -> None:
        self.requests: list[httpx.Request] = []
        self.reply = reply
        self.status = status

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if callable(self.reply):
            return self.reply(request)  # type: ignore[no-any-return]
        return httpx.Response(self.status, json=self.reply if self.reply is not None else {})


def _client(recorder: _Recorder, *, agent_id: str = "chat-1") -> CloudRestClient:
    return CloudRestClient(
        api_url="http://api.test/",
        token=TOKEN,
        agent_id=agent_id,
        transport=httpx.MockTransport(recorder),
    )


def _assert_agent_call(request: httpx.Request, *, agent_id: str) -> None:
    assert request.headers["Authorization"] == f"Bearer {TOKEN}"
    assertion = parse_agent_assertion(dict(request.headers))
    assert assertion is not None and assertion.session_id == agent_id
    assert TOKEN not in str(request.url) and "ticket=" not in str(request.url)


@pytest.mark.parametrize(
    ("call", "method", "path"),
    [
        pytest.param(lambda c: c.mint_ticket(), "POST", "/api/v1/ws/tickets", id="ticket"),
        pytest.param(lambda c: c.get_chat("c1"), "GET", "/api/v1/chats/c1", id="get-chat"),
        pytest.param(lambda c: c.list_chats(), "GET", "/api/v1/chats", id="list-chats"),
        pytest.param(lambda c: c.get_object("o1"), "GET", "/api/v1/objects/o1", id="get-object"),
        pytest.param(
            lambda c: c.upload_payload("o1", envelope={"kind": "rows"}, receipt={"sql": "x"}),
            "POST",
            "/api/v1/objects/o1/payload",
            id="upload-payload",
        ),
        pytest.param(
            lambda c: c.register_machine(
                name="box", provider="runpod", provider_pod_id="pod-1", machine_type_code="cpu3c"
            ),
            "POST",
            "/api/v1/machines/register",
            id="register",
        ),
        pytest.param(
            lambda c: c.heartbeat_machine("m1"),
            "POST",
            "/api/v1/machines/m1/heartbeat",
            id="heartbeat",
        ),
    ],
)
async def test_every_call_carries_the_bearer_and_the_agent_assertion(
    call: Any, method: str, path: str
) -> None:
    def reply(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/ws/tickets"):
            return httpx.Response(200, json={"ticket": "t", "expires_in": 30})
        return httpx.Response(200, json={"id": "m1", "items": []})

    recorder = _Recorder(reply=reply)
    await call(_client(recorder))
    [request] = recorder.requests
    assert request.method == method
    assert request.url.path == path
    _assert_agent_call(request, agent_id="chat-1")


async def test_the_body_and_query_of_each_call_are_the_contract_shapes() -> None:
    recorder = _Recorder(reply={"items": [], "next_cursor": None, "id": "m1"})
    client = _client(recorder)
    await client.list_chats(cursor="abc", limit=10)
    await client.upload_payload("o1", envelope={"kind": "rows"}, receipt={"sql": "select 1"})
    await client.register_machine(
        name="box-7", provider="runpod", provider_pod_id="pod-7", machine_type_code="cpu3c"
    )
    listed, uploaded, registered = recorder.requests
    assert dict(listed.url.params) == {"limit": "10", "cursor": "abc"}
    assert json.loads(uploaded.content) == {
        "envelope": {"kind": "rows"},
        "receipt": {"sql": "select 1"},
    }
    # The register body IS the route's request model: every field it requires,
    # under the names it declares (the seam test drives the real route with it).
    body = json.loads(registered.content)
    assert body == {
        "provider": "runpod",
        "provider_pod_id": "pod-7",
        "name": "box-7",
        "machine_type_code": "cpu3c",
        "daemon_instance_id": None,
    }
    assert MachineRegisterRequest.model_validate(body).provider_pod_id == "pod-7"


async def test_a_heartbeat_answered_with_no_content_is_not_an_error() -> None:
    """The heartbeat route answers ``204``; reading that as a failure would
    log every heartbeat as the box being unreachable and never mark it ready."""
    recorder = _Recorder(reply=lambda _request: httpx.Response(204))
    assert await _client(recorder).heartbeat_machine("m1") == {}
    [request] = recorder.requests
    assert request.url.path == "/api/v1/machines/m1/heartbeat"


async def test_a_heartbeat_gives_up_before_it_delays_the_next_one() -> None:
    """The beat loop is serial — beat, await the answer, sleep the interval — so
    a request that hangs pushes the following beats apart. On the client's
    default 30 s budget a tunnel that accepts the connection and never answers
    spaces them past the window the cloud judges this box by, and a perfectly
    healthy machine reports itself unreachable. So the heartbeat's own budget
    is shorter than the interval, and every other call keeps the default."""
    recorder = _Recorder(
        reply=lambda request: (
            httpx.Response(204)
            if request.url.path.endswith("/heartbeat")
            else httpx.Response(200, json={"id": "c1"})
        )
    )
    client = _client(recorder)
    await client.heartbeat_machine("m1")
    await client.get_chat("c1")
    beat, chat = recorder.requests
    # Every beat says what this build can do: the server tells a reader that a
    # model switch runs on the next turn only for a box that says so, and
    # placement keeps a shared workspace's chats on a box that says workspaces.
    assert json.loads(beat.content)["capabilities"] == [
        "folder_flush_v1",
        "model_switch_v1",
        "workspaces",
    ]
    assert beat.extensions["timeout"]["read"] == DEFAULT_HEARTBEAT_TIMEOUT_SECONDS
    assert chat.extensions["timeout"]["read"] == DEFAULT_REST_TIMEOUT_SECONDS
    assert DEFAULT_HEARTBEAT_TIMEOUT_SECONDS < DEFAULT_HEARTBEAT_INTERVAL_SECONDS, (
        "a beat that outlasts the interval delays the next one"
    )
    assert DEFAULT_HEARTBEAT_INTERVAL_SECONDS + DEFAULT_HEARTBEAT_TIMEOUT_SECONDS < (
        settings.compute_heartbeat_ready_seconds
    ), "one hung beat must not be enough to make the cloud give up on the box"


async def test_a_ticket_is_read_from_the_response_body() -> None:
    client = _client(_Recorder(reply={"ticket": "the-ticket", "expires_in": 30}))
    assert await client.mint_ticket() == "the-ticket"


async def test_an_error_status_raises_with_the_routes_code() -> None:
    recorder = _Recorder(reply={"detail": {"code": "chat_not_found", "message": "no"}}, status=404)
    with pytest.raises(CloudApiError) as info:
        await _client(recorder).get_chat("nope")
    assert info.value.status == 404
    assert info.value.code == "chat_not_found"
    assert info.value.message == "no"
    assert info.value.unauthorized is False


async def test_a_401_is_unauthorized_and_a_string_detail_is_the_message() -> None:
    recorder = _Recorder(reply={"detail": "expired"}, status=401)
    with pytest.raises(CloudApiError) as info:
        await _client(recorder).mint_ticket()
    assert info.value.unauthorized is True
    assert info.value.message == "expired"
    assert info.value.code == ""


async def test_for_agent_speaks_as_another_agent_with_the_same_credential() -> None:
    recorder = _Recorder(reply={"id": "c"})
    client = _client(recorder, agent_id="machine:box")
    await client.for_agent("chat-9").get_chat("c")
    _assert_agent_call(recorder.requests[0], agent_id="chat-9")
    assert client.agent_id == "machine:box"


def test_a_bad_agent_id_fails_at_construction_not_per_call() -> None:
    with pytest.raises(AgentHeaderError):
        CloudRestClient(api_url="http://api.test", token=TOKEN, agent_id="-bad id")


@pytest.mark.parametrize(
    ("api_url", "ws"),
    [
        pytest.param("http://127.0.0.1:8000", "ws://127.0.0.1:8000/api/v1/ws", id="http"),
        pytest.param(
            "https://api.example.com/", "wss://api.example.com/api/v1/ws", id="https-trailing"
        ),
    ],
)
def test_the_socket_url_derives_from_the_api_url(api_url: str, ws: str) -> None:
    assert ws_url_for(api_url) == ws


@pytest.mark.parametrize("api_url", ["ftp://x", "api.test", ""])
def test_a_non_http_api_url_is_refused(api_url: str) -> None:
    with pytest.raises(ValueError):
        CloudRestClient(api_url=api_url, token=TOKEN, agent_id="a")


async def test_the_event_stream_yields_frames_with_the_agent_assertion() -> None:
    body = (
        "retry: 2000\n: connected\n\n"
        'id: 7\nevent: chat.updated\ndata: {"type": "chat.updated", "entity": "chat", '
        '"entity_id": "c1", "version": 1, "org_id": "o"}\n\n'
        'event: reset\ndata: {"reason": "overflow"}\n\n'
    )

    def reply(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/events"
        assert dict(request.url.params) == {"after": "3"}
        return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

    recorder = _Recorder(reply=reply)
    frames = [frame async for frame in _client(recorder).events(after=3)]
    _assert_agent_call(recorder.requests[0], agent_id="chat-1")
    # The marker that the stream is up comes first, before any frame the
    # server sent; the comment-only blocks are not frames.
    assert [f["type"] for f in frames] == [STREAM_OPENED, "chat.updated", "reset"]
    frames = frames[1:]
    assert frames[0]["id"] == "7"
    assert frames[0]["data"]["entity_id"] == "c1"
    assert frames[1]["data"] == {"reason": "overflow"}


async def test_a_refused_event_stream_raises() -> None:
    recorder = _Recorder(reply={"detail": "nope"}, status=401)
    with pytest.raises(CloudApiError) as info:
        async for _ in _client(recorder).events():
            pass
    assert info.value.status == 401


def test_the_header_names_come_from_the_one_module_that_defines_them() -> None:
    client = CloudRestClient(api_url="http://api.test", token=TOKEN, agent_id="chat-2")
    headers = client.headers()
    assert headers[ACTOR_HEADER] == "agent"
    assert headers[AGENT_ID_HEADER] == "chat-2"
    assert headers["Authorization"] == f"Bearer {TOKEN}"


@pytest.mark.parametrize(
    ("status", "code"),
    [
        pytest.param(402, "insufficient_credit", id="402-insufficient-credit"),
        pytest.param(429, "no_compute_grant", id="429-no-compute-grant"),
        pytest.param(429, "compute_limit_reached", id="429-compute-limit-reached"),
        pytest.param(429, "compute_user_limit_reached", id="429-compute-user-limit-reached"),
    ],
)
async def test_a_refusal_in_the_apps_error_envelope_carries_its_code(
    status: int, code: str
) -> None:
    """The backend's canonical refusal is ``{"error": {"code", "message"}}`` —
    the shape a compute refusal arrives in — and the client reads the code from
    it rather than from FastAPI's bare ``detail``."""
    recorder = _Recorder(reply={"error": {"code": code, "message": "refused"}}, status=status)
    with pytest.raises(CloudApiError) as info:
        await _client(recorder).get_chat("c1")
    assert info.value.status == status
    assert info.value.code == code
    assert info.value.message == "refused"


async def test_a_publisher_state_report_cut_by_a_busy_backend_is_sent_again() -> None:
    """A lost ``publishing`` report leaves the wake standing on the row, and a
    standing wake re-took the chat after every sleep. Setting a state twice is
    setting it once, so a report the backend cut (a 503 under lock pressure) is
    sent again rather than dropped."""
    seen: list[str] = []

    def backend(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content)["state"])
        if len(seen) == 1:
            return httpx.Response(503, json={"detail": {"code": "db_lock_timeout"}})
        return httpx.Response(200, json={"id": "chat-1"})

    client = CloudRestClient(
        api_url="http://api.test",
        token="device-jwt",
        agent_id="chat-1",
        transport=httpx.MockTransport(backend),
    )
    assert await client.report_publisher_state("chat-1", state="publishing") == {"id": "chat-1"}
    assert seen == ["publishing", "publishing"]
