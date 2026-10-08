"""The open default catalog: the current Anthropic and OpenAI models, each at its
provider's public list price with no markup."""

from __future__ import annotations

from decimal import Decimal

import pytest
from alkera_core.llm_provider import Provider
from alkera_core.model_catalog_defaults import DefaultModel, default_models

#: The latest generation of each provider on 2026-10-06, by hand. A model added
#: or retired from the open defaults is a deliberate edit here too.
CURRENT = {
    "claude-sonnet-5.5",
    "claude-opus-5.5",
    "claude-fable-5.1",
    "claude-haiku-4.5",
    "gpt-6-astra",
    "gpt-6.1-sol",
    "gpt-6-luna",
}

#: Input and output list prices per 1M tokens, from the providers' pricing pages.
LIST_PRICES = {
    "claude-sonnet-5.5": ("2.00", "10.00"),
    "claude-opus-5.5": ("4.00", "20.00"),
    "claude-fable-5.1": ("10.00", "50.00"),
    "claude-haiku-4.5": ("1.00", "5.00"),
    "gpt-6-astra": ("10.00", "50.00"),
    "gpt-6.1-sol": ("2.00", "10.00"),
    "gpt-6-luna": ("0.10", "0.50"),
}


def _by_slug() -> dict[str, DefaultModel]:
    return {model.slug: model for model in default_models()}


def test_the_open_defaults_are_the_current_anthropic_and_openai_models() -> None:
    models = default_models()

    assert {model.slug for model in models} == CURRENT
    assert len(models) == len(CURRENT)
    assert {model.provider for model in models} == {Provider.ANTHROPIC, Provider.OPENAI}


@pytest.mark.parametrize("slug", sorted(LIST_PRICES))
def test_a_default_model_carries_its_list_price_as_pass_through(slug: str) -> None:
    model = _by_slug()[slug]
    list_in, list_out = LIST_PRICES[slug]

    assert (model.input_sell, model.output_sell) == (list_in, list_out)
    # Pass-through: what the provider charges is what the model costs.
    assert (Decimal(model.input_cost), Decimal(model.output_cost)) == (
        Decimal(list_in),
        Decimal(list_out),
    )


def test_every_default_model_has_a_price() -> None:
    unpriced = [
        model.slug
        for model in default_models()
        if Decimal(model.input_sell) <= 0 or Decimal(model.output_sell) <= 0
    ]
    assert unpriced == []
