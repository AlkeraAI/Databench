"""Ship the supervisor's events off the box, without ever slowing it down.

The supervisor's events (``alkera_cli.supervisor.org_events``) are what ops alarm on: an
org's worker crash-looping, a chat for another org reaching a box. On the box
they only reach the journal, which nobody reads until something is already
wrong. This forwards them.

Three parts, each on its own side of the critical path:

- :class:`ShippingHandler` is a :mod:`logging` handler on the events' logger.
  It applies the shared allowlist (``alkera_core.compute.box_logs``) and puts
  the record in a :class:`Ring`. That is all it does: no I/O, no waiting.
- :class:`Ring` is a bounded buffer. Full, it drops its oldest record and
  counts the drop; the count is shipped as its own event.
- :class:`Shipper` is a daemon thread that drains the ring into a
  :class:`Sink` every few seconds. Each send is bounded by timeouts; a failed
  send is put back and retried with backoff; a sink that hangs holds only the
  shipper thread, never the supervisor, and the ring stays at its cap.

The sink follows the box's shape (:func:`choose_sink`): an EC2 box writes
straight to CloudWatch Logs on its instance role, into the node log group the
alarms filter on, one stream per machine; any other box (RunPod, a personal
box, a local developer box) posts to the backend on its machine credential,
which re-applies the allowlist and re-emits each event to its own log.

Standard library only at import: boto3 is loaded by the CloudWatch sink when
it first sends, on the shipper thread.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Final, Protocol

from alkera_core.compute.box_logs import (
    INGEST_PATH,
    MAX_BATCH_BYTES,
    MAX_BATCH_EVENTS,
    SHIPPER_DROPPED,
    sanitize,
)

from alkera_cli.host.backoff import doubled
from alkera_cli.supervisor import org_events
from alkera_cli.supervisor.http import ssl_context

logger = logging.getLogger(__name__)

Record = dict[str, object]

#: The most records the ring holds; past it the oldest go.
RING_CAPACITY: Final = 2000
#: How often the shipper drains the ring.
FLUSH_SECONDS: Final = 5.0
#: The longest a failed sink is left before the next try.
BACKOFF_MAX_SECONDS: Final = 60.0
#: How long a stop waits for the last flush.
CLOSE_SECONDS: Final = 3.0
#: Each send's network bound.
SEND_TIMEOUT_SECONDS: Final = 10.0

#: The environment a box names its log destination in (``node.env``).
ENV_LOG_GROUP: Final = "ALKERA_BOX_LOG_GROUP"
ENV_LOG_REGION: Final = "ALKERA_BOX_LOG_REGION"
#: CloudWatch takes up to 10,000 events and 1 MiB a call; a box sends far less.
CLOUDWATCH_MAX_EVENTS: Final = 500
CLOUDWATCH_MAX_BYTES: Final = 256 * 1024


def _size(record: Mapping[str, object]) -> int:
    return len(json.dumps(record, default=str)) + 1


class Ring:
    """A bounded, thread-safe buffer of records that drops its oldest."""

    def __init__(self, capacity: int = RING_CAPACITY) -> None:
        self.capacity = capacity
        self._items: deque[Record] = deque()
        self._lock = threading.Lock()
        self._dropped = 0
        self.dropped_total = 0

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)

    def push(self, record: Record) -> None:
        with self._lock:
            self._items.append(record)
            self._trim()

    def put_back(self, records: Sequence[Record]) -> None:
        """Return records a failed send took, ahead of everything newer; any
        past the cap are the oldest and go."""
        with self._lock:
            self._items.extendleft(reversed(records))
            self._trim()

    def take(self, *, max_events: int, max_bytes: int) -> list[Record]:
        """The oldest records that fit in one send (always at least one)."""
        out: list[Record] = []
        used = 0
        with self._lock:
            while self._items and len(out) < max_events:
                size = _size(self._items[0])
                if out and used + size > max_bytes:
                    break
                out.append(self._items.popleft())
                used += size
        return out

    def take_dropped(self) -> int:
        """The drops since the last call."""
        with self._lock:
            dropped, self._dropped = self._dropped, 0
        return dropped

    def restore_dropped(self, count: int) -> None:
        """Count drops a failed report took back in."""
        with self._lock:
            self._dropped += count

    def _trim(self) -> None:
        while len(self._items) > self.capacity:
            self._items.popleft()
            self._dropped += 1
            self.dropped_total += 1


class ShippingHandler(logging.Handler):
    """Puts each allowed event record in the ring; never blocks, never raises."""

    def __init__(self, ring: Ring) -> None:
        super().__init__(level=logging.DEBUG)
        self._ring = ring

    def emit(self, record: logging.LogRecord) -> None:
        try:
            fields = getattr(record, "event_fields", None)
            stamp = datetime.fromtimestamp(record.created, UTC).isoformat()
            clean = sanitize(
                record.msg,
                record.levelname,
                stamp,
                fields if isinstance(fields, Mapping) else {},
            )
            if clean is not None:
                self._ring.push(clean)
        except Exception:  # logging must never take the supervisor down
            self.handleError(record)


class Sink(Protocol):
    """Where a batch goes. ``send`` raises on any failure."""

    max_events: int
    max_bytes: int

    def send(self, records: Sequence[Record]) -> None: ...


class BackendSink:
    """POSTs a batch to the backend on the box's machine credential."""

    max_events = MAX_BATCH_EVENTS
    max_bytes = MAX_BATCH_BYTES

    def __init__(
        self, *, api_url: str, credential: str, timeout: float = SEND_TIMEOUT_SECONDS
    ) -> None:
        self._url = api_url.rstrip("/") + INGEST_PATH
        self._credential = credential
        self._timeout = timeout
        self._context = ssl_context()

    def __repr__(self) -> str:
        return "BackendSink(credential=...)"

    def send(self, records: Sequence[Record]) -> None:
        events = [
            {
                "timestamp": r.get("timestamp"),
                "level": r.get("level"),
                "event": r.get("event"),
                "fields": {k: v for k, v in r.items() if k not in ("timestamp", "level", "event")},
            }
            for r in records
        ]
        data = json.dumps({"events": events}, default=str).encode("utf-8")
        request = urllib.request.Request(self._url, data=data, method="POST")  # noqa: S310 -- the deployment's own API URL
        request.add_header("Content-Type", "application/json")
        request.add_header("Authorization", f"Bearer {self._credential}")
        try:
            with urllib.request.urlopen(request, timeout=self._timeout, context=self._context):  # noqa: S310
                return
        except urllib.error.HTTPError as exc:
            if 400 <= exc.code < 500 and exc.code not in (401, 408, 429):
                # The backend read the batch and refused it as such (a field
                # it will never take): retrying the same batch changes nothing.
                logger.warning("the backend refused a log batch (%d); dropped", exc.code)
                return
            raise


