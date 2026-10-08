"""Every harness-runtime and plugin bound an operator can now move.

Two claims per knob. The table below claims the variable is *read* — a constant
that quietly collapses back to a literal, or whose variable is renamed on one
side only, fails it. The behaviour tests below the table claim the constant is
*used*: each one drives the real call site with a value only the environment
could have put there, so a call site that stops consulting its setting fails
even while the constant still reads it.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import sys
from collections.abc import Iterator
from datetime import UTC, datetime
from types import ModuleType

import pytest
from alkera_cli.host.limits import env_fraction

#: A module whose constants are bound from another module's — rereading one
#: alone would read the cached copy and prove nothing. Named here so a knob's
#: row stays a plain module name.
_REREAD_CHAIN = {
    "alkera_cli.plugins.plugin_base.bash_exec": ("alkera_cli.plugins.plugin_base.bash_ids",),
    "alkera_cli.harness.safety_judge": ("alkera_cli.harness.gateway_completion",),
}


@contextlib.contextmanager
def _fresh(module_name: str) -> Iterator[ModuleType]:
    """``module_name`` re-executed as a fresh interpreter would bind it.

    The fresh module (and anything it takes its own constants from) is installed
    in ``sys.modules`` for the duration — dataclass and typing machinery resolves
    annotations through it — and the previously imported objects are put back
    afterwards, so no other test sees the substitution.
    """
    names = (*_REREAD_CHAIN.get(module_name, ()), module_name)
    saved = {name: sys.modules.get(name) for name in names}
    try:
        module: ModuleType | None = None
        for name in names:
            spec = importlib.util.find_spec(name)
            assert spec is not None and spec.loader is not None
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            spec.loader.exec_module(module)
        assert module is not None
        yield module
    finally:
        for name, previous in saved.items():
            if previous is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous


def _constant(module_name: str, constant: str) -> object:
    with _fresh(module_name) as module:
        return getattr(module, constant)


# ---------------------------------------------------------------------------
# The parser the fractions use (the two number parsers are pinned next door in
# test_harness_limits.py)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param(None, 0.9, id="unset-is-the-default"),
        pytest.param("  ", 0.9, id="blank-is-the-default"),
        pytest.param("most", 0.9, id="junk-is-the-default"),
        pytest.param("0.5", 0.5, id="a-share-is-the-share"),
        pytest.param("1", 1.0, id="the-whole-is-allowed"),
        pytest.param("0", 0.9, id="zero-is-refused-not-unbounded"),
        pytest.param("-0.2", 0.9, id="negative-is-refused"),
        pytest.param("1.5", 0.9, id="past-the-whole-is-refused"),
        pytest.param("90", 0.9, id="a-percentage-is-not-a-fraction"),
    ],
)
def test_env_fraction(raw: str | None, expected: float) -> None:
    """A share of a window has no "no bound" reading, so every value outside
    ``(0, 1]`` leaves the shipped behaviour standing."""
    assert env_fraction(raw, default=0.9) == expected


# ---------------------------------------------------------------------------
# Every knob: default, moved, refused
# ---------------------------------------------------------------------------

#: ``(module, constant, env var, raw, moved value, shipped default, on-zero)``.
#: ``on_zero`` is what a deployment gets when it types ``0``: ``None`` for a bound
#: that can be removed outright, and the shipped default for the ones that hold
#: memory or a model's context in place -- "unbounded" is not a reading those can
#: honour, so nothing an operator types may make them so.
_KNOBS = [
    # --- the agent process ------------------------------------------------
    pytest.param(
        "alkera_cli.harness.adapters.opencode_http",
        "LISTEN_TIMEOUT_S",
        "ALKERA_OPENCODE_LISTEN_TIMEOUT_SECONDS",
        "45",
        45.0,
        300.0,
        float("inf"),
        id="agent-bind-deadline",
    ),
    pytest.param(
        "alkera_cli.harness.adapters.opencode_http",
        "START_ATTEMPTS",
        "ALKERA_OPENCODE_START_ATTEMPTS",
        "6",
        6,
        3,
        None,
        id="agent-start-attempts",
    ),
    pytest.param(
        "alkera_cli.harness.adapters.opencode_http",
        "READY_TIMEOUT_SECONDS",
        "ALKERA_OPENCODE_READY_TIMEOUT_SECONDS",
        "20",
        20.0,
        5.0,
        None,
        id="agent-first-answer-window",
    ),
    pytest.param(
        "alkera_cli.harness.adapters.opencode_http",
        "_CONNECT_TIMEOUT_SECONDS",
        "ALKERA_OPENCODE_CONNECT_TIMEOUT_SECONDS",
        "12",
        12.0,
        5.0,
        None,
        id="agent-connect-budget",
    ),
    pytest.param(
        "alkera_cli.harness.adapters.opencode_http",
        "_WRITE_TIMEOUT_SECONDS",
        "ALKERA_OPENCODE_WRITE_TIMEOUT_SECONDS",
        "90",
        90.0,
        30.0,
        None,
        id="agent-write-budget",
    ),
    pytest.param(
        "alkera_cli.harness.adapters.opencode_http",
        "_CONTROL_READ_TIMEOUT_SECONDS",
        "ALKERA_OPENCODE_CONTROL_READ_TIMEOUT_SECONDS",
        "300",
        300.0,
        120.0,
        None,
        id="agent-control-read-budget",
    ),
    pytest.param(
        "alkera_cli.harness.adapters.opencode_http",
        "_HEALTH_PROBE_TIMEOUT_SECONDS",
        "ALKERA_OPENCODE_HEALTH_PROBE_TIMEOUT_SECONDS",
        "2",
        2.0,
        5.0,
        None,
        id="agent-health-probe-budget",
    ),
    pytest.param(
        "alkera_cli.harness.adapters.opencode_http",
        "IDLE_TOOL_CLOSE_GRACE_SECONDS",
        "ALKERA_OPENCODE_IDLE_TOOL_CLOSE_GRACE_SECONDS",
        "1.5",
        1.5,
        5.0,
        None,
        id="idle-tool-close-grace",
    ),
    pytest.param(
        "alkera_cli.harness.adapters.opencode_http",
        "ERROR_BODY_CHARS",
        "ALKERA_OPENCODE_ERROR_BODY_CHARS",
        "4000",
        4000,
        500,
        None,
        id="agent-error-body-snippet",
    ),
    pytest.param(
        "alkera_cli.harness.adapters.opencode_http",
        "STARTUP_TAIL_LINES",
        "ALKERA_OPENCODE_STARTUP_TAIL_LINES",
        "400",
        400,
        50,
        50,
        id="agent-startup-tail",
    ),
    pytest.param(
        "alkera_cli.harness.adapters.opencode_http",
        "TERMINATE_GRACE_SECONDS",
        "ALKERA_OPENCODE_TERMINATE_GRACE_SECONDS",
        "10",
        10.0,
        3.0,
        None,
        id="agent-terminate-grace",
    ),
    pytest.param(
        "alkera_cli.harness.adapters.opencode_http",
        "KILL_GRACE_SECONDS",
        "ALKERA_OPENCODE_KILL_GRACE_SECONDS",
        "8",
        8.0,
        2.0,
        None,
        id="agent-kill-grace",
    ),
    pytest.param(
        "alkera_cli.harness.claude_binary",
        "VERSION_PROBE_TIMEOUT_SECONDS",
        "ALKERA_CLAUDE_VERSION_PROBE_TIMEOUT_SECONDS",
        "30",
        30.0,
        5.0,
        None,
        id="claude-version-probe",
    ),
    pytest.param(
        "alkera_cli.harness.orphan_sweep",
        "_CREATE_TIME_EPSILON",
        "ALKERA_AGENT_CREATE_TIME_EPSILON",
        "0.25",
        0.25,
        1.0,
        1.0,
        id="orphan-identity-tolerance",
    ),
    # --- the runtime ------------------------------------------------------
    pytest.param(
        "alkera_cli.harness.runtime",
        "PERSIST_DRAIN_SECONDS",
        "ALKERA_PERSIST_DRAIN_TIMEOUT",
        "45",
        45.0,
        5.0,
        None,
        id="transcript-drain",
    ),
    pytest.param(
        "alkera_cli.harness.runtime",
        "_SETTLEMENT_HISTORY",
        "ALKERA_SETTLEMENT_HISTORY",
        "1024",
        1024,
        256,
        None,
        id="settlement-history",
    ),
    pytest.param(
        "alkera_cli.harness.runtime",
        "_DRAIN_CEILING_SECONDS",
        "ALKERA_SUBAGENT_DRAIN_CEILING",
        "20",
        20.0,
        5.0,
        None,
        id="subagent-drain-ceiling",
    ),
    pytest.param(
        "alkera_cli.harness.runtime",
        "_SEED_KILL_TIMEOUT",
        "ALKERA_SEED_KILL_TIMEOUT",
        "60",
        60.0,
        5.0,
        None,
        id="seed-worker-reap",
    ),
    pytest.param(
        "alkera_cli.harness.runtime",
        "_REDISCOVER_MIN_INTERVAL_S",
        "ALKERA_CONNECTION_REDISCOVER_INTERVAL",
        "0.5",
        0.5,
        5.0,
        0.0,
        id="connection-rediscover-interval",
    ),
    pytest.param(
        "alkera_cli.harness.event_bus",
        "DEFAULT_QUEUE_MAXSIZE",
        "ALKERA_EVENT_QUEUE_MAXSIZE",
        "65536",
        65536,
        4096,
        0,
        id="subscriber-queue",
    ),
    pytest.param(
        "alkera_cli.harness.chat_files",
        "STAGE_FILE_MAX_BYTES",
        "ALKERA_STAGE_FILE_MAX_BYTES",
        "52428800",
        52428800,
        10 * 1024 * 1024,
        10 * 1024 * 1024,
        id="staged-file-size",
    ),
    # --- the auto-mode judge ---------------------------------------------
    pytest.param(
        "alkera_cli.harness.gateway_completion",
        "TIMEOUT_SECONDS",
        "ALKERA_JUDGE_TIMEOUT_SECONDS",
        "90",
        90.0,
        30.0,
        None,
        id="judge-call-budget",
    ),
    pytest.param(
        "alkera_cli.harness.gateway_completion",
        "MAX_TOKENS",
        "ALKERA_JUDGE_MAX_TOKENS",
        "1024",
        1024,
        256,
        256,
        id="judge-reply-budget",
    ),
    pytest.param(
        "alkera_cli.harness.safety_judge",
        "JUDGE_GOAL_CHARS",
        "ALKERA_JUDGE_GOAL_CHARS",
        "4000",
        4000,
        600,
        None,
        id="judge-goal-window",
    ),
    pytest.param(
        "alkera_cli.harness.safety_judge",
        "JUDGE_COMMAND_CHARS",
        "ALKERA_JUDGE_COMMAND_CHARS",
        "8000",
        8000,
        1000,
        None,
        id="judge-command-window",
    ),
    pytest.param(
        "alkera_cli.harness.safety_judge",
        "JUDGE_TARGETS",
        "ALKERA_JUDGE_TARGETS",
        "50",
        50,
        20,
        None,
        id="judge-target-list",
    ),
    pytest.param(
        "alkera_cli.harness.safety_judge",
        "JUDGE_REASONS",
        "ALKERA_JUDGE_REASONS",
        "50",
        50,
        10,
        None,
        id="judge-reason-list",
    ),
    # --- the plugin tools -------------------------------------------------
    pytest.param(
        "alkera_cli.plugins.plugin_base.bash_ids",
        "MAX_LINES",
        "ALKERA_BASH_MAX_LINES",
        "10000",
        10000,
        2000,
        None,
        id="bash-output-lines",
    ),
    pytest.param(
        "alkera_cli.plugins.plugin_base.bash_ids",
        "MAX_BYTES",
        "ALKERA_BASH_MAX_BYTES",
        "262144",
        262144,
        50 * 1024,
        None,
        id="bash-output-bytes",
    ),
    pytest.param(
        "alkera_cli.plugins.plugin_base.bash_ids",
        "FORCE_KILL_SECONDS",
        "ALKERA_BASH_FORCE_KILL_SECONDS",
        "30",
        30.0,
        3.0,
        None,
        id="bash-kill-grace",
    ),
    pytest.param(
        "alkera_cli.plugins.plugin_base.bash_ids",
        "DEFAULT_TIMEOUT_MS",
        "ALKERA_BASH_DEFAULT_TIMEOUT_MS",
        "3600000",
        3600000,
        None,
        None,
        id="bash-default-timeout",
    ),
    pytest.param(
        "alkera_cli.plugins.plugin_base.bash_ids",
        "STUCK_SECONDS",
        "ALKERA_BASH_STUCK_SECONDS",
        "7200",
        7200.0,
        1800.0,
        None,
        id="bash-stuck-bound",
    ),
    pytest.param(
        "alkera_cli.plugins.plugin_base.delivery",
        "PREVIEW_ROW_CAP",
        "ALKERA_RESULT_PREVIEW_ROWS",
        "500",
        500,
        50,
        50,
        id="inline-row-cap",
    ),
    pytest.param(
        "alkera_cli.plugins.plugin_base.delivery",
        "RESULT_INLINE_BYTE_CAP",
        "ALKERA_RESULT_INLINE_BYTES",
        "262144",
        262144,
        64 * 1024,
        64 * 1024,
        id="inline-byte-cap",
    ),
    pytest.param(
        "alkera_cli.plugins.plugin_base.background_tools",
        "_PREVIEW_CAP",
        "ALKERA_BACKGROUND_PREVIEW_CHARS",
        "20000",
        20000,
        2000,
        None,
        id="background-status-preview",
    ),
]

_KNOB_ARGS = ("module_name", "constant", "env_var", "raw", "moved", "shipped", "on_zero")


@pytest.mark.parametrize(_KNOB_ARGS, _KNOBS)
def test_a_bound_ships_as_the_value_it_has_always_had(
    monkeypatch: pytest.MonkeyPatch,
    module_name: str,
    constant: str,
    env_var: str,
    raw: str,
    moved: float | int,
    shipped: float | int,
    on_zero: float | int | None,
) -> None:
    """Folding a literal into a setting must not change what a deployment that
    sets nothing gets. A junk value is the same as none — a typo can neither
    move a bound nor remove one."""
    del raw, moved, on_zero
    monkeypatch.delenv(env_var, raising=False)
    assert _constant(module_name, constant) == shipped
    monkeypatch.setenv(env_var, "lots")
    assert _constant(module_name, constant) == shipped


@pytest.mark.parametrize(_KNOB_ARGS, _KNOBS)
def test_a_bound_is_what_its_environment_variable_asked_for(
    monkeypatch: pytest.MonkeyPatch,
    module_name: str,
    constant: str,
    env_var: str,
    raw: str,
    moved: float | int,
    shipped: float | int,
    on_zero: float | int | None,
) -> None:
    del shipped, on_zero
    monkeypatch.setenv(env_var, raw)
    assert _constant(module_name, constant) == moved


@pytest.mark.parametrize(_KNOB_ARGS, _KNOBS)
def test_zero_removes_a_bound_or_keeps_the_one_it_cannot_remove(
    monkeypatch: pytest.MonkeyPatch,
    module_name: str,
    constant: str,
    env_var: str,
    raw: str,
    moved: float | int,
    shipped: float | int,
    on_zero: float | int | None,
) -> None:
    """Zero is how a deployment says "no bound here". A bound that holds memory
    or a model's context in place has no such reading and keeps its default, so
    nothing an operator types can make the harness unbounded where it must not be."""
    del raw, moved, shipped
    monkeypatch.setenv(env_var, "0")
    value = _constant(module_name, constant)
    if on_zero is None:
        assert value is None
    else:
        assert value == on_zero


def test_every_knob_is_spelled_once() -> None:
    """Two knobs on one variable would make one of them unreachable."""
    names = [p.values[2] for p in _KNOBS]
    assert len(names) == len(set(names))


# ---------------------------------------------------------------------------
# The call sites actually read them
# ---------------------------------------------------------------------------


def test_bash_output_is_tailed_at_the_line_count_the_environment_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALKERA_BASH_MAX_LINES", "3")
    with _fresh("alkera_cli.plugins.plugin_base.bash_exec") as bash_exec:
        limits = bash_exec.ExecLimits()
        text = "\n".join(f"line {i}" for i in range(50))
        tailed, cut = bash_exec.tail(text, limits.max_lines, limits.max_bytes)
    assert cut is True
    assert tailed.splitlines() == ["line 47", "line 48", "line 49"]


def test_bash_output_is_not_tailed_when_both_thresholds_are_removed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing is lost when the thresholds bite — the spill file holds it all —
    so a deployment may ask for the whole output inline."""
    monkeypatch.setenv("ALKERA_BASH_MAX_LINES", "0")
    monkeypatch.setenv("ALKERA_BASH_MAX_BYTES", "0")
    with _fresh("alkera_cli.plugins.plugin_base.bash_exec") as bash_exec:
        limits = bash_exec.ExecLimits()
        text = "\n".join(f"line {i}" for i in range(5000))
        tailed, cut = bash_exec.tail(text, limits.max_lines, limits.max_bytes)
    assert cut is False
    assert tailed == text


