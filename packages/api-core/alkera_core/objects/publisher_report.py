"""What a chat's machine reporting on it does to the chat's spec, as a pure
dict-in / dict-out step, so every report reads the same way wherever it is
applied and the rule is testable without a database.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from alkera_core.chat_refusals import ChatRefusalKind


def clear_refusal(spec: dict[str, Any]) -> None:
    """Drop the box's refusal from ``spec`` in place, with its stamp: every
    path that ends a refusal (a move, a report, a machine leaving) goes
    through here so they never part."""
    spec.pop("publisher_refusal", None)
    spec.pop("publisher_refusal_at", None)
    spec.pop("publisher_refusal_kind", None)


def apply_publisher_report(
    spec: Mapping[str, Any],
    *,
    state: str,
    reason: str,
    now: str,
    kind: ChatRefusalKind | None = None,
) -> dict[str, Any]:
    """The spec after the bound box's report.

    * ``refused``: the gateway's reason (or the state itself) is the refusal
      the chat shows, stamped with when it was said (``publisher_refusal_at``)
      so a reader can tell a refusal from before a wake or a restart from one
      the box stands by now, and with its ``kind`` (``alkera_core.chat_refusals``), which
      is what a reader is shown; any other report clears all three.
    * ``publishing``: the box holds the chat's session, which answers any wake
      a reader asked for, so the wake is cleared with it.
    * ``waiting``: the box has the chat's message but no free slot to open it
      in; ``slot_wait_at`` keeps the first such report (the wait began then).
      Any other report clears it.
    * ``asleep``: nothing here; the chat-end transition records the sleep.
    """
    out = dict(spec)
    refusal = (reason or state) if state == "refused" else None
    if refusal:
        out["publisher_refusal"] = refusal
        out["publisher_refusal_at"] = now
        if kind is None:
            out.pop("publisher_refusal_kind", None)
        else:
            out["publisher_refusal_kind"] = kind
    else:
        clear_refusal(out)
    if state == "publishing":
        out["mirror_state"] = "awake"
        out.pop("wake_requested_at", None)
    if state != "waiting":
        out.pop("slot_wait_at", None)
    elif not out.get("slot_wait_at"):
        out["slot_wait_at"] = now
    return out


__all__ = ["apply_publisher_report", "clear_refusal"]
