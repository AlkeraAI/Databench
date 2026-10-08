"""The opt-in OTel exporter: signal mapping, content gating, the observer
fan-out beside org-audit reporting, and the runtime feeds."""

from __future__ import annotations

import importlib.util
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from alkera_cli.harness import EventBus, HarnessRuntime
from alkera_cli.harness._fake import FakeAdapter
from alkera_cli.harness.adapter import SessionConfig
from alkera_cli.harness.runtime import AdapterFactory
from alkera_cli.observability import audit_report, otel_export
from alkera_cli.observability.audit_report import _install_observer
from alkera_cli.observability.otel_export import (
    OtelExporter,
    close_default_exporter,
    decision_signal,
    default_exporter,
    tool_result_signal,
)
from alkera_cli.plugins.plugin_base.permissions.audit import (
    DecisionRecord,
    DecisionSink,
    set_decision_observer,
)
from alkera_core.project.directory import ProjectDirectory
from alkera_core.schemas.chat.events import Heartbeat, PartCreated
from alkera_core.schemas.chat.parts import TextPart, ToolCallPart

_T = datetime(2026, 5, 26, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _clean_slate() -> Iterator[None]:
    close_default_exporter()
    yield
    close_default_exporter()
    set_decision_observer(None)


def _record(**overrides: Any) -> DecisionRecord:
    base: dict[str, Any] = {
        "at": 1_752_900_000.0,
        "session_id": "sess-1",
        "request_id": "req-1",
        "source": "harness",
        "capability": "fs",
        "effect": "write",
        "operation": "write README.md",
        "raw": None,
        "targets": ["README.md"],
        "mode": "default",
        "decision": "reject",
        "decided_by": "rule",
        "reasons": ["disallowed by policy"],
    }
    base.update(overrides)
    return DecisionRecord(**base)


def _tool_part(**overrides: Any) -> ToolCallPart:
    base: dict[str, Any] = {
        "part_id": "p1",
        "message_id": "m1",
        "call_id": "c1",
        "name": "bash",
        "input": {"command": "cat /etc/passwd"},
        "state": "completed",
    }
    base.update(overrides)
    return ToolCallPart(**base)


def _part_created(part: Any) -> PartCreated:
    return PartCreated(event_id="pc1", time=_T, session_id="sess-1", part=part)


def _settings(**kw: Any) -> SimpleNamespace:
    return SimpleNamespace(
        alkera_otel_enabled=kw.get("enabled", False),
        alkera_otel_endpoint=kw.get("endpoint"),
        alkera_otel_log_content=kw.get("log_content", False),
    )


class _Recorder:
    """A decision-observer consumer that records, or detonates on demand."""

    def __init__(self, *, fail: bool = False) -> None:
        self.records: list[DecisionRecord] = []
        self.fail = fail

    def on_decision_record(self, record: DecisionRecord) -> None:
        if self.fail:
            raise RuntimeError("consumer down")
        self.records.append(record)


# --------------------------------------------------------------------------- #
# signal mapping + content gating
# --------------------------------------------------------------------------- #


def test_decision_signal_defaults_exclude_content() -> None:
    record = _record(raw="select ssn from customers", operation="x" * 300)
    name, attrs = decision_signal(record, log_content=False)
    assert name == "alkera.tool_decision"
    assert attrs["session.id"] == "sess-1"
    assert attrs["source"] == "harness"
    assert attrs["capability"] == "fs"
    assert attrs["effect"] == "write"
    assert attrs["decision"] == "reject"
    assert attrs["decided_by"] == "rule"
    assert attrs["mode"] == "default"
    assert attrs["targets"] == ["README.md"]
    assert attrs["reasons"] == ["disallowed by policy"]
    assert attrs["request_id"] == "req-1"
    assert len(attrs["operation"]) == 200  # clamped, same as the org-audit wire
    assert "raw" not in attrs  # the full statement never exports by default


def test_decision_signal_content_optin_adds_raw() -> None:
    record = _record(raw="select ssn from customers")
    _name, attrs = decision_signal(record, log_content=True)
    assert attrs["raw"] == "select ssn from customers"


def test_decision_signal_omits_empty_correlation_ids() -> None:
    _name, attrs = decision_signal(_record(request_id="", tool_call_id=None), log_content=False)
    assert "request_id" not in attrs
    assert "tool_call_id" not in attrs


def test_tool_result_signal_maps_a_finalized_tool_part() -> None:
    signal = tool_result_signal(_part_created(_tool_part()), session_id="sess-9", log_content=False)
    assert signal is not None
    name, attrs = signal
    assert name == "alkera.tool_result"
    assert attrs == {
        "session.id": "sess-9",
        "tool_name": "bash",
        "status": "completed",
        "tool_call_id": "c1",
    }  # exactly the metadata — no input, no output, ever by default


def test_tool_result_signal_content_optin_adds_input_and_error() -> None:
    part = _tool_part(state="error", error_text="boom\n" + "x" * 600)
    signal = tool_result_signal(_part_created(part), session_id="s", log_content=True)
    assert signal is not None
    _name, attrs = signal
    assert "cat /etc/passwd" in attrs["input"]
    assert attrs["error_text"].startswith("boom")
    assert len(attrs["error_text"]) == 500


@pytest.mark.parametrize(
    "event",
    [
        pytest.param(
            _part_created(TextPart(part_id="p1", message_id="m1", text="hi")), id="text-part"
        ),
        pytest.param(
            Heartbeat(event_id="hb", time=_T, session_id="s", last_activity_ms=1),
            id="non-part-event",
        ),
    ],
)
def test_tool_result_signal_ignores_everything_else(event: Any) -> None:
    assert tool_result_signal(event, session_id="s", log_content=True) is None


def test_exporter_swallows_a_failing_emit() -> None:
    def boom(_name: str, _attrs: dict[str, Any]) -> None:
        raise RuntimeError("collector down")

    exporter = OtelExporter(emit=boom)
    exporter.on_decision_record(_record())  # must not raise
    exporter.on_chat_event("s", _part_created(_tool_part()))
    exporter.session_started(session_id="s", project="p", detail={})
    exporter.session_finished(session_id="s", project="p", detail={})


# --------------------------------------------------------------------------- #
# the single observer slot fans out to both channels
# --------------------------------------------------------------------------- #


def _fanned_out(
    tmp_path: Path,
    *,
    reporter: _Recorder | None,
    exporter_consumer: _Recorder | None,
) -> DecisionSink:
    _install_observer(reporter, exporter_consumer)  # type: ignore[arg-type]
    return DecisionSink(tmp_path / "chat")


def test_both_channels_receive_each_decision(tmp_path: Path) -> None:
    reporter, exporter = _Recorder(), _Recorder()
    sink = _fanned_out(tmp_path, reporter=reporter, exporter_consumer=exporter)
    sink.record(_record())
    assert len(reporter.records) == 1
    assert len(exporter.records) == 1


@pytest.mark.parametrize("failing", ["reporter", "exporter"])
def test_one_channel_failing_never_starves_the_other(tmp_path: Path, failing: str) -> None:
    reporter = _Recorder(fail=failing == "reporter")
    exporter = _Recorder(fail=failing == "exporter")
    sink = _fanned_out(tmp_path, reporter=reporter, exporter_consumer=exporter)
    sink.record(_record())  # must not raise either
    survivor = exporter if failing == "reporter" else reporter
    assert len(survivor.records) == 1


def test_export_works_logged_out(tmp_path: Path) -> None:
    exporter = _Recorder()
    sink = _fanned_out(tmp_path, reporter=None, exporter_consumer=exporter)
    sink.record(_record())
    assert len(exporter.records) == 1


def test_closing_the_exporter_renarrows_the_observer_slot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    emitted: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(otel_export, "get_settings", lambda: _settings(enabled=True))
    monkeypatch.setattr(
        otel_export,
        "_build_emitter",
        lambda endpoint: (lambda name, attrs: emitted.append((name, attrs)), lambda: None),
    )
    reporter = _Recorder()
    monkeypatch.setattr(audit_report, "_default", reporter)  # the live reporter singleton
    _install_observer(reporter, default_exporter())  # type: ignore[arg-type]
    sink = DecisionSink(tmp_path / "chat")
    sink.record(_record())
    assert len(reporter.records) == 1 and len(emitted) == 1

    close_default_exporter()
    sink.record(_record())
    assert len(reporter.records) == 2  # reporting survives the exporter's close
    assert len(emitted) == 1  # the closed exporter never sees another decision
    assert otel_export.peek_exporter() is None  # and close never rebuilt one


# --------------------------------------------------------------------------- #
# configuration gating + singleton lifecycle
# --------------------------------------------------------------------------- #


def test_default_exporter_off_unless_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(otel_export, "get_settings", lambda: _settings(enabled=False))
    assert default_exporter() is None


def test_default_exporter_builds_once_and_threads_content_optin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    emitted: list[tuple[str, dict[str, Any]]] = []
    shutdowns: list[bool] = []
    monkeypatch.setattr(
        otel_export, "get_settings", lambda: _settings(enabled=True, log_content=True)
    )
    monkeypatch.setattr(
        otel_export,
        "_build_emitter",
        lambda endpoint: (
            lambda name, attrs: emitted.append((name, attrs)),
            lambda: shutdowns.append(True),
        ),
    )
    exporter = default_exporter()
    assert exporter is not None
    assert default_exporter() is exporter  # one per process
    exporter.on_decision_record(_record(raw="select 1"))
    assert emitted[0][1]["raw"] == "select 1"  # log_content reached the exporter
    close_default_exporter()
    assert shutdowns == [True]  # close flushes the SDK pipeline


@pytest.mark.skipif(
    importlib.util.find_spec("opentelemetry") is not None,
    reason="OTel SDK installed; the missing-SDK path is unreachable",
)
def test_default_exporter_disables_itself_without_the_sdk(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(otel_export, "get_settings", lambda: _settings(enabled=True))
    with caplog.at_level("WARNING"):
        assert default_exporter() is None
    assert "alkera-cli[otel]" in caplog.text


@pytest.mark.parametrize(
    ("base", "expected"),
    [
        pytest.param(None, None, id="unset-defers-to-otel-env"),
        pytest.param("http://collector:4318", "http://collector:4318/v1/logs", id="base-url"),
        pytest.param(
            "http://collector:4318/", "http://collector:4318/v1/logs", id="trailing-slash"
        ),
        pytest.param(
            "http://collector:4318/v1/logs", "http://collector:4318/v1/logs", id="already-full"
        ),
    ],
)
def test_logs_endpoint_resolution(base: str | None, expected: str | None) -> None:
    assert otel_export._logs_endpoint(base) == expected


def test_real_sdk_pipeline_delivers_events(monkeypatch: pytest.MonkeyPatch) -> None:
    """Runs only where the optional extra is installed (CI after the Linux
    re-lock); pins the lazy SDK wiring against the real OpenTelemetry API."""
    pytest.importorskip("opentelemetry.sdk")
    import opentelemetry.exporter.otlp.proto.http._log_exporter as otlp_module
    from opentelemetry.sdk._logs.export.in_memory_log_exporter import InMemoryLogExporter

    memory = InMemoryLogExporter()
    monkeypatch.setattr(otlp_module, "OTLPLogExporter", lambda *a, **k: memory)
    built = otel_export._build_emitter(None)
    assert built is not None
    emit, shutdown = built
    emit("alkera.tool_decision", {"decision": "allow", "session.id": "s"})
    shutdown()
    records = memory.get_finished_logs()
    assert records
    attributes = dict(records[0].log_record.attributes or {})
    assert attributes["event.name"] == "alkera.tool_decision"
    assert attributes["decision"] == "allow"


# --------------------------------------------------------------------------- #
# runtime feeds: session lifecycle + the persist tap
# --------------------------------------------------------------------------- #


class _FakeFactory(AdapterFactory):
    def __init__(self) -> None:
        super().__init__(binary=None)
        self.adapters: list[FakeAdapter] = []

    def __call__(  # type: ignore[override]
        self,
        config: SessionConfig,
        *,
        bus: EventBus,
        harness_type: str = "agent",
    ) -> FakeAdapter:
        adapter = FakeAdapter()
        adapter._bus = bus
        adapter._harness_type = harness_type
        self.adapters.append(adapter)
        return adapter


class _FailingReporter:
    """An org-audit reporter whose session hooks always explode."""

    def session_started(self, **_kw: Any) -> None:
        raise RuntimeError("reporter down")

    def session_finished(self, **_kw: Any) -> None:
        raise RuntimeError("reporter down")

    def session_cost(self, **_kw: Any) -> None:
        raise RuntimeError("reporter down")


def _capture_exporter() -> tuple[OtelExporter, list[tuple[str, dict[str, Any]]]]:
    signals: list[tuple[str, dict[str, Any]]] = []
    return OtelExporter(emit=lambda name, attrs: signals.append((name, attrs))), signals


@pytest.mark.asyncio
async def test_runtime_feeds_sessions_and_tool_results(tmp_path: Path) -> None:
    exporter, signals = _capture_exporter()
    factory = _FakeFactory()
    runtime = HarnessRuntime(
        ProjectDirectory(tmp_path / "work" / ".alkera"),
        adapter_factory=factory,
        otel_exporter=exporter,
    )
    session = await runtime.open_chat(create=True)
    sid = session.session_id
    await factory.adapters[0].feed(
        PartCreated(event_id="pc1", time=_T, session_id=sid, part=_tool_part())
    )
    await runtime.close_chat(sid)

    names = [name for name, _attrs in signals]
    assert names[0] == "alkera.session_started"
    assert "alkera.tool_result" in names
    assert names[-1] == "alkera.session_finished"
    by_name = dict(signals)
    assert by_name["alkera.session_started"]["project"] == "work"
    assert by_name["alkera.session_started"]["resumed"] is False
    tool = by_name["alkera.tool_result"]
    assert tool["session.id"] == sid
    assert tool["tool_name"] == "bash"
    assert tool["status"] == "completed"
    assert "input" not in tool  # redaction holds through the real pipeline
    finished = by_name["alkera.session_finished"]
    assert finished["queries"] == 0
    assert finished["trace_hash"]  # the pinned trace digest rides OTel too


@pytest.mark.asyncio
async def test_a_failing_reporter_never_starves_the_exporter(tmp_path: Path) -> None:
    exporter, signals = _capture_exporter()
    runtime = HarnessRuntime(
        ProjectDirectory(tmp_path / "work" / ".alkera"),
        adapter_factory=_FakeFactory(),
        audit_reporter=_FailingReporter(),  # type: ignore[arg-type]
        otel_exporter=exporter,
    )
    session = await runtime.open_chat(create=True)
    await runtime.close_chat(session.session_id)
    names = [name for name, _attrs in signals]
    assert "alkera.session_started" in names
    assert "alkera.session_finished" in names
