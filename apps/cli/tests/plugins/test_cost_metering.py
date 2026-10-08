"""Two-phase metering: estimate → cap-gate → execute → settle.

Exercises ``gate_cost`` / ``settle_cost`` directly AND end-to-end through
``SqlQueryTool`` via ``ToolRegistry.dispatch`` (the resolver chokepoint): a
within-budget query records its settled actual; an over-per-query estimate is
rejected before it runs; a window overage prompts to raise and honors the
broker's answer; a connection with no cost capability meters as free ($0) and
never blocks.
"""

from __future__ import annotations

import asyncio
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
from alkera_cli.plugins.plugin_base import (
    CapabilitySet,
    Connection,
    Effect,
    Environment,
    ToolRegistry,
)
from alkera_cli.plugins.plugin_base.capabilities import EstimateSQLCostCapability, RunSQLCapability
from alkera_cli.plugins.plugin_base.cost import (
    CostDeniedError,
    CostLedger,
    CostLedgerEntry,
    CostLimits,
    CostReservation,
    CostState,
    gate_cost,
    release_cost,
    reserve_or_refuse,
    settle_cost,
)
from alkera_cli.plugins.plugin_base.permissions import PermissionsConfig
from alkera_cli.plugins.plugin_base.permissions.audit import DecisionSink
from alkera_cli.plugins.plugin_base.sql_tools import register_sql_tools
from alkera_core.project.directory import ProjectDirectory

_NOW = datetime(2026, 6, 10, 12, 0, tzinfo=UTC)


# --- fakes -----------------------------------------------------------------


class _FakeRunSQL(RunSQLCapability):
    def __init__(self, result: QueryResult) -> None:
        self._result = result
        self.calls: list[tuple[str, Effect]] = []

    async def run(
        self, sql: str, *, effect: Effect, cap_token: CapToken | None, limit: int | None
    ) -> QueryResult:
        self.calls.append((sql, effect))
        return self._result


class _FakeCost(EstimateSQLCostCapability):
    """An estimate, and a settle that can refine to a different actual."""

    def __init__(self, *, estimate_usd: float, actual_usd: float | None = None) -> None:
        self._estimate = estimate_usd
        self._actual = actual_usd

    async def estimate(self, descriptor: ActionDescriptor) -> CostEstimate:
        return CostEstimate(usd_amount=self._estimate)

    async def settle(
        self, descriptor: ActionDescriptor, result: QueryResult, estimate: CostEstimate
    ) -> CostActual:
        amount = estimate.usd_amount if self._actual is None else self._actual
        return CostActual(usd_amount=amount)


class _Broker:
    def __init__(self, option: str) -> None:
        self._option = option
        self.prompts = 0

    async def resolve(self, request: Any) -> str:
        self.prompts += 1
        return self._option


def _descriptor() -> ActionDescriptor:
    return ActionDescriptor(
        capability="sql", effect=Effect.READ, operation="select", raw="SELECT 1"
    )


# ---------------------------------------------------------------------------
# gate_cost — pre-execution check
# ---------------------------------------------------------------------------


async def test_gate_cost_within_budget_reserves(tmp_path: Path) -> None:
    ledger = CostLedger(ProjectDirectory(tmp_path / ".alkera"))
    res = await gate_cost(
        _descriptor(),
        limits=CostLimits(),
        ledger=ledger,
        session_id="c1",
        cost_capability=_FakeCost(estimate_usd=0.5),
        now=_NOW,
    )
    assert res.estimate.usd_amount == 0.5
    # The reservation already counts toward the windows (atomic check+reserve).
    assert ledger.chat_spent("c1") == pytest.approx(0.5)


async def test_gate_cost_no_capability_is_free(tmp_path: Path) -> None:
    ledger = CostLedger(ProjectDirectory(tmp_path / ".alkera"))
    res = await gate_cost(
        _descriptor(), limits=CostLimits(), ledger=ledger, session_id="c1", now=_NOW
    )
    assert res.estimate.usd_amount == 0.0  # no EstimateSQLCostCapability → $0 (fail-safe)


async def test_gate_cost_per_query_over_cap_raises(tmp_path: Path) -> None:
    ledger = CostLedger(ProjectDirectory(tmp_path / ".alkera"))
    with pytest.raises(CostDeniedError, match="per-query"):
        await gate_cost(
            _descriptor(),
            limits=CostLimits(per_query_usd=1.0),
            ledger=ledger,
            session_id="c1",
            cost_capability=_FakeCost(estimate_usd=2.0),
            now=_NOW,
        )


async def test_gate_cost_window_overage_prompts_and_broker_allows(tmp_path: Path) -> None:
    ledger = CostLedger(ProjectDirectory(tmp_path / ".alkera"))
    ledger.record("c1", CostLedgerEntry(estimate_usd=24.8), now=_NOW)  # near the $25 chat cap
    broker = _Broker("allow_once")
    res = await gate_cost(
        _descriptor(),
        limits=CostLimits(),
        ledger=ledger,
        session_id="c1",
        cost_capability=_FakeCost(estimate_usd=0.5),  # 24.8 + 0.5 > 25
        broker=broker,
        now=_NOW,
    )
    assert res.estimate.usd_amount == 0.5
    assert broker.prompts == 1  # the human was asked to raise the cap
    assert ledger.chat_spent("c1") == pytest.approx(25.3)  # reserved after approval


