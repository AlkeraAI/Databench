"""The new-chat Default Model + Effort rules, as the CLI reaches them.

The rules themselves live in :mod:`alkera_core.chat_defaults`: the backend
resolves the same defaults for a browser reader, and two copies of "what a
transient gateway outage must not do to a saved default" would drift. This
module re-exports them and owns the one CLI step on top:
:func:`resolve_and_persist_chat_defaults`, which every surface that seeds a new
chat (the TUI, the daemon, the headless runner) calls so a stale saved default
converges the same way everywhere.
"""

from __future__ import annotations

from collections.abc import Sequence

from alkera_core.chat_defaults import (
    ChatDefaults,
    EffortModel,
    default_effort_for,
    hosted_default_model,
    middle_effort,
    resolve_chat_defaults,
)
from alkera_core.schemas.preferences import Preferences

from alkera_cli.preferences.user import load_preferences, update_preferences


def resolve_and_persist_chat_defaults(
    selectable: Sequence[EffortModel], *, org_id: str | None = None
) -> ChatDefaults:
    """Resolve the saved Default Chat Model + Effort against ``selectable`` and
    write the correction back to ``preferences.yml`` when it is stale.

    ``org_id`` is the org whose catalog ``selectable`` is: the saved default is
    that org's copy, and so is the correction.

    ``selectable`` is the live catalog already filtered to pickable models. An
    empty one means the gateway was unreachable, and nothing is written: a
    one-off outage must never wipe the saved default. Blocking file I/O; an
    async caller runs it in an executor."""
    prefs = load_preferences(org_id)
    resolved = resolve_chat_defaults(
        selectable, prefs.default_chat_model, prefs.default_chat_effort
    )
    if not (resolved.reset and selectable):
        return resolved

    def mutate(current: Preferences) -> Preferences:
        data = current.model_dump()
        data["default_chat_model"] = resolved.saved_model
        data["default_chat_effort"] = resolved.saved_effort
        return Preferences.model_validate(data)

    update_preferences(mutate, org_id=org_id)
    return resolved


__all__ = [
    "ChatDefaults",
    "default_effort_for",
    "hosted_default_model",
    "middle_effort",
    "resolve_and_persist_chat_defaults",
    "resolve_chat_defaults",
]
