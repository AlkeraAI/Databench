"""The per-turn budget a cloud chat runs under, and the meter that enforces it.

**A cloud turn has no built-in ceiling.** By default nothing here stops a turn
and the meter only counts: spend is limited by the credit meter at the gateway,
against a balance the owner can see. A turn parked on a permission or a
question may wait hours for its person, and that wait is not the turn's.

A deployment that wants a ceiling sets ``ALKERA_TURN_WALL_CLOCK_SECONDS``,
``ALKERA_TURN_MAX_TOOL_CALLS`` or ``ALKERA_TURN_MAX_TOKENS``. Unset, blank,
unreadable and non-positive all mean "no such cap". Where a cap is set the
mirror stops the turn the moment it is exceeded and says so in the transcript;
the wall clock pauses while an ask is pending.

A token cap is a **cost** cap, not a context-size cap, so a message's usage is
WEIGHED by :class:`TokenWeights` (cache reads a tenth, cache writes a quarter
more, everything else at face value). The aggregate ``total`` a harness reports
beside its components is ignored, since it restates them. A message's figure
REPLACES the last one seen for that message, because a harness may re-report a
completed message or report a running total.

``TurnMeter`` is pure: feed it the harness events of one turn and a clock, and
it answers whether the turn is over budget.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field, fields
from typing import Any

from alkera_core.schemas.chat import MessageCompleted, ToolCall

#: No cap of any kind unless a deployment sets one. A turn is never killed for
#: taking long, for waiting on a person, for the work it did or for what it
#: spent — the credit meter at the gateway is what limits spend.
DEFAULT_WALL_CLOCK_SECONDS: float | None = None
DEFAULT_MAX_TOOL_CALLS: int | None = None
DEFAULT_MAX_TOKENS: int | None = None

#: What one token of each kind costs the budget. The defaults follow the shape
#: every provider prices at: a cache read is a fraction of a fresh input token,
#: a cache write a little more than one, and output/reasoning are charged like
#: input. They are ratios, not prices — the cap is in weighted tokens.
DEFAULT_INPUT_WEIGHT = 1.0
DEFAULT_OUTPUT_WEIGHT = 1.0
DEFAULT_REASONING_WEIGHT = 1.0
DEFAULT_CACHE_READ_WEIGHT = 0.1
DEFAULT_CACHE_WRITE_WEIGHT = 1.25

ENV_WALL_CLOCK = "ALKERA_TURN_WALL_CLOCK_SECONDS"
ENV_MAX_TOOL_CALLS = "ALKERA_TURN_MAX_TOOL_CALLS"
ENV_MAX_TOKENS = "ALKERA_TURN_MAX_TOKENS"
#: ``input=1,cache_read=0.05`` — only the weights named are overridden.
ENV_TOKEN_WEIGHTS = "ALKERA_TURN_TOKEN_WEIGHTS"  # noqa: S105 — an env var name, not a secret


def _optional_positive(raw: str | None) -> float | None:
    """A positive number, or ``None`` — unset, blank, unreadable and
    non-positive all mean "no such cap", never a silently substituted one."""
    if raw is None or not raw.strip():
        return None
    try:
        value = float(raw.strip())
    except ValueError:
        return None
    return value if value > 0 else None


@dataclass(frozen=True, slots=True)
class TokenWeights:
    """What each kind of token costs the token cap.

    Only these five kinds are counted. A harness also reports an aggregate
    (opencode and the claude adapter both send ``total``), and that aggregate
    RESTATES the five, so adding it would charge every token twice. An
    unrecognised key is likewise not guessed at: weighing it at face value
    would re-open that hole for the next alias a harness invents.
    """

    input: float = DEFAULT_INPUT_WEIGHT
    output: float = DEFAULT_OUTPUT_WEIGHT
    reasoning: float = DEFAULT_REASONING_WEIGHT
    cache_read: float = DEFAULT_CACHE_READ_WEIGHT
    cache_write: float = DEFAULT_CACHE_WRITE_WEIGHT

    def __post_init__(self) -> None:
        if any(w < 0 for w in (self.input, self.output, self.reasoning)) or any(
            w < 0 for w in (self.cache_read, self.cache_write)
        ):
            raise ValueError("a token weight cannot be negative")

    def weigh(self, tokens: Mapping[str, Any]) -> float:
        """What one message's reported usage costs the budget."""
        total = 0.0
        for kind, weight in (
            ("input", self.input),
            ("output", self.output),
            ("reasoning", self.reasoning),
            ("cache_read", self.cache_read),
            ("cache_write", self.cache_write),
        ):
            count = tokens.get(kind)
            if isinstance(count, (int, float)) and not isinstance(count, bool) and count > 0:
                total += float(count) * weight
        return total

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> TokenWeights:
        """``ALKERA_TURN_TOKEN_WEIGHTS="cache_read=0.05,output=2"``. An unknown
        name, a missing value, a non-number and a negative are each ignored, so
        a typo in an operator's env leaves the documented defaults standing
        rather than silently uncapping (or hair-triggering) the turn."""
        raw = (os.environ if env is None else env).get(ENV_TOKEN_WEIGHTS)
        if raw is None or not raw.strip():
            return cls()
        known = {f.name for f in fields(cls)}
        overrides: dict[str, float] = {}
        for clause in raw.split(","):
            name, sep, value = clause.partition("=")
            name = name.strip()
            if not sep or name not in known:
                continue
            try:
                weight = float(value.strip())
            except ValueError:
                continue
            if weight >= 0:
                overrides[name] = weight
        return cls(**overrides)


