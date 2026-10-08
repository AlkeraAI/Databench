"""The box's log shipper: what leaves the box, where it goes, and that a log
or network outage never reaches the supervisor.

The allowlist itself is pinned in ``packages/api-core/tests/compute/
test_box_logs.py``; these drive it through the real logger the supervisor's
events go to, into real sinks (a local HTTP server standing in for the
backend, a recording client standing in for CloudWatch Logs).
"""

from __future__ import annotations

import ast
import json
import logging
import subprocess
import sys
import threading
import time
from collections.abc import Iterator, Sequence
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, ClassVar

import pytest
from alkera_cli.supervisor import box_log_shipping as log_shipping
from alkera_cli.supervisor import org_events
from alkera_cli.supervisor.box_log_shipping import (
    BackendSink,
    CloudWatchSink,
    Record,
    Ring,
    Shipper,
    choose_sink,
    install,
)
from alkera_core.compute import box_logs
from alkera_core.compute.box_logs import (
    EVENT_FIELDS,
    INGEST_PATH,
    MAX_BATCH_BYTES,
    SHIPPER_DROPPED,
)
from alkera_core.schemas.box_logs import BoxLogBatch

ORG = "8ef423bc-531e-4d2a-baa5-8bfb6e9e8863"
CHAT = "be0a3393-082f-4acf-918d-6eac00904c9b"
MACHINE = "12345678-1234-5678-1234-567812345678"
CREDENTIAL = "alkm_3f9a1c2b7d8e4f60a1b2c3d4e5f6a7b8"
SECRETS = (
    CREDENTIAL,
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1c2VyIn0.c2lnbmF0dXJlc2lnbmF0dXJl",
    "sk-proj-AbCdEfGhIjKlMnOpQrStUv",
    "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
    "summarise the board deck for the acquisition",
)
CLI_ROOT = Path(__file__).resolve().parents[2] / "alkera_cli"


def _rec(n: int) -> Record:
    return {"timestamp": None, "level": "info", "event": "supervisor.worker.killed", "slot": n}


