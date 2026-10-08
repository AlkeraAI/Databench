"""The bound on a turn whose model call keeps failing.

An agent retries a failed model call on its own — a connection refused, a 5xx,
an overloaded or rate-limited provider — with growing delays and, in opencode's
case, no cap on the attempts: a turn whose gateway cannot be reached retries
until something else ends it. Each attempt reaches the harness as a
:class:`Retrying` event, and this module is what reads them.

:class:`ModelRetryBudget` is the pure core: it is fed every event of the
running turn with the time it was seen, and answers with the text the turn is
failed with once the retrying has gone on for too many consecutive attempts or
for too long. Any sign the model answered — text, reasoning, a tool call, a
finished message — ends the streak. The turn lifecycle owns the side effects.

The text names the kind of failure and the count, and nothing the provider
said: a provider's message can carry its error body, a host or a URL, none of
which is the reader's.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from alkera_core.schemas.chat import (
    AgentMessageChunk,
    AgentThoughtChunk,
    Event,
    MessageCompleted,
    PartCreated,
    Retrying,
    ToolCall,
    ToolCallUpdate,
)

#: Consecutive failed model calls one turn may retry through.
DEFAULT_MAX_MODEL_RETRIES = 6
#: How long one turn may spend retrying a model call, from its first failure.
DEFAULT_MODEL_RETRY_WINDOW_SECONDS = 300.0

#: What proves a model call got through: the model said or did something.
_MODEL_ANSWERED = (
    AgentMessageChunk,
    AgentThoughtChunk,
    PartCreated,
    ToolCall,
    ToolCallUpdate,
    MessageCompleted,
)

_OVERLOADED = ("overload", "exhausted")
_RATE_LIMITED = ("rate limit", "rate increased", "too many requests", "usage limit", "429")
_UNREACHABLE = (
    "connect",
    "refused",
    "unreachable",
    "fetch failed",
    "network",
    "socket",
    "timed out",
    "timeout",
    "enotfound",
    "eai_again",
    "dns",
    "reset by peer",
)
_UNAVAILABLE = (
    "unavailable",
    "internal server error",
    "bad gateway",
    "gateway timeout",
    "server error",
    "500",
    "502",
    "503",
    "504",
    "529",
)


def failure_kind(reason: str | None) -> str:
    """Which kind of failure a retry's message describes, as one word."""
    text = (reason or "").lower()
    for kind, needles in (
        ("overloaded", _OVERLOADED),
        ("rate_limited", _RATE_LIMITED),
        ("unreachable", _UNREACHABLE),
        ("unavailable", _UNAVAILABLE),
    ):
        if any(needle in text for needle in needles):
            return kind
    return "failing"


_LEADS = {
    "overloaded": "The provider was overloaded",
    "rate_limited": "The provider refused the request",
    "unreachable": "The model could not be reached",
    "unavailable": "The provider was unavailable",
    "failing": "The model call kept failing",
}


def give_up_text(reason: str | None, attempts: int, *, waited_seconds: float | None) -> str:
    """The failure a turn ends with: the kind and the count, never the
    provider's own words."""
    lead = _LEADS[failure_kind(reason)]
    count = f"{attempts} attempt{'' if attempts == 1 else 's'}"
    if waited_seconds is None:
        return f"{lead} after {count}; the turn was stopped."
    return f"{lead} after {count} in {_duration(waited_seconds)}; the turn was stopped."


def _duration(seconds: float) -> str:
    whole = max(1, round(seconds))
    if whole < 60:
        return f"{whole} second{'' if whole == 1 else 's'}"
    minutes = round(whole / 60)
    return f"{minutes} minute{'' if minutes == 1 else 's'}"


@dataclass
class ModelRetryBudget:
    """Counts one turn's consecutive model-call retries and says when to stop.

    ``None`` for either bound leaves that bound off. The streak belongs to one
    turn: a different turn starts it again.
    """

    max_attempts: int | None = DEFAULT_MAX_MODEL_RETRIES
    max_seconds: float | None = DEFAULT_MODEL_RETRY_WINDOW_SECONDS
    _turn: str | None = field(default=None, init=False)
    _attempts: int = field(default=0, init=False)
    _last_attempt: int = field(default=0, init=False)
    _since: float | None = field(default=None, init=False)
    _reason: str | None = field(default=None, init=False)

    @property
    def attempts(self) -> int:
        """Consecutive failed calls the current streak has seen."""
        return self._attempts

    def reset(self) -> None:
        """End the streak."""
        self._attempts = 0
        self._last_attempt = 0
        self._since = None
        self._reason = None

    def observe(self, turn: str, event: Event, now: float) -> str | None:
        """Feed one event of ``turn`` seen at ``now`` (a monotonic reading).

        Returns the text the turn fails with when a bound is crossed, else
        ``None``. Every event is fed — not only :class:`Retrying` — so the time
        bound is crossed by the harness's own liveness beat even when the
        agent's next attempt is scheduled past it.
        """
        if turn != self._turn:
            self._turn = turn
            self.reset()
        if isinstance(event, Retrying):
            # One attempt may be reported more than once (a status repeated);
            # a number the agent already reported is not a new failure.
            if event.attempt <= 0 or event.attempt != self._last_attempt:
                self._attempts += 1
            self._last_attempt = event.attempt
            self._reason = event.reason
            if self._since is None:
                self._since = now
        elif isinstance(event, _MODEL_ANSWERED):
            self.reset()
            return None
        if self._attempts == 0 or self._since is None:
            return None
        if self.max_attempts is not None and self._attempts >= self.max_attempts:
            return give_up_text(self._reason, self._attempts, waited_seconds=None)
        waited = now - self._since
        if self.max_seconds is not None and waited >= self.max_seconds:
            return give_up_text(self._reason, self._attempts, waited_seconds=waited)
        return None


__all__ = [
    "DEFAULT_MAX_MODEL_RETRIES",
    "DEFAULT_MODEL_RETRY_WINDOW_SECONDS",
    "ModelRetryBudget",
    "failure_kind",
    "give_up_text",
]
