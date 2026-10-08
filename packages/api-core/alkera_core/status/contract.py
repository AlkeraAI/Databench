"""The one status a person reads on a pill, decided on the server.

A read that feeds a pill carries a :class:`StatusFact`: which state the thing
is in, the word and the sentence that say so, the tone to draw it in, why,
since when, and what the reader may do about it. The server writes every word,
with names already resolved, so no client keeps a map from a state to copy or
to a colour and two surfaces cannot disagree about what a state is called.

A subject (a chat, a workspace, a machine) registers its vocabulary once with
:func:`register`. :func:`Vocabulary.fact` is the only constructor of a fact:
it refuses a state or a reason the vocabulary does not hold and a sentence
with a name left unfilled, so a read cannot emit a word that has no label,
tone and sentence.
"""

from __future__ import annotations

import string
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Final, Literal, get_args

from pydantic import BaseModel

#: How a state is drawn. One set for every subject; the pill owns the colours.
Tone = Literal["neutral", "info", "success", "warning", "danger", "muted"]
TONES: Final[frozenset[str]] = frozenset(get_args(Tone))

#: What a reader may do about a state. The client knows how to perform each
#: kind; one it does not know draws no button.
ActionKind = Literal[
    "send_again",
    "wake",
    "start_machine",
    "replace_machine",
    "add_credits",
    "raise_cap",
    "change_machine",
    "retry_move",
    "cancel_move",
    "open_machine",
]
ACTION_KINDS: Final[frozenset[str]] = frozenset(get_args(ActionKind))


class StatusAction(BaseModel):
    """The one thing the reader may do about the state, with the button's words."""

    kind: ActionKind
    label: str


class StatusFact(BaseModel):
    """Where one thing stands, as every surface draws it."""

    #: Whose vocabulary ``state`` and ``reason_code`` belong to.
    subject: str
    #: For styling hooks and tests. Never a key into client copy.
    state: str
    #: The pill's word or two.
    label: str
    tone: Tone
    #: Why, as a code of the subject's vocabulary; empty when the state says it all.
    reason_code: str = ""
    #: One complete sentence, names resolved.
    sentence: str
    #: When the state began, from the evidence that decided it.
    since: datetime | None = None
    #: When the passage of time alone would change this status (a wait that
    #: becomes "stalled"), so a client knows when to read again without
    #: knowing any bound. ``None`` when only an event can change it.
    recheck_at: datetime | None = None
    action: StatusAction | None = None


@dataclass(frozen=True, slots=True)
class ReasonDef:
    """Why a state holds. Its sentence replaces the state's own; a label or a
    tone here overrides the state's for this reason alone."""

    sentence: str
    label: str = ""
    tone: Tone | None = None


@dataclass(frozen=True, slots=True)
class StateDef:
    """One state of a subject: what it is called, how it is drawn, what it
    says with no reason, and the reasons it may carry."""

    label: str
    tone: Tone
    sentence: str
    reasons: Mapping[str, ReasonDef] = field(default_factory=dict)


class StatusVocabularyError(ValueError):
    """A vocabulary that cannot be registered, or a fact outside one."""


def _placeholders(sentence: str) -> set[str]:
    return {name for _text, name, _spec, _conv in string.Formatter().parse(sentence) if name}


def _check_words(subject: str, where: str, label: str, sentence: str) -> None:
    if not label.strip():
        raise StatusVocabularyError(f"{subject}.{where} has no label")
    if not sentence.strip():
        raise StatusVocabularyError(f"{subject}.{where} has no sentence")
    if not sentence.rstrip().endswith((".", "…")):
        raise StatusVocabularyError(f"{subject}.{where}: a sentence ends with a full stop")
    for words in (label, sentence):
        if "—" in words:
            raise StatusVocabularyError(f"{subject}.{where}: no em dash in words a person reads")