def _wait(predicate: Any, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("timed out waiting")
        time.sleep(0.01)


class Recording:
    """A sink that keeps every batch it was sent."""

    max_events = 50
    max_bytes = MAX_BATCH_BYTES

    def __init__(self) -> None:
        self.batches: list[list[Record]] = []

    def send(self, records: Sequence[Record]) -> None:
        self.batches.append([dict(r) for r in records])

    @property
    def records(self) -> list[Record]:
        return [r for batch in self.batches for r in batch]


class Hanging(Recording):
    """A sink that never answers until released."""

    def __init__(self) -> None:
        super().__init__()
        self.release = threading.Event()
        self.entered = threading.Event()

    def send(self, records: Sequence[Record]) -> None:
        self.entered.set()
        self.release.wait()
        raise OSError("gone")


class Failing(Recording):
    """A sink that fails the first ``times`` sends, then works."""

    def __init__(self, times: int) -> None:
        super().__init__()
        self.times = times

    def send(self, records: Sequence[Record]) -> None:
        if self.times > 0:
            self.times -= 1
            raise OSError("unreachable")
        super().send(records)


@pytest.fixture
def events_logged() -> Iterator[None]:
    """The events logger lets every level through, as ``configure`` would at
    debug, and is put back after."""
    log = logging.getLogger(org_events.LOGGER)
    level, propagate = log.level, log.propagate
    log.setLevel(logging.DEBUG)
    log.propagate = False
    try:
        yield
    finally:
        log.setLevel(level)
        log.propagate = propagate


# -- the ring ---------------------------------------------------------------------


def test_a_full_ring_drops_its_oldest_and_counts_them() -> None:
    ring = Ring(capacity=3)
    for n in range(5):
        ring.push(_rec(n))
    assert len(ring) == 3
    assert ring.take_dropped() == 2
    assert ring.take_dropped() == 0
    assert [r["slot"] for r in ring.take(max_events=10, max_bytes=10_000)] == [2, 3, 4]


def test_records_put_back_go_ahead_of_newer_ones_and_the_oldest_go_past_the_cap() -> None:
    ring = Ring(capacity=4)
    for n in range(4):
        ring.push(_rec(n))
    taken = ring.take(max_events=2, max_bytes=10_000)
    ring.push(_rec(4))
    ring.push(_rec(5))
    ring.put_back(taken)
    assert [r["slot"] for r in ring.take(max_events=10, max_bytes=10_000)] == [2, 3, 4, 5]
    assert ring.dropped_total == 2


def test_a_batch_stays_under_its_byte_budget_but_always_moves() -> None:
    ring = Ring()
    for n in range(10):
        ring.push(_rec(n))
    one = len(json.dumps(_rec(0))) + 1
    assert len(ring.take(max_events=10, max_bytes=one * 3)) == 3
    assert len(ring.take(max_events=10, max_bytes=1)) == 1
    assert len(ring.take(max_events=2, max_bytes=10_000)) == 2


# -- what leaves the box ------------------------------------------------------------


def test_nothing_outside_the_allowlist_leaves_the_box(events_logged: None) -> None:
    sink = Recording()
    shipper = install({}, sink=sink, flush_seconds=0.01)
    assert shipper is not None
    log = logging.getLogger(org_events.LOGGER)
    try:
        # An event the box may ship, carrying fields it may not and secrets
        # in the one free-text field it may.
        org_events.emit(
            box_logs.ORG_REAP_FAILED,
            level="error",
            slot=1,
            org_id=ORG,
            error=f"rm failed: Bearer {SECRETS[0]} {SECRETS[1]} {SECRETS[2]} {SECRETS[3]}",
            prompt=SECRETS[4],
            content=SECRETS[4],
            token=SECRETS[0],
            file="/home/user/.ssh/id_rsa",
        )
        org_events.emit(
            box_logs.ORG_ADMISSION_REFUSED,
            level="error",
            slot=1,
            org_id=ORG,
            chat_id=CHAT,
            reason="the chat's row names another org",
            count=1,
        )
        # Records on the same logger that are not shipped events.
        log.error("chat.message.sent", extra={"event_fields": {"content": SECRETS[4]}})
        log.error("a plain line naming %s", SECRETS[0])
        log.error(SECRETS[4])
        _wait(lambda: len(sink.records) >= 2)
        time.sleep(0.05)
    finally:
        shipper.close()
    shipped = json.dumps(sink.records)
    for secret in SECRETS:
        assert secret not in shipped
    assert [r["event"] for r in sink.records] == [
        box_logs.ORG_REAP_FAILED,
        box_logs.ORG_ADMISSION_REFUSED,
    ]
    reap, refused = sink.records
    assert set(reap) == {"timestamp", "level", "event", "slot", "org_id", "error"}
    assert reap["error"].startswith("rm failed:")
    # A reason that is not one of the codes is dropped, not shipped as prose.
    assert set(refused) == {"timestamp", "level", "event", "slot", "org_id", "chat_id", "count"}


def test_after_close_the_logger_feeds_the_shipper_no_more(events_logged: None) -> None:
    sink = Recording()
    shipper = install({}, sink=sink, flush_seconds=0.01)
    assert shipper is not None
    shipper.close()
    org_events.emit(box_logs.WORKER_KILLED, level="warning", slot=0, org_id=ORG)
    assert len(shipper.ring) == 0
    assert all(
        not isinstance(h, log_shipping.ShippingHandler)
        for h in logging.getLogger(org_events.LOGGER).handlers
    )


def test_every_supervisor_event_is_shippable_with_every_field_it_is_emitted_with() -> None:
    """A field the supervisor starts emitting that the allowlist does not
    know would be dropped on its way off the box without anyone noticing; an
    event it does not know would not leave at all."""
    names = {
        attr: value
        for attr, value in vars(box_logs).items()
        if isinstance(value, str) and value in org_events.EVENTS
    }
    emitted: dict[str, set[str]] = {}
    for path in CLI_ROOT.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if not (isinstance(node, ast.Call) and node.args):
                continue
            func = node.func
            called = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if called not in ("emit", "emit_for"):
                continue
            # ``emit_for(slot, EVENT, ...)`` names the slot's index and org itself.
            implied = {"slot", "org_id"} if called == "emit_for" else set()
            if called == "emit_for" and len(node.args) < 2:
                continue
            first = node.args[1] if called == "emit_for" else node.args[0]
            attr = (
                first.attr
                if isinstance(first, ast.Attribute)
                else first.id
                if isinstance(first, ast.Name)
                else None
            )
            if attr not in names:
                continue
            kwargs = {kw.arg for kw in node.keywords if kw.arg and kw.arg != "level"} | implied
            emitted.setdefault(names[attr], set()).update(kwargs)
    assert set(emitted) == set(org_events.EVENTS), "every event has a call site"
    unknown = {e: sorted(f - EVENT_FIELDS[e]) for e, f in emitted.items() if f - EVENT_FIELDS[e]}
    assert unknown == {}


# -- an outage never reaches the supervisor -------------------------------------------


@pytest.mark.parametrize("kind", ["hangs", "fails"])
def test_a_sink_that_hangs_or_fails_forever_holds_the_ring_at_its_cap(
    events_logged: None, kind: str
) -> None:
    class Down(Recording):
        def send(self, records: Sequence[Record]) -> None:
            raise OSError("the log service is down")

    sink: Recording = Hanging() if kind == "hangs" else Down()
    ring = Ring(capacity=200)
    shipper = Shipper(ring, sink, flush_seconds=0.01, backoff_max=0.02)
    shipper.attach(logging.getLogger(org_events.LOGGER))
    shipper.start()
    try:
        if isinstance(sink, Hanging):
            org_events.emit(box_logs.WORKER_KILLED, level="warning", slot=0, org_id=ORG)
            assert sink.entered.wait(5)
        started = time.monotonic()
        for n in range(5000):
            org_events.emit(box_logs.WORKER_KILLED, level="warning", slot=n, org_id=ORG)
        elapsed = time.monotonic() - started
        # Five thousand events in well under a second each way: none of them
        # waited on the sink.
        assert elapsed < 5.0
        # At its cap, less at most the one batch a send has in hand.
        assert ring.capacity - sink.max_events <= len(ring) <= ring.capacity
        assert ring.dropped_total >= 5000 - 2 * ring.capacity
        if kind == "hangs":
            assert len(ring) == ring.capacity
        else:
            _wait(lambda: shipper.failures >= 3)
            assert ring.capacity - sink.max_events <= len(ring) <= ring.capacity
        assert sink.records == []
    finally:
        if isinstance(sink, Hanging):
            sink.release.set()
        shipper.close(timeout=2.0)


def test_a_failed_send_is_retried_in_order_and_the_drops_are_reported() -> None:
    sink = Failing(times=2)
    ring = Ring(capacity=3)
    shipper = Shipper(ring, sink)
    for n in range(5):
        ring.push(_rec(n))
    assert shipper.flush_once() is False
    assert shipper.flush_once() is False
    assert len(ring) == 3
    assert shipper.flush_once() is True
    events = [(r["event"], r.get("slot"), r.get("count")) for r in sink.records]
    assert events == [
        ("supervisor.worker.killed", 2, None),
        ("supervisor.worker.killed", 3, None),
        ("supervisor.worker.killed", 4, None),
        (SHIPPER_DROPPED, None, 2),
    ]


def test_a_drop_report_never_pushes_a_record_out_of_a_full_batch() -> None:
    sink = Failing(times=1)
    sink.max_events = 3
    ring = Ring(capacity=4)
    for n in range(6):
        ring.push(_rec(n))
    shipper = Shipper(ring, sink)
    assert shipper.flush_once() is False
    while len(ring):
        assert shipper.flush_once() is True
    slots = [r["slot"] for r in sink.records if r["event"] != SHIPPER_DROPPED]
    assert slots == [2, 3, 4, 5]
    assert [r["count"] for r in sink.records if r["event"] == SHIPPER_DROPPED] == [2]


def test_close_waits_for_a_hung_sink_no_longer_than_its_bound() -> None:
    sink = Hanging()
    ring = Ring()
    ring.push(_rec(0))
    shipper = Shipper(ring, sink, flush_seconds=0.01)
    shipper.start()
    assert sink.entered.wait(5)
    started = time.monotonic()
    shipper.close(timeout=0.2)
    assert time.monotonic() - started < 1.0
    sink.release.set()


# -- where it goes ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        pytest.param(
            {
                "ALKERA_MACHINE_PROVIDER": "ec2",
                "ALKERA_BOX_LOG_GROUP": "/ec2/example-chat-box",
                "ALKERA_BOX_LOG_REGION": "us-east-1",
                "ALKERA_ALLOCATION_ID": MACHINE,
                "ALKERA_API_URL": "https://api.example.test",
                "ALKERA_MACHINE_CREDENTIAL": CREDENTIAL,
            },
            "cloudwatch",
            id="ec2-told-its-group",
        ),
        pytest.param(
            {
                "ALKERA_MACHINE_PROVIDER": "ec2",
                "ALKERA_ALLOCATION_ID": MACHINE,
                "ALKERA_API_URL": "https://api.example.test",
                "ALKERA_MACHINE_CREDENTIAL": CREDENTIAL,
            },
            "backend",
            id="ec2-without-a-group",
        ),
        pytest.param(
            {
                "ALKERA_MACHINE_PROVIDER": "runpod",
                "ALKERA_BOX_LOG_GROUP": "/ec2/example-chat-box",
                "ALKERA_BOX_LOG_REGION": "us-east-1",
                "ALKERA_ALLOCATION_ID": MACHINE,
                "ALKERA_API_URL": "https://api.example.test",
                "ALKERA_MACHINE_CREDENTIAL": CREDENTIAL,
            },
            "backend",
            id="runpod-has-no-instance-role",
        ),
        pytest.param(
            {
                "ALKERA_MACHINE_PROVIDER": "personal",
                "ALKERA_API_URL": "https://api.example.test",
                "ALKERA_MACHINE_CREDENTIAL": CREDENTIAL,
            },
            "backend",
            id="personal",
        ),
        pytest.param({"ALKERA_API_URL": "https://api.example.test"}, None, id="no-credential"),
        pytest.param({}, None, id="nothing"),
    ],
)
def test_the_sink_follows_the_boxs_shape(env: dict[str, str], expected: str | None) -> None:
    sink = choose_sink(env)
    if expected is None:
        assert sink is None
        assert install(env) is None
    elif expected == "cloudwatch":
        assert isinstance(sink, CloudWatchSink)
        assert (sink.group, sink.stream) == ("/ec2/example-chat-box", MACHINE)
    else:
        assert isinstance(sink, BackendSink)
    assert CREDENTIAL not in repr(sink)


