"""Cost metering, the CLI-side money path.

The cost gate is a money path, so this is exhaustive, not happy-path: the
``from_config`` overlay precedence, the ``check_cost`` decision matrix across
every window + the org-managed branch + the boundary (``>`` not ``>=``), the
self-resetting day/week buckets, and the on-disk ledger (per-chat isolation,
cross-chat day/week rollup, rollover, crash-safe reads, concurrent records).
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alkera_cli.plugins.plugin_base.cost import (
    DEFAULT_CHAT_USD,
    DEFAULT_DAY_USD,
    DEFAULT_PER_QUERY_USD,
    DEFAULT_WEEK_USD,
    CostDecision,
    CostLedger,
    CostLedgerEntry,
    CostLimits,
    CostState,
    check_cost,
)
from alkera_core.project.directory import ProjectDirectory


def _ledger(tmp_path: Path) -> CostLedger:
    return CostLedger(ProjectDirectory(tmp_path / ".alkera"))


# ---------------------------------------------------------------------------
# CostLimits.from_config — the permissions.yml ``cost:`` overlay
# ---------------------------------------------------------------------------


def test_limits_defaults_when_empty() -> None:
    for cost in (None, {}):
        lim = CostLimits.from_config(cost)
        assert lim.per_query_usd == DEFAULT_PER_QUERY_USD
        assert lim.chat_usd == DEFAULT_CHAT_USD
        assert lim.day_usd == DEFAULT_DAY_USD
        assert lim.week_usd == DEFAULT_WEEK_USD
        assert lim.org_managed is False


def test_limits_partial_override_keeps_other_defaults() -> None:
    lim = CostLimits.from_config({"per_query_usd_cap": 5.0, "week_usd_cap": 1000.0})
    assert lim.per_query_usd == 5.0
    assert lim.week_usd == 1000.0
    # Untouched keys keep their defaults.
    assert lim.chat_usd == DEFAULT_CHAT_USD
    assert lim.day_usd == DEFAULT_DAY_USD


def test_limits_org_managed_flag() -> None:
    assert CostLimits.from_config({"org_managed": True}).org_managed is True


def test_limits_connection_override_shadows_base() -> None:
    cost = {
        "per_query_usd_cap": 1.0,
        "chat_usd_cap": 25.0,
        "connection_overrides": {
            "snow_prod": {"per_query_usd_cap": 10.0, "org_managed": True},
        },
    }
    # The targeted connection picks up the override; base keys still apply.
    prod = CostLimits.from_config(cost, connection="snow_prod")
    assert prod.per_query_usd == 10.0  # overridden
    assert prod.chat_usd == 25.0  # from base
    assert prod.org_managed is True  # override flag

    # A different / no connection uses the base only.
    other = CostLimits.from_config(cost, connection="duck_local")
    assert other.per_query_usd == 1.0
    assert other.org_managed is False
    assert CostLimits.from_config(cost).per_query_usd == 1.0


# ---------------------------------------------------------------------------
# check_cost — the decision matrix
# ---------------------------------------------------------------------------

_LIMITS = CostLimits(per_query_usd=1.0, chat_usd=25.0, day_usd=100.0, week_usd=400.0)


@pytest.mark.parametrize("estimate", [0.0, -1.0, -0.0001])
def test_nonpositive_estimate_always_allows(estimate: float) -> None:
    # Unknown / free / $0 never blocks — even when every window is already maxed.
    check = check_cost(
        estimate, limits=_LIMITS, chat_spent_usd=1e9, day_spent_usd=1e9, week_spent_usd=1e9
    )
    assert check.decision is CostDecision.ALLOW


def test_per_query_over_is_hard_reject() -> None:
    check = check_cost(1.5, limits=_LIMITS)
    assert check.decision is CostDecision.REJECT
    assert check.window == "per_query"
    assert check.cap_usd == 1.0


def test_per_query_over_rejects_even_when_not_org_managed() -> None:
    # The per-query cap is never "raisable" — it's a hard reject regardless.
    assert CostLimits(per_query_usd=1.0).org_managed is False
    assert check_cost(2.0, limits=CostLimits(per_query_usd=1.0)).decision is CostDecision.REJECT


def test_per_query_exactly_at_cap_allows() -> None:
    # Boundary: the cap is ``>``, not ``>=`` — exactly at the cap is fine.
    assert check_cost(1.0, limits=_LIMITS).decision is CostDecision.ALLOW


@pytest.mark.parametrize(
    ("chat", "day", "week", "expected_window"),
    [
        (24.9, 0.0, 0.0, "chat"),  # chat would tip over 25
        (0.0, 99.9, 0.0, "day"),  # day would tip over 100
        (0.0, 0.0, 399.9, "week"),  # week would tip over 400
    ],
)
def test_window_overage_prompts_to_raise(
    chat: float, day: float, week: float, expected_window: str
) -> None:
    check = check_cost(
        0.5, limits=_LIMITS, chat_spent_usd=chat, day_spent_usd=day, week_spent_usd=week
    )
    assert check.decision is CostDecision.PROMPT_RAISE
    assert check.window == expected_window
    assert check.projected_usd > check.cap_usd


def test_window_check_order_chat_before_day_before_week() -> None:
    # All three would be over; chat is reported first (it's the tightest scope).
    check = check_cost(
        0.5, limits=_LIMITS, chat_spent_usd=25.0, day_spent_usd=100.0, week_spent_usd=400.0
    )
    assert check.window == "chat"


def test_window_overage_rejects_when_org_managed() -> None:
    lim = CostLimits(chat_usd=25.0, org_managed=True)
    check = check_cost(0.5, limits=lim, chat_spent_usd=24.9)
    assert check.decision is CostDecision.REJECT
    assert check.window == "chat"


def test_projected_exactly_at_cap_allows() -> None:
    # spent + estimate == cap is allowed (boundary is strict ``>``). A per-query
    # cap high enough that the $5 estimate clears it isolates the window boundary.
    lim = CostLimits(per_query_usd=10.0, chat_usd=25.0, day_usd=100.0, week_usd=400.0)
    check = check_cost(5.0, limits=lim, chat_spent_usd=20.0, day_spent_usd=0.0, week_spent_usd=0.0)
    assert check.decision is CostDecision.ALLOW


def test_within_all_windows_allows() -> None:
    check = check_cost(
        0.5, limits=_LIMITS, chat_spent_usd=1.0, day_spent_usd=2.0, week_spent_usd=3.0
    )
    assert check.decision is CostDecision.ALLOW
    assert check.window is None


# ---------------------------------------------------------------------------
# CostLedgerEntry.charged_usd
# ---------------------------------------------------------------------------


def test_charged_usd_prefers_actual_then_estimate() -> None:
    assert CostLedgerEntry(estimate_usd=0.5).charged_usd == 0.5  # unsettled → estimate
    assert CostLedgerEntry(estimate_usd=0.5, actual_usd=0.3).charged_usd == 0.3  # settled
    # Settled to exactly zero must still read as 0.0, not fall back to estimate.
    assert CostLedgerEntry(estimate_usd=0.5, actual_usd=0.0).charged_usd == 0.0


# ---------------------------------------------------------------------------
# CostState — self-resetting day/week buckets
# ---------------------------------------------------------------------------

_MON = datetime(2026, 6, 8, 12, 0, tzinfo=UTC)  # Monday, ISO week 24
_TUE = datetime(2026, 6, 9, 9, 0, tzinfo=UTC)  # next day, same ISO week
_NEXT_MON = datetime(2026, 6, 15, 1, 0, tzinfo=UTC)  # next week (week 25)


def test_state_rolled_resets_day_keeps_week_within_same_week() -> None:
    st = CostState(day_key="2026-06-08", day_usd=10.0, week_key="2026-W24", week_usd=10.0)
    rolled = st.rolled(now=_TUE)
    assert rolled.day_usd == 0.0  # new calendar day → day bucket zeroed
    assert rolled.day_key == "2026-06-09"
    assert rolled.week_usd == 10.0  # same ISO week → week bucket survives
    assert rolled.week_key == "2026-W24"


def test_state_rolled_resets_both_on_new_week() -> None:
    st = CostState(day_key="2026-06-09", day_usd=10.0, week_key="2026-W24", week_usd=80.0)
    rolled = st.rolled(now=_NEXT_MON)
    assert rolled.day_usd == 0.0
    assert rolled.week_usd == 0.0
    assert rolled.week_key == "2026-W25"


def test_state_rolled_is_pure() -> None:
    st = CostState(day_key="2026-06-08", day_usd=10.0, week_key="2026-W24", week_usd=10.0)
    st.rolled(now=_NEXT_MON)
    assert st.day_usd == 10.0 and st.week_usd == 10.0  # original untouched


def test_state_with_charge_rolls_then_adds_to_both_windows() -> None:
    st = CostState()  # empty keys → first charge rolls into the current window
    charged = st.with_charge(3.0, now=_MON)
    assert charged.day_usd == 3.0
    assert charged.week_usd == 3.0
    assert charged.day_key == "2026-06-08"
    assert charged.week_key == "2026-W24"
    # A second charge the same day accumulates.
    again = charged.with_charge(2.0, now=_MON)
    assert again.day_usd == 5.0 and again.week_usd == 5.0
    # A charge a new day resets day but adds to the surviving week.
    nextday = again.with_charge(1.0, now=_TUE)
    assert nextday.day_usd == 1.0
    assert nextday.week_usd == 6.0


# ---------------------------------------------------------------------------
# CostLedger — the on-disk store
# ---------------------------------------------------------------------------


def _entry(usd: float, *, actual: float | None = None, eid: str = "e") -> CostLedgerEntry:
    return CostLedgerEntry(entry_id=eid, estimate_usd=usd, actual_usd=actual)


def test_ledger_chat_spent_sums_entries(tmp_path: Path) -> None:
    led = _ledger(tmp_path)
    led.record("chatA", _entry(0.5, eid="1"), now=_MON)
    led.record("chatA", _entry(0.5, actual=0.3, eid="2"), now=_MON)  # settled lower
    assert led.chat_spent("chatA") == pytest.approx(0.8)  # 0.5 + 0.3


def test_ledger_chats_are_isolated(tmp_path: Path) -> None:
    led = _ledger(tmp_path)
    led.record("chatA", _entry(1.0), now=_MON)
    led.record("chatB", _entry(2.0), now=_MON)
    assert led.chat_spent("chatA") == pytest.approx(1.0)
    assert led.chat_spent("chatB") == pytest.approx(2.0)
    assert led.chat_spent("never-seen") == 0.0  # missing ledger → 0


def test_ledger_day_week_accumulate_across_chats(tmp_path: Path) -> None:
    led = _ledger(tmp_path)
    led.record("chatA", _entry(1.0), now=_MON)
    led.record("chatB", _entry(2.0), now=_MON)
    chat, day, week = led.windows("chatA", now=_MON)
    assert chat == pytest.approx(1.0)  # chat-scoped
    assert day == pytest.approx(3.0)  # cross-chat
    assert week == pytest.approx(3.0)


def test_ledger_day_rolls_over_week_survives(tmp_path: Path) -> None:
    led = _ledger(tmp_path)
    led.record("c", _entry(5.0), now=_MON)
    led.record("c", _entry(7.0), now=_TUE)  # next day, same week
    st = led.state(now=_TUE)
    assert st.day_usd == pytest.approx(7.0)  # only the new day's charge
    assert st.week_usd == pytest.approx(12.0)  # both, same week


def test_ledger_reading_state_in_a_new_day_zeroes_day(tmp_path: Path) -> None:
    led = _ledger(tmp_path)
    led.record("c", _entry(9.0), now=_MON)
    # Querying the next day rolls the day bucket even without a new charge.
    _, day, week = led.windows("c", now=_TUE)
    assert day == 0.0
    assert week == pytest.approx(9.0)


def test_ledger_corrupt_state_file_starts_fresh(tmp_path: Path) -> None:
    led = _ledger(tmp_path)
    led._state_path.write_text("{not valid json")
    # A corrupt rollup must not wedge the gate — it reads as empty.
    assert led.state(now=_MON).day_usd == 0.0
    led.record("c", _entry(1.0), now=_MON)
    assert led.state(now=_MON).day_usd == pytest.approx(1.0)


def test_ledger_skips_corrupt_ledger_row(tmp_path: Path) -> None:
    led = _ledger(tmp_path)
    led.record("c", _entry(1.0), now=_MON)
    # Append a junk line directly; chat_spent skips it, keeps the valid charge.
    path = led._ledger_path("c")
    with path.open("a") as fh:
        fh.write("{garbage\n")
    led.record("c", _entry(2.0), now=_MON)
    assert led.chat_spent("c") == pytest.approx(3.0)


def test_ledger_concurrent_records_no_lost_updates(tmp_path: Path) -> None:
    # Concurrent record()s — as the daemon issues from its asyncio.to_thread pool
    # — must serialize correctly: every charge lands in both the chat ledger and
    # the day/week state, none clobbered. This used to flake (and could 60s-hang)
    # via two FileLock races (instance state stomped across threads → orphaned
    # lock file; a vanished holder file mistaken for stale → two holders, lost
    # state updates), both since fixed in FileLock itself (see
    # alkera_core.project.locking + packages/api-core/tests/project/test_locking.py). CostLedger
    # additionally queues threads on an in-process mutex so in-process waiters
    # block fairly instead of poll-spinning the 2s acquisition timeout, and a
    # genuine failure surfaces loudly via `errors`.
    led = _ledger(tmp_path)
    n = 25
    errors: list[BaseException] = []

    def worker(i: int) -> None:
        try:
            led.record("c", _entry(1.0, eid=str(i)), now=_MON)
        except BaseException as exc:  # surface ANY worker failure loudly
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # A failed record() fails the test with its real reason, never a silent drop.
    assert not errors, f"record() raised under contention: {errors!r}"
    # Every charge landed in BOTH the chat ledger and the locked day/week state.
    assert led.chat_spent("c") == pytest.approx(float(n))
    assert led.state(now=_MON).day_usd == pytest.approx(float(n))
