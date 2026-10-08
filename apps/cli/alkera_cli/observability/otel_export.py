"""Opt-in OTLP export of agent activity to the org's own collector (the SIEM
path). Distinct from ``telemetry.py``, which governs phoning home to Alkera;
this never sends anywhere but the customer-configured endpoint.

Mirrors the vendors' customer-side events (``claude_code.tool_decision``,
``codex.tool_decision``) with an ``alkera.*`` vocabulary: every permission
decision (``alkera.tool_decision``), every finalized tool call
(``alkera.tool_result``), and session lifecycle (``alkera.session_started`` /
``alkera.session_finished``). Off by default; statement and argument content is
excluded unless ``ALKERA_OTEL_LOG_CONTENT`` is set, matching both vendors'
redacted defaults. ``alkera.api_request`` is not exported: no adapter emits the
schema's per-LLM-call events, so there is no client-side feed for it.

The OpenTelemetry SDK ships as the optional ``alkera-cli[otel]`` extra and is
imported lazily; unconfigured (or uninstalled), every hook is a no-op.
"""

from __future__ import annotations

import contextlib
import importlib.metadata
import json
import logging
import threading
from collections.abc import Callable
from typing import Any

from alkera_core.schemas.chat.events import Event, PartCreated
from alkera_core.schemas.chat.parts import ToolCallPart

from alkera_cli.host.config import get_settings
from alkera_cli.plugins.plugin_base.permissions.audit import DecisionRecord

logger = logging.getLogger(__name__)

Emit = Callable[[str, dict[str, Any]], None]

_OPERATION_TRUNCATE = 200
_INPUT_TRUNCATE = 2000
_ERROR_TRUNCATE = 500


def decision_signal(record: DecisionRecord, *, log_content: bool) -> tuple[str, dict[str, Any]]:
    """One decision as an ``alkera.tool_decision`` event. The default carries
    the same fields the org-audit channel does; ``log_content`` adds ``raw``
    (the full statement or command line)."""
    attributes: dict[str, Any] = {
        "session.id": record.session_id,
        "source": record.source,
        "capability": record.capability,
        "effect": record.effect,
        "operation": record.operation[:_OPERATION_TRUNCATE],
        "targets": list(record.targets[:50]),
        "mode": record.mode,
        "decision": record.decision,
        "decided_by": record.decided_by,
        "reasons": list(record.reasons[:10]),
    }
    if record.request_id:
        attributes["request_id"] = record.request_id
    if record.tool_call_id:
        attributes["tool_call_id"] = record.tool_call_id
    if log_content and record.raw:
        attributes["raw"] = record.raw
    return "alkera.tool_decision", attributes


def tool_result_signal(
    event: Event, *, session_id: str, log_content: bool
) -> tuple[str, dict[str, Any]] | None:
    """A finalized tool part as an ``alkera.tool_result`` event, or ``None``
    for every other chat event. ``status`` carries the part's final state
    (``completed`` / ``error``, or the partial state of a cancelled turn's
    synthesized close). Output never exports; input only with ``log_content``."""
    if not isinstance(event, PartCreated) or not isinstance(event.part, ToolCallPart):
        return None
    part = event.part
    attributes: dict[str, Any] = {
        "session.id": session_id,
        "tool_name": part.name,
        "status": part.state,
        "tool_call_id": part.call_id,
    }
    if log_content:
        attributes["input"] = json.dumps(part.input, default=str)[:_INPUT_TRUNCATE]
        if part.error_text:
            attributes["error_text"] = part.error_text[:_ERROR_TRUNCATE]
    return "alkera.tool_result", attributes


