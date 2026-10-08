"""The turn budget: no cap of any kind built in, caps from the environment
where a deployment wants them, and a meter that trips on the wall clock, the
tool-call count and the token total only where one is set — with the boundary
cases (exactly at the cap is fine, one over is not)."""

from __future__ import annotations

import itertools
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alkera_cli.cloud.budget import (
    DEFAULT_MAX_TOKENS,
    DEFAULT_MAX_TOOL_CALLS,
    DEFAULT_WALL_CLOCK_SECONDS,
    ENV_MAX_TOKENS,
    ENV_MAX_TOOL_CALLS,
    ENV_TOKEN_WEIGHTS,
    ENV_WALL_CLOCK,
    TokenWeights,
    TurnBudget,
    TurnMeter,
)
from alkera_core.schemas.chat import Event, MessageCompleted, ToolCall
from pydantic import TypeAdapter

_T = datetime(2026, 9, 6, tzinfo=UTC)


class _Clock:
    def __init__(self) -> None:
        self.at = 0.0

    def __call__(self) -> float:
        return self.at


def _tool(n: int) -> ToolCall:
    return ToolCall(event_id=f"t{n}", time=_T, session_id="s", tool_call_id=f"c{n}", message_id="m")


_MESSAGE = itertools.count()


def _tokens(**counts: int) -> MessageCompleted:
    """One completed message of its own — the harness opens a new assistant
    message per step, so two figures of one turn never share a message id."""
    n = next(_MESSAGE)
    return MessageCompleted(
        event_id=f"mc{n}", time=_T, session_id="s", message_id=f"m{n}", tokens=counts
    )


def test_no_cap_is_built_in() -> None:
    budget = TurnBudget.from_env({})
    assert budget.wall_clock_seconds is DEFAULT_WALL_CLOCK_SECONDS is None
    assert budget.max_tool_calls is DEFAULT_MAX_TOOL_CALLS is None
    assert budget.max_tokens is DEFAULT_MAX_TOKENS is None
    shown = budget.as_dict()
    assert (shown["wall_clock_seconds"], shown["max_tool_calls"], shown["max_tokens"]) == (
        None,
        None,
        None,
    ), "the turn state a reader sees reports no ceiling either"


def test_no_wall_clock_ever_stops_a_turn_by_default() -> None:
    """A turn is never killed for taking long. The one that used to be: an ask
    parked on a person who was not watching the chat, cut off at five minutes
    and its tool aborted. A turn an hour old, ask or no ask, is still alive."""
    clock = _Clock()
    meter = TurnMeter(TurnBudget.from_env({}), clock=clock)
    meter.start()
    for _ in range(16):
        clock.at += 16.0  # ~254 s of real work across sixteen tool calls
        assert meter.note(_tool(next(_MESSAGE))) is None, f"stopped at {clock.at:.0f}s"
    clock.at += 3600.0
    assert meter.check() is None
    assert meter.running


def test_a_turn_waiting_on_a_person_is_not_on_the_clock() -> None:
    """A deployment that opts into a wall clock still never counts the time a
    person takes to answer. Driven across the hour on the ambient clock, the way
    the mirror's own meter runs: frozen time, an ask held for an hour, released,
    and only the agent's own seconds tell."""
    import time

    from freezegun import freeze_time

    with freeze_time("2026-09-15 09:00:00") as frozen:
        meter = TurnMeter(TurnBudget(wall_clock_seconds=30), clock=time.time)
        meter.start()
        frozen.tick(10)
        meter.hold()  # the ask is parked on a reader
        assert meter.waiting
        frozen.tick(3600)  # nobody is watching the chat for an hour
        assert meter.check() is None, "an ask pending for an hour is still answerable"
        assert meter.elapsed() == pytest.approx(10.0)
        meter.release()  # they came back and answered
        assert not meter.waiting
        frozen.tick(19)
        assert meter.check() is None  # 29 s of the agent's own time
        frozen.tick(2)
        exceeded = meter.check()
        assert exceeded is not None and exceeded.cap == "wall_clock"
        assert exceeded.observed == pytest.approx(31.0), "the hour waited was forgiven"


def test_a_held_meter_never_fires_even_past_its_own_clock() -> None:
    clock = _Clock()
    meter = TurnMeter(TurnBudget(wall_clock_seconds=5), clock=clock)
    meter.start()
    clock.at = 4.0
    meter.hold()
    clock.at = 4.0 + 7 * 24 * 3600  # a week
    assert meter.check() is None
    meter.release()
    assert meter.check() is None  # 4 s of the agent's own time
    clock.at += 1.5
    exceeded = meter.check()
    assert exceeded is not None and exceeded.cap == "wall_clock"


