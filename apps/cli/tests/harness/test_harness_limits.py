"""How a harness bound reads its environment.

The asymmetry is the whole point: a typo must land on the default (a deployment
must not lose a bound to a fat-fingered value), while an explicit zero must
remove the bound (an operator who wants a turn bounded only by the agent itself
has to be able to say so).
"""

from __future__ import annotations

import importlib.util
from types import ModuleType

import pytest
from alkera_cli.host.limits import env_count, env_seconds


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param(None, 900.0, id="unset-is-the-default"),
        pytest.param("", 900.0, id="blank-is-the-default"),
        pytest.param("   ", 900.0, id="whitespace-is-the-default"),
        pytest.param("soon", 900.0, id="junk-is-the-default-not-unbounded"),
        pytest.param("30", 30.0, id="a-number-is-the-bound"),
        pytest.param(" 1.5 ", 1.5, id="a-fraction-is-the-bound"),
        pytest.param("0", None, id="zero-removes-the-bound"),
        pytest.param("-1", None, id="negative-removes-the-bound"),
    ],
)
def test_env_seconds(raw: str | None, expected: float | None) -> None:
    assert env_seconds(raw, default=900.0) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param(None, 8, id="unset-is-the-default"),
        pytest.param("lots", 8, id="junk-is-the-default-not-unbounded"),
        pytest.param("2.5", 8, id="a-fraction-is-not-a-count"),
        pytest.param("16", 16, id="a-whole-number-is-the-bound"),
        pytest.param("0", None, id="zero-removes-the-bound"),
        pytest.param("-4", None, id="negative-removes-the-bound"),
    ],
)
def test_env_count(raw: str | None, expected: int | None) -> None:
    assert env_count(raw, default=8) == expected


def test_a_default_may_itself_be_unbounded() -> None:
    """A knob whose built-in answer is "no bound" still reads a value when given one."""
    assert env_seconds(None, default=None) is None
    assert env_seconds("60", default=None) == 60.0


# ---------------------------------------------------------------------------
# The knobs an operator actually sets
# ---------------------------------------------------------------------------

#: Every bound the harness binds from the environment at import: the module that
#: holds it, the constant, the variable an operator sets, and what the variable
#: buys. A knob that stops reading its variable — renamed, or collapsed back to a
#: literal because the default is what CI happens to run with — fails here.
_ENV_BOUNDS = [
    pytest.param(
        "alkera_cli.harness.background",
        "DEFAULT_MAX_RUNNING_JOBS",
        "ALKERA_MAX_BACKGROUND_JOBS",
        "24",
        24,
        id="background-job-cap",
    ),
    pytest.param(
        "alkera_cli.harness.runtime",
        "SUBAGENT_REPORT_CEILING_SECONDS",
        "ALKERA_SUBAGENT_REPORT_CEILING",
        "120",
        120.0,
        id="subagent-report-ceiling",
    ),
    pytest.param(
        "alkera_cli.harness.runtime",
        "SUBAGENT_TURN_BUDGET_SECONDS",
        "ALKERA_SUBAGENT_TURN_BUDGET",
        "600",
        600.0,
        id="subagent-research-budget",
    ),
    pytest.param(
        "alkera_cli.harness.adapters.opencode_http",
        "SSE_READ_TIMEOUT_SECONDS",
        "ALKERA_OPENCODE_SSE_READ_TIMEOUT_SECONDS",
        "7.5",
        7.5,
        id="event-stream-read-budget",
    ),
]


def _reread(module_name: str) -> ModuleType:
    """The module's constants as a fresh interpreter would bind them, without
    disturbing the module object every other test already imported."""
    spec = importlib.util.find_spec(module_name)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(("module_name", "constant", "env_var", "raw", "expected"), _ENV_BOUNDS)
def test_a_harness_bound_is_what_its_environment_variable_asked_for(
    monkeypatch: pytest.MonkeyPatch,
    module_name: str,
    constant: str,
    env_var: str,
    raw: str,
    expected: float | int,
) -> None:
    monkeypatch.setenv(env_var, raw)
    assert getattr(_reread(module_name), constant) == expected


@pytest.mark.parametrize(("module_name", "constant", "env_var", "raw", "expected"), _ENV_BOUNDS)
def test_a_harness_bound_can_be_removed_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
    module_name: str,
    constant: str,
    env_var: str,
    raw: str,
    expected: float | int,
) -> None:
    """Zero is how a deployment says "no bound here" — the mechanism is then held
    by the agent itself, not by a clock."""
    monkeypatch.setenv(env_var, "0")
    assert getattr(_reread(module_name), constant) is None


#: The bounds that ship as "no bound": an operator may put one back, but nothing
#: in the code may decide for them.
_UNBOUNDED_BY_DEFAULT = [
    pytest.param(
        "alkera_cli.harness.runtime",
        "SUBAGENT_TURN_BUDGET_SECONDS",
        "ALKERA_SUBAGENT_TURN_BUDGET",
        id="research-budget",
    ),
    pytest.param(
        "alkera_cli.harness.runtime",
        "SUBAGENT_REPORT_CEILING_SECONDS",
        "ALKERA_SUBAGENT_REPORT_CEILING",
        id="report-ceiling",
    ),
    pytest.param(
        "alkera_cli.harness.background",
        "DEFAULT_MAX_RUNNING_JOBS",
        "ALKERA_MAX_BACKGROUND_JOBS",
        id="background-job-cap",
    ),
]


@pytest.mark.parametrize(("module_name", "constant", "env_var"), _UNBOUNDED_BY_DEFAULT)
def test_a_bound_is_absent_unless_an_operator_asks_for_it(
    monkeypatch: pytest.MonkeyPatch, module_name: str, constant: str, env_var: str
) -> None:
    """A worker runs for as long as its work takes, and a turn starts as much
    work as it has. With nothing in the environment — and with junk in it, which
    must never invent a bound — the shipped answer is ``None``."""
    monkeypatch.delenv(env_var, raising=False)
    assert getattr(_reread(module_name), constant) is None
    monkeypatch.setenv(env_var, "soon")
    assert getattr(_reread(module_name), constant) is None