async def test_gate_cost_window_overage_broker_denies_raises(tmp_path: Path) -> None:
    ledger = CostLedger(ProjectDirectory(tmp_path / ".alkera"))
    ledger.record("c1", CostLedgerEntry(estimate_usd=24.8), now=_NOW)
    broker = _Broker("reject_once")
    with pytest.raises(CostDeniedError, match="chat"):
        await gate_cost(
            _descriptor(),
            limits=CostLimits(),
            ledger=ledger,
            session_id="c1",
            cost_capability=_FakeCost(estimate_usd=0.5),
            broker=broker,
            now=_NOW,
        )
    assert broker.prompts == 1
    assert ledger.chat_spent("c1") == pytest.approx(24.8)  # denied → nothing reserved


# ---------------------------------------------------------------------------
# reserve_or_refuse — the NON-prompting pre-flight for a backgrounded job. The
# money-safety contract: where gate_cost would RAISE (hard cap) or PROMPT (window
# overage), reserve_or_refuse instead returns the refusal REASON and reserves
# NOTHING — a detached job never blocks on, nor silently bypasses, a cost prompt.
# ---------------------------------------------------------------------------


async def test_reserve_or_refuse_within_budget_reserves(tmp_path: Path) -> None:
    ledger = CostLedger(ProjectDirectory(tmp_path / ".alkera"))
    out = await reserve_or_refuse(
        _descriptor(),
        limits=CostLimits(),
        ledger=ledger,
        session_id="c1",
        cost_capability=_FakeCost(estimate_usd=0.5),
        now=_NOW,
    )
    assert isinstance(out, CostReservation)
    assert out.estimate.usd_amount == 0.5
    assert ledger.chat_spent("c1") == pytest.approx(0.5)  # reserved atomically


async def test_reserve_or_refuse_per_query_over_cap_refuses_without_raising(tmp_path: Path) -> None:
    # Where gate_cost RAISES CostDeniedError, reserve_or_refuse returns the reason.
    ledger = CostLedger(ProjectDirectory(tmp_path / ".alkera"))
    out = await reserve_or_refuse(
        _descriptor(),
        limits=CostLimits(per_query_usd=1.0),
        ledger=ledger,
        session_id="c1",
        cost_capability=_FakeCost(estimate_usd=2.0),
        now=_NOW,
    )
    assert isinstance(out, str)
    assert "per-query" in out
    # The money-safety invariant: a refused query reserves NOTHING (no dangling charge).
    assert ledger.chat_spent("c1") == pytest.approx(0.0)


async def test_reserve_or_refuse_window_overage_refuses_without_prompting(tmp_path: Path) -> None:
    # The PROMPT_RAISE case: gate_cost would ask the human. reserve_or_refuse takes
    # NO broker at all — it can't prompt — so a window overage is simply refused.
    ledger = CostLedger(ProjectDirectory(tmp_path / ".alkera"))
    ledger.record("c1", CostLedgerEntry(estimate_usd=24.8), now=_NOW)  # near the $25 chat cap
    out = await reserve_or_refuse(
        _descriptor(),
        limits=CostLimits(),
        ledger=ledger,
        session_id="c1",
        cost_capability=_FakeCost(estimate_usd=0.5),  # 24.8 + 0.5 > 25
        now=_NOW,
    )
    assert isinstance(out, str)
    assert "chat" in out
    # Reserved nothing beyond the pre-existing 24.8 — the would-prompt charge was refused.
    assert ledger.chat_spent("c1") == pytest.approx(24.8)


async def test_reserve_or_refuse_no_capability_is_free(tmp_path: Path) -> None:
    # A $0 / unknown estimate always passes (reserves $0), like gate_cost.
    ledger = CostLedger(ProjectDirectory(tmp_path / ".alkera"))
    out = await reserve_or_refuse(
        _descriptor(), limits=CostLimits(), ledger=ledger, session_id="c1", now=_NOW
    )
    assert isinstance(out, CostReservation)
    assert out.estimate.usd_amount == 0.0


async def test_background_query_cancel_releases_reservation(tmp_path: Path) -> None:
    # MONEY SAFETY: a backgrounded query that is CANCELLED (background_cancel / chat
    # close drain) must RELEASE its reservation. asyncio.CancelledError is a
    # BaseException, not an Exception — so _execute_and_build's release must catch
    # BaseException, else a real metered charge leaks (reserved, never settled/released).
    from alkera_cli.plugins.plugin_base.cost import reserve_or_refuse
    from alkera_cli.plugins.plugin_base.sql_tools import QueryBySql, SqlQueryTool

    ledger = CostLedger(ProjectDirectory(tmp_path / ".alkera"))
    reservation = await reserve_or_refuse(
        _descriptor(),
        limits=CostLimits(),
        ledger=ledger,
        session_id="c1",
        cost_capability=_FakeCost(estimate_usd=0.5),
        now=_NOW,
    )
    assert isinstance(reservation, CostReservation)
    assert ledger.chat_spent("c1") == pytest.approx(0.5)  # the charge is reserved

    gate = asyncio.Event()

    class _SlowRunSQL(RunSQLCapability):
        async def run(
            self, sql: str, *, effect: Effect, cap_token: CapToken | None, limit: int | None
        ) -> QueryResult:
            await gate.wait()  # hang until the task is cancelled
            return QueryResult(columns=[], rows=[], row_count=0)

    registry = ToolRegistry(
        ProjectDirectory(tmp_path / ".alkera").blobs(),
        cost_ledger=ledger,
        decision_sink=DecisionSink(tmp_path / ".alkera"),
    )
    ctx = registry.build_context(session_id="c1")
    task = asyncio.ensure_future(
        SqlQueryTool()._execute_and_build(
            cap=_SlowRunSQL(),
            conn=Connection(handle="db", plugin="postgres"),
            sql="SELECT 1",
            descriptor=_descriptor(),
            spec=QueryBySql(connection="db", sql="SELECT 1"),
            reservation=reservation,
            cost_cap=_FakeCost(estimate_usd=0.5),
            cap_token=None,
            ctx=ctx,
        )
    )
    await asyncio.sleep(0.05)  # let it reach cap.run (awaiting the gate)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # Released on cancellation → the reserved charge is netted back out.
    assert ledger.chat_spent("c1") == pytest.approx(0.0)