class _Backend(BaseHTTPRequestHandler):
    status = 202
    seen: ClassVar[list[tuple[str, dict[str, str], bytes]]] = []

    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers["Content-Length"]))
        type(self).seen.append((self.path, dict(self.headers), body))
        self.send_response(type(self).status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args: Any) -> None:
        return None


@pytest.fixture
def backend() -> Iterator[tuple[str, type[_Backend]]]:
    handler = type("Handler", (_Backend,), {"seen": [], "status": 202})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", handler
    finally:
        server.shutdown()
        server.server_close()


def test_the_backend_sink_posts_a_batch_the_ingestion_route_accepts(
    backend: tuple[str, type[_Backend]],
) -> None:
    url, handler = backend
    ring = Ring()
    for n in range(80):
        ring.push(
            {
                "timestamp": "2026-10-05T12:00:00+00:00",
                "level": "warning",
                "event": "supervisor.worker.exited",
                "slot": n,
                "backoff": 2.0,
                "ran": 1.5,
            }
        )
    shipper = Shipper(ring, BackendSink(api_url=url + "/", credential=CREDENTIAL))
    assert shipper.flush_once() is True
    assert shipper.flush_once() is True
    assert len(ring) == 0
    assert len(handler.seen) == 2
    for path, headers, body in handler.seen:
        assert path == INGEST_PATH
        assert headers["Authorization"] == f"Bearer {CREDENTIAL}"
        # Under the edge's body limit, and in the shape the route takes.
        assert len(body) <= MAX_BATCH_BYTES + 512
        batch = BoxLogBatch.model_validate_json(body)
        assert {e.event for e in batch.events} == {"supervisor.worker.exited"}
        assert all(set(e.fields) == {"slot", "backoff", "ran"} for e in batch.events)
    total = sum(len(BoxLogBatch.model_validate_json(b).events) for *_, b in handler.seen)
    assert total == 80


