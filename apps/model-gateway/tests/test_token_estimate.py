"""The input-token estimator itself: the calibration, the tokenizer seam, and
the properties that make the estimate usable as an admission bound.

The headline property is measured, not asserted from taste: the recorded real
provider interactions vendored under ``vendor/opencode`` pair a genuine request
body with the ``input_tokens`` that provider reported for it, so the estimator
can be scored against the truth it is approximating. Those cases fail if the
calibration drifts into under-holding (billing past the reservation) or back
into the blunt over-holding it replaced.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from alkera_core.config import settings
from model_gateway.adapters import AnthropicMessagesCodec, OpenAIResponsesCodec, _body_char_count
from model_gateway.estimate import (
    CALIBRATIONS,
    FamilyCalibration,
    estimate_input_tokens,
    iter_body_text,
    resolve_max_output_tokens,
)


def _carries_an_attachment(body: dict[str, Any]) -> bool:
    return any(kind is not None for _, kind in iter_body_text(body))


_RECORDINGS = (
    Path(__file__).resolve().parents[3]
    / "vendor"
    / "opencode"
    / "packages"
    / "llm"
    / "test"
    / "fixtures"
    / "recordings"
)


class _FakeEncoding:
    """Stands in for a tiktoken encoding: one token per four characters, which no
    ratio in the table produces, so a test can tell the two paths apart."""

    def __init__(self) -> None:
        self.calls = 0

    def encode_ordinary(self, text: str) -> list[int]:
        self.calls += 1
        return [0] * max(1, len(text) // 4)


def _prose(chars: int) -> str:
    unit = "the quiet river runs past the old stone bridge at dusk "
    return (unit * ((chars // len(unit)) + 1))[:chars]


# --------------------------------------------------------------------------- #
# Calibration, scored against real recorded provider usage.
# --------------------------------------------------------------------------- #


def _sse_objects(raw: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for line in raw.split("\n"):
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            parsed = json.loads(payload)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            out.append(parsed)
    return out


def _recorded_cases() -> list[tuple[str, str, dict[str, Any], int]]:
    """``(id, family, request body, input tokens the provider reported)``."""
    cases: list[tuple[str, str, dict[str, Any], int]] = []
    for path in sorted(_RECORDINGS.rglob("*.json")):
        recording = json.loads(path.read_text())
        for index, interaction in enumerate(recording.get("interactions", [])):
            request = interaction.get("request") or {}
            response = interaction.get("response") or {}
            url, body, raw = request.get("url", ""), request.get("body"), response.get("body")
            if not isinstance(body, str) or not isinstance(raw, str):
                continue
            try:
                parsed = json.loads(body)
            except ValueError:
                continue
            if not isinstance(parsed, dict):
                continue
            tokens = 0
            if "anthropic.com" in url:
                family = "anthropic"
                for obj in _sse_objects(raw):
                    if obj.get("type") == "message_start":
                        usage = obj.get("message", {}).get("usage", {}) or {}
                        tokens = (
                            int(usage.get("input_tokens", 0) or 0)
                            + int(usage.get("cache_read_input_tokens", 0) or 0)
                            + int(usage.get("cache_creation_input_tokens", 0) or 0)
                        )
                        break
            elif "openai.com" in url:
                family = "openai"
                for obj in _sse_objects(raw):
                    usage = obj.get("response", {}).get("usage") if obj.get("response") else None
                    if isinstance(usage, dict):
                        tokens = max(tokens, int(usage.get("input_tokens", 0) or 0))
            else:
                continue
            if tokens > 0:
                cases.append((f"{path.parent.name}/{path.stem}#{index}", family, parsed, tokens))
    return cases


_RECORDED = _recorded_cases()

#: Whether this checkout carries the recorded corpus at all. It lives in the
#: opencode vendor tree, which the CI job that runs this suite drops from its
#: sparse-checkout (`!/vendor/opencode`) because only the e2e job needs a
#: hundred-odd megabytes of it. A checkout that never fetched the directory is a
#: checkout shape, not evidence about the calibration, so the cases that read it
#: skip there and say which of the two it was — while a directory that IS
#: present and yields nothing still fails, which is what the guard is for.
_CORPUS_PRESENT = _RECORDINGS.is_dir()

_needs_corpus = pytest.mark.skipif(
    not _CORPUS_PRESENT,
    reason=f"{_RECORDINGS} is not in this checkout (vendor/opencode is sparse-checked out)",
)


@_needs_corpus
def test_the_recorded_provider_corpus_is_still_where_the_calibration_measured_it() -> None:
    """The calibration is data, and these cases are its only evidence. A moved or
    emptied corpus must fail loudly rather than quietly scoring nothing."""
    assert len(_RECORDED) >= 8
    assert {family for _, family, _, _ in _RECORDED} == {"anthropic", "openai"}


@pytest.fixture(params=["tokenizer", "ratio"])
def real_estimator_path(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> str:
    """Score the corpus on BOTH paths the estimate can take.

    ``ratio`` forces the fallback by refusing every encoding. ``tokenizer``
    leaves this environment as it is: tiktoken is a locked dependency, so it is
    normally the real one — and where it is genuinely absent the case degrades
    to the fallback, which these assertions already hold for. Either way nothing
    skips, and the never-under property below is proven on the path that
    actually runs in production AND on the one a broken install falls back to.
    """
    if request.param == "ratio":
        monkeypatch.setattr("model_gateway.estimate._encoding", lambda _name: None)
    return request.param


@pytest.mark.parametrize(
    ("family", "body", "reported"),
    [pytest.param(f, b, t, id=name) for name, f, b, t in _RECORDED],
)
def test_the_estimate_covers_what_the_provider_really_billed(
    family: str, body: dict[str, Any], reported: int, real_estimator_path: str
) -> None:
    """Never under the truth: a hold below the settled cost is admission control
    that does not admit anything, it just defers the overdraft to settle.

    This is the property that has to survive the tokenizer being swapped in or
    out, so it is asserted on both paths — a real tokenizer is more ACCURATE
    than the ratio, and accuracy is the direction that risks dipping under."""
    assert estimate_input_tokens(body, family=family) >= reported


@pytest.mark.parametrize(
    ("family", "body", "reported"),
    [
        pytest.param(f, b, t, id=name)
        for name, f, b, t in _RECORDED
        # Scored on the bulk TEXT recordings only — the ones the calibration was
        # measured from, and the ones a too-blunt estimate actually refuses. A
        # small request is dominated by two flat terms that are deliberately not
        # ratios: the provider's tool scaffolding, and an attachment's flat
        # content charge. Those legitimately push a 6 KB image request above its
        # chars/3 figure, which says nothing about how a 2 MB prompt is priced.
        if _body_char_count(b) > 5_000 and not _carries_an_attachment(b)
    ],
)
def test_the_estimate_is_closer_than_the_flat_divisor_it_replaced(
    family: str, body: dict[str, Any], reported: int, real_estimator_path: str
) -> None:
    """The bug, measured: chars/3 held a third to three-quarters more than the
    provider charged, which 402s a funded account on a big prompt. Both paths
    have to beat it — the fallback ratio is what serves a deployment where the
    tokenizer did not load, and it is no use if it is as blunt as what it
    replaced."""
    estimate = estimate_input_tokens(body, family=family)
    blunt = max(1, _body_char_count(body) // 3)
    assert estimate < blunt
    assert estimate <= reported * 1.5


# --------------------------------------------------------------------------- #
# The tokenizer seam.
# --------------------------------------------------------------------------- #


def test_a_loaded_tokenizer_is_what_counts_the_text(monkeypatch: pytest.MonkeyPatch) -> None:
    enc = _FakeEncoding()
    monkeypatch.setattr("model_gateway.estimate._encoding", lambda _name: enc)
    text = _prose(40_000)
    estimate = estimate_input_tokens({"model": "gpt-5", "input": text}, family="openai")
    assert enc.calls > 0
    # The fake counts at 4 chars/token where the openai ratio is 4.0 over text
    # plus the model/field names — so the tokenizer's figure is what came out.
    assert abs(estimate - len(text) // 4) < 200


def test_an_absent_tokenizer_degrades_to_the_measured_ratio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """tiktoken is optional and its first load can need the network. Neither may
    reach the admission path as an exception."""
    monkeypatch.setattr("model_gateway.estimate._encoding", lambda _name: None)
    text = _prose(40_000)
    estimate = estimate_input_tokens({"model": "gpt-5", "input": text}, family="openai")
    ratio = CALIBRATIONS["openai"].chars_per_token
    assert abs(estimate - len(text) / ratio) < 200


def test_a_tokenizer_that_cannot_load_is_asked_once_and_never_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A gateway must not reach for an encoding load on the request path at all —
    the load fetches the BPE ranks over the network, with no timeout, and the
    estimator runs inside admission. The warm-up tries each encoding once for
    the life of the process; the traffic that follows adds nothing."""
    import model_gateway.estimate as estimate_module

    attempts: list[str] = []

    class _Boom:
        def get_encoding(self, name: str) -> Any:
            attempts.append(name)
            raise RuntimeError("no network")

    monkeypatch.setitem(__import__("sys").modules, "tiktoken", _Boom())
    estimate_module.reset_encodings_for_tests()
    try:
        assert estimate_module.warm_encodings(timeout=5.0) == dict.fromkeys(
            estimate_module.ENCODING_NAMES, False
        )
        warmed = list(attempts)
        body = {"model": "gpt-5", "input": _prose(1_000)}
        for _ in range(5):
            assert estimate_input_tokens(body, family="openai") > 0
        # Once per encoding, at warm-up, and never once per request.
        assert sorted(warmed) == sorted(estimate_module.ENCODING_NAMES)
        assert attempts == warmed
    finally:
        estimate_module.reset_encodings_for_tests()