def test_the_judge_sees_the_whole_goal_when_the_operator_widens_the_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The goal is the judge's most decision-relevant input, and a long ask whose
    operative sentence lands past the cut reads as out of scope."""
    from alkera_cli.contracts.tool_types import ActionDescriptor

    goal = "a" * 3000 + " DELETE THE STAGING TABLE"
    descriptor = ActionDescriptor(capability="sql", effect="write", operation="delete", raw="x")

    monkeypatch.setenv("ALKERA_JUDGE_GOAL_CHARS", "4000")
    with _fresh("alkera_cli.harness.safety_judge") as judge:
        wide = judge._build_judge_payload(descriptor, goal, workspace_root=None)
    monkeypatch.delenv("ALKERA_JUDGE_GOAL_CHARS")
    with _fresh("alkera_cli.harness.safety_judge") as judge:
        shipped = judge._build_judge_payload(descriptor, goal, workspace_root=None)
    assert wide["task_goal"] == goal
    assert shipped["task_goal"] == goal[:600]


def test_the_judge_sees_the_whole_command_when_the_window_is_removed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from alkera_cli.contracts.tool_types import ActionDescriptor

    statement = "DROP TABLE t; " * 400
    descriptor = ActionDescriptor(capability="sql", effect="write", operation="drop", raw=statement)
    monkeypatch.setenv("ALKERA_JUDGE_COMMAND_CHARS", "0")
    with _fresh("alkera_cli.harness.safety_judge") as judge:
        payload = judge._build_judge_payload(descriptor, "clean up", workspace_root=None)
    assert payload["action"]["command_or_sql"] == statement


def test_the_judge_reason_list_is_as_long_as_the_operator_allows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from alkera_cli.contracts.tool_types import ActionDescriptor

    reasons = [f"reason-{i}" for i in range(40)]
    descriptor = ActionDescriptor(
        capability="sql", effect="write", operation="delete", raw="x", reasons=reasons
    )
    monkeypatch.setenv("ALKERA_JUDGE_REASONS", "40")
    with _fresh("alkera_cli.harness.safety_judge") as judge:
        payload = judge._build_judge_payload(descriptor, "goal", workspace_root=None)
    assert payload["action"]["reasons"] == reasons


def test_the_result_preview_is_measured_against_the_byte_budget_that_was_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The preview is bounded by its MEASURED field cost, so a narrower budget
    yields a strictly shorter head of the same text."""
    text = "abcdefgh" * 2000
    monkeypatch.setenv("ALKERA_RESULT_PREVIEW_BYTES", "64")
    with _fresh("alkera_cli.plugins.plugin_base.delivery") as delivery:
        narrow = delivery.result_preview(text)
    monkeypatch.setenv("ALKERA_RESULT_PREVIEW_BYTES", "4096")
    with _fresh("alkera_cli.plugins.plugin_base.delivery") as delivery:
        wide = delivery.result_preview(text)
    assert 0 < len(narrow) < len(wide)