class CloudWatchSink:
    """Writes a batch to one CloudWatch Logs stream on the instance role."""

    max_events = CLOUDWATCH_MAX_EVENTS
    max_bytes = CLOUDWATCH_MAX_BYTES

    def __init__(
        self,
        *,
        group: str,
        stream: str,
        region: str,
        client_factory: Callable[[], Any] | None = None,
    ) -> None:
        self.group = group
        self.stream = stream
        self._region = region
        self._factory = client_factory or self._boto_client
        self._client: Any = None
        self._stream_ready = False

    def _boto_client(self) -> Any:
        import boto3
        from botocore.config import Config

        config = Config(
            connect_timeout=5,
            read_timeout=SEND_TIMEOUT_SECONDS,
            retries={"max_attempts": 2, "mode": "standard"},
        )
        return boto3.client("logs", region_name=self._region, config=config)

    def _ensure_stream(self, client: Any) -> None:
        if self._stream_ready:
            return
        try:
            client.create_log_stream(logGroupName=self.group, logStreamName=self.stream)
        except Exception as exc:
            if _aws_code(exc) != "ResourceAlreadyExistsException":
                raise
        self._stream_ready = True

    def send(self, records: Sequence[Record]) -> None:
        if self._client is None:
            self._client = self._factory()
        self._ensure_stream(self._client)
        stamped = sorted(
            ((_millis(r.get("timestamp")), line(r)) for r in records), key=lambda p: p[0]
        )
        events = [{"timestamp": at, "message": message} for at, message in stamped]
        try:
            self._client.put_log_events(
                logGroupName=self.group, logStreamName=self.stream, logEvents=events
            )
        except Exception as exc:
            if _aws_code(exc) == "ResourceNotFoundException":
                self._stream_ready = False
            raise


def line(record: Mapping[str, object]) -> str:
    """A record as the one JSON line a log group holds."""
    return json.dumps(dict(record), default=str)


def _aws_code(exc: Exception) -> str:
    response = getattr(exc, "response", None)
    if isinstance(response, Mapping):
        error = response.get("Error")
        if isinstance(error, Mapping):
            return str(error.get("Code", ""))
    return ""


def _millis(stamp: object) -> int:
    if isinstance(stamp, str):
        try:
            return int(datetime.fromisoformat(stamp).timestamp() * 1000)
        except ValueError:
            pass
    return int(time.time() * 1000)


