"""Reading a CLI-side bound from the environment.

A chat turn may legitimately run for hours or days, on a machine nobody who
picked a number ever saw, so every bound the CLI keeps over a live turn has to
be adjustable by the operator — and, where the mechanism allows it, removable
outright. These parsers give every such knob the same contract:

* unset, blank or unparseable reads as the built-in default (a typo in a
  deployment's environment must never silently remove a bound, nor set it to
  zero);
* a value at or below zero reads as ``None`` — no bound at all, the shape
  ``TurnBudget`` already uses for the per-turn caps.

A bound that is ``None`` means the mechanism it guards is bounded by something
real instead: the agent process being gone, the caller cancelling, or the
request itself failing.

``env_fraction`` is the exception to the second rule: a share of a window has
no "unbounded" reading, so anything outside ``(0, 1]`` is refused and the
default stands.
"""

from __future__ import annotations

__all__ = ["env_count", "env_fraction", "env_seconds"]


def env_seconds(raw: str | None, *, default: float | None) -> float | None:
    """Seconds from an environment value; ``None`` means unbounded."""
    parsed = _number(raw)
    if parsed is None:
        return default
    return parsed if parsed > 0 else None


def env_count(raw: str | None, *, default: int | None) -> int | None:
    """A whole count from an environment value; ``None`` means unbounded."""
    parsed = _number(raw)
    if parsed is None or parsed != int(parsed):
        return default
    return int(parsed) if parsed > 0 else None


def env_fraction(raw: str | None, *, default: float) -> float:
    """A share of a whole, in ``(0, 1]``. Anything else keeps the default.

    Unlike the two above there is no "no bound" reading: a fraction of zero
    would mean the thing it scales never happens, and a fraction above one
    would mean it happens past the whole it is a share of. Both are typos, and
    a typo must leave the shipped behaviour standing.
    """
    parsed = _number(raw)
    if parsed is None or not 0.0 < parsed <= 1.0:
        return default
    return parsed


def _number(raw: str | None) -> float | None:
    """The value as a number, or ``None`` when there is nothing usable to read."""
    if raw is None or not raw.strip():
        return None
    try:
        return float(raw.strip())
    except ValueError:
        return None
