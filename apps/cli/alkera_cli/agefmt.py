"""Human relative-age formatting for model-facing context results.

The LLM is bad at knowing the current wall-clock time, so a raw epoch
``updated_at`` tells it little about freshness. ``format_age_ago`` turns the
``(now, then)`` pair into a compact relative string — "10m ago", "1h43m ago",
"1d3h42m ago" — that a model can read directly.
"""

from __future__ import annotations

_MINUTE = 60
_HOUR = 3600
_DAY = 86400


def format_age_ago(now: float, then: float) -> str:
    """A compact "time ago" string for an epoch-seconds ``then`` relative to ``now``.

    Granularity is down to the minute (the largest non-zero unit down to minutes,
    matching examples like ``1d3h42m ago``):

    * ``then <= 0`` (unstamped/legacy item) → ``""`` so the caller can omit it.
    * ``< 1 minute`` (incl. a negative delta from clock skew or a file written
      "in the future") → ``"just now"``.
    * ``< 1 hour`` → ``"{m}m ago"``.
    * ``< 1 day`` → ``"{h}h{m}m ago"``.
    * ``>= 1 day`` → ``"{d}d{h}h{m}m ago"``.
    """
    if then <= 0:
        return ""
    delta = int(now - then)
    if delta < _MINUTE:
        return "just now"
    if delta < _HOUR:
        return f"{delta // _MINUTE}m ago"
    if delta < _DAY:
        hours, rem = divmod(delta, _HOUR)
        return f"{hours}h{rem // _MINUTE}m ago"
    days, rem = divmod(delta, _DAY)
    hours, rem = divmod(rem, _HOUR)
    return f"{days}d{hours}h{rem // _MINUTE}m ago"


__all__ = ["format_age_ago"]