@dataclass(frozen=True, slots=True)
class TurnBudget:
    """The caps, all three of them absent by default: ``None`` is a turn with no
    such ceiling, and nothing here is a ceiling until a deployment sets one. A
    cap that IS set must be positive — ``0`` never means "unlimited", so an
    operator who writes one cannot accidentally disarm the cap they meant."""

    wall_clock_seconds: float | None = DEFAULT_WALL_CLOCK_SECONDS
    max_tool_calls: int | None = DEFAULT_MAX_TOOL_CALLS
    max_tokens: int | None = DEFAULT_MAX_TOKENS
    token_weights: TokenWeights = field(default_factory=TokenWeights)

    def __post_init__(self) -> None:
        if self.wall_clock_seconds is not None and self.wall_clock_seconds <= 0:
            raise ValueError("a wall-clock budget must be positive, or absent")
        if (self.max_tool_calls is not None and self.max_tool_calls <= 0) or (
            self.max_tokens is not None and self.max_tokens <= 0
        ):
            raise ValueError("a turn budget cap must be positive, or absent")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> TurnBudget:
        source = os.environ if env is None else env
        tool_calls = _optional_positive(source.get(ENV_MAX_TOOL_CALLS))
        tokens = _optional_positive(source.get(ENV_MAX_TOKENS))
        return cls(
            wall_clock_seconds=_optional_positive(source.get(ENV_WALL_CLOCK)),
            max_tool_calls=None if tool_calls is None else int(tool_calls),
            max_tokens=None if tokens is None else int(tokens),
            token_weights=TokenWeights.from_env(source),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "wall_clock_seconds": self.wall_clock_seconds,
            "max_tool_calls": self.max_tool_calls,
            "max_tokens": self.max_tokens,
            "token_weights": asdict(self.token_weights),
        }


@dataclass(frozen=True, slots=True)
class BudgetExceeded:
    """Which cap was hit, with the figures the transcript shows."""

    cap: str
    limit: float
    observed: float

    @property
    def message(self) -> str:
        if self.cap == "wall_clock":
            return (
                f"Stopped: the turn ran past its {self.limit:g} s wall-clock budget "
                f"({self.observed:.0f} s)."
            )
        if self.cap == "tool_calls":
            return (
                f"Stopped: the turn made more than {self.limit:g} tool calls ({self.observed:g})."
            )
        return f"Stopped: the turn used more than {self.limit:g} tokens ({self.observed:g})."