async def test_gate_cost_window_overage_no_broker_raises(tmp_path: Path) -> None:
    ledger = CostLedger(ProjectDirectory(tmp_path / ".alkera"))
    ledger.record("c1", CostLedgerEntry(estimate_usd=24.8), now=_NOW)
    with pytest.raises(CostDeniedError):
        await gate_cost(
            _descriptor(),
            limits=CostLimits(),
            ledger=ledger,
            session_id="c1",
            cost_capability=_FakeCost(estimate_usd=0.5),
            broker=None,  # fail-closed: can't prompt → denied
            now=_NOW,
        )


async def test_gate_cost_org_managed_window_rejects_without_prompt(tmp_path: Path) -> None:
    ledger = CostLedger(ProjectDirectory(tmp_path / ".alkera"))
    ledger.record("c1", CostLedgerEntry(estimate_usd=24.8), now=_NOW)
    broker = _Broker("allow_once")
    with pytest.raises(CostDeniedError):
        await gate_cost(
            _descriptor(),
            limits=CostLimits(org_managed=True),
            ledger=ledger,
            session_id="c1",
            cost_capability=_FakeCost(estimate_usd=0.5),
            broker=broker,
            now=_NOW,
        )
    assert broker.prompts == 0  # org-managed → hard reject, never prompts


# ---------------------------------------------------------------------------
# reserve → settle / release reconciliation
# ---------------------------------------------------------------------------


async def _reserve(ledger: CostLedger, *, estimate_usd: float, actual_usd: float | None = None):
    return await gate_cost(
        _descriptor(),
        limits=CostLimits(),
        ledger=ledger,
        session_id="c1",
        connection="wh",
        operation="select",
        cost_capability=_FakeCost(estimate_usd=estimate_usd, actual_usd=actual_usd),
        now=_NOW,
    )


async def test_settle_reconciles_reservation_to_refined_actual(tmp_path: Path) -> None:
    ledger = CostLedger(ProjectDirectory(tmp_path / ".alkera"))
    res = await _reserve(ledger, estimate_usd=0.5, actual_usd=0.3)
    assert ledger.chat_spent("c1") == pytest.approx(0.5)  # reserved at the estimate
    await settle_cost(
        _descriptor(),
        QueryResult(),
        res,
        ledger=ledger,
        session_id="c1",
        cost_capability=_FakeCost(estimate_usd=0.5, actual_usd=0.3),
        now=_NOW,
    )
    assert ledger.chat_spent("c1") == pytest.approx(0.3)  # reconciled down to the actual


async def test_settle_without_capability_keeps_estimate(tmp_path: Path) -> None:
    ledger = CostLedger(ProjectDirectory(tmp_path / ".alkera"))
    res = await _reserve(ledger, estimate_usd=0.7)
    await settle_cost(_descriptor(), QueryResult(), res, ledger=ledger, session_id="c1", now=_NOW)
    assert ledger.chat_spent("c1") == pytest.approx(0.7)  # estimate stands as the actual


async def test_release_refunds_a_failed_reservation(tmp_path: Path) -> None:
    # C11: a failed execution releases the reservation so a query that didn't
    # complete isn't billed — but the charge WAS visible (reserved) first.
    ledger = CostLedger(ProjectDirectory(tmp_path / ".alkera"))
    res = await _reserve(ledger, estimate_usd=0.9)
    assert ledger.chat_spent("c1") == pytest.approx(0.9)
    release_cost(res, ledger=ledger, session_id="c1", now=_NOW)
    assert ledger.chat_spent("c1") == pytest.approx(0.0)  # netted back out
    # day/week state also refunded
    st = ledger.state(now=_NOW)
    assert st.day_usd == pytest.approx(0.0)


def test_with_charge_within_period_nets_a_refund() -> None:
    # A refund inside the SAME window subtracts normally (no spurious clamp): a
    # $10 reserve then a $3 refund leaves $7, not a clamped $0.
    st = CostState().with_charge(10.0, now=_NOW)
    st = st.with_charge(-3.0, now=_NOW)
    assert st.day_usd == pytest.approx(7.0)
    assert st.week_usd == pytest.approx(7.0)


