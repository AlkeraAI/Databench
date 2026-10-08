"""The editor's chat-activity surfaces (Decisions | Cost | Safety) read two
per-chat logs over new daemon RPCs. These tests pin the readers + the
filter/paginate/newest-first behavior with a fake session, no harness subprocess.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from alkera_cli.daemon.methods import harness as h
from alkera_cli.plugins.plugin_base.cost import CostLedger, CostLedgerEntry
from alkera_cli.plugins.plugin_base.permissions.audit import DecisionRecord, DecisionSink
from alkera_core.project.directory import ProjectDirectory


def _fake_session(project: ProjectDirectory, session_id: str) -> SimpleNamespace:
    return SimpleNamespace(project=project, session_id=session_id)


def _seed_decisions(project: ProjectDirectory) -> None:
    # Decisions land in the CHAT directory (the runtime binds its sink to
    # ``chat.path``), one log per conversation.
    sink = DecisionSink(project.chats_path / "s1")
    sink.record(
        DecisionRecord(
            session_id="s1",
            capability="sql",
            effect="read",
            operation="SELECT 1",
            decision="allow",
            decided_by="rule",
            at=1.0,
        )
    )
    sink.record(
        DecisionRecord(
            session_id="s1",
            capability="shell",
            effect="write",
            operation="rm -rf x",
            decision="prompt",
            decided_by="judge",
            reasons=["irreversible"],
            at=2.0,
        )
    )
    DecisionSink(project.chats_path / "other").record(
        DecisionRecord(
            session_id="other", capability="x", decision="allow", decided_by="judge", at=3.0
        )
    )


@pytest.mark.asyncio
async def test_list_decisions_scopes_to_session_newest_first(tmp_path, monkeypatch):
    project = ProjectDirectory(tmp_path / ".alkera")
    _seed_decisions(project)
    monkeypatch.setattr(h, "_require_session", lambda _server, _sid: _fake_session(project, "s1"))

    resp = await h.harness_list_decisions(None, h.HarnessListDecisionsRequest(session_id="s1"))

    # Only this chat's rows, newest-first.
    assert [d.capability for d in resp.decisions] == ["shell", "sql"]
    assert resp.has_more is False
    # The flattened view carries the queryable fields the tabs render.
    assert resp.decisions[0].decided_by == "judge"
    assert resp.decisions[0].reasons == ["irreversible"]


@pytest.mark.asyncio
async def test_list_decisions_decided_by_filter_powers_safety_tab(tmp_path, monkeypatch):
    project = ProjectDirectory(tmp_path / ".alkera")
    _seed_decisions(project)
    monkeypatch.setattr(h, "_require_session", lambda _server, _sid: _fake_session(project, "s1"))

    resp = await h.harness_list_decisions(
        None, h.HarnessListDecisionsRequest(session_id="s1", decided_by="judge")
    )

    assert [d.decided_by for d in resp.decisions] == ["judge"]
    assert [d.operation for d in resp.decisions] == ["rm -rf x"]


@pytest.mark.asyncio
async def test_list_decisions_reads_the_chat_log_not_the_project_root(tmp_path, monkeypatch):
    project = ProjectDirectory(tmp_path / ".alkera")
    # The project-root log is the fallback for decisions made OUTSIDE a chat; a
    # chat's own decisions live in its chat directory. The reader once pointed
    # at the root, so every populated chat rendered an empty Decisions tab.
    DecisionSink(project.path).record(
        DecisionRecord(session_id="s1", operation="root-only", decision="allow", at=1.0)
    )
    DecisionSink(project.chats_path / "s1").record(
        DecisionRecord(session_id="s1", operation="in-chat", decision="allow", at=2.0)
    )
    monkeypatch.setattr(h, "_require_session", lambda _server, _sid: _fake_session(project, "s1"))

    resp = await h.harness_list_decisions(None, h.HarnessListDecisionsRequest(session_id="s1"))

    assert [d.operation for d in resp.decisions] == ["in-chat"]


@pytest.mark.asyncio
async def test_list_decisions_paginates(tmp_path, monkeypatch):
    project = ProjectDirectory(tmp_path / ".alkera")
    sink = DecisionSink(project.chats_path / "s1")
    for i in range(5):
        sink.record(DecisionRecord(session_id="s1", operation=f"op{i}", at=float(i)))
    monkeypatch.setattr(h, "_require_session", lambda _server, _sid: _fake_session(project, "s1"))

    first = await h.harness_list_decisions(
        None, h.HarnessListDecisionsRequest(session_id="s1", limit=2, offset=0)
    )
    assert [d.operation for d in first.decisions] == ["op4", "op3"]
    assert first.has_more is True

    last = await h.harness_list_decisions(
        None, h.HarnessListDecisionsRequest(session_id="s1", limit=2, offset=4)
    )
    assert [d.operation for d in last.decisions] == ["op0"]
    assert last.has_more is False


@pytest.mark.asyncio
async def test_list_cost_ledger_newest_first_with_charged_usd(tmp_path, monkeypatch):
    project = ProjectDirectory(tmp_path / ".alkera")
    ledger = CostLedger(project)
    now = datetime(2026, 6, 12, tzinfo=UTC)
    ledger.record(
        "s1",
        CostLedgerEntry(
            entry_id="e1", connection="snow", operation="SELECT", estimate_usd=0.10, at=1.0
        ),
        now=now,
    )
    ledger.record(
        "s1",
        CostLedgerEntry(
            entry_id="e2",
            connection="snow",
            operation="DROP",
            estimate_usd=0.20,
            actual_usd=0.25,
            at=2.0,
        ),
        now=now,
    )
    monkeypatch.setattr(h, "_require_session", lambda _server, _sid: _fake_session(project, "s1"))

    resp = await h.harness_list_cost_ledger(None, h.HarnessListCostLedgerRequest(session_id="s1"))

    assert [e.entry_id for e in resp.entries] == ["e2", "e1"]
    # charged_usd = settled actual where known, else the estimate.
    assert resp.entries[0].charged_usd == pytest.approx(0.25)
    assert resp.entries[1].charged_usd == pytest.approx(0.10)
    assert resp.has_more is False


def test_cost_ledger_read_entries_is_a_crash_safe_oldest_first_reader(tmp_path):
    project = ProjectDirectory(tmp_path / ".alkera")
    ledger = CostLedger(project)
    now = datetime(2026, 6, 12, tzinfo=UTC)
    ledger.record("s1", CostLedgerEntry(entry_id="a", at=1.0), now=now)
    ledger.record("s1", CostLedgerEntry(entry_id="b", at=2.0), now=now)
    # A torn trailing line (writer killed mid-append) is skipped, not fatal.
    with (project.chats_path / "s1" / "cost_ledger.jsonl").open("a", encoding="utf-8") as fh:
        fh.write('{"entry_id": "tor')

    entries = ledger.read_entries("s1")

    assert [e.entry_id for e in entries] == ["a", "b"]
    assert ledger.read_entries("missing-chat") == []
