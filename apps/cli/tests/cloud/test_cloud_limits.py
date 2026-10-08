"""The cloud daemon's tunable bounds: what each one reads, and that the call
site reads it rather than a figure frozen into a module.

Two halves. The first pins the PARSER per field — the default when the
environment says nothing usable, the value when it says something, and what
zero means for that particular bound (no bound at all for the dedupe memories
and the page walk; the default for a duration, where zero is a call that fails
before it is sent). The second drives the real call site with the environment
moved and watches the behaviour move with it, so a call site that went back to
a constant fails here rather than in production.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any

import httpx
import pytest
from alkera_cli.cloud.box_auth import BearerAuth
from alkera_cli.cloud.folder import ChatFolders
from alkera_cli.cloud.limits import (
    DEFAULT_CHAT_LIST_PAGES,
    DEFAULT_FILES_TIMEOUT_SECONDS,
    DEFAULT_FOLDER_BEAT_FLOOR_SECONDS,
    DEFAULT_FOLDER_BEAT_SECONDS,
    DEFAULT_FOLDER_BEAT_TIMEOUT_SECONDS,
    DEFAULT_HANDBACK_ATTEMPTS,
    DEFAULT_HEARTBEAT_TIMEOUT_SECONDS,
    DEFAULT_MIRROR_START_TIMEOUT_SECONDS,
    DEFAULT_OWN_EVENT_MEMORY,
    DEFAULT_PUBLISH_CHUNK_INTERVAL_SECONDS,
    DEFAULT_PUBLISH_LANDING_SECONDS,
    DEFAULT_RECONNECT_BASE_SECONDS,
    DEFAULT_RECONNECT_CAP_SECONDS,
    DEFAULT_RECONNECT_HEALTHY_PERIOD_SECONDS,
    DEFAULT_RELAY_MEMORY,
    DEFAULT_RERUN_LIMIT,
    DEFAULT_REST_TIMEOUT_SECONDS,
    ENV_CHAT_LIST_PAGES,
    ENV_FILES_TIMEOUT_SECONDS,
    ENV_FOLDER_BEAT_FLOOR_SECONDS,
    ENV_FOLDER_BEAT_SECONDS,
    ENV_FOLDER_BEAT_TIMEOUT_SECONDS,
    ENV_HANDBACK_ATTEMPTS,
    ENV_HEARTBEAT_TIMEOUT_SECONDS,
    ENV_MIRROR_START_TIMEOUT_SECONDS,
    ENV_OWN_EVENT_MEMORY,
    ENV_PUBLISH_CHUNK_INTERVAL_SECONDS,
    ENV_PUBLISH_LANDING_SECONDS,
    ENV_RECONNECT_BASE_SECONDS,
    ENV_RECONNECT_CAP_SECONDS,
    ENV_RECONNECT_HEALTHY_PERIOD_SECONDS,
    ENV_RELAY_MEMORY,
    ENV_RERUN_LIMIT,
    ENV_REST_TIMEOUT_SECONDS,
    chat_list_pages,
    files_timeout_seconds,
    folder_beat_floor_seconds,
    folder_beat_seconds,
    folder_beat_timeout_seconds,
    handback_attempts,
    heartbeat_timeout_seconds,
    mirror_start_timeout_seconds,
    own_event_memory,
    publish_chunk_interval_seconds,
    publish_landing_seconds,
    reconnect_bounds,
    relay_memory,
    rerun_limit,
    rest_timeout_seconds,
)
from alkera_cli.cloud.rest import SSE_READ_TIMEOUT_SECONDS, CloudRestClient
from alkera_cli.host.backoff import ReconnectBackoff

pytestmark = pytest.mark.anyio

INF = float("inf")

#: ``(accessor, env var, shipped default, what zero means)`` for every bound in
#: the block. Zero is the interesting case and it differs by kind: a dedupe
#: memory or a page walk can meaningfully have no bound, a duration cannot.
FIELDS: list[tuple[str, Callable[..., Any], str, float, float]] = [
    (
        "rest timeout",
        rest_timeout_seconds,
        ENV_REST_TIMEOUT_SECONDS,
        DEFAULT_REST_TIMEOUT_SECONDS,
        DEFAULT_REST_TIMEOUT_SECONDS,
    ),
    (
        "heartbeat timeout",
        heartbeat_timeout_seconds,
        ENV_HEARTBEAT_TIMEOUT_SECONDS,
        DEFAULT_HEARTBEAT_TIMEOUT_SECONDS,
        DEFAULT_HEARTBEAT_TIMEOUT_SECONDS,
    ),
    (
        "files timeout",
        files_timeout_seconds,
        ENV_FILES_TIMEOUT_SECONDS,
        DEFAULT_FILES_TIMEOUT_SECONDS,
        DEFAULT_FILES_TIMEOUT_SECONDS,
    ),
    (
        "folder beat",
        folder_beat_seconds,
        ENV_FOLDER_BEAT_SECONDS,
        DEFAULT_FOLDER_BEAT_SECONDS,
        DEFAULT_FOLDER_BEAT_SECONDS,
    ),
    (
        "folder beat floor",
        folder_beat_floor_seconds,
        ENV_FOLDER_BEAT_FLOOR_SECONDS,
        DEFAULT_FOLDER_BEAT_FLOOR_SECONDS,
        DEFAULT_FOLDER_BEAT_FLOOR_SECONDS,
    ),
    (
        "folder beat timeout",
        folder_beat_timeout_seconds,
        ENV_FOLDER_BEAT_TIMEOUT_SECONDS,
        DEFAULT_FOLDER_BEAT_TIMEOUT_SECONDS,
        DEFAULT_FOLDER_BEAT_TIMEOUT_SECONDS,
    ),
    (
        "mirror start timeout",
        mirror_start_timeout_seconds,
        ENV_MIRROR_START_TIMEOUT_SECONDS,
        DEFAULT_MIRROR_START_TIMEOUT_SECONDS,
        DEFAULT_MIRROR_START_TIMEOUT_SECONDS,
    ),
    (
        "publish chunk interval",
        publish_chunk_interval_seconds,
        ENV_PUBLISH_CHUNK_INTERVAL_SECONDS,
        DEFAULT_PUBLISH_CHUNK_INTERVAL_SECONDS,
        DEFAULT_PUBLISH_CHUNK_INTERVAL_SECONDS,
    ),
    (
        "publish landing wait",
        publish_landing_seconds,
        ENV_PUBLISH_LANDING_SECONDS,
        DEFAULT_PUBLISH_LANDING_SECONDS,
        DEFAULT_PUBLISH_LANDING_SECONDS,
    ),
    (
        "handback attempts",
        handback_attempts,
        ENV_HANDBACK_ATTEMPTS,
        DEFAULT_HANDBACK_ATTEMPTS,
        DEFAULT_HANDBACK_ATTEMPTS,
    ),
    ("rerun limit", rerun_limit, ENV_RERUN_LIMIT, DEFAULT_RERUN_LIMIT, DEFAULT_RERUN_LIMIT),
    ("relay memory", relay_memory, ENV_RELAY_MEMORY, DEFAULT_RELAY_MEMORY, INF),
    ("own event memory", own_event_memory, ENV_OWN_EVENT_MEMORY, DEFAULT_OWN_EVENT_MEMORY, INF),
    ("chat list pages", chat_list_pages, ENV_CHAT_LIST_PAGES, DEFAULT_CHAT_LIST_PAGES, INF),
]

_IDS = [name for name, *_ in FIELDS]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def _no_inherited_cloud_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """A developer's own environment must not decide what these tests see."""
    for _name, _accessor, env_name, _default, _zero in FIELDS:
        monkeypatch.delenv(env_name, raising=False)
    for env_name in (
        ENV_RECONNECT_BASE_SECONDS,
        ENV_RECONNECT_CAP_SECONDS,
        ENV_RECONNECT_HEALTHY_PERIOD_SECONDS,
    ):
        monkeypatch.delenv(env_name, raising=False)
    yield