def test_with_charge_cross_period_refund_clamps_to_zero() -> None:
    # The regression: a reservation charged before midnight whose refund lands
    # after midnight must NOT drive the fresh window negative (which would
    # silently inflate the cap for the rest of the new day/week). The rolled
    # window starts at 0; the negative delta clamps there instead of going to -10.
    before = datetime(2026, 6, 10, 23, 59, tzinfo=UTC)
    after = datetime(2026, 6, 11, 0, 1, tzinfo=UTC)  # next calendar day
    charged = CostState().with_charge(10.0, now=before)
    assert charged.day_usd == pytest.approx(10.0)
    refunded = charged.with_charge(-10.0, now=after)  # release after the roll
    assert refunded.day_usd == pytest.approx(0.0), "cross-day refund drove the day window negative"
    assert refunded.week_usd == pytest.approx(0.0)


def test_rolled_never_rolls_a_newer_window_backward() -> None:
    # A writer with a STALE now (a settle that captured its timestamp before the
    # last roll, or clock skew between daemons) must not reset the newer
    # persisted window back to its own older period — that erases every charge
    # the new period accumulated.
    before = datetime(2026, 6, 10, 23, 59, tzinfo=UTC)
    after = datetime(2026, 6, 11, 0, 5, tzinfo=UTC)
    st = CostState().with_charge(20.0, now=after)  # day B holds $20
    stale = st.rolled(now=before)  # a day-A reader/writer arrives late
    assert stale.day_key == st.day_key, "the day window rolled backward"
    assert stale.day_usd == pytest.approx(20.0)


def test_cross_day_refund_with_intervening_spend_skips_the_fresh_window() -> None:
    # The two-chat repro: Q1 reserves $10 at 23:59 (day A); Q2 reserves $20 at
    # 00:05 (the state rolls — day B = $20); Q1's $10 refund then lands. The
    # refund belongs to day A (over), so day B must stay EXACTLY $20 — neither
    # erased to 0 (the backward-roll bug) nor reduced to $10 (the clamp alone).
    # The same ISO week spans both days, so the WEEK nets: 30 - 10 = 20.
    before = datetime(2026, 6, 10, 23, 59, tzinfo=UTC)
    after = datetime(2026, 6, 11, 0, 5, tzinfo=UTC)
    st = CostState().with_charge(10.0, now=before)  # Q1's reserve, day A
    st = st.with_charge(20.0, now=after)  # Q2's reserve rolls to day B
    assert st.day_usd == pytest.approx(20.0)
    assert st.week_usd == pytest.approx(30.0)
    refunded = st.with_charge(-10.0, now=after, charged_at=before)  # Q1 released
    assert refunded.day_usd == pytest.approx(20.0), "day B absorbed day A's refund"
    assert refunded.week_usd == pytest.approx(20.0), "the week did not net the refund"


async def test_ledger_release_with_a_stale_now_cannot_erase_the_new_day(tmp_path: Path) -> None:
    # The full ledger path of the repro above, with the release arriving with a
    # STALE pre-midnight `now` (the worst case: backward roll + wrong window).
    before = datetime(2026, 6, 10, 23, 59, tzinfo=UTC)
    after = datetime(2026, 6, 11, 0, 5, tzinfo=UTC)
    ledger = CostLedger(ProjectDirectory(tmp_path / ".alkera"))
    limits = CostLimits(per_query_usd=50.0)
    _check, q1 = ledger.reserve(
        "c1", estimate_usd=10.0, limits=limits, now=before, connection="wh", operation="q"
    )
    assert q1 is not None
    _check, q2 = ledger.reserve(
        "c2", estimate_usd=20.0, limits=limits, now=after, connection="wh", operation="q"
    )
    assert q2 is not None
    assert ledger.state(now=after).day_usd == pytest.approx(20.0)
    # Q1 fails and releases, carrying its stale pre-midnight timestamp.
    ledger.settle_reservation("c1", q1, actual_usd=0.0, now=before)
    st = ledger.state(now=after)
    assert st.day_usd == pytest.approx(20.0), "a stale release erased day B's spend"
    assert st.week_usd == pytest.approx(20.0)


async def test_release_after_midnight_does_not_inflate_the_daily_cap(tmp_path: Path) -> None:
    # End-to-end of the same bug through release_cost: reserve $10 at 23:59, the
    # query fails and releases at 00:01. The new day must read $0 spent — not
    # -$10, which would let the $100 cap behave like $110 for the rest of day B.
    before = datetime(2026, 6, 10, 23, 59, tzinfo=UTC)
    after = datetime(2026, 6, 11, 0, 1, tzinfo=UTC)
    ledger = CostLedger(ProjectDirectory(tmp_path / ".alkera"))
    # Raise the per-query cap so the $10 reserve is allowed — the daily cap ($100)
    # is the one whose post-midnight integrity this test exercises.
    limits = CostLimits(per_query_usd=50.0)
    res = await gate_cost(
        _descriptor(),
        limits=limits,
        ledger=ledger,
        session_id="c1",
        cost_capability=_FakeCost(estimate_usd=10.0),
        broker=_Broker("allow_once"),
        now=before,
    )
    assert ledger.state(now=before).day_usd == pytest.approx(10.0)
    release_cost(res, ledger=ledger, session_id="c1", now=after)
    assert ledger.state(now=after).day_usd == pytest.approx(0.0)