def test_the_inline_preview_can_never_exceed_the_inline_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The preview rides inside the measured result envelope, so a preview budget
    set above the envelope's own cap would defeat the cap it sits under."""
    monkeypatch.setenv("ALKERA_RESULT_INLINE_BYTES", "4096")
    monkeypatch.setenv("ALKERA_RESULT_PREVIEW_BYTES", "1000000")
    with _fresh("alkera_cli.plugins.plugin_base.delivery") as delivery:
        assert delivery.RESULT_PREVIEW_BYTES == 4096


def test_a_background_status_preview_is_cut_to_the_configured_tail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALKERA_BACKGROUND_PREVIEW_CHARS", "16")

    class _Job:
        job_id = "j1"
        kind = "bash"
        state = "running"
        preview = "y" * 400
        started_at = 0.0
        result = None

    with _fresh("alkera_cli.plugins.plugin_base.background_tools") as background_tools:
        view = background_tools._view(_Job(), 1.0)
    assert view.output_preview == "…\n" + "y" * 16


def test_every_call_to_the_agent_carries_the_loopback_budgets_that_were_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALKERA_OPENCODE_CONNECT_TIMEOUT_SECONDS", "11")
    monkeypatch.setenv("ALKERA_OPENCODE_WRITE_TIMEOUT_SECONDS", "77")
    with _fresh("alkera_cli.harness.adapters.opencode_http") as opencode_http:
        timeout = opencode_http._agent_timeout(3.0)
        blocking = opencode_http._BLOCKING_TIMEOUT
    assert timeout.connect == 11.0
    assert timeout.pool == 11.0
    assert timeout.write == 77.0
    assert timeout.read == 3.0
    # The compaction call is the one that must never carry a read budget.
    assert blocking.read is None
    assert blocking.connect == 11.0


