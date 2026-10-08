"""The workflow and activity loggers are stdlib loggers, not structlog ones.

Everywhere else in this codebase a logger comes from ``alkera_core.logging``
and takes its fields as keyword arguments. ``temporalio.workflow.logger`` and
``temporalio.activity.logger`` are ``logging.LoggerAdapter`` instances: a
keyword field reaches ``logging.Logger._log`` and raises ``TypeError``. Raised
inside workflow code that fails the workflow task, which the server retries for
ever — a drain that hits it stops draining until the code is fixed.

The habit is one keystroke away from every other logging call in the package,
so the rule is enforced over the source rather than per call site: no keyword
argument other than the ones ``logging`` itself accepts may be passed to
either logger.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

WORKER_PACKAGE = Path(__file__).resolve().parents[1] / "worker"

STDLIB_LOG_KEYWORDS = frozenset({"exc_info", "stack_info", "stacklevel", "extra"})
"""What ``logging.Logger._log`` accepts. Anything else is a structlog habit."""

LOG_METHODS = frozenset(
    {"debug", "info", "warning", "warn", "error", "exception", "critical", "log"}
)


def _is_sdk_logger(node: ast.expr) -> bool:
    """``workflow.logger`` / ``activity.logger``, however the module is aliased."""
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "logger"
        and isinstance(node.value, ast.Name)
        and node.value.id in {"workflow", "activity"}
    )


def _offending_calls(source: str) -> list[tuple[int, str]]:
    """Every ``(line, keyword)`` a temporal logger is handed that stdlib logging
    would refuse."""
    found: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (
            isinstance(func, ast.Attribute)
            and func.attr in LOG_METHODS
            and _is_sdk_logger(func.value)
        ):
            continue
        for keyword in node.keywords:
            if keyword.arg is None or keyword.arg not in STDLIB_LOG_KEYWORDS:
                found.append((node.lineno, keyword.arg or "**kwargs"))
    return found


def _worker_sources() -> list[Path]:
    return sorted(WORKER_PACKAGE.rglob("*.py"))


def test_the_scan_finds_the_shape_it_is_meant_to_find() -> None:
    """The detector itself, exhausted: structlog kwargs are caught on both
    loggers, the stdlib keywords and positional %-args are allowed, and a
    structlog logger of our own is none of its business."""
    assert _offending_calls("workflow.logger.warning('e', workflow_id=1)") == [(1, "workflow_id")]
    assert _offending_calls("activity.logger.info('e', row_id=1, attempt=2)") == [
        (1, "row_id"),
        (1, "attempt"),
    ]
    assert _offending_calls("workflow.logger.log(30, 'e', **fields)") == [(1, "**kwargs")]
    assert _offending_calls("workflow.logger.warning('e', 'arg', exc_info=True, extra={})") == []
    assert _offending_calls("workflow.logger.error('e %s', DISPATCH_ID)") == []
    assert _offending_calls("log.warning('e', workflow_id=1)") == [], "structlog takes fields"


def test_the_worker_package_has_sources_to_scan() -> None:
    assert len(_worker_sources()) > 20, "the scan would pass vacuously"


@pytest.mark.parametrize("path", _worker_sources(), ids=lambda p: str(p.name))
def test_no_temporal_logger_call_passes_a_structlog_field(path: Path) -> None:
    offenders = _offending_calls(path.read_text(encoding="utf-8"))
    assert offenders == [], (
        f"{path}: the temporal loggers are stdlib loggers — put the fields in the "
        f"message (%-args) or in extra=, never as keyword arguments: {offenders}"
    )