class Shipper:
    """The thread that drains the ring into the sink."""

    def __init__(
        self,
        ring: Ring,
        sink: Sink,
        *,
        machine_id: str = "",
        flush_seconds: float = FLUSH_SECONDS,
        backoff_max: float = BACKOFF_MAX_SECONDS,
    ) -> None:
        self.ring = ring
        self._sink = sink
        self._machine_id = machine_id
        self._flush = flush_seconds
        self._backoff_max = backoff_max
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="box-log-shipper", daemon=True)
        self.sent = 0
        self.failures = 0
        self._handler: ShippingHandler | None = None
        self._log: logging.Logger | None = None

    def attach(self, log: logging.Logger) -> None:
        """Feed the ring from ``log``'s records."""
        self._handler = ShippingHandler(self.ring)
        self._log = log
        log.addHandler(self._handler)

    def start(self) -> None:
        self._thread.start()

    def close(self, timeout: float = CLOSE_SECONDS) -> None:
        """Stop taking records and stop after one last flush, waiting at most
        ``timeout`` for it."""
        if self._log is not None and self._handler is not None:
            self._log.removeHandler(self._handler)
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout)

    def flush_once(self) -> bool:
        """Send one batch; whether the sink took it (``True`` when there was
        nothing to send)."""
        dropped = self.ring.take_dropped()
        report = (
            sanitize(SHIPPER_DROPPED, "warning", datetime.now(UTC).isoformat(), {"count": dropped})
            if dropped
            else None
        )
        room = self._sink.max_events - (1 if report is not None else 0)
        batch = self.ring.take(max_events=room, max_bytes=self._sink.max_bytes) if room else []
        if report is not None:
            batch.append(report)
        if not batch:
            return True
        if self._machine_id:
            batch = [{**r, "machine_id": self._machine_id} for r in batch]
        try:
            self._sink.send(batch)
        except Exception as exc:
            self.failures += 1
            logger.debug("shipping the box's events failed: %s", exc)
            kept = [
                {k: v for k, v in r.items() if k != "machine_id"}
                for r in batch
                if r.get("event") != SHIPPER_DROPPED
            ]
            self.ring.put_back(kept)
            # Report the drops on the next send that goes through.
            self.ring.restore_dropped(dropped)
            return False
        self.sent += len(batch)
        return True

    def _run(self) -> None:
        delay = self._flush
        while not self._stop.wait(delay):
            ok = self._drain()
            delay = self._flush if ok else doubled(delay, floor=self._flush, cap=self._backoff_max)
        self._drain()

    def _drain(self) -> bool:
        """Flush until the ring is empty or a send fails."""
        while True:
            if not self.flush_once():
                return False
            if len(self.ring) == 0:
                return True


def choose_sink(env: Mapping[str, str]) -> Sink | None:
    """The sink this box ships to, from its environment: CloudWatch Logs for
    an EC2 box that was told its log group, the backend for any box with a
    machine credential, else nothing."""
    provider = env.get("ALKERA_MACHINE_PROVIDER", "").strip()
    group = env.get(ENV_LOG_GROUP, "").strip()
    region = env.get(ENV_LOG_REGION, "").strip()
    machine = env.get("ALKERA_ALLOCATION_ID", "").strip()
    if provider == "ec2" and group and region and machine:
        return CloudWatchSink(group=group, stream=machine, region=region)
    api_url = env.get("ALKERA_API_URL", "").strip()
    credential = env.get("ALKERA_MACHINE_CREDENTIAL", "").strip()
    if api_url and credential:
        return BackendSink(api_url=api_url, credential=credential)
    return None


def install(
    env: Mapping[str, str], *, sink: Sink | None = None, flush_seconds: float = FLUSH_SECONDS
) -> Shipper | None:
    """Hook a shipper onto the supervisor's events logger and start it;
    ``None`` (nothing hooked) when the box has nowhere to ship."""
    chosen = sink if sink is not None else choose_sink(env)
    if chosen is None:
        logger.info("the box's events are not shipped: no log destination")
        return None
    ring = Ring()
    # The backend knows the machine from the credential; a CloudWatch line
    # names it, since the stream is all that otherwise does.
    machine = env.get("ALKERA_ALLOCATION_ID", "").strip()
    shipper = Shipper(
        ring,
        chosen,
        machine_id=machine if isinstance(chosen, CloudWatchSink) else "",
        flush_seconds=flush_seconds,
    )
    shipper.attach(logging.getLogger(org_events.LOGGER))
    shipper.start()
    return shipper


__all__ = [
    "BACKOFF_MAX_SECONDS",
    "CLOSE_SECONDS",
    "ENV_LOG_GROUP",
    "ENV_LOG_REGION",
    "FLUSH_SECONDS",
    "RING_CAPACITY",
    "BackendSink",
    "CloudWatchSink",
    "Ring",
    "Shipper",
    "ShippingHandler",
    "Sink",
    "choose_sink",
    "install",
    "line",
]