def test_holds_nest_and_a_stray_release_is_harmless() -> None:
    clock = _Clock()
    meter = TurnMeter(TurnBudget(wall_clock_seconds=5), clock=clock)
    meter.start()
    meter.hold()
    meter.hold()
    clock.at = 100.0
    meter.release()
    assert meter.waiting
    assert meter.check() is None
    meter.release()
    assert not meter.waiting
    meter.release()  # nothing held: nothing happens
    assert meter.elapsed() == pytest.approx(0.0)
    clock.at = 106.0
    assert meter.check() is not None


def test_a_two_engine_turn_that_explores_both_schemas_is_not_stopped() -> None:
    """A live question answered across two warehouses spent 26 tool calls on
    schema exploration alone and was cut off at 25. Nothing built in stops a
    turn of that shape now."""
    meter = TurnMeter(TurnBudget.from_env({}), clock=_Clock())
    meter.start()
    for n in range(1, 41):
        assert meter.note(_tool(n)) is None, f"stopped at tool call {n}"
    assert meter.tool_calls == 40
    assert meter.check() is None


def test_the_environment_overrides_each_cap() -> None:
    budget = TurnBudget.from_env(
        {ENV_WALL_CLOCK: "30", ENV_MAX_TOOL_CALLS: "3", ENV_MAX_TOKENS: "1000"}
    )
    assert (budget.wall_clock_seconds, budget.max_tool_calls, budget.max_tokens) == (30.0, 3, 1000)


@pytest.mark.parametrize(
    "kwargs",
    [
        pytest.param({"wall_clock_seconds": 0}, id="wall-clock-zero"),
        pytest.param({"max_tool_calls": 0}, id="tool-calls-zero"),
        pytest.param({"max_tokens": -1}, id="tokens-negative"),
    ],
)
def test_a_non_positive_cap_is_refused(kwargs: dict[str, float]) -> None:
    with pytest.raises(ValueError):
        TurnBudget(**kwargs)


def test_a_meter_that_was_never_started_reports_nothing() -> None:
    meter = TurnMeter(TurnBudget(max_tool_calls=1), clock=_Clock())
    assert meter.note(_tool(1)) is None
    assert meter.note(_tool(2)) is None
    assert meter.check() is None


def test_tool_calls_at_the_cap_pass_and_one_over_trips() -> None:
    meter = TurnMeter(TurnBudget(max_tool_calls=2), clock=_Clock())
    meter.start()
    assert meter.note(_tool(1)) is None
    assert meter.note(_tool(2)) is None
    exceeded = meter.note(_tool(3))
    assert exceeded is not None
    assert exceeded.cap == "tool_calls"
    assert exceeded.limit == 2 and exceeded.observed == 3
    assert "more than 2 tool calls" in exceeded.message
    # Sticky: the same verdict until the next start.
    assert meter.check() is exceeded
    assert meter.note(_tool(4)) is None  # already tripped; nothing new is reported


def test_tokens_sum_over_every_message_of_the_turn() -> None:
    meter = TurnMeter(TurnBudget(max_tokens=100), clock=_Clock())
    meter.start()
    assert meter.note(_tokens(input=40, output=20)) is None
    assert meter.note(_tokens(input=30, output=10)) is None  # exactly 100
    exceeded = meter.note(_tokens(output=1))
    assert exceeded is not None and exceeded.cap == "tokens"
    assert exceeded.observed == 101


def test_the_wall_clock_trips_without_any_event() -> None:
    clock = _Clock()
    meter = TurnMeter(TurnBudget(wall_clock_seconds=10), clock=clock)
    meter.start()
    clock.at = 10.0
    assert meter.check() is None  # exactly at the cap
    clock.at = 10.5
    exceeded = meter.check()
    assert exceeded is not None and exceeded.cap == "wall_clock"
    assert exceeded.message.startswith("Stopped: the turn ran past its 10 s")


def test_start_resets_the_tallies_and_stop_ends_the_turn() -> None:
    clock = _Clock()
    meter = TurnMeter(TurnBudget(max_tool_calls=1, wall_clock_seconds=5), clock=clock)
    meter.start()
    meter.note(_tool(1))
    assert meter.note(_tool(2)) is not None
    meter.start()
    assert meter.tool_calls == 0
    assert meter.check() is None
    meter.stop()
    clock.at = 100.0
    assert meter.check() is None  # a stopped turn cannot time out
    assert meter.running is False


# ---------------------------------------------------------------------------
# What the token cap counts — a cost cap, not a context-size cap
# ---------------------------------------------------------------------------