class TurnMeter:
    """Tallies one turn against a :class:`TurnBudget`.

    A cap the budget leaves absent is not checked, so with the default budget
    the meter only counts: tool calls and weighted tokens are what the turn
    reports, not what it is judged against.

    The wall clock, when there is one, measures the AGENT's time only: a turn
    is held (``hold``) while an ask is parked on a person and released when
    they answer, and the held stretch is subtracted from the turn's elapsed
    time — so an ask left for an hour costs the turn nothing, and no cap fires
    while one is pending.
    """

    def __init__(self, budget: TurnBudget, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._budget = budget
        self._clock = clock
        self._started_at: float | None = None
        self._tool_calls = 0
        self._message_tokens: dict[str, float] = {}
        self._exceeded: BudgetExceeded | None = None
        self._holds = 0
        self._held_at: float | None = None
        self._waited = 0.0

    @property
    def running(self) -> bool:
        return self._started_at is not None

    @property
    def waiting(self) -> bool:
        """Whether the turn is parked on a person right now."""
        return self._holds > 0

    def hold(self) -> None:
        """The turn is waiting on a person: its clock stops until ``release``."""
        if self._holds == 0:
            self._held_at = self._clock()
        self._holds += 1

    def release(self) -> None:
        """The person answered: the clock runs again, the wait forgiven."""
        if self._holds == 0:
            return
        self._holds -= 1
        if self._holds == 0 and self._held_at is not None:
            self._waited += self._clock() - self._held_at
            self._held_at = None

    def elapsed(self) -> float:
        """The agent's own time in this turn — every wait on a person taken out."""
        if self._started_at is None:
            return 0.0
        now = self._clock()
        waited = self._waited
        if self._held_at is not None:
            waited += now - self._held_at
        return now - self._started_at - waited

    @property
    def tool_calls(self) -> int:
        return self._tool_calls

    @property
    def tokens(self) -> int:
        """The turn's weighted token figure — what the cap is compared against."""
        return int(sum(self._message_tokens.values()))

    def start(self) -> None:
        self._started_at = self._clock()
        self._tool_calls = 0
        self._message_tokens.clear()
        self._exceeded = None
        self._holds = 0
        self._held_at = None
        self._waited = 0.0

    def stop(self) -> None:
        self._started_at = None
        self._holds = 0
        self._held_at = None

    def note(self, event: Any) -> BudgetExceeded | None:
        """Account one harness event; the first cap it pushes over, if any."""
        if self._started_at is None or self._exceeded is not None:
            return None
        if isinstance(event, ToolCall):
            self._tool_calls += 1
        elif isinstance(event, MessageCompleted):
            # The figure a message reports REPLACES the last one seen for that
            # message — the harness re-reports a completed message, and a
            # cumulative reporter's later figure already contains its earlier
            # one. Distinct messages still add up.
            self._message_tokens[event.message_id] = self._budget.token_weights.weigh(event.tokens)
        return self.check()

    def check(self) -> BudgetExceeded | None:
        """The cap currently exceeded, if any. A cap the deployment did not set
        is not one the turn can exceed. The wall clock is checked here rather
        than on the event stream so a turn that stops emitting events still
        times out — never while the turn waits on a person: the ask is theirs
        to answer in their own time."""
        if self._started_at is None:
            return None
        if self._exceeded is not None:
            return self._exceeded
        seconds = self._budget.wall_clock_seconds
        calls = self._budget.max_tool_calls
        tokens = self._budget.max_tokens
        elapsed = self.elapsed()
        if seconds is not None and self._holds == 0 and elapsed > seconds:
            self._exceeded = BudgetExceeded("wall_clock", seconds, elapsed)
        elif calls is not None and self._tool_calls > calls:
            self._exceeded = BudgetExceeded("tool_calls", calls, self._tool_calls)
        elif tokens is not None and self.tokens > tokens:
            self._exceeded = BudgetExceeded("tokens", tokens, self.tokens)
        return self._exceeded


__all__ = [
    "DEFAULT_CACHE_READ_WEIGHT",
    "DEFAULT_CACHE_WRITE_WEIGHT",
    "DEFAULT_INPUT_WEIGHT",
    "DEFAULT_MAX_TOKENS",
    "DEFAULT_MAX_TOOL_CALLS",
    "DEFAULT_OUTPUT_WEIGHT",
    "DEFAULT_REASONING_WEIGHT",
    "DEFAULT_WALL_CLOCK_SECONDS",
    "ENV_MAX_TOKENS",
    "ENV_MAX_TOOL_CALLS",
    "ENV_TOKEN_WEIGHTS",
    "ENV_WALL_CLOCK",
    "BudgetExceeded",
    "TokenWeights",
    "TurnBudget",
    "TurnMeter",
]