# --------------------------------------------------------------------------- #
# what each field reads
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(("name", "accessor", "env_name", "default", "zero"), FIELDS, ids=_IDS)
@pytest.mark.parametrize("raw", [None, "", "   ", "not a number", "three"])
def test_an_unusable_environment_reads_as_the_shipped_default(
    name: str,
    accessor: Callable[..., Any],
    env_name: str,
    default: float,
    zero: float,
    raw: str | None,
) -> None:
    """A typo in a deployment's environment must never silently move a bound:
    unset, blank and unparseable all read as the figure the code shipped
    with."""
    env: Mapping[str, str] = {} if raw is None else {env_name: raw}
    assert accessor(env) == default, name


@pytest.mark.parametrize(("name", "accessor", "env_name", "default", "zero"), FIELDS, ids=_IDS)
def test_a_value_the_deployment_sets_is_what_the_field_reads(
    name: str,
    accessor: Callable[..., Any],
    env_name: str,
    default: float,
    zero: float,
) -> None:
    """The whole point of the block: a figure in the environment wins over the
    default, and it is not the default."""
    asked = default * 3 + 7
    wanted = int(asked) if isinstance(default, int) else asked
    assert accessor({env_name: str(wanted)}) == wanted, name
    assert wanted != default


@pytest.mark.parametrize(("name", "accessor", "env_name", "default", "zero"), FIELDS, ids=_IDS)
@pytest.mark.parametrize("raw", ["0", "-1", "-30"])
def test_zero_and_below_mean_what_the_field_can_mean_by_them(
    name: str,
    accessor: Callable[..., Any],
    env_name: str,
    default: float,
    zero: float,
    raw: str,
) -> None:
    """No bound at all where a caller could mean that — a dedupe set that
    remembers every id, a walk that reads to the end — and the default where it
    could not: a budget of zero is a call that fails before it is sent, and a
    cadence of zero is a spin, neither of which anybody asks for."""
    assert accessor({env_name: raw}) == zero, name


