"""The one checker for moving a chat between models.

The rule: never lose reasoning. A chat whose history carries a reasoning
format may only move to a model that reads it; an empty history moves freely;
an effort change on the same model is always allowed.
"""

from __future__ import annotations

import pytest
from alkera_core.chat_models.harnesses import (
    HarnessCapabilities,
    capabilities_of,
    register,
)
from alkera_core.chat_models.switching import (
    ModelFacts,
    SwitchVerdict,
    evaluate_effort,
    evaluate_switch,
    group_message_for,
    ledger_after_turn,
    message_for,
)

OPUS_55 = ModelFacts(
    id="claude-opus-5.5",
    display_name="Claude Opus 5.5",
    wire="anthropic",
    efforts=("low", "high"),
    reasoning_format="anthropic:claude-opus-5-5",
    reads_reasoning_formats=frozenset({"anthropic:claude-opus-5"}),
)
FABLE_51 = ModelFacts(
    id="claude-fable-5.1",
    display_name="Claude Fable 5.1",
    wire="anthropic",
    reasoning_format="anthropic:claude-fable-5-1",
    reads_reasoning_formats=frozenset({"anthropic:claude-opus-5-5", "anthropic:claude-opus-5"}),
)
OPUS_5 = ModelFacts(
    id="claude-opus-5",
    display_name="Claude Opus 5",
    wire="anthropic",
    reasoning_format="anthropic:claude-opus-5",
)
GPT = ModelFacts(
    id="gpt-5.5",
    display_name="GPT-5.5",
    wire="openai",
    reasoning_format="openai:gpt-5.5",
)
PLAIN = ModelFacts(id="plain", display_name="Plain", wire="anthropic", reasoning_format=None)

BOTH = frozenset({"anthropic", "openai"})
ANTHROPIC_ONLY = frozenset({"anthropic"})


def _verdict(
    current: ModelFacts | None,
    target: ModelFacts,
    ledger: list[str],
    *,
    wires: frozenset[str] = BOTH,
    offered: bool = True,
) -> SwitchVerdict:
    return evaluate_switch(current, target, ledger=ledger, harness_wires=wires, offered=offered)


@pytest.mark.parametrize(
    ("current", "target", "ledger", "wires", "offered", "state", "reason"),
    [
        pytest.param(
            OPUS_55,
            OPUS_55,
            [OPUS_55.reasoning_format],
            BOTH,
            True,
            "current",
            None,
            id="same-model-is-current",
        ),
        pytest.param(
            OPUS_55,
            OPUS_55,
            ["anything:else"],
            BOTH,
            False,
            "current",
            None,
            id="same-model-ignores-ledger-and-offer",
        ),
        pytest.param(
            OPUS_55, GPT, [], BOTH, False, "unavailable", "model_not_offered", id="not-offered"
        ),
        pytest.param(
            OPUS_55,
            GPT,
            [],
            ANTHROPIC_ONLY,
            True,
            "unavailable",
            "harness_wire_unsupported",
            id="wire-the-harness-cannot-drive",
        ),
        pytest.param(
            OPUS_55, GPT, [], BOTH, True, "available", None, id="empty-ledger-crosses-wires"
        ),
        pytest.param(
            OPUS_55,
            GPT,
            [OPUS_55.reasoning_format],
            BOTH,
            True,
            "unavailable",
            "reasoning_not_readable",
            id="cross-wire-with-history",
        ),
        pytest.param(
            OPUS_55,
            FABLE_51,
            [OPUS_55.reasoning_format],
            BOTH,
            True,
            "available",
            None,
            id="target-reads-the-history",
        ),
        pytest.param(
            FABLE_51,
            OPUS_55,
            [FABLE_51.reasoning_format],
            BOTH,
            True,
            "unavailable",
            "reasoning_not_readable",
            id="directional-the-reverse-is-refused",
        ),
        pytest.param(
            OPUS_5,
            OPUS_55,
            [OPUS_5.reasoning_format],
            BOTH,
            True,
            "available",
            None,
            id="opus-5.5-reads-opus-5",
        ),
        pytest.param(
            OPUS_55,
            OPUS_5,
            [OPUS_55.reasoning_format],
            BOTH,
            True,
            "unavailable",
            "reasoning_not_readable",
            id="opus-5-cannot-read-opus-5.5",
        ),
        pytest.param(
            OPUS_5,
            FABLE_51,
            [OPUS_5.reasoning_format, OPUS_55.reasoning_format],
            BOTH,
            True,
            "available",
            None,
            id="mixed-ledger-target-reads-both",
        ),
        pytest.param(
            OPUS_5,
            OPUS_55,
            [OPUS_5.reasoning_format, FABLE_51.reasoning_format],
            BOTH,
            True,
            "unavailable",
            "reasoning_not_readable",
            id="mixed-ledger-target-misses-one",
        ),
        pytest.param(None, GPT, [], BOTH, True, "available", None, id="no-pin-yet"),
        pytest.param(
            PLAIN,
            OPUS_5,
            [],
            BOTH,
            True,
            "available",
            None,
            id="a-model-with-no-reasoning-left-nothing",
        ),
        pytest.param(
            OPUS_55,
            OPUS_55,
            [],
            frozenset(),
            True,
            "current",
            None,
            id="an-unknown-harness-still-keeps-its-model",
        ),
        pytest.param(
            OPUS_55,
            OPUS_5,
            [],
            frozenset(),
            True,
            "unavailable",
            "harness_wire_unsupported",
            id="an-unknown-harness-offers-no-switch",
        ),
    ],
)
def test_the_checker(
    current: ModelFacts | None,
    target: ModelFacts,
    ledger: list[str],
    wires: frozenset[str],
    offered: bool,
    state: str,
    reason: str | None,
) -> None:
    verdict = _verdict(current, target, ledger, wires=wires, offered=offered)
    assert (verdict.state, verdict.reason_code) == (state, reason)
    assert verdict.allowed is (state != "unavailable")


