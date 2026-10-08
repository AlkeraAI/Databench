"""Tests for the pure new-chat default resolver (`alkera_cli.preferences.chat_defaults`).

Exhausts the rule matrix: never-set, stale, valid, no-effort models, the
don't-wipe-on-empty-catalog invariant, and the effort fallback order (catalog
default → middle-most).
"""

from __future__ import annotations

import pytest
from alkera_cli.contracts.gateway_model import GatewayModel
from alkera_cli.preferences.chat_defaults import (
    ChatDefaults,
    default_effort_for,
    hosted_default_model,
    middle_effort,
    resolve_chat_defaults,
)
from alkera_core.config import settings
from alkera_core.gateway import HOSTED_DEFAULT_MODEL_SLUG


def _model(
    model_id: str,
    *,
    efforts: tuple[str, ...] = (),
    default_effort: str | None = None,
    wire: str = "anthropic",
) -> GatewayModel:
    return GatewayModel(
        id=model_id,
        display_name=model_id.title(),
        wire=wire,  # type: ignore[arg-type]
        efforts=efforts,
        default_effort=default_effort,
    )


OPUS = _model("opus", efforts=("low", "medium", "high"), default_effort="high")
GPT = _model("gpt", efforts=("minimal", "low", "medium", "high"), default_effort=None)
HAIKU = _model("haiku", efforts=())  # no effort variants
CATALOG = [OPUS, GPT, HAIKU]

# The hosted pin, deliberately NOT first in the list: a catalog where it already
# came first could not tell "we honoured the pin" from "we took entry zero".
HOSTED = _model(HOSTED_DEFAULT_MODEL_SLUG, efforts=("low", "medium", "high"), default_effort="high")
HOSTED_CATALOG = [OPUS, HOSTED, GPT]


@pytest.fixture
def hosted(monkeypatch: pytest.MonkeyPatch) -> None:
    """Alkera's hosted SaaS. ``self_hosted`` is the explicit override that wins
    over the Stripe inference, so a test box with no keys still reads as hosted."""
    monkeypatch.setattr(settings, "self_hosted", False)


@pytest.fixture
def self_hosted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "self_hosted", True)


# --- middle_effort ---------------------------------------------------------


@pytest.mark.parametrize(
    ("efforts", "expected"),
    [
        pytest.param((), None, id="empty"),
        pytest.param(("only",), "only", id="one"),
        pytest.param(("low", "high"), "high", id="even-leans-upper"),
        pytest.param(("low", "medium", "high"), "medium", id="odd-three"),
        pytest.param(("minimal", "low", "medium", "high"), "medium", id="even-four"),
    ],
)
def test_middle_effort(efforts: tuple[str, ...], expected: str | None) -> None:
    assert middle_effort(efforts) == expected


# --- default_effort_for ----------------------------------------------------


def test_default_effort_uses_catalog_default_when_offered() -> None:
    assert default_effort_for(OPUS) == "high"  # catalog default, not the middle


def test_default_effort_falls_back_to_middle_when_no_catalog_default() -> None:
    assert default_effort_for(GPT) == "medium"  # no catalog default → middle-most


def test_default_effort_ignores_a_catalog_default_not_in_the_list() -> None:
    model = _model("x", efforts=("low", "high"), default_effort="bogus")
    assert default_effort_for(model) == "high"  # bogus dropped → middle (upper)


def test_default_effort_none_for_model_without_efforts() -> None:
    assert default_effort_for(HAIKU) is None


# --- resolve_chat_defaults: initialization & reset -------------------------


def test_never_set_picks_first_model_and_its_default_effort() -> None:
    """Nothing saved → the first model, flagged defaulted, and NOT a reset: the
    default is never written back as a pick, or the next change of platform
    default could not reach this reader."""
    result = resolve_chat_defaults(CATALOG, None, None)
    assert result == ChatDefaults("opus", "high", reset=False, defaulted=True)
    assert (result.saved_model, result.saved_effort) == (None, None)


def test_unknown_saved_model_resets_to_first() -> None:
    """A stale pick IS corrected — to "no pick", so the reader follows the
    platform default from then on rather than today's answer frozen in."""
    result = resolve_chat_defaults(CATALOG, "deleted-model", "high")
    assert result == ChatDefaults("opus", "high", reset=True, defaulted=True)
    assert (result.saved_model, result.saved_effort) == (None, None)


def test_an_orphan_effort_with_no_model_is_not_rewritten() -> None:
    """An effort saved without a model steers nothing (it belongs to no model),
    so it is neither honoured nor worth a write."""
    result = resolve_chat_defaults(CATALOG, None, "low")
    assert result == ChatDefaults("opus", "high", reset=False, defaulted=True)


@pytest.mark.parametrize(
    ("saved", "effort", "expected"),
    [
        pytest.param("opus::low", None, ChatDefaults("opus", "low", reset=True), id="variant"),
        pytest.param(
            "opus::ultra", None, ChatDefaults("opus", "high", reset=True), id="variant-bad-effort"
        ),
        pytest.param(
            "opus::low", "medium", ChatDefaults("opus", "medium", reset=True), id="explicit-wins"
        ),
        pytest.param(
            "gone::low", None, ChatDefaults("opus", "high", reset=True, defaulted=True), id="gone"
        ),
    ],
)
def test_a_saved_model_effort_variant_is_read_as_its_halves(
    saved: str, effort: str | None, expected: ChatDefaults
) -> None:
    result = resolve_chat_defaults(CATALOG, saved, effort)
    assert result == expected
    if not expected.defaulted:
        # The correction stores the halves, so the next read is stable.
        again = resolve_chat_defaults(CATALOG, result.saved_model, result.saved_effort)
        assert again == ChatDefaults(result.model_id, result.effort, reset=False)


