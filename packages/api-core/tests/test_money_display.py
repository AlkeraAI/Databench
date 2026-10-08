"""The user-facing money reading, held to the same vectors as the web formatter."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from alkera_core.money import BELOW_ONE_CENT, NO_AMOUNT, usd_display, usd_label

_VECTORS_PATH = Path(__file__).resolve().parents[3] / "packages/chat-model/src/moneyVectors.json"
# JSON is UTF-8 by definition; the platform's locale codec (cp1252 on Windows) is not.
_VECTORS: dict[str, Any] = json.loads(_VECTORS_PATH.read_text(encoding="utf-8"))


def test_the_shared_vectors_are_plain_ascii() -> None:
    # Every reader — vitest's JSON import, this module, any future one — must see the
    # same readings. An escaped "—" parses identically under any codec; a raw
    # em dash parsed as cp1252 became "â€”" and failed the Windows gate.
    assert _VECTORS_PATH.read_bytes().isascii()


@pytest.mark.parametrize(
    ("nanos", "reads"),
    [pytest.param(int(v["nanos"]), v["reads"], id=v["id"]) for v in _VECTORS["nanos"]],
)
def test_usd_label_reads_nanos_like_the_web(nanos: int, reads: str) -> None:
    assert usd_label(nanos) == reads


@pytest.mark.parametrize(
    ("usd", "reads"),
    [pytest.param(v["usd"], v["reads"], id=v["id"]) for v in _VECTORS["usd"]],
)
def test_usd_display_reads_amounts_like_the_web(usd: float | int | str, reads: str) -> None:
    assert usd_display(usd) == reads


def test_a_float_rounds_from_its_decimal_not_its_binary_value() -> None:
    # 1.005 is stored as 1.00499999…; the exact binary value would round down.
    assert f"{1.005:.2f}" == "1.00"
    assert usd_display(1.005) == "$1.01"


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(float("nan"), id="nan"),
        pytest.param(float("inf"), id="inf"),
        pytest.param("Infinity", id="infinity-string"),
        pytest.param("1,000", id="grouped-string"),
        pytest.param("", id="empty"),
    ],
)
def test_a_value_that_is_not_an_amount_reads_as_none(value: float | str) -> None:
    assert usd_display(value) == NO_AMOUNT


def test_decimal_input_is_exact() -> None:
    assert usd_display(Decimal("0.004999999999")) == BELOW_ONE_CENT
    assert usd_display(Decimal("0.005")) == "$0.01"