class OtelExporter:
    """Maps agent activity onto OTel events through an injected ``emit``.

    The emitter is the only SDK-touching piece; tests inject a recorder. Every
    hook swallows its own failures — export never affects a decision, a chat,
    or the org-audit channel."""

    def __init__(
        self,
        *,
        emit: Emit,
        log_content: bool = False,
        shutdown: Callable[[], None] | None = None,
    ) -> None:
        self._emit = emit
        self._log_content = log_content
        self._shutdown = shutdown

    def on_decision_record(self, record: DecisionRecord) -> None:
        """The decision-observer hook (fanned out beside org-audit reporting)."""
        name, attributes = decision_signal(record, log_content=self._log_content)
        self._safe_emit(name, attributes)

    def on_chat_event(self, session_id: str, event: Event) -> None:
        """The chat-persistence tap; emits only for finalized tool parts."""
        signal = tool_result_signal(event, session_id=session_id, log_content=self._log_content)
        if signal is not None:
            self._safe_emit(*signal)

    def session_started(self, *, session_id: str, project: str, detail: dict[str, Any]) -> None:
        self._safe_emit(
            "alkera.session_started", {"session.id": session_id, "project": project, **detail}
        )

    def session_finished(self, *, session_id: str, project: str, detail: dict[str, Any]) -> None:
        self._safe_emit(
            "alkera.session_finished", {"session.id": session_id, "project": project, **detail}
        )

    def close(self) -> None:
        """Flush and shut the SDK pipeline down (process exit)."""
        if self._shutdown is None:
            return
        try:
            self._shutdown()
        except Exception:
            logger.warning("otel shutdown failed", exc_info=True)

    def _safe_emit(self, name: str, attributes: dict[str, Any]) -> None:
        try:
            self._emit(name, attributes)
        except Exception:
            logger.warning("otel emit failed", exc_info=True)


def _logs_endpoint(base: str | None) -> str | None:
    """``ALKERA_OTEL_ENDPOINT`` is a collector base URL; OTLP/HTTP logs land at
    its ``/v1/logs``. ``None`` defers to the standard ``OTEL_EXPORTER_OTLP_*``
    env vars (default ``http://localhost:4318``)."""
    if not base:
        return None
    trimmed = base.rstrip("/")
    return trimmed if trimmed.endswith("/v1/logs") else f"{trimmed}/v1/logs"


def _build_emitter(endpoint: str | None) -> tuple[Emit, Callable[[], None]] | None:
    try:
        from opentelemetry._events import Event as OtelEvent
        from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
        from opentelemetry.sdk._events import EventLoggerProvider
        from opentelemetry.sdk._logs import LoggerProvider
        from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
        from opentelemetry.sdk.resources import Resource
    except ImportError:
        logger.warning(
            "ALKERA_OTEL_ENABLED is set but the OpenTelemetry SDK is not installed; "
            "install alkera-cli[otel]"
        )
        return None
    resource_attributes: dict[str, Any] = {"service.name": "alkera-cli"}
    with contextlib.suppress(Exception):
        resource_attributes["service.version"] = importlib.metadata.version("alkera-cli")
    url = _logs_endpoint(endpoint)
    exporter = OTLPLogExporter(endpoint=url) if url else OTLPLogExporter()
    provider = LoggerProvider(resource=Resource.create(resource_attributes))
    provider.add_log_record_processor(BatchLogRecordProcessor(exporter))
    event_logger = EventLoggerProvider(logger_provider=provider).get_event_logger("alkera")

    def emit(name: str, attributes: dict[str, Any]) -> None:
        event_logger.emit(OtelEvent(name=name, attributes=attributes))

    def shutdown() -> None:
        provider.shutdown()

    return emit, shutdown


_default: OtelExporter | None = None
_default_built = False
_default_lock = threading.Lock()


def default_exporter() -> OtelExporter | None:
    """The process-wide exporter; ``None`` unless ``ALKERA_OTEL_ENABLED`` is set
    and the SDK is importable. Built once, on first use."""
    global _default, _default_built
    with _default_lock:
        if _default_built:
            return _default
        _default_built = True
        settings = get_settings()
        if not settings.alkera_otel_enabled:
            return None
        built = _build_emitter(settings.alkera_otel_endpoint)
        if built is None:
            return None
        emit, shutdown = built
        _default = OtelExporter(
            emit=emit, log_content=settings.alkera_otel_log_content, shutdown=shutdown
        )
        return _default


def peek_exporter() -> OtelExporter | None:
    """The live exporter if one was already built — never builds one. Teardown
    paths recompose the observer slot through this, so closing can't resurrect
    a fresh SDK pipeline."""
    with _default_lock:
        return _default


def close_default_exporter() -> None:
    """Flush + stop the shared exporter (process shutdown)."""
    global _default, _default_built
    with _default_lock:
        exporter, _default, _default_built = _default, None, False
    if exporter is None:
        return
    # Drop this exporter from the shared decision-observer slot BEFORE shutting
    # the SDK pipeline down, so no decision can land on a closed emitter.
    from alkera_cli.observability.audit_report import reinstall_observer

    reinstall_observer()
    exporter.close()


__all__ = [
    "OtelExporter",
    "close_default_exporter",
    "decision_signal",
    "default_exporter",
    "peek_exporter",
    "tool_result_signal",
]