@pytest.mark.parametrize(
    ("attempts", "expected"),
    [
        pytest.param(1, (), id="one-attempt-never-waits"),
        pytest.param(3, (1.0, 2.0), id="the-shipped-three"),
        pytest.param(6, (1.0, 2.0, 4.0, 8.0, 16.0), id="a-longer-leash-backs-off"),
    ],
)
def test_the_start_backoff_follows_the_attempt_count(
    attempts: int, expected: tuple[float, ...]
) -> None:
    from alkera_cli.harness.adapters.opencode_http import start_retry_delays

    assert start_retry_delays(attempts) == expected


def test_an_unbounded_start_backs_off_without_sleeping_out_of_patience() -> None:
    from alkera_cli.harness.adapters.opencode_http import (
        _START_RETRY_DELAY_CAP,
        start_retry_delays,
    )

    delays = start_retry_delays(None)
    assert len(delays) > len((1.0, 2.0))
    assert max(delays) == _START_RETRY_DELAY_CAP


def test_a_bind_deadline_that_was_removed_never_expires(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The two deadline sites do plain arithmetic on this, so "no deadline" has
    to be a number that no clock reaches rather than ``None``."""
    monkeypatch.setenv("ALKERA_OPENCODE_LISTEN_TIMEOUT_SECONDS", "0")
    with _fresh("alkera_cli.harness.adapters.opencode_http") as opencode_http:
        deadline = opencode_http.LISTEN_TIMEOUT_S
    assert deadline == float("inf")
    assert 0.0 + deadline > 1e300


def test_a_slow_subscriber_drops_only_past_the_queue_the_environment_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A default subscription is what the cloud mirror — the writer of the web
    transcript — takes, so how far it may fall behind before events are silently
    dropped is a deployment's call, and zero means never drop."""
    from alkera_core.schemas.chat import Heartbeat

    stamp = datetime(2026, 9, 19, tzinfo=UTC)

    async def _drops(events: int) -> int:
        with _fresh("alkera_cli.harness.event_bus") as event_bus:
            bus = event_bus.EventBus()
            # Held, never consumed: the queue fills. (A dropped subscription
            # unregisters itself, so letting it go would prove nothing.)
            subscription = bus.subscribe()
            assert subscription is not None
            for i in range(events):
                await bus.publish(
                    Heartbeat(event_id=f"hb{i}", time=stamp, session_id="s", last_activity_ms=0)
                )
            return bus.drops_for_oldest_subscriber()

    monkeypatch.setenv("ALKERA_EVENT_QUEUE_MAXSIZE", "2")
    assert asyncio.run(_drops(6)) == 4

    monkeypatch.setenv("ALKERA_EVENT_QUEUE_MAXSIZE", "0")
    assert asyncio.run(_drops(6)) == 0