def test_an_unreachable_tokenizer_falls_back_inside_the_configured_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The air-gapped shape: an EMPTY cache and a blackholed network, where
    tiktoken's own fetch blocks far past any boot budget. The warm-up must give
    up on its own deadline, leave the estimator on the calibrated ratio, and
    never raise — a tokenizer is an accuracy improvement, not a boot
    requirement.

    The deadline is SHARED across the encodings, because they load in parallel:
    spending it once per name would multiply the boot cost by however many
    encodings the calibration table happens to hold, and the setting would no
    longer bound what it says it bounds. The bound below is tight enough that a
    per-name deadline blows it."""
    import model_gateway.estimate as estimate_module

    encodings = len(estimate_module.ENCODING_NAMES)
    assert encodings >= 2, "a shared deadline is only observable with several encodings"
    timeout = 1.0
    # Generous enough for thread start-up on a loaded host, and still strictly
    # under what a per-name deadline would cost — so the assertion discriminates
    # between the two rather than passing on both.
    slack = 0.6
    assert timeout + slack < timeout * encodings

    started = threading.Event()
    release = threading.Event()

    class _Blackhole:
        def get_encoding(self, name: str) -> Any:
            started.set()
            release.wait(30)
            raise RuntimeError("connect timed out")

    monkeypatch.setitem(__import__("sys").modules, "tiktoken", _Blackhole())
    estimate_module.reset_encodings_for_tests()
    try:
        began = time.monotonic()
        ready = estimate_module.warm_encodings(timeout=timeout)
        waited = time.monotonic() - began
        assert started.is_set(), "the warm-up never attempted the load"
        assert waited >= timeout, f"the warm-up gave up at {waited:.2f}s, before its own deadline"
        assert waited < timeout + slack, (
            f"boot waited {waited:.2f}s for {encodings} hung loads on a {timeout:.2f}s "
            "deadline — the deadline is being spent once per encoding, not shared"
        )
        assert not any(ready.values())
        text = _prose(40_000)
        estimate = estimate_input_tokens({"model": "gpt-5", "input": text}, family="openai")
        ratio = CALIBRATIONS["openai"].chars_per_token
        assert abs(estimate - len(text) / ratio) < 200
    finally:
        release.set()
        estimate_module.reset_encodings_for_tests()


def test_a_slow_encoding_is_adopted_when_it_finally_lands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The deadline bounds the BOOT, not the load. A mirror that is merely slow
    costs the early requests their tokenizer and is picked up for the rest —
    otherwise a one-off blip would pin a whole process on the ratio."""
    import model_gateway.estimate as estimate_module

    release = threading.Event()

    class _Slow:
        def get_encoding(self, name: str) -> Any:
            release.wait(30)
            return _FakeEncoding()

    monkeypatch.setitem(__import__("sys").modules, "tiktoken", _Slow())
    estimate_module.reset_encodings_for_tests()
    try:
        assert not any(estimate_module.warm_encodings(timeout=0.1).values())
        assert estimate_module._encoding("o200k_base") is None
        release.set()
        deadline = time.monotonic() + 10
        while estimate_module._encoding("o200k_base") is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert estimate_module._encoding("o200k_base") is not None
    finally:
        release.set()
        estimate_module.reset_encodings_for_tests()