@dataclass(frozen=True, slots=True)
class Vocabulary:
    """Every state one subject can be in."""

    subject: str
    states: Mapping[str, StateDef]

    def fact(
        self,
        state: str,
        *,
        reason: str = "",
        since: datetime | None = None,
        recheck_at: datetime | None = None,
        action: StatusAction | None = None,
        **names: str,
    ) -> StatusFact:
        """The fact for ``state`` (and ``reason``), its sentence filled from
        ``names``. A state or reason outside the vocabulary is refused, and
        so is a sentence left with a name unfilled or filled with nothing."""
        definition = self.states.get(state)
        if definition is None:
            raise StatusVocabularyError(f"{self.subject} has no state {state!r}")
        label, tone, sentence = definition.label, definition.tone, definition.sentence
        if reason:
            why = definition.reasons.get(reason)
            if why is None:
                raise StatusVocabularyError(f"{self.subject}.{state} has no reason {reason!r}")
            sentence = why.sentence
            label = why.label or label
            tone = why.tone or tone
        wanted = _placeholders(sentence) | _placeholders(label)
        missing = sorted(name for name in wanted if not names.get(name, "").strip())
        if missing:
            raise StatusVocabularyError(
                f"{self.subject}.{state}{'.' + reason if reason else ''} needs {missing}"
            )
        return StatusFact(
            subject=self.subject,
            state=state,
            label=label.format(**names),
            tone=tone,
            reason_code=reason,
            sentence=_filled(sentence, names),
            since=since,
            recheck_at=recheck_at,
            action=action,
        )


class Generic(str):
    """A noun standing in for a name the reader may not see or the server
    does not have ("the machine"). It is written lowercase and raised when it
    opens the sentence; a real name keeps its own spelling wherever it falls."""

    __slots__ = ()


def _filled(sentence: str, names: Mapping[str, str]) -> str:
    first = next(iter(string.Formatter().parse(sentence)), None)
    opens_with = first[1] if first is not None and first[0] == "" else None
    filled = sentence.format(**names)
    if opens_with and isinstance(names.get(opens_with), Generic):
        return filled[:1].upper() + filled[1:]
    return filled


_VOCABULARIES: dict[str, Vocabulary] = {}


def register(subject: str, states: Mapping[str, StateDef]) -> Vocabulary:
    """Register ``subject``'s vocabulary. A subject is registered once, and
    every state and reason must have a label, a legal tone and a sentence."""
    if subject in _VOCABULARIES:
        raise StatusVocabularyError(f"status subject {subject!r} is already registered")
    if not states:
        raise StatusVocabularyError(f"status subject {subject!r} has no states")
    for name, definition in states.items():
        if definition.tone not in TONES:
            raise StatusVocabularyError(f"{subject}.{name} has no such tone {definition.tone!r}")
        _check_words(subject, name, definition.label, definition.sentence)
        for code, why in definition.reasons.items():
            if why.tone is not None and why.tone not in TONES:
                raise StatusVocabularyError(f"{subject}.{name}.{code} has no such tone")
            _check_words(subject, f"{name}.{code}", why.label or definition.label, why.sentence)
    vocabulary = Vocabulary(
        subject=subject,
        states=MappingProxyType(
            {
                name: StateDef(
                    label=definition.label,
                    tone=definition.tone,
                    sentence=definition.sentence,
                    reasons=MappingProxyType(dict(definition.reasons)),
                )
                for name, definition in states.items()
            }
        ),
    )
    _VOCABULARIES[subject] = vocabulary
    return vocabulary


def registered() -> Mapping[str, Vocabulary]:
    """Every registered vocabulary, by subject."""
    return MappingProxyType(dict(_VOCABULARIES))


def action(kind: ActionKind, label: str) -> StatusAction:
    """An action a fact offers."""
    return StatusAction(kind=kind, label=label)


__all__ = [
    "ACTION_KINDS",
    "TONES",
    "ActionKind",
    "Generic",
    "ReasonDef",
    "StateDef",
    "StatusAction",
    "StatusFact",
    "StatusVocabularyError",
    "Tone",
    "Vocabulary",
    "action",
    "register",
    "registered",
]