@pytest.mark.parametrize(
    ("status", "retried"),
    [
        pytest.param(500, True, id="server-error"),
        pytest.param(503, True, id="unavailable"),
        pytest.param(429, True, id="throttled"),
        pytest.param(401, True, id="credential-refused"),
        pytest.param(422, False, id="batch-refused"),
        pytest.param(404, False, id="not-found"),
    ],
)
def test_a_refusal_is_retried_only_when_retrying_could_help(
    backend: tuple[str, type[_Backend]], status: int, retried: bool
) -> None:
    url, handler = backend
    handler.status = status
    ring = Ring()
    ring.push(_rec(0))
    shipper = Shipper(ring, BackendSink(api_url=url, credential=CREDENTIAL))
    assert shipper.flush_once() is (not retried)
    assert len(ring) == (1 if retried else 0)


def test_an_unreachable_backend_fails_the_send_within_its_timeout() -> None:
    ring = Ring()
    ring.push(_rec(0))
    sink = BackendSink(api_url="http://127.0.0.1:9", credential=CREDENTIAL, timeout=1.0)
    started = time.monotonic()
    assert Shipper(ring, sink).flush_once() is False
    assert time.monotonic() - started < 5.0
    assert len(ring) == 1


class _AwsError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class _Logs:
    """Stands in for the CloudWatch Logs client: AWS is the costly boundary."""

    def __init__(self, *, stream_exists: bool = False) -> None:
        self.streams: set[tuple[str, str]] = set()
        self.created = 0
        self.puts: list[dict[str, Any]] = []
        self.exists = stream_exists
        self.lose_stream = False

    def create_log_stream(self, *, logGroupName: str, logStreamName: str) -> None:  # noqa: N803
        self.created += 1
        if self.exists or (logGroupName, logStreamName) in self.streams:
            raise _AwsError("ResourceAlreadyExistsException")
        self.streams.add((logGroupName, logStreamName))

    def put_log_events(self, **kwargs: Any) -> None:
        if self.lose_stream:
            self.lose_stream = False
            self.exists = False
            self.streams.clear()
            raise _AwsError("ResourceNotFoundException")
        self.puts.append(kwargs)