def test_the_refusal_names_what_blocks_and_offers_a_new_chat() -> None:
    verdict = _verdict(OPUS_5, OPUS_55, [FABLE_51.reasoning_format, OPUS_5.reasoning_format])
    assert verdict.blocking_formats == ("anthropic:claude-fable-5-1",)
    assert verdict.escape_new_chat_model == OPUS_55.id
    assert _verdict(OPUS_55, GPT, [], offered=False).escape_new_chat_model is None


@pytest.mark.parametrize(
    ("effort", "allowed"),
    [("low", True), ("high", True), (None, True), ("max", False), ("", False)],
)
def test_an_effort_is_judged_against_the_model_alone(effort: str | None, allowed: bool) -> None:
    assert evaluate_effort(OPUS_55, effort) is allowed


def test_the_ledger_grows_by_the_model_that_ran_once() -> None:
    ledger = ledger_after_turn([], OPUS_5)
    ledger = ledger_after_turn(ledger, OPUS_55)
    ledger = ledger_after_turn(ledger, OPUS_5)
    assert ledger == ["anthropic:claude-opus-5", "anthropic:claude-opus-5-5"]
    assert ledger_after_turn(ledger, PLAIN) == ledger
    assert ledger_after_turn(["a", "a"], None) == ["a"]


@pytest.mark.parametrize(
    ("verdict", "names", "expected"),
    [
        pytest.param(
            SwitchVerdict(
                state="unavailable",
                reason_code="reasoning_not_readable",
                blocking_formats=("anthropic:claude-opus-5-5",),
            ),
            {"anthropic:claude-opus-5-5": "Claude Opus 5.5"},
            "This chat has reasoning from Claude Opus 5.5 that GPT-5.5 can't read. "
            "Start a new chat to use GPT-5.5.",
            id="reasoning",
        ),
        pytest.param(
            SwitchVerdict(
                state="unavailable",
                reason_code="reasoning_not_readable",
                blocking_formats=("model:gone", "anthropic:a", "anthropic:b"),
            ),
            {"anthropic:a": "A", "anthropic:b": "B"},
            "This chat has reasoning from model:gone, A and B that GPT-5.5 can't read. "
            "Start a new chat to use GPT-5.5.",
            id="several-and-one-gone-from-the-catalog",
        ),
        pytest.param(
            SwitchVerdict(state="unavailable", reason_code="harness_wire_unsupported"),
            {},
            "This chat's agent runs Anthropic models only. Start a new chat to use GPT-5.5.",
            id="wire",
        ),
        pytest.param(
            SwitchVerdict(state="unavailable", reason_code="model_not_offered"),
            {},
            "GPT-5.5 isn't available in this workspace.",
            id="offered",
        ),
        pytest.param(SwitchVerdict(state="available"), {}, None, id="available-says-nothing"),
    ],
)
def test_one_sentence_per_reason(
    verdict: SwitchVerdict, names: dict[str, str], expected: str | None
) -> None:
    assert message_for(verdict, GPT, format_names=names) == expected


def test_facts_read_from_a_pin_or_a_catalog_row() -> None:
    facts = ModelFacts.from_mapping(
        {
            "id": "m",
            "wire": "anthropic",
            "efforts": ["low", 3],
            "reasoning_format": "anthropic:m",
            "reads_reasoning_formats": ["anthropic:x", "", None],
        }
    )
    assert facts.display_name == "m"
    assert facts.efforts == ("low",)
    assert facts.readable == frozenset({"anthropic:m", "anthropic:x"})
    old_pin = ModelFacts.from_mapping({"id": "m", "wire": "anthropic"})
    assert old_pin.reasoning_format is None and old_pin.readable == frozenset()


def test_the_harness_registry_is_extended_by_registration() -> None:
    assert capabilities_of("agent").wires == BOTH
    assert capabilities_of("claude-agent").wires == ANTHROPIC_ONLY
    assert capabilities_of(None).wires == BOTH  # a cloud chat runs opencode
    assert capabilities_of("unheard-of").wires == frozenset()
    register("test-harness", HarnessCapabilities(wires=frozenset({"openai"})))
    assert capabilities_of("test-harness").wires == frozenset({"openai"})


@pytest.mark.parametrize(
    ("verdict", "expected"),
    [
        pytest.param(SwitchVerdict(state="available"), None, id="available"),
        pytest.param(
            SwitchVerdict(state="unavailable", reason_code="model_not_offered"),
            "Not available in this workspace.",
            id="not-offered",
        ),
        pytest.param(
            SwitchVerdict(state="unavailable", reason_code="harness_wire_unsupported"),
            "This chat's agent runs Anthropic models only. Use these in a new chat.",
            id="wire",
        ),
        pytest.param(
            SwitchVerdict(
                state="unavailable",
                reason_code="reasoning_not_readable",
                blocking_formats=("anthropic:a", "anthropic:b"),
            ),
            "This chat has reasoning from A and B that these models can't read. "
            "Use them in a new chat.",
            id="reasoning",
        ),
    ],
)
def test_the_group_line_says_why_without_naming_a_model(
    verdict: SwitchVerdict, expected: str | None
) -> None:
    got = group_message_for(verdict, format_names={"anthropic:a": "A", "anthropic:b": "B"})
    assert got == expected
    if got is not None:
        assert GPT.display_name not in got
