"""The status contract: a fact can only be built from a registered vocabulary,
and every word a registered vocabulary holds is fit for a person to read."""

from __future__ import annotations

import re
from datetime import UTC, datetime

import pytest
from alkera_core.status import (
    TONES,
    Generic,
    ReasonDef,
    StateDef,
    StatusVocabularyError,
    action,
    register,
    registered,
)


def _vocabulary(name: str, **states: StateDef):
    return register(f"test_{name}", states)


def test_a_fact_carries_the_states_own_words() -> None:
    vocabulary = _vocabulary("own_words", resting=StateDef("Resting", "muted", "Nothing to do."))
    since = datetime(2026, 10, 6, 9, 0, tzinfo=UTC)
    fact = vocabulary.fact("resting", since=since)
    assert fact.model_dump() == {
        "subject": "test_own_words",
        "state": "resting",
        "label": "Resting",
        "tone": "muted",
        "reason_code": "",
        "sentence": "Nothing to do.",
        "since": since,
        "recheck_at": None,
        "action": None,
    }


def test_a_reason_replaces_the_sentence_and_may_replace_label_and_tone() -> None:
    vocabulary = _vocabulary(
        "reasons",
        down=StateDef(
            "Down",
            "danger",
            "It is down.",
            {
                "plain": ReasonDef("{machine} isn't responding."),
                "loud": ReasonDef("It left.", label="Gone", tone="warning"),
            },
        ),
    )
    plain = vocabulary.fact("down", reason="plain", machine="lab-b")
    assert (plain.label, plain.tone, plain.reason_code, plain.sentence) == (
        "Down",
        "danger",
        "plain",
        "lab-b isn't responding.",
    )
    loud = vocabulary.fact("down", reason="loud")
    assert (loud.label, loud.tone, loud.sentence) == ("Gone", "warning", "It left.")


def test_a_sentence_opening_with_a_lowercase_fragment_is_capitalized() -> None:
    vocabulary = _vocabulary(
        "capital", down=StateDef("Down", "danger", "{machine} isn't responding.")
    )
    assert vocabulary.fact("down", machine=Generic("the machine")).sentence == (
        "The machine isn't responding."
    )
    # A name the server resolved keeps its own spelling.
    assert vocabulary.fact("down", machine="lab-b").sentence == "lab-b isn't responding."


def test_a_fact_carries_its_action() -> None:
    vocabulary = _vocabulary("action", off=StateDef("Stopped", "muted", "It is stopped."))
    fact = vocabulary.fact("off", action=action("start_machine", "Start"))
    assert fact.action is not None
    assert (fact.action.kind, fact.action.label) == ("start_machine", "Start")


@pytest.mark.parametrize(
    ("state", "kwargs", "why"),
    [
        pytest.param("missing", {}, "no state 'missing'", id="unknown-state"),
        pytest.param("down", {"reason": "nope"}, "no reason 'nope'", id="unknown-reason"),
        pytest.param("down", {"reason": "named"}, r"needs \['machine'\]", id="unfilled-name"),
        pytest.param(
            "down", {"reason": "named", "machine": "  "}, r"needs \['machine'\]", id="blank-name"
        ),
    ],
)
def test_a_fact_outside_the_vocabulary_is_refused(
    state: str, kwargs: dict[str, str], why: str
) -> None:
    vocabulary = registered().get("test_refusals") or _vocabulary(
        "refusals",
        down=StateDef("Down", "danger", "It is down.", {"named": ReasonDef("{machine} left.")}),
    )
    with pytest.raises(StatusVocabularyError, match=why):
        vocabulary.fact(state, **kwargs)


@pytest.mark.parametrize(
    ("states", "why"),
    [
        pytest.param({}, "has no states", id="empty"),
        pytest.param({"a": StateDef("", "info", "Fine.")}, "has no label", id="no-label"),
        pytest.param({"a": StateDef("A", "info", " ")}, "has no sentence", id="no-sentence"),
        pytest.param({"a": StateDef("A", "purple", "Fine.")}, "no such tone", id="bad-tone"),  # type: ignore[arg-type]
        pytest.param({"a": StateDef("A", "info", "No stop")}, "full stop", id="no-full-stop"),
        pytest.param(
            {"a": StateDef("A", "info", "Wait — no.")}, "no em dash", id="em-dash-sentence"
        ),
        pytest.param(
            {"a": StateDef("A", "info", "Fine.", {"r": ReasonDef("")})},
            "has no sentence",
            id="reason-no-sentence",
        ),
        pytest.param(
            {"a": StateDef("A", "info", "Fine.", {"r": ReasonDef("Why.", tone="loud")})},  # type: ignore[arg-type]
            "no such tone",
            id="reason-bad-tone",
        ),
    ],
)
def test_a_vocabulary_unfit_to_read_is_not_registered(
    states: dict[str, StateDef], why: str, request: pytest.FixtureRequest
) -> None:
    subject = f"test_unfit_{request.node.callspec.id}"
    with pytest.raises(StatusVocabularyError, match=why):
        register(subject, states)
    assert subject not in registered()


def test_a_subject_is_registered_once() -> None:
    _vocabulary("once", a=StateDef("A", "info", "Fine."))
    with pytest.raises(StatusVocabularyError, match="already registered"):
        _vocabulary("once", a=StateDef("A", "info", "Fine."))


_PRODUCT = {
    subject: vocabulary
    for subject, vocabulary in registered().items()
    if not subject.startswith("test_")
}


def test_the_product_subjects_are_registered() -> None:
    assert {"chat", "workspace"} <= set(_PRODUCT)


@pytest.mark.parametrize("subject", sorted(_PRODUCT))
def test_every_word_a_person_reads_is_fit(subject: str) -> None:
    """Sentence case, no shouting, no jargon ids, one or two sentences: the
    bar every label and sentence the product can emit is held to."""
    for name, definition in _PRODUCT[subject].states.items():
        rows = [(name, definition.label, definition.tone, definition.sentence)]
        rows += [
            (
                f"{name}.{code}",
                why.label or definition.label,
                why.tone or definition.tone,
                why.sentence,
            )
            for code, why in definition.reasons.items()
        ]
        for where, label, tone, sentence in rows:
            assert tone in TONES, where
            assert label[0].isupper() and label[1:] == label[1:].lower(), (
                f"{subject}.{where}: {label!r} is not sentence case"
            )
            assert len(label) <= 24, f"{subject}.{where}: a pill holds a word or two"
            assert sentence.count(". ") <= 1, f"{subject}.{where}: at most two sentences"
            assert not re.search(r"[A-Za-z]_[a-z]", sentence), (
                f"{subject}.{where}: {sentence!r} shows a code"
            )
            assert ":" not in sentence, f"{subject}.{where}: no colon in a status sentence"