def test_saved_model_with_invalid_effort_redrives_effort() -> None:
    # gpt has no catalog default → middle-most ("medium"); the stale "ultra" is dropped.
    result = resolve_chat_defaults(CATALOG, "gpt", "ultra")
    assert result == ChatDefaults("gpt", "medium", reset=True)


def test_valid_saved_model_and_effort_are_left_untouched() -> None:
    result = resolve_chat_defaults(CATALOG, "opus", "low")
    assert result == ChatDefaults("opus", "low", reset=False)


def test_saved_effort_wins_over_catalog_default() -> None:
    # "low" is offered by opus, so the user's choice is kept even though the catalog
    # default is "high" — a valid saved effort always wins.
    result = resolve_chat_defaults(CATALOG, "opus", "low")
    assert result.effort == "low"


def test_model_without_efforts_clears_a_stale_saved_effort() -> None:
    result = resolve_chat_defaults(CATALOG, "haiku", "high")
    assert result == ChatDefaults("haiku", None, reset=True)


def test_model_without_efforts_and_no_saved_effort_is_stable() -> None:
    result = resolve_chat_defaults(CATALOG, "haiku", None)
    assert result == ChatDefaults("haiku", None, reset=False)


# --- the don't-wipe-on-outage invariant ------------------------------------


def test_empty_catalog_returns_saved_values_untouched() -> None:
    """A gateway outage (empty selectable list) must NOT wipe the saved default —
    return it verbatim with reset=False so nothing gets persisted over the gap."""
    result = resolve_chat_defaults([], "opus", "high")
    assert result == ChatDefaults("opus", "high", reset=False)


def test_empty_catalog_keeps_even_an_unverifiable_model() -> None:
    # We can't verify "opus" against an empty catalog; we keep it rather than reset.
    result = resolve_chat_defaults([], "some-model", "some-effort")
    assert result == ChatDefaults("some-model", "some-effort", reset=False)


def test_empty_catalog_with_never_set_stays_unset() -> None:
    result = resolve_chat_defaults([], None, None)
    assert result == ChatDefaults(None, None, reset=False)


# --- the hosted default ----------------------------------------------------


def test_hosted_default_names_the_pin_on_the_saas(hosted: None) -> None:
    assert hosted_default_model() == HOSTED_DEFAULT_MODEL_SLUG


def test_hosted_default_names_nothing_on_a_self_hosted_install(self_hosted: None) -> None:
    assert hosted_default_model() is None


def test_never_set_on_the_saas_starts_on_the_hosted_default(hosted: None) -> None:
    """The product decision: everyone who has not chosen starts on one model,
    not on whatever the catalog happens to list first."""
    result = resolve_chat_defaults(HOSTED_CATALOG, None, None)
    assert result == ChatDefaults(HOSTED_DEFAULT_MODEL_SLUG, "high", reset=False, defaulted=True)


def test_the_hosted_default_is_claude_sonnet_5_5() -> None:
    """The product decision, spelled once: a new account starts on Sonnet 5.5."""
    assert HOSTED_DEFAULT_MODEL_SLUG == "claude-sonnet-5.5"


def test_never_set_on_a_self_hosted_install_still_takes_the_first_model(
    self_hosted: None,
) -> None:
    """A customer's own install is not steered onto a model Alkera picked — the
    same catalog resolves to its own first entry."""
    result = resolve_chat_defaults(HOSTED_CATALOG, None, None)
    assert result == ChatDefaults("opus", "high", reset=False, defaulted=True)


def test_a_saas_catalog_without_the_pin_falls_back_to_the_first_model(hosted: None) -> None:
    """An org whose entitlement or BYOK filter excludes the pinned model must
    still get a model — the pin degrades, it does not empty the seed."""
    result = resolve_chat_defaults(CATALOG, None, None)
    assert result == ChatDefaults("opus", "high", reset=False, defaulted=True)


def test_an_explicit_choice_beats_the_hosted_default(hosted: None) -> None:
    """The pin only fills a gap. A person who picked a model keeps it, and
    nothing is rewritten under them."""
    result = resolve_chat_defaults(HOSTED_CATALOG, "gpt", "low")
    assert result == ChatDefaults("gpt", "low", reset=False)


def test_a_stale_choice_on_the_saas_is_corrected_to_the_hosted_default(hosted: None) -> None:
    """A saved model the catalog dropped has to reset to something; on the SaaS
    that something is the pin, not entry zero."""
    result = resolve_chat_defaults(HOSTED_CATALOG, "retired-model", "low")
    assert result == ChatDefaults(HOSTED_DEFAULT_MODEL_SLUG, "high", reset=True, defaulted=True)


def test_an_outage_on_the_saas_does_not_impose_the_hosted_default(hosted: None) -> None:
    """The pin must not become a second way to lose a saved default: an empty
    catalog is still an outage, and the saved values come back untouched."""
    result = resolve_chat_defaults([], "gpt", "low")
    assert result == ChatDefaults("gpt", "low", reset=False)
