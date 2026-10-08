"""Warehouse spend on a machine that serves several people's chats.

A box keeps one project for every chat it holds, so the day and week spend
totals must belong to the chat's owner, not to the machine: one org's spend must
never show in another org's budget warning, count toward its caps or block its
queries. These drive real ``sql.query`` dispatches through real scoped registry
views over one real ``CostLedger`` (only the warehouse driver and its estimate
are faked) and pin that each owner meters alone, while a person's own unscoped
session keeps the single project total it always had.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from alkera_cli.contracts.tool_types import (
    ActionDescriptor,
    CapToken,
    CostActual,
    CostEstimate,
    QueryResult,
)
from alkera_cli.plugins.plugin_base import CapabilitySet, Connection, Effect, ToolRegistry
from alkera_cli.plugins.plugin_base.capabilities import EstimateSQLCostCapability, RunSQLCapability
from alkera_cli.plugins.plugin_base.cost import CostLedger, CostLedgerEntry
from alkera_cli.plugins.plugin_base.permissions import PermissionsConfig
from alkera_cli.plugins.plugin_base.permissions.audit import DecisionSink
from alkera_cli.plugins.plugin_base.sql_tools import register_sql_tools
from alkera_core.project.directory import ProjectDirectory

OWNER_A = "00000000-0000-4000-8000-00000000000a"
OWNER_B = "00000000-0000-4000-8000-00000000000b"
OWNER_A2 = "00000000-0000-4000-8000-0000000000a2"
#: Each org's warehouse, as the box binds a team record to it.
REC_A, REC_B = "rec-a", "rec-b"


class _RunSQL(RunSQLCapability):
    def __init__(self) -> None:
        self.calls = 0

    async def run(
        self, sql: str, *, effect: Effect, cap_token: CapToken | None, limit: int | None
    ) -> QueryResult:
        self.calls += 1
        return QueryResult(columns=["x"], rows=[[1]], row_count=1)


class _Cost(EstimateSQLCostCapability):
    def __init__(self, usd: float) -> None:
        self._usd = usd

    async def estimate(self, descriptor: ActionDescriptor) -> CostEstimate:
        return CostEstimate(usd_amount=self._usd)

    async def settle(
        self, descriptor: ActionDescriptor, result: QueryResult, estimate: CostEstimate
    ) -> CostActual:
        return CostActual(usd_amount=estimate.usd_amount)


class _Box:
    """One project, one registry build, two orgs' warehouses on it."""

    def __init__(self, tmp_path: Path, *, usd: float = 0.5) -> None:
        self.project = ProjectDirectory(tmp_path / ".alkera")
        self.ledger = CostLedger(self.project)
        self.registry = ToolRegistry(
            self.project.blobs(),
            cost_ledger=self.ledger,
            decision_sink=DecisionSink(self.project.path),
        )
        self.runs: dict[str, _RunSQL] = {}
        for handle, record in (("wh-a", REC_A), ("wh-b", REC_B)):
            conn = Connection(handle=handle, plugin="fake_wh", dialect="snowflake")
            conn._runtime_bindings["team_record_id"] = record
            run = _RunSQL()
            caps = CapabilitySet()
            caps.add(run)
            caps.add(_Cost(usd))
            self.registry.register_connection(conn, capabilities=caps)
            self.runs[handle] = run
        register_sql_tools(self.registry)

    def view(self, record: str, owner: str) -> ToolRegistry:
        return self.registry.restricted(
            frozenset(), connection_ids=frozenset({record}), knowledge_owner=owner
        )

    @staticmethod
    async def query(view: ToolRegistry, handle: str, chat: str) -> dict[str, Any]:
        return await view.dispatch(
            "sql.query",
            {"mode": "sql", "connection": handle, "sql": "SELECT 1"},
            session_id=chat,
            permissions=PermissionsConfig(),
        )


def _charge(ledger: Any, chat: str, usd: float) -> None:
    ledger.record(chat, CostLedgerEntry(estimate_usd=usd), now=datetime.now(UTC))


async def test_another_orgs_spend_never_blocks_a_query(tmp_path: Path) -> None:
    """Org A spends the whole $100 day cap. Org B's next query on the same box
    still runs: before the fix it was refused with a cap A's spend had filled."""
    box = _Box(tmp_path)
    a, b = box.view(REC_A, OWNER_A), box.view(REC_B, OWNER_B)
    _charge(a.cost_ledger, "chat-a-seed", 99.9)

    refused = await box.query(a, "wh-a", "chat-a")
    assert "cost limit" in refused.get("error", ""), refused
    assert "day cap" in refused["error"]

    answered = await box.query(b, "wh-b", "chat-b")
    assert "error" not in answered, answered
    assert box.runs["wh-b"].calls == 1


