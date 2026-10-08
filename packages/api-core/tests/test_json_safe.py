"""The $nonfinite codec — non-finite floats survive a JSON boundary, recoverably."""

from __future__ import annotations

import json
import math
from typing import Any

import pytest
from alkera_core.json_safe import NONFINITE_KEY, json_restore, json_safe


@pytest.mark.parametrize(
    ("value", "token"),
    [
        pytest.param(float("nan"), "nan", id="nan"),
        pytest.param(float("inf"), "inf", id="+inf"),
        pytest.param(float("-inf"), "-inf", id="-inf"),
    ],
)
def test_non_finite_wraps_to_sentinel(value: float, token: str) -> None:
    assert json_safe(value) == {NONFINITE_KEY: token}


@pytest.mark.parametrize(
    "value",
    [pytest.param(0.0, id="zero"), pytest.param(1.5, id="float"), pytest.param(-3.25, id="neg")],
)
def test_finite_floats_untouched(value: float) -> None:
    assert json_safe(value) == value


@pytest.mark.parametrize(
    "value",
    [42, True, False, "x", None, [], {}],
    ids=["int", "true", "false", "str", "none", "list", "dict"],
)
def test_non_floats_untouched(value: Any) -> None:
    assert json_safe(value) == value


def test_recurses_nested_structures() -> None:
    obj = {"rows": [[1, float("inf")], [float("nan"), "ok"]], "n": 2, "meta": {"x": float("-inf")}}
    out = json_safe(obj)
    assert out == {
        "rows": [[1, {NONFINITE_KEY: "inf"}], [{NONFINITE_KEY: "nan"}, "ok"]],
        "n": 2,
        "meta": {"x": {NONFINITE_KEY: "-inf"}},
    }


def test_recurses_into_tuples_and_wraps_their_non_finite_floats() -> None:
    # `json.dumps` serializes a tuple as a JSON array regardless, so a non-finite float
    # inside a tuple would otherwise reach the encoder unwrapped and emit a bare `NaN`.
    # json_safe must descend into tuples (returning a list) and wrap them.
    out = json_safe({"values": (float("nan"), 1.0, (float("inf"),))})
    assert out == {"values": [{NONFINITE_KEY: "nan"}, 1.0, [{NONFINITE_KEY: "inf"}]]}
    json.dumps(out, allow_nan=False)  # strict-parseable: no NaN/Infinity token escaped


def test_json_safe_is_strict_serializable() -> None:
    # The oracle: allow_nan=False raises on a real non-finite float, so a clean
    # dump proves no invalid NaN/Infinity token reaches a strict parser.
    obj = {"a": float("nan"), "b": [float("inf"), 1.0], "c": float("-inf")}
    json.dumps(json_safe(obj), allow_nan=False)  # must not raise
    assert "NaN" not in json.dumps(json_safe(obj))
    assert "Infinity" not in json.dumps(json_safe(obj))


def test_json_safe_is_idempotent() -> None:
    once = json_safe({"x": float("nan"), "y": float("inf")})
    assert json_safe(once) == once


# --- round-trip (the point: the type is recoverable, not lost) -------------


def test_restore_is_the_inverse() -> None:
    obj = {"rows": [[float("inf"), 1.0]], "x": float("-inf"), "n": 7, "s": "keep"}
    restored = json_restore(json_safe(obj))
    assert restored["rows"][0][0] == float("inf")
    assert restored["rows"][0][1] == 1.0
    assert restored["x"] == float("-inf")
    assert restored["n"] == 7 and restored["s"] == "keep"


def test_restore_nan_round_trips() -> None:
    restored = json_restore(json_safe(float("nan")))
    assert isinstance(restored, float) and math.isnan(restored)


def test_restore_survives_a_json_round_trip() -> None:
    # The real path: wrap -> dumps -> loads -> restore yields the original float.
    wire = json.dumps(json_safe({"v": float("inf")}))
    assert json_restore(json.loads(wire))["v"] == float("inf")


def test_restore_leaves_non_wrapper_dicts_alone() -> None:
    # An unknown token, or extra keys, is NOT a wrapper — left untouched.
    assert json_restore({NONFINITE_KEY: "garbage"}) == {NONFINITE_KEY: "garbage"}
    assert json_restore({NONFINITE_KEY: "nan", "extra": 1}) == {NONFINITE_KEY: "nan", "extra": 1}
    assert json_restore({"a": 1}) == {"a": 1}