def test_the_warm_up_loads_from_a_baked_cache_with_no_network(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The offline load the image exists to make possible: the ranks are on disk,
    TIKTOKEN_CACHE_DIR points at them, and the warm-up succeeds without the
    loader ever being allowed to reach the network."""
    import model_gateway.estimate as estimate_module

    monkeypatch.delenv("TIKTOKEN_CACHE_DIR", raising=False)
    monkeypatch.setattr(settings, "gateway_tokenizer_cache_dir", str(tmp_path))
    baked = {name: _FakeEncoding() for name in estimate_module.ENCODING_NAMES}

    class _Offline:
        def get_encoding(self, name: str) -> Any:
            # tiktoken reads this env var to find the ranks; an empty/unset dir
            # is exactly the case that goes to the network.
            cache = os.environ.get("TIKTOKEN_CACHE_DIR")
            if cache != str(tmp_path):
                raise RuntimeError(f"would have fetched {name} over the network (cache={cache!r})")
            return baked[name]

    monkeypatch.setitem(__import__("sys").modules, "tiktoken", _Offline())
    estimate_module.reset_encodings_for_tests()
    try:
        assert estimate_module.warm_encodings(timeout=5.0) == dict.fromkeys(
            estimate_module.ENCODING_NAMES, True
        )
        for name in estimate_module.ENCODING_NAMES:
            assert estimate_module._encoding(name) is baked[name]
    finally:
        estimate_module.reset_encodings_for_tests()


def test_an_environment_cache_dir_wins_over_the_setting(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The image sets TIKTOKEN_CACHE_DIR itself; the setting is what a source
    checkout configures. An operator who redirects the cache by env must not
    have it overwritten from settings."""
    import model_gateway.estimate as estimate_module

    monkeypatch.setenv("TIKTOKEN_CACHE_DIR", str(tmp_path / "operator"))
    monkeypatch.setattr(settings, "gateway_tokenizer_cache_dir", str(tmp_path / "setting"))
    estimate_module._apply_cache_dir()
    assert os.environ["TIKTOKEN_CACHE_DIR"] == str(tmp_path / "operator")


def test_the_image_bakes_every_encoding_the_estimator_will_ask_for() -> None:
    """The gateway image downloads the ranks at build time so the runtime never
    does. A calibration that gains an encoding the Dockerfile does not bake puts
    the network back on the admission path, silently."""
    from model_gateway.estimate import ENCODING_NAMES

    # The gateway image is built from the open tree's Dockerfile.
    repo = Path(__file__).resolve().parents[3]
    dockerfile = (repo / "Databench" / "apps" / "model-gateway" / "Dockerfile").read_text(
        encoding="utf-8"
    )
    assert "TIKTOKEN_CACHE_DIR" in dockerfile
    for name in ENCODING_NAMES:
        assert f"'{name}'" in dockerfile, f"{name} is not baked into the gateway image"


def test_an_older_openai_model_is_counted_with_the_encoding_of_its_era(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked: list[str] = []
    monkeypatch.setattr(
        "model_gateway.estimate._encoding",
        lambda name: asked.append(name) or _FakeEncoding(),  # type: ignore[func-returns-value]
    )
    estimate_input_tokens({"model": "gpt-4-turbo", "input": "hi"}, family="openai")
    assert asked == ["cl100k_base"]
    asked.clear()
    estimate_input_tokens({"model": "gpt-5", "input": "hi"}, family="openai")
    assert asked == ["o200k_base"]


# --------------------------------------------------------------------------- #
# Properties that keep the estimate a usable bound.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("family", ["anthropic", "openai"])
def test_an_uncalibrated_family_keeps_the_blunt_divisor(family: str) -> None:
    """A family nobody has measured must not inherit another's ratio — the blunt
    over-estimate is the only safe guess when the relationship is unknown."""
    body = {"model": "m", "input": _prose(30_000)}
    assert estimate_input_tokens(body, family="mystery-provider") == pytest.approx(
        _body_char_count(body) // 3, rel=0.01
    )
    assert estimate_input_tokens(body, family=family) < _body_char_count(body) // 3


@pytest.mark.parametrize("family", ["anthropic", "openai"])
def test_a_tool_bearing_request_holds_the_scaffolding_the_provider_bills(family: str) -> None:
    """Anthropic charges several hundred tokens for a tool-use system prompt that
    never appears in the body. Not holding it under-prices every agent turn."""
    base: dict[str, Any] = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
    with_tools = {**base, "tools": [{"name": "t", "description": "d"}]}
    overhead = CALIBRATIONS[family].tool_overhead_tokens
    assert overhead > 0
    grew = estimate_input_tokens(with_tools, family=family) - estimate_input_tokens(
        base, family=family
    )
    assert grew >= overhead


@pytest.mark.parametrize("family", ["anthropic", "openai"])
def test_the_estimate_is_monotone_in_the_body(family: str) -> None:
    base = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
    grown = {**base, "messages": [{"role": "user", "content": _prose(9_000)}]}
    assert estimate_input_tokens(grown, family=family) > estimate_input_tokens(base, family=family)


@pytest.mark.parametrize(
    "codec", [AnthropicMessagesCodec(), OpenAIResponsesCodec()], ids=["anthropic", "openai"]
)
def test_an_attachment_is_still_charged_by_content_not_by_its_base64(codec: Any) -> None:
    """The charge moved from characters to tokens with the calibration. It must
    not have moved in VALUE: a 400 KB base64 image still holds roughly the ~2k
    tokens a provider prices an image at, not 130k."""
    body = {
        "model": "m",
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {"type": "base64", "data": "A" * 400_000},
                    }
                ],
            }
        ],
    }
    estimate = codec.estimate_input_tokens(body)
    assert 2_000 <= estimate < 10_000


