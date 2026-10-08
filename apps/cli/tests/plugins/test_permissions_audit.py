"""The append-only decisions audit log.

Pins: a record round-trips through the sink; the SQL gate writes its decision
(allow / human-approve / fail-closed) with the right provenance; auto-ALLOW and
auto-REJECT (which never reach a chat event) ARE captured; concurrent appends are
crash-safe; and a torn trailing line is skipped on read.
"""

from __future__ import annotations

import threading
from pathlib import Path

from alkera_cli.plugins.plugin_base.permissions import (
    DecisionRecord,
    DecisionSink,
    classify_command,
    descriptor_from_sql,
    gate_sql_action,
)
from alkera_cli.plugins.plugin_base.permissions.gate import GateBinding


async def _gate(descriptor, *, mode: str, sink: DecisionSink, **handles):  # type: ignore[no-untyped-def]
    binding = GateBinding(decision_sink=sink, **handles)
    return (await gate_sql_action(descriptor, mode=mode, binding=binding)).cap_token


def _sink(tmp_path: Path) -> DecisionSink:
    # The sink takes a directory (a chat dir in production); it creates it + the
    # log + lock on first write.
    return DecisionSink(tmp_path / ".alkera")


def test_record_round_trips(tmp_path: Path) -> None:
    sink = _sink(tmp_path)
    rec = DecisionRecord.from_descriptor(
        classify_command("rm -rf x"),
        decision="reject",
        decided_by="floor",
        mode="bypass",
        session_id="s",
        request_id="r",
    )
    sink.record(rec)
    back = sink.read()
    assert len(back) == 1
    assert back[0].decision == "reject"
    assert back[0].decided_by == "floor"
    assert back[0].capability == "shell"
    assert back[0].effect == "destroy"
    assert back[0].schema_version == DecisionRecord.SCHEMA_VERSION


class _Broker:
    def __init__(self, option: str) -> None:
        self.option = option

    async def resolve(self, request: object) -> str:
        return self.option


async def test_sql_gate_records_human_approval(tmp_path: Path) -> None:
    sink = _sink(tmp_path)
    desc = descriptor_from_sql("INSERT INTO t VALUES (1)", dialect="snowflake", connection="c")
    token = await _gate(
        desc, mode="default", broker=_Broker("allow_once"), sink=sink, session_id="s"
    )
    assert token is not None
    [rec] = sink.read()
    assert rec.source == "sql_gate"
    assert rec.decision == "allow"
    assert rec.decided_by == "human"
    assert rec.capability == "sql"


async def test_sql_gate_records_fail_closed_without_broker(tmp_path: Path) -> None:
    sink = _sink(tmp_path)
    desc = descriptor_from_sql("INSERT INTO t VALUES (1)", dialect="snowflake", connection="c")
    token = await _gate(desc, mode="default", broker=None, sink=sink)
    assert token is None
    [rec] = sink.read()
    assert rec.decision == "reject"
    assert rec.decided_by == "fail_closed"


def test_concurrent_appends_are_crash_safe(tmp_path: Path) -> None:
    sink = _sink(tmp_path)

    def _writer(n: int) -> None:
        for i in range(20):
            sink.record(DecisionRecord(session_id="s", request_id=f"{n}-{i}", decision="allow"))

    threads = [threading.Thread(target=_writer, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(sink.read()) == 80  # every append landed; none corrupted


def test_torn_trailing_line_is_skipped(tmp_path: Path) -> None:
    sink = _sink(tmp_path)
    sink.record(DecisionRecord(session_id="s", request_id="r1", decision="allow"))
    # simulate a writer killed mid-line (no trailing newline, partial JSON)
    with sink._path.open("a", encoding="utf-8") as fh:
        fh.write('{"request_id": "r2", "decisi')
    back = sink.read()
    assert [r.request_id for r in back] == ["r1"]  # the torn tail is dropped


def test_build_context_per_chat_sink_overrides_registry_sink(tmp_path: Path) -> None:
    # The session binding's per-chat sink overrides the registry's project-level
    # fallback, so an in-chat SQL-tool decision lands in the chat's own log.
    from alkera_cli.plugins.plugin_base.tool import ToolRegistry
    from alkera_core.project.directory import ProjectDirectory

    project = ProjectDirectory(tmp_path / ".alkera")
    project_sink = DecisionSink(project.path)
    registry = ToolRegistry(project.blobs(), decision_sink=project_sink)

    # no override → the registry's project sink
    assert registry.build_context().decision_sink is project_sink
    # an override (the per-chat sink) wins
    chat_sink = DecisionSink(tmp_path / "chats" / "abc")
    assert registry.build_context(decision_sink=chat_sink).decision_sink is chat_sink
