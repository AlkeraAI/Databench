"""How a settled ask and a mode change read, on every surface.

A permission ask is answered on one surface and seen settled on all of them: a
person presses "Allow once" in the web app and the card in the Slack thread
turns into the same outcome line, and the reverse. A mode change is a card on
both too. The words come from here, once, and reach the web through the
generated ``@alkera/chat-model`` registry (``export.py``); the web's twin
(``resolutionLine`` / ``modeChangePresentation`` in ``permissionPresentation.ts``)
is held to :data:`OUTCOME_VECTORS` by a test on each side.

Extended by registration: a new option id gets its verb in
:data:`RESOLUTION_VERBS`, a new surface its phrase in :data:`SURFACE_PHRASES`,
a new kind of non-human decider its line in :data:`DECIDER_LINES`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from alkera_core.permission_presentation.modes import mode_spec

#: Where a person was when they acted, as the outcome line says it.
SURFACE_PHRASES: dict[str, str] = {"web": "on web", "slack": "in Slack"}

#: The outcome a settled ask names, by the option it was settled on.
RESOLUTION_VERBS: dict[str, str] = {
    "allow_once": "Allowed",
    "allow_always": "Always allowed",
    "reject_once": "Denied",
    "reject_always": "Always denied",
    "cancelled": "Cancelled",
}

#: The outcome for an option this build has never heard of.
ANSWERED = "Answered"

#: The line for an ask nobody answered: the whole line, since no person is
#: behind it. ``policy`` is a re-decision under a mode switched while the ask
#: waited -- the only way a policy settles an ask a person was already shown.
DECIDER_LINES: dict[str, str] = {
    "policy": "Decided by the new mode",
    "timeout": "No answer in time",
}

#: How a mode change reads.
MODE_SET_TITLE = "Mode set to {label}"
CHANGED_BY = "Changed{where}{who}"


def resolution_line(
    option_id: str | None,
    *,
    decided_by: str | None = "user",
    decider_name: str | None = None,
    surface: str | None = None,
) -> str:
    """The line a settled ask shows in place of its buttons.

    ``Allowed on web by Dana Okafor``, ``Denied in Slack by Sam Lee``,
    ``Decided by the new mode``. A decider or a surface the resolution does not
    name is left out rather than guessed.
    """
    if decided_by in DECIDER_LINES:
        return DECIDER_LINES[decided_by]
    verb = RESOLUTION_VERBS.get(option_id or "", ANSWERED)
    where = SURFACE_PHRASES.get(surface or "")
    who = f"by {decider_name}" if decider_name else None
    return " ".join(piece for piece in (verb, where, who) if piece)


def mode_label(value: str | None) -> str | None:
    if not value:
        return None
    spec = mode_spec(value)
    return spec.label if spec is not None else value


@dataclass(frozen=True, slots=True)
class ModeChangePresentation:
    """A mode change as a card: ``title`` leads, ``change`` shows old -> new,
    ``attribution`` who changed it and where."""

    title: str
    change: str
    attribution: str

    def to_json(self) -> dict[str, Any]:
        return {"title": self.title, "change": self.change, "attribution": self.attribution}


def mode_change(
    mode: str,
    *,
    previous_mode: str | None = None,
    changer_name: str | None = None,
    surface: str | None = None,
) -> ModeChangePresentation:
    label = mode_label(mode) or mode
    before = mode_label(previous_mode)
    change = f"{before} → {label}" if before and before != label else label
    where = SURFACE_PHRASES.get(surface or "")
    return ModeChangePresentation(
        title=MODE_SET_TITLE.format(label=label),
        change=change,
        attribution=CHANGED_BY.format(
            where=f" {where}" if where else "",
            who=f" by {changer_name}" if changer_name else "",
        ),
    )


#: The cases both twins are held to: each input and the words it must read as.
OUTCOME_VECTORS: dict[str, list[dict[str, Any]]] = {
    "resolutions": [
        {
            "optionId": "allow_once",
            "decidedBy": "user",
            "deciderName": "Robin Lee",
            "surface": "web",
            "line": "Allowed on web by Robin Lee",
        },
        {
            "optionId": "reject_once",
            "decidedBy": "user",
            "deciderName": "Sam Lee",
            "surface": "slack",
            "line": "Denied in Slack by Sam Lee",
        },
        {
            "optionId": "allow_always",
            "decidedBy": "user",
            "deciderName": None,
            "surface": None,
            "line": "Always allowed",
        },
        {
            "optionId": "allow_once",
            "decidedBy": "policy",
            "deciderName": None,
            "surface": None,
            "line": "Decided by the new mode",
        },
        {
            "optionId": "reject_once",
            "decidedBy": "timeout",
            "deciderName": None,
            "surface": None,
            "line": "No answer in time",
        },
        {
            "optionId": "something_new",
            "decidedBy": "user",
            "deciderName": "Dana",
            "surface": "slack",
            "line": "Answered in Slack by Dana",
        },
    ],
    "modeChanges": [
        {
            "mode": "bypass",
            "previousMode": "default",
            "changerName": "Robin Lee",
            "surface": "web",
            "presentation": {
                "title": "Mode set to Bypass permissions",
                "change": "Default → Bypass permissions",
                "attribution": "Changed on web by Robin Lee",
            },
        },
        {
            "mode": "plan",
            "previousMode": None,
            "changerName": "Sam Lee",
            "surface": "slack",
            "presentation": {
                "title": "Mode set to Plan",
                "change": "Plan",
                "attribution": "Changed in Slack by Sam Lee",
            },
        },
        {
            "mode": "read_only",
            "previousMode": "auto",
            "changerName": None,
            "surface": None,
            "presentation": {
                "title": "Mode set to Read-only",
                "change": "Auto → Read-only",
                "attribution": "Changed",
            },
        },
    ],
}


def outcome_document() -> dict[str, Any]:
    """The words and the vectors, as the generated TS registry carries them."""
    return {
        "surfacePhrases": dict(SURFACE_PHRASES),
        "resolutionVerbs": dict(RESOLUTION_VERBS),
        "answered": ANSWERED,
        "deciderLines": dict(DECIDER_LINES),
        "modeSetTitle": MODE_SET_TITLE,
        "changedBy": CHANGED_BY,
        "vectors": OUTCOME_VECTORS,
    }


__all__ = [
    "ANSWERED",
    "DECIDER_LINES",
    "OUTCOME_VECTORS",
    "RESOLUTION_VERBS",
    "SURFACE_PHRASES",
    "ModeChangePresentation",
    "mode_change",
    "mode_label",
    "outcome_document",
    "resolution_line",
]