async def test_a_budget_warning_quotes_only_the_chats_own_owner(tmp_path: Path) -> None:
    """A at 85% of the day cap warns A with its own figure. B's first query reads B's own $0.50, so
    B gets no warning and never sees A's figures."""
    box = _Box(tmp_path)
    a, b = box.view(REC_A, OWNER_A), box.view(REC_B, OWNER_B)
    _charge(a.cost_ledger, "chat-a-seed", 85.0)

    warned = await box.query(a, "wh-a", "chat-a")
    assert any("($85.50 used)" in w for w in warned["cost_warnings"]), warned

    quiet = await box.query(b, "wh-b", "chat-b")
    assert "error" not in quiet, quiet
    assert quiet.get("cost_warnings", []) == []
    assert b.cost_ledger.state(now=datetime.now(UTC)).day_usd == pytest.approx(0.5)
    assert a.cost_ledger.state(now=datetime.now(UTC)).day_usd == pytest.approx(85.5)


async def test_two_members_of_one_org_meter_apart(tmp_path: Path) -> None:
    """Spend is attributed to the chat's owner, the grain the server budgets
    members at: a colleague's spend on the same warehouse is not this owner's."""
    box = _Box(tmp_path)
    first, second = box.view(REC_A, OWNER_A), box.view(REC_A, OWNER_A2)
    _charge(first.cost_ledger, "chat-1-seed", 99.9)
    assert "cost limit" in (await box.query(first, "wh-a", "chat-1")).get("error", "")
    assert "error" not in await box.query(second, "wh-a", "chat-2")


async def test_one_owners_chats_share_their_day_total(tmp_path: Path) -> None:
    """The day cap still binds across an owner's chats: two chats of one owner,
    viewed separately, meter against one total."""
    box = _Box(tmp_path)
    _charge(box.view(REC_A, OWNER_A).cost_ledger, "chat-1", 99.9)
    refused = await box.query(box.view(REC_A, OWNER_A), "wh-a", "chat-2")
    assert "day cap" in refused.get("error", ""), refused


async def test_a_chat_with_no_owner_meters_in_no_ones_total(tmp_path: Path) -> None:
    """A scoped chat the box was not told the owner of meters alone: never in
    a named owner's total, in either direction."""
    box = _Box(tmp_path)
    nobody, a = box.view(REC_A, ""), box.view(REC_A, OWNER_A)
    _charge(a.cost_ledger, "chat-a", 99.9)
    assert "error" not in await box.query(nobody, "wh-a", "chat-x")
    assert a.cost_ledger.state(now=datetime.now(UTC)).day_usd == pytest.approx(99.9)


async def test_a_persons_own_session_keeps_the_single_project_total(tmp_path: Path) -> None:
    """An unscoped registry (a person's own daemon) meters every chat against
    the one ``cost_state.json`` it always had."""
    box = _Box(tmp_path)
    assert box.registry.cost_ledger is box.ledger
    _charge(box.ledger, "chat-1", 99.9)
    refused = await box.query(box.registry, "wh-a", "chat-2")
    assert "day cap" in refused.get("error", ""), refused
    assert box.project.cost_state_path.is_file()
    assert not (box.project.path / "cost_state").exists()


def test_a_principals_ledger_is_one_handle_so_warnings_dedupe(tmp_path: Path) -> None:
    """The view asks for the owner's ledger on every call; it is the same
    handle each time, so a window crossing 80% warns once per period, not on
    every query."""
    ledger = CostLedger(ProjectDirectory(tmp_path / ".alkera"))
    assert ledger.for_principal(OWNER_A) is ledger.for_principal(OWNER_A)
    assert ledger.for_principal(OWNER_A) is not ledger.for_principal(OWNER_B)


@pytest.mark.parametrize(
    "owner",
    [
        pytest.param("../../etc/passwd", id="traversal"),
        pytest.param("a/b", id="separator"),
        pytest.param("", id="empty"),
    ],
)
def test_an_owner_never_reaches_the_state_path(tmp_path: Path, owner: str) -> None:
    project = ProjectDirectory(tmp_path / ".alkera")
    ledger = CostLedger(project).for_principal(owner)
    _charge(ledger, "chat", 1.0)
    files = list((project.path / "cost_state").glob("*.json"))
    assert len(files) == 1
    assert files[0].parent == project.path / "cost_state"
