"""Budget warnings + per-tool caps + the ``/cost set`` persistence."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from alkera_cli.plugins.plugin_base.cost import (
    BUDGET_WARN_THRESHOLD,
    CostLedger,
    CostLimits,
    budget_warnings,
    cost_overview,
)
from alkera_cli.plugins.plugin_base.permissions.config import update_local_cost_caps
from alkera_core.project.directory import ProjectDirectory

_NOW = datetime(2026, 6, 11, tzinfo=UTC)


# --- budget_warnings --------------------------------------------------------


def test_budget_warnings_fire_in_the_band() -> None:
    limits = CostLimits(chat_usd=100, day_usd=100, week_usd=100)
    # chat at 85% → warn; day at 50% → no; week over 100% → no (that's the gate).
    warns = budget_warnings(limits, chat_spent_usd=85, day_spent_usd=50, week_spent_usd=120)
    windows = [w for w, _ in warns]
    assert windows == ["chat"]


def test_budget_warning_reads_its_amounts_in_the_one_money_reading() -> None:
    # Grouped, half-up from the decimal (1.005 → $1.01, where "%.2f" prints 1.00).
    limits = CostLimits(chat_usd=1250, day_usd=1.25)
    warns = dict(
        budget_warnings(limits, chat_spent_usd=1100.005, day_spent_usd=1.005, week_spent_usd=0)
    )
    assert warns["chat"] == "chat spend is at 88% of the $1,250.00 cap ($1,100.01 used)"
    assert warns["day"] == "day spend is at 80% of the $1.25 cap ($1.01 used)"


def test_budget_warning_threshold_is_80pct() -> None:
    limits = CostLimits(chat_usd=100)
    assert budget_warnings(limits, chat_spent_usd=79, day_spent_usd=0, week_spent_usd=0) == []
    assert budget_warnings(
        limits, chat_spent_usd=BUDGET_WARN_THRESHOLD * 100, day_spent_usd=0, week_spent_usd=0
    )


# --- per-tool / per-connection cap precedence -------------------------------


def test_tool_override_is_tightest() -> None:
    cost = {
        "per_query_usd_cap": 1.0,
        "connection_overrides": {"bq": {"per_query_usd_cap": 5.0}},
        "tool_overrides": {"sql.query": {"per_query_usd_cap": 0.1}},
    }
    # tool override beats connection beats base
    assert CostLimits.from_config(cost, connection="bq", tool="sql.query").per_query_usd == 0.1
    assert CostLimits.from_config(cost, connection="bq").per_query_usd == 5.0
    assert CostLimits.from_config(cost).per_query_usd == 1.0


def test_org_managed_is_sticky_across_overlays() -> None:
    cost = {"tool_overrides": {"sql.query": {"org_managed": True}}}
    assert CostLimits.from_config(cost, tool="sql.query").org_managed is True
    assert CostLimits.from_config(cost).org_managed is False


# --- the ledger dedups warnings per period ----------------------------------


def test_ledger_warns_once_per_period(tmp_path: Path) -> None:
    ledger = CostLedger(ProjectDirectory(tmp_path / ".alkera"))
    limits = CostLimits(per_query_usd=100, chat_usd=10)
    # push the chat window past 80% (within the per-query cap)
    _, entry = ledger.reserve(
        "s", estimate_usd=9.0, limits=limits, now=_NOW, connection="c", operation="query"
    )
    assert entry is not None
    first = ledger.new_budget_warnings("s", limits=limits, now=_NOW)
    assert first and "chat" in first[0]
    # a second query in the same period does NOT re-warn the chat window
    assert ledger.new_budget_warnings("s", limits=limits, now=_NOW) == []


# --- /cost set persistence --------------------------------------------------


def test_update_local_cost_caps_persists(tmp_path: Path) -> None:
    alkera = tmp_path / ".alkera"
    alkera.mkdir()
    update_local_cost_caps(alkera, {"chat": 50.0, "day": 200.0})
    text = (alkera / "permissions.local.yml").read_text()
    assert "chat_usd_cap" in text and "50" in text
    assert "day_usd_cap" in text and "200" in text
    # it round-trips into the limits
    import yaml

    cost = yaml.safe_load((alkera / "permissions.local.yml").read_text())["cost"]
    limits = CostLimits.from_config(cost)
    assert limits.chat_usd == 50.0
    assert limits.day_usd == 200.0


def test_update_local_cost_caps_rejects_unknown_window(tmp_path: Path) -> None:
    alkera = tmp_path / ".alkera"
    alkera.mkdir()
    with pytest.raises(ValueError, match="unknown cost window"):
        update_local_cost_caps(alkera, {"nonsense": 5.0})


def test_update_local_cost_caps_rejects_negative_and_nonfinite(tmp_path: Path) -> None:
    alkera = tmp_path / ".alkera"
    alkera.mkdir()
    with pytest.raises(ValueError, match="finite, non-negative"):
        update_local_cost_caps(alkera, {"chat": -5.0})
    with pytest.raises(ValueError, match="finite, non-negative"):
        update_local_cost_caps(alkera, {"day": float("nan")})
    with pytest.raises(ValueError, match="finite, non-negative"):
        update_local_cost_caps(alkera, {"week": float("inf")})
    # nothing was written on the rejected path
    assert not (alkera / "permissions.local.yml").exists()


def test_cost_overview_shape(tmp_path: Path) -> None:
    ledger = CostLedger(ProjectDirectory(tmp_path / ".alkera"))
    ov = cost_overview(ledger, "s", CostLimits(), now=_NOW)
    assert set(ov["spent"]) == {"chat", "day", "week"}
    assert set(ov["caps"]) == {"per_query", "chat", "day", "week"}
    assert ov["spent"]["chat"] == 0.0


def test_unknown_cost_keys_catches_typos() -> None:
    from alkera_cli.plugins.plugin_base.cost import unknown_cost_keys

    assert unknown_cost_keys(None) == []
    assert unknown_cost_keys({"chat_usd_cap": 5, "org_managed": True}) == []
    # a top-level typo + a typo inside an override are both surfaced
    cost = {
        "chat_usd_cpa": 5,  # typo
        "tool_overrides": {"sql.query": {"per_query_usd_cpa": 1}},  # typo in override
        "connection_overrides": {"bq": {"day_usd_cap": 9}},  # valid
    }
    assert unknown_cost_keys(cost) == [
        "chat_usd_cpa",
        "tool_overrides.sql.query.per_query_usd_cpa",
    ]