async def test_reserve_blocks_concurrent_second_query_over_cap(tmp_path: Path) -> None:
    # C2: the gate is check+reserve-atomic, so once query 1 reserves over the
    # chat cap, query 2's gate sees the reservation and is denied — proving the
    # cap holds under concurrency (not just that the ledger write is atomic).
    ledger = CostLedger(ProjectDirectory(tmp_path / ".alkera"))
    ledger.record("c1", CostLedgerEntry(estimate_usd=24.6), now=_NOW)  # near the $25 chat cap
    # Query 1 ($0.5) reserves: 24.6 + 0.5 = 25.1 > 25 → would prompt; approve it.
    first = await gate_cost(
        _descriptor(),
        limits=CostLimits(),
        ledger=ledger,
        session_id="c1",
        cost_capability=_FakeCost(estimate_usd=0.5),
        broker=_Broker("allow_once"),
        now=_NOW,
    )
    assert first.estimate.usd_amount == 0.5
    # Query 2 ($0.5) now sees 25.1 reserved → another overage. With NO broker it
    # is denied (fail-closed) — the cap held; the two didn't both slip through.
    with pytest.raises(CostDeniedError):
        await gate_cost(
            _descriptor(),
            limits=CostLimits(),
            ledger=ledger,
            session_id="c1",
            cost_capability=_FakeCost(estimate_usd=0.5),
            broker=None,
            now=_NOW,
        )


# ---------------------------------------------------------------------------
# end-to-end through SqlQueryTool.dispatch (the resolver chokepoint)
# ---------------------------------------------------------------------------


def _registry(
    tmp_path: Path, *, estimate_usd: float, actual_usd: float | None = None, with_cost: bool = True
) -> tuple[ToolRegistry, CostLedger, _FakeRunSQL]:
    project = ProjectDirectory(tmp_path / ".alkera")
    ledger = CostLedger(project)
    registry = ToolRegistry(
        project.blobs(), cost_ledger=ledger, decision_sink=DecisionSink(project.path)
    )
    conn = Connection(
        handle="wh", plugin="fake_wh", dialect="snowflake", environment=Environment.PROD
    )
    run_cap = _FakeRunSQL(QueryResult(columns=["x"], rows=[[1]], row_count=1))
    caps = CapabilitySet()
    caps.add(run_cap)
    if with_cost:
        caps.add(_FakeCost(estimate_usd=estimate_usd, actual_usd=actual_usd))
    registry.register_connection(conn, capabilities=caps)
    register_sql_tools(registry)
    return registry, ledger, run_cap


async def _dispatch(registry: ToolRegistry, *, broker: Any = None) -> dict[str, Any]:
    return await registry.dispatch(
        "sql.query",
        {"mode": "sql", "connection": "wh", "sql": "SELECT 1"},
        session_id="c1",
        permissions=PermissionsConfig(),
        broker=broker,
    )


async def test_e2e_within_budget_records_actual(tmp_path: Path) -> None:
    registry, ledger, run_cap = _registry(tmp_path, estimate_usd=0.5, actual_usd=0.3)
    out = await _dispatch(registry)
    assert "error" not in out
    assert out["row_count"] == 1
    assert len(run_cap.calls) == 1  # executed
    assert ledger.chat_spent("c1") == pytest.approx(0.3)  # settled actual recorded


async def test_e2e_per_query_over_cap_rejected_before_execute(tmp_path: Path) -> None:
    registry, ledger, run_cap = _registry(tmp_path, estimate_usd=2.0)  # > $1 default
    out = await _dispatch(registry)
    assert "error" in out
    assert "cost limit" in out["error"]
    assert run_cap.calls == []  # never executed
    assert ledger.chat_spent("c1") == 0.0  # nothing recorded


async def test_e2e_free_connection_meters_zero_never_blocks(tmp_path: Path) -> None:
    # No cost capability → estimate $0 → always allowed; a $0 entry is still
    # recorded (metering ran).
    registry, ledger, run_cap = _registry(tmp_path, estimate_usd=0.0, with_cost=False)
    out = await _dispatch(registry)
    assert "error" not in out
    assert len(run_cap.calls) == 1
    assert ledger.chat_spent("c1") == 0.0


async def test_e2e_window_overage_prompts_then_runs_on_allow(tmp_path: Path) -> None:
    registry, ledger, run_cap = _registry(tmp_path, estimate_usd=0.5, actual_usd=0.5)
    ledger.record("c1", CostLedgerEntry(estimate_usd=24.8), now=datetime.now(UTC))
    broker = _Broker("allow_once")
    out = await _dispatch(registry, broker=broker)
    assert "error" not in out
    assert broker.prompts == 1
    assert len(run_cap.calls) == 1


async def test_e2e_window_overage_denied_blocks(tmp_path: Path) -> None:
    registry, ledger, run_cap = _registry(tmp_path, estimate_usd=0.5)
    ledger.record("c1", CostLedgerEntry(estimate_usd=24.8), now=datetime.now(UTC))
    broker = _Broker("reject_once")
    out = await _dispatch(registry, broker=broker)
    assert "error" in out
    assert "cost limit" in out["error"]
    assert run_cap.calls == []