_RECORDED_TURN = Path(__file__).parent / "fixtures" / "recorded_turn_0f0bec47.json"


def _recorded_turn() -> list[Event]:
    """The persisted harness events of one real cloud turn (chat 0f0bec47, a
    Tinybird data question: two schema listings, two describes, five tool
    calls) — the turn the token cap wrongly aborted."""
    raw = json.loads(_RECORDED_TURN.read_text(encoding="utf-8"))
    adapter: TypeAdapter[Event] = TypeAdapter(Event)
    return [adapter.validate_python(event) for event in raw]


def test_default_weights_are_the_documented_cost_shape() -> None:
    weights = TokenWeights.from_env({})
    assert (weights.input, weights.output, weights.reasoning) == (1.0, 1.0, 1.0)
    assert (weights.cache_read, weights.cache_write) == (0.1, 1.25)


@pytest.mark.parametrize(
    ("tokens", "expected"),
    [
        pytest.param({"input": 1000}, 1000.0, id="input-at-full-weight"),
        pytest.param({"output": 1000}, 1000.0, id="output-at-full-weight"),
        pytest.param({"reasoning": 1000}, 1000.0, id="reasoning-at-full-weight"),
        pytest.param({"cache_read": 1000}, 100.0, id="cache-read-is-a-tenth"),
        pytest.param({"cache_write": 1000}, 1250.0, id="cache-write-costs-a-quarter-more"),
        pytest.param({"total": 90_000}, 0.0, id="total-restates-the-others-so-it-is-ignored"),
        pytest.param({"quibble": 1000}, 0.0, id="an-unknown-key-is-not-guessed-at"),
        pytest.param({"input": -5}, 0.0, id="a-negative-count-cannot-buy-budget-back"),
        pytest.param(
            {"input": 500, "output": 100, "cache_read": 30_000, "total": 30_600},
            500 + 100 + 3000,
            id="one-real-step-of-the-recorded-turn",
        ),
    ],
)
def test_each_kind_of_token_is_weighed_the_way_cost_works(
    tokens: dict[str, int], expected: float
) -> None:
    assert TokenWeights().weigh(tokens) == pytest.approx(expected)


def test_the_environment_overrides_one_weight_and_leaves_the_rest() -> None:
    weights = TokenWeights.from_env({ENV_TOKEN_WEIGHTS: "cache_read=0.25, output=2"})
    assert (weights.cache_read, weights.output) == (0.25, 2.0)
    assert (weights.input, weights.reasoning, weights.cache_write) == (1.0, 1.0, 1.25)


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param("", id="empty"),
        pytest.param("cache_read", id="no-value"),
        pytest.param("cache_read=nope", id="not-a-number"),
        pytest.param("cache_read=-1", id="negative"),
        pytest.param("nonsense=3", id="unknown-key"),
    ],
)
def test_a_bad_weight_override_leaves_the_defaults(raw: str) -> None:
    assert TokenWeights.from_env({ENV_TOKEN_WEIGHTS: raw}) == TokenWeights()


def test_a_zero_weight_is_honoured_because_a_weight_is_not_a_cap() -> None:
    weights = TokenWeights.from_env({ENV_TOKEN_WEIGHTS: "cache_read=0"})
    assert weights.cache_read == 0.0
    assert weights.weigh({"cache_read": 100_000}) == 0.0


def test_a_message_reported_twice_is_counted_once() -> None:
    """opencode re-reports a completed assistant message — the same
    ``message_id``, a new ``event_id``, milliseconds apart. The second report
    restates that message's usage; it is not more usage."""
    meter = TurnMeter(TurnBudget(max_tokens=1000), clock=_Clock())
    meter.start()
    first = MessageCompleted(
        event_id="a", time=_T, session_id="s", message_id="m1", tokens={"input": 400}
    )
    again = MessageCompleted(
        event_id="b", time=_T, session_id="s", message_id="m1", tokens={"input": 400}
    )
    assert meter.note(first) is None
    assert meter.note(again) is None
    assert meter.tokens == 400


def test_a_cumulative_reporter_does_not_re_add_its_own_running_total() -> None:
    """A harness that re-reports ONE message with a growing figure (its
    session-cumulative usage) has each report REPLACE the last, not add to it."""
    meter = TurnMeter(TurnBudget(max_tokens=1000), clock=_Clock())
    meter.start()
    for step, running in enumerate((300, 700, 900), start=1):
        event = MessageCompleted(
            event_id=f"e{step}",
            time=_T,
            session_id="s",
            message_id="m1",
            tokens={"input": running},
        )
        assert meter.note(event) is None
    assert meter.tokens == 900


