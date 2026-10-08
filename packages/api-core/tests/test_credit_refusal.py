"""The money-refusal vocabulary: what the gateway stamps and what the
translator lifts back off whatever wrapper the agent put around it.

The two halves are asserted against each other here — a body built by
``envelope()`` must come back as the same refusal through the parser, through
every wrapper an agent is known to use — so a change to either side that the
other cannot read turns this file red rather than degrading the reader's card
to a raw sentence.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from alkera_core.credit_refusal import (
    MESSAGES,
    CreditRefusal,
    CreditRefusalCode,
    credit_refusal_from_error_payload,
    refusal_message,
)

_RESET = datetime(2026, 10, 1, tzinfo=UTC)

POOL = CreditRefusal(
    code=CreditRefusalCode.POOL_EXHAUSTED,
    message="The Data Science pool is used up.",
    team_id="t-1",
    team_name="Data Science",
)
CYCLE = CreditRefusal(
    code=CreditRefusalCode.CYCLE_EXHAUSTED,
    message="Your cycle allowance is used up.",
    resets_at=_RESET,
    manage_url="https://app.alkera.dev/settings/billing",
)


def test_every_code_has_a_plain_sentence() -> None:
    for code in CreditRefusalCode:
        sentence = refusal_message(code, team_name="Data Science")
        assert sentence.endswith("."), code
        assert "{" not in sentence, code
        assert "http" not in sentence, code
    assert set(MESSAGES) == set(CreditRefusalCode)


def test_the_envelope_is_the_api_s_error_shape_with_only_the_facts_set() -> None:
    assert POOL.envelope() == {
        "type": "error",
        "error": {
            "type": "gateway_error",
            "code": "credit.pool_exhausted",
            "message": "The Data Science pool is used up.",
            "team_id": "t-1",
            "team_name": "Data Science",
        },
    }
    assert CYCLE.envelope() == {
        "type": "error",
        "error": {
            "type": "gateway_error",
            "code": "credit.cycle_exhausted",
            "message": "Your cycle allowance is used up.",
            "resets_at": "2026-10-01T00:00:00+00:00",
            "manage_url": "https://app.alkera.dev/settings/billing",
        },
    }


def _opencode_wrapper(body: dict[str, Any]) -> dict[str, Any]:
    """How opencode surfaces a non-200 from the gateway: the AI SDK's
    ``APICallError`` with the raw body kept as a STRING under
    ``responseBody`` and only the message lifted out."""
    return {
        "name": "APIError",
        "data": {
            "message": body["error"]["message"],
            "statusCode": 402,
            "isRetryable": False,
            "responseBody": json.dumps(body),
        },
    }


@pytest.mark.parametrize(
    ("wrap", "refusal"),
    [
        pytest.param(lambda body: body, POOL, id="the-envelope-itself"),
        pytest.param(lambda body: body["error"], CYCLE, id="the-inner-error-object"),
        pytest.param(_opencode_wrapper, POOL, id="opencode-responseBody-string"),
        pytest.param(_opencode_wrapper, CYCLE, id="opencode-responseBody-string-with-reset"),
        pytest.param(lambda body: json.dumps(body), POOL, id="the-whole-payload-as-a-string"),
        pytest.param(
            lambda body: {"data": {"message": json.dumps(body)}},
            POOL,
            id="stringified-inside-message",
        ),
        pytest.param(lambda body: {"errors": [body]}, CYCLE, id="inside-a-list"),
    ],
)
def test_the_parser_lifts_the_refusal_off_every_wrapper(wrap: Any, refusal: CreditRefusal) -> None:
    parsed = credit_refusal_from_error_payload(wrap(refusal.envelope()))
    assert parsed is not None
    assert parsed.code is refusal.code
    assert parsed.message == refusal.message
    assert parsed.team_id == refusal.team_id
    assert parsed.team_name == refusal.team_name
    assert parsed.resets_at == refusal.resets_at
    assert parsed.manage_url == refusal.manage_url


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(None, id="nothing"),
        pytest.param("no credit available (seat or pool)", id="prose-that-mentions-credit"),
        pytest.param({"data": {"message": "insufficient credit"}}, id="message-without-a-code"),
        pytest.param({"error": {"code": "context_length_exceeded"}}, id="another-code"),
        pytest.param({"error": {"code": "credit.something_else"}}, id="an-unknown-credit-code"),
        pytest.param({"data": {"responseBody": "{not json"}}, id="a-body-that-is-not-json"),
    ],
)
def test_prose_and_other_codes_never_classify_as_a_money_refusal(payload: Any) -> None:
    """The code is the signal: a message that merely says "credit" is not one,
    or a provider outage whose text mentions credit would send a reader to top
    up over an outage."""
    assert credit_refusal_from_error_payload(payload) is None


def test_a_malformed_reset_drops_the_reset_but_keeps_the_refusal() -> None:
    parsed = credit_refusal_from_error_payload(
        {"code": "credit.cycle_exhausted", "message": "x", "resets_at": "next tuesday"}
    )
    assert parsed is not None
    assert parsed.code is CreditRefusalCode.CYCLE_EXHAUSTED
    assert parsed.resets_at is None


def test_a_code_with_no_message_gets_the_code_s_own_sentence() -> None:
    parsed = credit_refusal_from_error_payload(
        {"code": "credit.member_pool_limit_exhausted", "team_name": "Growth"}
    )
    assert parsed is not None
    assert parsed.message == "Your limit on the Growth pool is used up."


def test_the_approval_code_is_in_the_vocabulary_and_round_trips() -> None:
    """Reserved for a spend cap an approver must lift. No gateway state emits it
    yet; the reader's surface already renders it, so the day one does nothing
    on the reading side changes."""
    code = CreditRefusalCode.CAP_APPROVAL_REQUIRED
    body = CreditRefusal(code=code, message=refusal_message(code)).envelope()
    parsed = credit_refusal_from_error_payload(_opencode_wrapper(body))
    assert parsed is not None
    assert parsed.code is code
    assert parsed.message == "This request needs a spend-cap approval."


def test_the_persisted_shape_survives_an_unknown_field_from_a_newer_writer() -> None:
    """The row is persisted on the transcript: a newer machine's extra field
    must ride through an older reader untouched."""
    row = {**POOL.model_dump(mode="json"), "hint": "future"}
    back = CreditRefusal.model_validate(row)
    assert back.code is CreditRefusalCode.POOL_EXHAUSTED
    assert back.model_dump(mode="json")["hint"] == "future"


# --------------------------------------------------------------------------- #
# Lineage — every shape this refusal has ever been written in still reads.
# --------------------------------------------------------------------------- #

_FIXTURES = sorted((Path(__file__).parent / "fixtures" / "credit_refusal").glob("v*.json"))


def test_the_refusal_lineage_has_a_fixture_per_shipped_version() -> None:
    """The fixtures ARE the regression net; an empty directory silently disarms
    every case below."""
    assert {p.stem for p in _FIXTURES} >= {"v1_0_0", "v1_1_0", "v1_2_0"}
    assert f"v{CreditRefusal.SCHEMA_VERSION.replace('.', '_')}" in {p.stem for p in _FIXTURES}


@pytest.mark.parametrize("path", _FIXTURES, ids=lambda p: p.stem)
def test_a_refusal_written_by_any_past_version_still_reads(path: Path) -> None:
    """A transcript row written months ago is read by today's code. An older
    payload must load, keep every fact it carried, and re-serialize at the
    current version."""
    payload = json.loads(path.read_text())
    refusal = CreditRefusal.model_validate(payload)
    assert refusal.code.value == payload["code"]
    assert refusal.message == payload["message"]
    assert refusal.schema_version == CreditRefusal.SCHEMA_VERSION
    assert refusal.estimated_cost_nanos == payload.get("estimated_cost_nanos")
    assert refusal.available_nanos == payload.get("available_nanos")


def test_a_refusal_from_before_the_amounts_reports_no_amounts() -> None:
    """The fields are additive: a payload written without them must not acquire
    invented zeros, which would read on screen as an empty balance."""
    old = CreditRefusal.model_validate(json.loads(_FIXTURES[0].read_text()))
    assert old.estimated_cost_nanos is None
    assert old.available_nanos is None
    assert "estimated_cost_credits" not in old.envelope()["error"]


def test_a_newer_writer_s_unknown_field_survives_an_older_reader() -> None:
    payload = json.loads(_FIXTURES[-1].read_text()) | {"some_future_fact": "kept"}
    refusal = CreditRefusal.model_validate(payload)
    assert refusal.model_dump()["some_future_fact"] == "kept"


# --------------------------------------------------------------------------- #
# The amounts, on the wire and back off it.
# --------------------------------------------------------------------------- #

_SHORTFALL = CreditRefusal(
    code=CreditRefusalCode.CYCLE_EXHAUSTED,
    message=refusal_message(
        CreditRefusalCode.CYCLE_EXHAUSTED,
        estimated_cost_nanos=2_084_000_000,
        available_nanos=5_000_000,
    ),
    estimated_cost_nanos=2_084_000_000,
    available_nanos=5_000_000,
)


def test_the_shortfall_rides_the_wire_as_display_credits() -> None:
    error = _SHORTFALL.envelope()["error"]
    assert error["estimated_cost_credits"] == 2_084
    assert error["available_credits"] == 5
    assert error["message"].endswith("This request is estimated at 2,084 credits and 5 are left.")


def test_a_refusal_survives_the_round_trip_with_its_amounts() -> None:
    """The gateway writes the envelope; the harness translator reads it back off
    whatever the agent wrapped it in. The figures must mean the same on both
    sides, in nano-USD, despite travelling as credits."""
    wrapped = {"data": {"responseBody": json.dumps(_SHORTFALL.envelope())}}
    parsed = credit_refusal_from_error_payload(wrapped)
    assert parsed is not None
    assert parsed.estimated_cost_nanos == 2_084_000_000
    assert parsed.available_nanos == 5_000_000
    assert parsed.envelope() == _SHORTFALL.envelope()


@pytest.mark.parametrize(
    "error",
    [
        pytest.param({"estimated_cost_credits": 100}, id="estimate-without-balance"),
        pytest.param({"available_credits": 5}, id="balance-without-estimate"),
        pytest.param(
            {"estimated_cost_credits": "100", "available_credits": 5}, id="estimate-as-a-string"
        ),
        pytest.param(
            {"estimated_cost_credits": 100, "available_credits": -5}, id="negative-balance"
        ),
        pytest.param(
            {"estimated_cost_credits": True, "available_credits": 5}, id="estimate-as-a-bool"
        ),
    ],
)
def test_a_half_or_malformed_pair_is_dropped_rather_than_half_reported(error: dict) -> None:
    """One figure alone says nothing a reader can act on, and the payload is
    parsed out of whatever an agent subprocess wrapped it in — so a shape that is
    not two whole non-negative counts is discarded, never coerced."""
    parsed = credit_refusal_from_error_payload(
        {"type": "error", "error": {"code": "credit.cycle_exhausted", **error}}
    )
    assert parsed is not None
    assert parsed.estimated_cost_nanos is None
    assert parsed.available_nanos is None
    assert "estimated_cost_credits" not in parsed.envelope()["error"]


def test_a_refusal_with_no_figures_keeps_the_bare_sentence() -> None:
    assert refusal_message(CreditRefusalCode.POOL_EXHAUSTED, team_name="Growth") == (
        "The Growth pool is used up."
    )