def test_the_cloudwatch_sink_writes_one_stream_named_by_the_machine() -> None:
    client = _Logs()
    sink = CloudWatchSink(
        group="/ec2/example-chat-box",
        stream=MACHINE,
        region="us-east-1",
        client_factory=lambda: client,
    )
    sink.send(
        [
            {"timestamp": "2026-10-05T12:00:02+00:00", "event": "supervisor.worker.killed"},
            {"timestamp": "2026-10-05T12:00:01+00:00", "event": "supervisor.worker.ready"},
        ]
    )
    sink.send([{"timestamp": "2026-10-05T12:00:03+00:00", "event": "supervisor.worker.killed"}])
    assert client.created == 1
    assert client.streams == {("/ec2/example-chat-box", MACHINE)}
    first = client.puts[0]
    assert (first["logGroupName"], first["logStreamName"]) == ("/ec2/example-chat-box", MACHINE)
    stamps = [e["timestamp"] for e in first["logEvents"]]
    assert stamps == sorted(stamps)
    assert json.loads(first["logEvents"][0]["message"])["event"] == "supervisor.worker.ready"


def test_the_cloudwatch_sink_takes_an_existing_stream_and_recreates_a_lost_one() -> None:
    client = _Logs(stream_exists=True)
    sink = CloudWatchSink(
        group="g", stream=MACHINE, region="us-east-1", client_factory=lambda: client
    )
    sink.send([_rec(0)])
    client.lose_stream = True
    with pytest.raises(_AwsError):
        sink.send([_rec(1)])
    sink.send([_rec(1)])
    assert client.created == 2
    assert len(client.puts) == 2