def test_a_count_that_is_not_whole_reads_as_the_default() -> None:
    """Half a page and a third of a remembered id are not figures anyone meant,
    and rounding one silently is how a deployment ends up with a bound it never
    asked for."""
    assert chat_list_pages({ENV_CHAT_LIST_PAGES: "2.5"}) == DEFAULT_CHAT_LIST_PAGES
    assert relay_memory({ENV_RELAY_MEMORY: "1.5"}) == DEFAULT_RELAY_MEMORY
    assert handback_attempts({ENV_HANDBACK_ATTEMPTS: "2.5"}) == DEFAULT_HANDBACK_ATTEMPTS


def test_the_reconnect_bounds_are_read_together_and_stay_valid_together() -> None:
    """The backoff refuses a cap below its base, so a deployment that raises the
    base and forgets the cap would take the box down at construction rather than
    at the first reconnect. The cap is widened to the base instead."""
    assert reconnect_bounds({}) == (
        DEFAULT_RECONNECT_BASE_SECONDS,
        DEFAULT_RECONNECT_CAP_SECONDS,
        DEFAULT_RECONNECT_HEALTHY_PERIOD_SECONDS,
    )
    base, cap, healthy = reconnect_bounds(
        {
            ENV_RECONNECT_BASE_SECONDS: "90",
            ENV_RECONNECT_CAP_SECONDS: "60",
            ENV_RECONNECT_HEALTHY_PERIOD_SECONDS: "45",
        }
    )
    assert (base, cap, healthy) == (90.0, 90.0, 45.0)
    ReconnectBackoff(base=base, cap=cap, healthy_period=healthy)


# --------------------------------------------------------------------------- #
# the call sites read the field, not a constant
# --------------------------------------------------------------------------- #


class _Recorder:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path.endswith("/heartbeat"):
            return httpx.Response(204)
        return httpx.Response(200, json={"id": "c1"})


def _client(recorder: _Recorder) -> CloudRestClient:
    return CloudRestClient(
        api_url="http://box.test",
        token="device-jwt",
        agent_id="machine:x",
        transport=httpx.MockTransport(recorder),
    )


async def test_every_rest_call_carries_the_deployments_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A box on a link where a transcript takes a minute has to be able to say
    so once, in its environment, rather than at every construction site."""
    monkeypatch.setenv(ENV_REST_TIMEOUT_SECONDS, "97")
    recorder = _Recorder()
    await _client(recorder).get_chat("c1")
    (call,) = recorder.requests
    assert call.extensions["timeout"]["read"] == 97.0


async def test_the_heartbeat_keeps_its_own_budget_and_the_deployment_sets_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The beat's budget is separate from every other call's because it has to
    stay under the beat interval — a hung beat must cost one missed beat, not
    push a healthy box past the window the cloud judges it by."""
    monkeypatch.setenv(ENV_REST_TIMEOUT_SECONDS, "97")
    monkeypatch.setenv(ENV_HEARTBEAT_TIMEOUT_SECONDS, "4")
    recorder = _Recorder()
    client = _client(recorder)
    await client.heartbeat_machine("m1")
    await client.get_chat("c1")
    beat, chat = recorder.requests
    assert beat.extensions["timeout"]["read"] == 4.0
    assert chat.extensions["timeout"]["read"] == 97.0