async def test_e2e_cancelled_query_refunds_the_reservation(tmp_path: Path) -> None:
    # A turn cancel / Stop injects asyncio.CancelledError into `await cap.run` AFTER the
    # reservation was booked. CancelledError is a BaseException, NOT an Exception — an
    # `except Exception` refund would be skipped, permanently leaking the reserved estimate
    # into the chat/day/week windows. The refund must still fire, and the cancel must
    # propagate (not be swallowed into an error dict).
    class _CancellingRunSQL(RunSQLCapability):
        async def run(
            self, sql: str, *, effect: Effect, cap_token: CapToken | None, limit: int | None
        ) -> QueryResult:
            raise asyncio.CancelledError

    project = ProjectDirectory(tmp_path / ".alkera")
    ledger = CostLedger(project)
    registry = ToolRegistry(
        project.blobs(), cost_ledger=ledger, decision_sink=DecisionSink(project.path)
    )
    conn = Connection(
        handle="wh", plugin="fake_wh", dialect="snowflake", environment=Environment.PROD
    )
    caps = CapabilitySet()
    caps.add(_CancellingRunSQL())
    caps.add(_FakeCost(estimate_usd=0.9))
    registry.register_connection(conn, capabilities=caps)
    register_sql_tools(registry)

    with pytest.raises(asyncio.CancelledError):
        await registry.dispatch(
            "sql.query",
            {"mode": "sql", "connection": "wh", "sql": "SELECT 1"},
            session_id="c1",
            permissions=PermissionsConfig(),
            broker=None,
        )
    # The reservation was booked at $0.9 then refunded — net zero in the chat ledger AND
    # the day/week windows (the leak this guards against would leave 0.9 stranded).
    assert ledger.chat_spent("c1") == pytest.approx(0.0)
    assert ledger.state(now=datetime.now(UTC)).day_usd == pytest.approx(0.0)


