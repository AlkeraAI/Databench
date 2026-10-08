"""Configure the daemon's structlog pipeline.

Two sinks:
- stderr: human-readable, picked up by the extension's output channel
- `~/.alkera/logs/daemon.log`: rotating (10 MB x 3), shared across daemons

stdout is reserved for JSON-RPC frames; never log there.

The PID is included in every log line so concurrent daemons (multiple
VS Code windows) can be disambiguated when grepping the shared file.

The file is the owner's alone: on a box it names every chat, every relation
and every sandbox argv the daemon handled, for every org it serves, beside
agents that run as other uids.
"""

from __future__ import annotations

import io
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

import structlog
from alkera_core.observability.redaction import scrub_mapping, scrub_path, scrub_text

from alkera_cli.daemon.paths import daemon_log_file

#: The daemon log and every file it rotates into: the owner reads them alone.
LOG_FILE_MODE = 0o600
LOG_DIR_MODE = 0o700


class PrivateRotatingFileHandler(RotatingFileHandler):
    """A :class:`RotatingFileHandler` whose files are owner-only whatever the
    process umask, including the fresh file each rollover opens. A log an
    earlier build left world-readable is closed when it is next opened, and
    so is its directory."""

    def _open(self) -> io.TextIOWrapper:
        path = Path(self.baseFilename)
        try:
            if path.parent.stat().st_mode & 0o077:
                path.parent.chmod(LOG_DIR_MODE)
        except OSError:
            pass
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, LOG_FILE_MODE)
        try:
            os.fchmod(fd, LOG_FILE_MODE)
        except (AttributeError, OSError):  # Windows has no fchmod
            pass
        return os.fdopen(fd, "a", encoding=self.encoding, errors=self.errors)


def _redact_processor(
    logger: Any, method_name: str, event_dict: structlog.types.EventDict
) -> structlog.types.EventDict:
    """Scrub secrets / PII / home-dir paths from every daemon log event."""
    try:
        return scrub_mapping(event_dict)
    except Exception:
        return event_dict


class _ScrubbingFilter(logging.Filter):
    """Scrub secrets / home-dir paths from stdlib log records.

    The structlog chain has its own `_redact_processor` (stderr), but the harness
    adapters log via stdlib `logging`, and those records reach the stdlib handlers
    — including the rotating daemon.log FILE — which would otherwise be unscrubbed.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
            scrubbed = scrub_path(scrub_text(msg))
            if scrubbed != msg:
                record.msg = scrubbed
                record.args = ()
        except Exception:  # noqa: S110 — a filter that raises would break all logging
            pass
        return True


_LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}


def configure(level: str = "info") -> None:
    """Configure structlog + stdlib logging once at daemon startup.

    Idempotent — re-calling reconfigures with the new level.
    """
    log_level = _LEVELS.get(level.lower(), logging.INFO)

    # Stdlib root logger: file + stderr handlers. structlog routes through it.
    root = logging.getLogger()
    root.setLevel(log_level)
    # Wipe pre-existing handlers so re-configuration is clean.
    for h in list(root.handlers):
        root.removeHandler(h)

    fmt = logging.Formatter(
        fmt=f"%(asctime)s pid={os.getpid()} [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )

    # One scrubber instance shared across handlers — stdlib records (the harness
    # adapters) are redacted on BOTH the stderr and file sinks.
    scrubber = _ScrubbingFilter()

    stderr_handler = logging.StreamHandler(stream=sys.stderr)
    stderr_handler.setFormatter(fmt)
    stderr_handler.setLevel(log_level)
    stderr_handler.addFilter(scrubber)
    root.addHandler(stderr_handler)

    try:
        file_handler = PrivateRotatingFileHandler(
            daemon_log_file(),
            maxBytes=10 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        file_handler.setFormatter(fmt)
        file_handler.setLevel(log_level)
        file_handler.addFilter(scrubber)
        root.addHandler(file_handler)
    except OSError as exc:
        # If the log file isn't writable (read-only HOME, permissions, etc.)
        # we degrade gracefully to stderr-only.
        stderr_handler.handle(
            logging.makeLogRecord(
                {
                    "name": "alkera.daemon.logging",
                    "levelno": logging.WARNING,
                    "levelname": "WARNING",
                    "msg": f"file logging disabled: {exc}",
                    "args": None,
                    "exc_info": None,
                }
            )
        )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            _redact_processor,
            structlog.dev.ConsoleRenderer(colors=False),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> Any:
    return structlog.get_logger(name)


__all__ = [
    "LOG_DIR_MODE",
    "LOG_FILE_MODE",
    "PrivateRotatingFileHandler",
    "configure",
    "get_logger",
]