def test_the_cloudwatch_sink_raises_any_other_refusal() -> None:
    class Denied(_Logs):
        def create_log_stream(self, **kwargs: Any) -> None:
            raise _AwsError("AccessDeniedException")

    sink = CloudWatchSink(group="g", stream=MACHINE, region="r", client_factory=Denied)
    with pytest.raises(_AwsError):
        sink.send([_rec(0)])


# -- the root process stays light --------------------------------------------------------


def test_the_shipper_loads_no_aws_sdk_structlog_or_rich_until_it_sends() -> None:
    probe = (
        "import sys\n"
        "from alkera_cli.supervisor import box_log_shipping as log_shipping\n"
        "log_shipping.choose_sink({'ALKERA_MACHINE_PROVIDER': 'ec2',"
        " 'ALKERA_BOX_LOG_GROUP': 'g', 'ALKERA_BOX_LOG_REGION': 'r',"
        " 'ALKERA_ALLOCATION_ID': 'm'})\n"
        "heavy = ('boto3', 'botocore', 'structlog', 'rich', 'pydantic', 'sqlalchemy')\n"
        "print(sorted(m for m in sys.modules if m.split('.')[0] in heavy))\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True, timeout=120
    )
    assert done.stdout.strip() == "[]"


# -- the supervisor's entry ships ----------------------------------------------------------


def test_the_supervisor_entry_ships_its_events_and_flushes_them_on_exit(
    backend: tuple[str, type[_Backend]], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``alkera cloud-mirror supervise`` hooks the shipper onto its events
    before the loop runs, and its last events still leave when it exits."""
    from alkera_cli.supervisor import cli

    url, handler = backend
    monkeypatch.setenv("ALKERA_API_URL", url)
    monkeypatch.setenv("ALKERA_MACHINE_CREDENTIAL", CREDENTIAL)
    monkeypatch.delenv("ALKERA_BOX_LOG_GROUP", raising=False)

    async def run(*, routing_file: Path | None) -> int:
        org_events.emit(
            box_logs.WORKER_CRASH_LOOP,
            level="error",
            slot=0,
            org_id=ORG,
            restarts=6,
            window=900.0,
            backoff=8.0,
        )
        return 0

    log = logging.getLogger(org_events.LOGGER)
    before = list(log.handlers)
    monkeypatch.setattr(cli, "run_supervisor", run)
    try:
        # The host this runs on is not under test: it has what the supervisor needs.
        assert cli.main(["--routing-file", str(tmp_path / "routing.json")], missing=list) == 0
    finally:
        for extra in [h for h in log.handlers if h not in before]:
            log.removeHandler(extra)
        log.propagate = True
    (posted,) = [BoxLogBatch.model_validate_json(body) for *_, body in handler.seen]
    (event,) = posted.events
    assert event.event == box_logs.WORKER_CRASH_LOOP
    assert event.fields == {
        "slot": 0,
        "org_id": ORG,
        "restarts": 6,
        "window": 900.0,
        "backoff": 8.0,
    }