async def test_e2e_statement_timeout_error_carries_a_user_preference_note(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # When the engine cancels a query for hitting the configured statement timeout, the tool
    # returns a note telling the model the limit is a USER-CHANGEABLE Alkera preference (with the
    # current value) — so it raises the limit / shrinks the query instead of blindly retrying —
    # AND the reservation is still refunded (the note wrapping runs after the refund).
    monkeypatch.setenv("ALKERA_SQL_STATEMENT_TIMEOUT_SECONDS", "900")

    class _TimingOutRunSQL(RunSQLCapability):
        async def run(
            self, sql: str, *, effect: Effect, cap_token: CapToken | None, limit: int | None
        ) -> QueryResult:
            raise RuntimeError("canceling statement due to statement timeout")

    project = ProjectDirectory(tmp_path / ".alkera")
    ledger = CostLedger(project)
    registry = ToolRegistry(
        project.blobs(), cost_ledger=ledger, decision_sink=DecisionSink(project.path)
    )
    conn = Connection(
        handle="wh", plugin="fake_wh", dialect="postgres", environment=Environment.PROD
    )
    caps = CapabilitySet()
    caps.add(_TimingOutRunSQL())
    caps.add(_FakeCost(estimate_usd=0.9))
    registry.register_connection(conn, capabilities=caps)
    register_sql_tools(registry)

    out = await registry.dispatch(
        "sql.query",
        {"mode": "sql", "connection": "wh", "sql": "SELECT 1"},
        session_id="c1",
        permissions=PermissionsConfig(),
        broker=None,
    )
    assert "error" in out
    err = out["error"]
    assert "preference" in err.lower()  # the model is told it's user-changeable
    assert "15 minutes" in err  # the current configured value
    assert "Data → Statement timeout" in err  # where to change it
    # The reservation was still refunded despite the wrapped error (refund precedes the note).
    assert ledger.chat_spent("c1") == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Window boundaries, end-to-end through the REAL tool dispatch. The tool grabs
# `datetime.now(UTC)` internally — no `now=` to pin — so freezegun is the only
# way a test can cross midnight through this path. This is the level at which
# the cross-period refund bug lived (a release after the day rolled drove the
# fresh window negative, inflating the cap).
# ---------------------------------------------------------------------------


class _FlakyRunSQL(RunSQLCapability):
    """Raises on the first run (the failed warehouse query), succeeds after.
    ``on_first_run`` lets a test advance frozen time mid-query — simulating the
    long warehouse query that crosses midnight before dying."""

    def __init__(self, on_first_run: Any = None) -> None:
        self.calls = 0
        self._on_first_run = on_first_run

    async def run(
        self, sql: str, *, effect: Effect, cap_token: CapToken | None, limit: int | None
    ) -> QueryResult:
        self.calls += 1
        if self.calls == 1:
            if self._on_first_run is not None:
                self._on_first_run()
            raise RuntimeError("warehouse died mid-query")
        return QueryResult(columns=["x"], rows=[[1]], row_count=1)


def _flaky_registry(
    tmp_path: Path, *, estimate_usd: float, on_first_run: Any = None
) -> tuple[ToolRegistry, CostLedger]:
    project = ProjectDirectory(tmp_path / ".alkera")
    ledger = CostLedger(project)
    registry = ToolRegistry(
        project.blobs(), cost_ledger=ledger, decision_sink=DecisionSink(project.path)
    )
    conn = Connection(
        handle="wh", plugin="fake_wh", dialect="snowflake", environment=Environment.PROD
    )
    caps = CapabilitySet()
    caps.add(_FlakyRunSQL(on_first_run))
    caps.add(_FakeCost(estimate_usd=estimate_usd))
    registry.register_connection(conn, capabilities=caps)
    register_sql_tools(registry)
    return registry, ledger


async def test_e2e_failed_query_released_across_midnight_keeps_day_b_clean(
    tmp_path: Path,
) -> None:
    # A LONG query: reserved at 23:59 on day A, it runs across midnight and dies
    # at 00:01 — so the release itself happens on day B (the worst case: the
    # refund belongs to a window that is already over). Day B must start from $0
    # — a leaked negative refund would read as -0.9 (cap inflation), a leaked
    # charge as +0.9 (cap deflation), and a stale-timestamped release writing
    # the window BACKWARD would erase day B entirely. The successful day-B query
    # then books EXACTLY its own $0.9 against the fresh window.
    from alkera_cli.plugins.plugin_base.wire import is_tool_error_result
    from freezegun import freeze_time

    with freeze_time("2026-06-10 23:59:00", real_asyncio=True) as frozen:
        registry, ledger = _flaky_registry(
            tmp_path,
            estimate_usd=0.9,
            on_first_run=lambda: frozen.move_to("2026-06-11 00:01:00"),
        )
        out_a = await _dispatch(registry)  # reserved 23:59, dies + releases at 00:01
        # The engine failure reaches the model as a flagged tool error carrying the
        # driver's own message — never a raw exception crashing the turn. The release
        # runs BEFORE the error is shaped, so the refund math below is unaffected.
        assert is_tool_error_result(out_a)
        assert "warehouse died mid-query" in out_a["error"]

        out_b = await _dispatch(registry)
        assert "error" not in out_b  # day B runs normally

        st = ledger.state(now=datetime.now(UTC))
        assert st.day_usd == pytest.approx(0.9), "day B inherited day A's refund/charge"
        assert st.week_usd == pytest.approx(0.9)  # same ISO week: refund netted, charge stands
        assert ledger.chat_spent("c1") == pytest.approx(0.9)  # failed query not billed


async def test_e2e_budget_warning_rearms_when_the_day_rolls(tmp_path: Path) -> None:
    # The 80% day-cap warning fires ONCE per period: a second day-A query stays
    # quiet (deduped), but after midnight the dedup key (the day key) changes —
    # crossing 80% on day B must warn AGAIN, not stay silenced forever.
    from freezegun import freeze_time

    with freeze_time("2026-06-10 12:00:00", real_asyncio=True) as frozen:
        registry, ledger, _run = _registry(tmp_path, estimate_usd=0.5, actual_usd=0.5)
        # Seed the day window (under a DIFFERENT chat so c1's chat cap is idle)
        # to just under 80% of the $100 day cap; c1's query crosses the band.
        ledger.record("seed-a", CostLedgerEntry(estimate_usd=79.8), now=datetime.now(UTC))
        out1 = await _dispatch(registry)
        assert any("day" in w for w in out1["cost_warnings"]), out1["cost_warnings"]
        out2 = await _dispatch(registry)
        assert not any("day" in w for w in out2["cost_warnings"])  # deduped within day A

        frozen.move_to("2026-06-11 12:00:00")
        ledger.record("seed-b", CostLedgerEntry(estimate_usd=79.8), now=datetime.now(UTC))
        out3 = await _dispatch(registry)
        assert any("day" in w for w in out3["cost_warnings"]), (
            "the day warning never re-armed after the window rolled"
        )


# ---------------------------------------------------------------------------
# Hostile/buggy NUMBERS at the cost boundary. The caps exist to bound plugin
# code, so plugin-supplied floats (and the hand-editable permissions.yml) are
# untrusted input: NaN makes every `> cap` comparison False (silent uncap) and
# poisons the unclamped chat ledger sum; negatives inflate the chat cap.
# ---------------------------------------------------------------------------


def test_nan_cap_in_config_falls_back_to_the_default() -> None:
    limits = CostLimits.from_config({"chat_usd_cap": float("nan"), "day_usd_cap": -5})
    assert limits.chat_usd == pytest.approx(25.0), "a NaN cap silently uncapped the window"
    assert limits.day_usd == pytest.approx(100.0), "a negative cap was not rejected"
    # An explicit `inf` is a deliberate, well-ordered "no limit" — it stands.
    assert CostLimits.from_config({"week_usd_cap": float("inf")}).week_usd == float("inf")
    # Garbage types fall back rather than raise (the file is hand-editable).
    assert CostLimits.from_config({"chat_usd_cap": "lots"}).chat_usd == pytest.approx(25.0)


async def test_nan_cap_does_not_uncap_enforcement(tmp_path: Path) -> None:
    # End-to-end: with a NaN chat cap and $1e6 already spent, a query must STILL
    # be gated by the (default) chat cap — not sail through an uncapped window.
    ledger = CostLedger(ProjectDirectory(tmp_path / ".alkera"))
    ledger.record("c1", CostLedgerEntry(estimate_usd=1_000_000.0), now=_NOW)
    limits = CostLimits.from_config({"chat_usd_cap": float("nan")})
    with pytest.raises(CostDeniedError):
        await gate_cost(
            _descriptor(),
            limits=limits,
            ledger=ledger,
            session_id="c1",
            cost_capability=_FakeCost(estimate_usd=0.5),
            now=_NOW,
        )


@pytest.mark.parametrize("bad", [float("nan"), -50.0], ids=["nan", "negative"])
async def test_hostile_estimate_is_sanitized_to_zero(tmp_path: Path, bad: float) -> None:
    # A connector estimate() returning NaN/negative must not slip the caps or
    # enter the ledger raw — it degrades to the documented $0/unknown semantic.
    ledger = CostLedger(ProjectDirectory(tmp_path / ".alkera"))
    res = await gate_cost(
        _descriptor(),
        limits=CostLimits(),
        ledger=ledger,
        session_id="c1",
        cost_capability=_FakeCost(estimate_usd=bad),
        now=_NOW,
    )
    assert res.entry is not None
    assert res.entry.estimate_usd == 0.0
    assert ledger.chat_spent("c1") == 0.0  # not negative, not NaN


async def test_negative_settle_actual_cannot_drive_chat_spent_negative(tmp_path: Path) -> None:
    # A settle() returning a negative actual would refund MORE than was reserved
    # (chat cap inflation); it clamps to $0. A NaN actual keeps the estimate.
    ledger = CostLedger(ProjectDirectory(tmp_path / ".alkera"))
    res = await _reserve(ledger, estimate_usd=0.5, actual_usd=-10.0)
    await settle_cost(
        _descriptor(),
        QueryResult(),
        res,
        ledger=ledger,
        session_id="c1",
        cost_capability=_FakeCost(estimate_usd=0.5, actual_usd=-10.0),
        now=_NOW,
    )
    assert ledger.chat_spent("c1") == pytest.approx(0.0)  # clamped, not -10

    res2 = await _reserve(ledger, estimate_usd=0.7, actual_usd=float("nan"))
    await settle_cost(
        _descriptor(),
        QueryResult(),
        res2,
        ledger=ledger,
        session_id="c1",
        cost_capability=_FakeCost(estimate_usd=0.7, actual_usd=float("nan")),
        now=_NOW,
    )
    # NaN actual → the estimate stands; the chat sum stays a real number.
    assert ledger.chat_spent("c1") == pytest.approx(0.7)


# ---------------------------------------------------------------------------
# CapToken vs the cost prompt. The token is minted at the PERMISSION gate, but
# the cost gate can then block on a human prompt for longer than the 5-minute
# TTL — the chokepoint restarts the TTL after its last blocking step so a
# doubly-approved action isn't refused by the connector.
# ---------------------------------------------------------------------------


def test_cap_token_refreshed_restarts_the_ttl() -> None:
    import time as _time
    from datetime import timedelta

    from freezegun import freeze_time

    d = _descriptor()
    with freeze_time("2026-06-10 12:00:00") as frozen:
        tok = CapToken.mint(d)  # 300s TTL
        frozen.tick(timedelta(minutes=6))  # the human deliberates past the TTL
        assert not tok.authorizes(d), "the un-refreshed token should have expired"
        fresh = tok.refreshed()
        assert fresh.authorizes(d)
        assert fresh.descriptor_fingerprint == tok.descriptor_fingerprint  # same action
        assert fresh.expires_at == pytest.approx(_time.time() + 300.0)


class _TokenCheckingRunSQL(RunSQLCapability):
    """Refuses a mutation whose token is missing or expired — the real connector
    contract (duckdb/snowflake/bigquery all enforce it); the plain fake skips the
    check and would mask an expired token."""

    def __init__(self) -> None:
        self.ran = 0

    async def run(
        self, sql: str, *, effect: Effect, cap_token: CapToken | None, limit: int | None
    ) -> QueryResult:
        import time as _time

        if effect != Effect.READ:
            if cap_token is None or _time.time() >= cap_token.expires_at:
                raise RuntimeError("cap token expired before execution")
        self.ran += 1
        return QueryResult(columns=["x"], rows=[[1]], row_count=1)


async def test_cap_token_survives_a_slow_cost_prompt(tmp_path: Path) -> None:
    # A write needs TWO human approvals: the permission prompt, then a cost
    # overage prompt. The human deliberates 6 minutes on EACH (frozen-clock
    # ticks inside the broker). Without the pre-execution refresh, the token
    # minted after the first prompt is dead by the time the second resolves —
    # and the connector would refuse an action the human just approved twice.
    from datetime import timedelta

    from freezegun import freeze_time

    with freeze_time("2026-06-10 12:00:00", real_asyncio=True) as frozen:
        project = ProjectDirectory(tmp_path / ".alkera")
        ledger = CostLedger(project)
        registry = ToolRegistry(
            project.blobs(), cost_ledger=ledger, decision_sink=DecisionSink(project.path)
        )
        conn = Connection(
            handle="wh", plugin="fake_wh", dialect="snowflake", environment=Environment.PROD
        )
        run_cap = _TokenCheckingRunSQL()
        caps = CapabilitySet()
        caps.add(run_cap)
        caps.add(_FakeCost(estimate_usd=0.5))
        registry.register_connection(conn, capabilities=caps)
        register_sql_tools(registry)
        # Near the $25 chat cap so the cost gate prompts after the permission gate.
        ledger.record("c1", CostLedgerEntry(estimate_usd=24.8), now=datetime.now(UTC))

        class _SlowHuman:
            prompts = 0

            async def resolve(self, request: Any) -> str:
                type(self).prompts += 1
                frozen.tick(timedelta(minutes=6))  # deliberation past the token TTL
                return "allow_once"

        out = await registry.dispatch(
            "sql.query",
            {"mode": "sql", "connection": "wh", "sql": "INSERT INTO t VALUES (1)"},
            session_id="c1",
            permissions=PermissionsConfig(),
            broker=_SlowHuman(),
        )
        assert "error" not in out, out.get("error")
        assert _SlowHuman.prompts == 2  # permission + cost-raise
        assert run_cap.ran == 1, "the doubly-approved write never executed"