@pytest.mark.parametrize(
    "codec", [AnthropicMessagesCodec(), OpenAIResponsesCodec()], ids=["anthropic", "openai"]
)
def test_text_hidden_behind_a_data_url_prefix_is_still_counted_in_full(codec: Any) -> None:
    """The flat attachment charge is scoped to a declared binary payload slot. A
    base64-*shaped* string anywhere else is ordinary billable text."""
    text = _prose(60_000)
    disguised = {"model": "m", "messages": [{"role": "user", "content": "data:x;base64," + text}]}
    plain = {"model": "m", "messages": [{"role": "user", "content": text}]}
    assert codec.estimate_input_tokens(disguised) >= codec.estimate_input_tokens(plain)
    assert codec.estimate_input_tokens(disguised) > 5_000


def test_every_calibrated_family_states_a_ratio_and_an_overhead() -> None:
    for family, calibration in CALIBRATIONS.items():
        assert isinstance(calibration, FamilyCalibration), family
        assert calibration.chars_per_token > 0, family
        assert calibration.tool_overhead_tokens >= 0, family


def test_an_empty_body_is_never_free() -> None:
    """A zero hold is an unbounded request."""
    assert estimate_input_tokens({}, family="anthropic") >= 1


# --------------------------------------------------------------------------- #
# The output reserve.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("asked", "default", "expected"),
    [
        pytest.param(32_000, 8_000, 32_000, id="request-wins"),
        pytest.param(None, 64_000, 64_000, id="catalog-when-unasked"),
        pytest.param(None, 0, 4_096, id="catalog-unknown"),
        pytest.param(None, None, 4_096, id="uncataloged"),
        pytest.param(0, 64_000, 64_000, id="zero-is-not-a-ceiling"),
        pytest.param(-5, 64_000, 64_000, id="negative-is-not-a-ceiling"),
        pytest.param(True, 64_000, 64_000, id="bool-is-not-a-count"),
        pytest.param("32000", 64_000, 64_000, id="string-is-not-a-count"),
    ],
)
def test_the_output_reserve_takes_the_first_real_ceiling_it_is_given(
    asked: Any, default: int | None, expected: int
) -> None:
    assert resolve_max_output_tokens(asked, default=default, floor=4_096) == expected
