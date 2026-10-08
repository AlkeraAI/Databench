"""Structured logging via structlog.

- In `local`, uses a pretty console renderer so logs are readable during dev.
- In `staging`/`production`, emits JSON for log aggregation.
"""

from __future__ import annotations

import json
import logging
import socket
import sys
from typing import Any

import structlog

from alkera_core.config import settings
from alkera_core.observability.redaction import scrub_mapping

# syslog severities by structlog level name (RFC 5424).
_SYSLOG_SEVERITY = {
    "critical": 2,
    "error": 3,
    "warning": 4,
    "info": 6,
    "debug": 7,
}
_SYSLOG_FACILITY = 16  # local0
_SYSLOG_MAX_BYTES = 8192


class _SyslogForwarder:
    """A structlog processor that ALSO ships each (already-redacted) event to a
    syslog/SIEM endpoint as a JSON-payload line. Forwarding never raises — logging
    must not — and the event is returned unchanged for the normal renderer.

    Endpoint form: ``host:port`` (UDP) or ``tcp://host:port``. The socket is created
    lazily and re-created on send failure.
    """

    def __init__(self, endpoint: str) -> None:
        proto, _, hostport = endpoint.rpartition("://")
        self._tcp = proto == "tcp"
        host, _, port = hostport.partition(":")
        self._addr = (host, int(port) if port else 514)
        self._sock: socket.socket | None = None

    def _socket(self) -> socket.socket:
        if self._sock is None:
            if self._tcp:
                self._sock = socket.create_connection(self._addr, timeout=2.0)
            else:
                self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        return self._sock

    def __call__(
        self, logger: Any, method_name: str, event_dict: structlog.types.EventDict
    ) -> structlog.types.EventDict:
        try:
            severity = _SYSLOG_SEVERITY.get(str(event_dict.get("level", "info")), 6)
            pri = _SYSLOG_FACILITY * 8 + severity
            payload = json.dumps(event_dict, default=str)
            frame = f"<{pri}>{payload}".encode()[:_SYSLOG_MAX_BYTES]
            sock = self._socket()
            if self._tcp:
                sock.sendall(frame + b"\n")
            else:
                sock.sendto(frame, self._addr)
        except Exception:
            # Drop the socket so the next event reconnects; never propagate.
            if self._sock is not None:
                try:
                    self._sock.close()
                finally:
                    self._sock = None
        return event_dict


def _redact_processor(
    logger: Any,
    method_name: str,
    event_dict: structlog.types.EventDict,
) -> structlog.types.EventDict:
    """Scrub secrets / PII / home-dir paths from every emitted log event.

    Runs AFTER `format_exc_info` so it also cleans formatted tracebacks. Logging
    must never raise, so failures fall back to the original event.
    """
    try:
        return scrub_mapping(event_dict)
    except Exception:
        return event_dict


#: The numeric level the last ``configure_logging()`` asked for.
_LEVEL: list[int] = [logging.NOTSET]

#: The level each structlog method stands for. A method name this build has
#: never heard of is never dropped -- silence is the one failure mode logging
#: may not have.
_METHOD_LEVEL: dict[str, int] = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warn": logging.WARNING,
    "warning": logging.WARNING,
    "error": logging.ERROR,
    "exception": logging.ERROR,
    "critical": logging.CRITICAL,
    "fatal": logging.CRITICAL,
}


def _level_filter(
    logger: Any,
    method_name: str,
    event_dict: structlog.types.EventDict,
) -> structlog.types.EventDict:
    """Drop an event below the level the last ``configure_logging()`` asked for.

    The level lives here rather than in ``wrapper_class`` because structlog
    bakes the wrapper into a bound logger the first time that logger is used:
    one that has already spoken keeps the level it was born with for the life of
    the process, so a second configure that turns the level down would never be
    heard by it, and one that turns it up would never silence it. The processor
    chain is rebuilt in place and DOES reach those loggers, so this is the half
    that can still decide.
    """
    if _METHOD_LEVEL.get(method_name, logging.CRITICAL) < _LEVEL[0]:
        raise structlog.DropEvent
    return event_dict


#: The processor chain handed to structlog, rebuilt IN PLACE on every configure.
#: ``cache_logger_on_first_use`` makes a bound logger keep a reference to the list
#: object that was configured when it was first used, so handing ``configure()`` a
#: fresh list on a later call would strand every logger already in use on the
#: previous chain -- it would keep running the old renderer, the old redaction pass
#: and the old syslog forwarder for the life of the process. One list, mutated in
#: place, keeps them all current.
_PROCESSORS: list[structlog.types.Processor] = []


def configure_logging() -> None:
    """Configure stdlib logging + structlog. Idempotent."""
    level = getattr(logging, settings.log_level.upper(), logging.INFO)

    # Logs go to stderr so stdout stays clean for actual program output
    # (e.g. the gen-openapi script pipes app.openapi() via stdout).
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stderr,
        level=level,
        force=True,
    )

    _LEVEL[0] = level

    shared_processors: list[structlog.types.Processor] = [
        _level_filter,
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        _redact_processor,
    ]

    # Ship the redacted event to a syslog/SIEM sink when configured (added AFTER
    # redaction so no secret ever leaves, BEFORE the renderer so it forwards the
    # structured dict).
    if settings.log_syslog_endpoint:
        shared_processors.append(_SyslogForwarder(settings.log_syslog_endpoint))

    if settings.is_local:
        renderer: structlog.types.Processor = structlog.dev.ConsoleRenderer(colors=True)
    else:
        renderer = structlog.processors.JSONRenderer()

    _PROCESSORS[:] = [*shared_processors, renderer]

    structlog.configure(
        processors=_PROCESSORS,
        # Permissive on purpose: the level is `_level_filter`'s to decide, and a
        # wrapper that filtered too would pin a warm logger to the level of the
        # configure that happened to be first.
        wrapper_class=structlog.make_filtering_bound_logger(logging.NOTSET),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> Any:
    """Return a structlog BoundLogger. Typed as Any because structlog's
    factory return type depends on the chosen wrapper_class."""
    return structlog.get_logger(name)