async def test_the_event_stream_reaches_the_route_on_the_same_budget_and_then_waits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The stream is a long poll: the connect half is the deployment's REST
    budget, and the read half is bounded well past the server's keepalive
    cadence — never unbounded, or a stream nothing feeds is listened to for
    ever."""
    monkeypatch.setenv(ENV_REST_TIMEOUT_SECONDS, "12")
    recorder = _Recorder()

    def _stream(request: httpx.Request) -> httpx.Response:
        recorder.requests.append(request)
        return httpx.Response(200, text="", headers={"content-type": "text/event-stream"})

    client = CloudRestClient(
        api_url="http://box.test",
        token="device-jwt",
        agent_id="machine:x",
        transport=httpx.MockTransport(_stream),
    )
    async for _frame in client.events():  # pragma: no cover - the body is empty
        break
    (call,) = recorder.requests
    assert call.extensions["timeout"]["connect"] == 12.0
    assert call.extensions["timeout"]["read"] == SSE_READ_TIMEOUT_SECONDS
    assert SSE_READ_TIMEOUT_SECONDS >= 4 * 15, "at least four of the server's keepalives"


def test_a_boxs_files_client_is_built_on_the_deployments_budget(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A chat folder is pulled and pushed whole, so the box moving a large one
    across a slow link is exactly the deployment that has to raise this."""
    monkeypatch.setenv(ENV_FILES_TIMEOUT_SECONDS, "480")
    folders = ChatFolders.for_box(
        api_url="http://box.test", auth=BearerAuth(lambda: "t"), chats_root=tmp_path / "chats"
    )
    assert folders._http is not None
    assert folders._http.timeout.read == 480.0


def test_the_reconnect_wait_is_the_deployments_and_not_a_fixed_two_seconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fleet behind a link that takes a minute to come back should not be
    made to re-ask at the shipped floor, and a box on a fast one should not be
    made to wait a shipped minute."""
    monkeypatch.setenv(ENV_RECONNECT_BASE_SECONDS, "5")
    monkeypatch.setenv(ENV_RECONNECT_CAP_SECONDS, "11")
    backoff = ReconnectBackoff(rng=lambda: 0.5)
    assert backoff.next_delay() == 5.0
    assert backoff.next_delay() == 10.0
    assert backoff.next_delay() == 11.0, "the deployment's cap, not the shipped 60"


def test_the_healthy_period_that_steps_the_wait_down_is_the_deployments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A box on a flappy link stays slow for as long as the deployment says,
    not for a fixed five minutes."""
    monkeypatch.setenv(ENV_RECONNECT_HEALTHY_PERIOD_SECONDS, "30")
    now = [0.0]
    backoff = ReconnectBackoff(rng=lambda: 0.5, clock=lambda: now[0])
    backoff.next_delay()
    backoff.next_delay()
    backoff.connected()
    now[0] = 29.0
    assert backoff.decay() == 2, "not one healthy period yet"
    now[0] = 61.0
    assert backoff.decay() == 0, "two of the deployment's periods, two steps down"


# --------------------------------------------------------------------------- #
# the folded figures have one spelling
# --------------------------------------------------------------------------- #

CLOUD = Path(__file__).resolve().parents[2] / "alkera_cli" / "cloud"

#: ``(module, accessor, the literal it replaced)``. The mirror's three — the
#: dedupe memories and the re-run page — are read inside loops and branches a
#: unit test cannot reach without a live turn, so what is pinned here is the
#: thing that would actually regress: the call site going back to a literal.
SPELLINGS = [
    ("mirror.py", "relay_memory(", "512"),
    ("mirror.py", "own_event_memory(", "1024"),
    ("mirror.py", "rerun_limit(", "1000"),
    ("mirror.py", "mirror_start_timeout_seconds(", "30.0"),
    ("mirror.py", "publish_chunk_interval_seconds(", "0.05"),
    ("folder.py", "handback_attempts(", "3"),
    ("box_files.py", "files_timeout_seconds(", "120.0"),
    ("box_files.py", "folder_beat_timeout_seconds(", "10.0"),
    ("service.py", "chat_list_pages(", "range(100)"),
    ("service.py", "folder_beat_seconds(", "15.0"),
    ("service.py", "folder_beat_floor_seconds(", "1.0"),
    ("service.py", "folder_beat_timeout_seconds(", "10.0"),
    ("rest.py", "rest_timeout_seconds(", "30.0"),
    ("rest.py", "heartbeat_timeout_seconds(", "10.0"),
]


@pytest.mark.parametrize(
    ("module", "accessor", "literal"),
    SPELLINGS,
    ids=[f"{module}:{accessor.rstrip('(')}" for module, accessor, _literal in SPELLINGS],
)
def test_the_call_site_asks_the_settings_block_rather_than_spelling_the_figure(
    module: str, accessor: str, literal: str
) -> None:
    """A fold is only worth having while the call site still reads it. A
    module that went back to its own literal is the regression this catches —
    the env var would still parse, still be documented, and change nothing."""
    source = (CLOUD / module).read_text(encoding="utf-8")
    assert accessor in source, f"{module} no longer reads {accessor.rstrip('(')}"