def test_a_per_message_reporter_sums_its_messages() -> None:
    """The opencode shape: one assistant message per step, each carrying only
    that step's usage — so the turn's figure is their sum."""
    meter = TurnMeter(TurnBudget(max_tokens=1000), clock=_Clock())
    meter.start()
    for step in range(1, 4):
        event = MessageCompleted(
            event_id=f"e{step}",
            time=_T,
            session_id="s",
            message_id=f"m{step}",
            tokens={"input": 300},
        )
        meter.note(event)
    assert meter.tokens == 900


def test_the_recorded_turn_costs_a_fraction_of_the_figure_it_was_aborted_on() -> None:
    """The regression. This exact turn was aborted with "the turn used more
    than 400000 tokens (429772)" after five tool calls, on three steps whose
    real usage sums to ~107k — almost all of it re-read cache. Weighed the way
    cost works it is a tenth of the figure it was stopped on, so even a
    deployment that opts the old ceiling back in clears it."""
    meter = TurnMeter(TurnBudget(max_tokens=400_000), clock=_Clock())
    meter.start()
    for event in _recorded_turn():
        assert meter.note(event) is None, f"the recorded turn was stopped by {event!r}"
    assert meter.tool_calls == 5
    assert meter.tokens < 40_000


def test_the_recorded_turn_still_trips_a_cap_it_genuinely_exceeds() -> None:
    """The cap is not defanged: the same stream against a cap below what the
    turn really costs still stops it."""
    meter = TurnMeter(TurnBudget(max_tokens=5_000), clock=_Clock())
    meter.start()
    exceeded = None
    for event in _recorded_turn():
        exceeded = meter.note(event)
        if exceeded is not None:
            break
    assert exceeded is not None and exceeded.cap == "tokens"
    assert exceeded.observed > 5_000
    assert "more than 5000 tokens" in exceeded.message


# ---------------------------------------------------------------------------
# No built-in ceiling: a cap exists only where a deployment set one
# ---------------------------------------------------------------------------


def test_a_default_turn_has_no_token_or_tool_call_ceiling() -> None:
    """The turn that ended this: an ordinary exploration stopped with "the turn
    used more than 400000 tokens (594207)" by a ceiling nobody chose and no
    reader could see. Spend is the credit meter's job at the gateway, so a turn
    now runs as long as its work takes."""
    meter = TurnMeter(TurnBudget.from_env({}), clock=_Clock())
    meter.start()
    for n in range(1, 501):
        assert meter.note(_tool(n)) is None, f"stopped at tool call {n}"
    for step in range(20):
        assert meter.note(_tokens(input=100_000)) is None, f"stopped at step {step}"
    assert (meter.tool_calls, meter.tokens) == (500, 2_000_000)
    assert meter.check() is None


def test_a_deployment_can_opt_into_a_token_cap() -> None:
    """The same turn under a deployment that asked for the old ceiling back."""
    meter = TurnMeter(TurnBudget.from_env({ENV_MAX_TOKENS: "400000"}), clock=_Clock())
    meter.start()
    exceeded = None
    for _ in range(20):
        exceeded = meter.note(_tokens(input=100_000))
        if exceeded is not None:
            break
    assert exceeded is not None and exceeded.cap == "tokens"
    assert "more than 400000 tokens" in exceeded.message


def test_a_deployment_can_opt_into_a_tool_call_cap() -> None:
    meter = TurnMeter(TurnBudget.from_env({ENV_MAX_TOOL_CALLS: "60"}), clock=_Clock())
    meter.start()
    for n in range(1, 61):
        assert meter.note(_tool(n)) is None, f"stopped at tool call {n}"
    exceeded = meter.note(_tool(61))
    assert exceeded is not None and exceeded.cap == "tool_calls"
    assert (exceeded.limit, exceeded.observed) == (60, 61)


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param("", id="empty"),
        pytest.param("nope", id="not-a-number"),
        pytest.param("0", id="zero"),
        pytest.param("-5", id="negative"),
    ],
)
def test_an_unreadable_cap_override_means_no_cap(raw: str) -> None:
    """Unset, blank, unreadable and non-positive all mean "no such cap" — never
    a silently substituted one, which is how an invisible ceiling arrives."""
    budget = TurnBudget.from_env(
        {ENV_WALL_CLOCK: raw, ENV_MAX_TOOL_CALLS: raw, ENV_MAX_TOKENS: raw}
    )
    assert budget.wall_clock_seconds is None
    assert budget.max_tool_calls is None
    assert budget.max_tokens is None
