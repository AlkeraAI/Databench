"""The money refusal a chat turn dies on, from the gateway to the durable row.

A chat turn that runs out of usage fails at the model gateway, which answers
``402`` with a stable code naming WHICH allowance ran out and the facts a
reader needs (the pool's team, the cycle's reset). Two halves have to agree
about that row for the reader to ever see it:

* the machine must put the refusal on the DURABLE transcript — the code and
  the team, under the event kind the browser folds — or the failure lives only
  in the live socket and a reload shows a chat that simply stopped; and
* the browser must key off that code (never the sentence) to say which
  allowance ran out and offer the one next step this reader has.

This file owns the first half. The second is pinned in
``apps/web/src/tests/pages/workspace/chat/chatErrors.test.tsx``, which folds
the SAME codes and facts — so a change to the vocabulary that moves one half
without the other turns one of the two suites red instead of silently
degrading the card back to a raw sentence.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest
from alkera_cli.cloud.publish import RoleIndex, append_entry
from alkera_cli.harness.gateway_completion import GatewayInsufficientCreditError, complete
from alkera_core.credit_refusal import CreditRefusal, CreditRefusalCode
from alkera_core.schemas.chat import SessionStatusChanged

_T = datetime(2026, 9, 16, tzinfo=UTC)
_RESET = datetime(2026, 10, 1, tzinfo=UTC)

#: The refusals the gateway names, as the translator lifts them off the wire.
#: Spelled here as the CONTRACT between the two halves: the browser's fold is
#: asserted against these same codes and facts.
REFUSALS = {
    "cycle": CreditRefusal(
        code=CreditRefusalCode.CYCLE_EXHAUSTED,
        message="Your cycle allowance is used up.",
        resets_at=_RESET,
    ),
    "member-budget": CreditRefusal(
        code=CreditRefusalCode.MEMBER_BUDGET_EXHAUSTED,
        message="Your budget on Acme is used up.",
        team_id="org-1",
        team_name="Acme",
    ),
    "pool": CreditRefusal(
        code=CreditRefusalCode.POOL_EXHAUSTED,
        message="The Data Science pool is used up.",
        team_id="team-1",
        team_name="Data Science",
    ),
    "member-pool-limit": CreditRefusal(
        code=CreditRefusalCode.MEMBER_POOL_LIMIT_EXHAUSTED,
        message="Your limit on the Growth pool is used up.",
        team_id="team-2",
        team_name="Growth",
        manage_url="https://app.alkera.dev/settings/billing",
    ),
    "no-funding": CreditRefusal(
        code=CreditRefusalCode.NO_FUNDING,
        message="Nothing funds this workspace's usage yet.",
    ),
}


def _refused(refusal: CreditRefusal) -> SessionStatusChanged:
    """The terminal event the harness publishes when a turn dies on a refusal."""
    return SessionStatusChanged(
        event_id="refused",
        time=_T,
        session_id="s",
        status="error",
        phase="error",
        detail=refusal.message,
        refusal=refusal,
    )


@pytest.mark.parametrize("name", list(REFUSALS), ids=list(REFUSALS))
def test_a_money_refusal_reaches_the_durable_transcript_with_its_code_and_team(
    name: str,
) -> None:
    """The refusal is PERSISTED, not merely streamed — code, team and reset
    included, under the event kind the browser folds.

    ``append_entry`` rewrites exactly one kind of detail — a raw agent-exit
    crash — into the workspace's own words. A money refusal is not one, so it
    must pass through untouched: the browser keys off the code, and a row the
    machine had stripped would leave it nothing to name the allowance by.
    """
    refusal = REFUSALS[name]
    entry = append_entry(_refused(refusal), RoleIndex())

    assert entry["kind"] == "session.status_changed"
    assert entry["payload"]["status"] == "error"
    assert entry["payload"]["detail"] == refusal.message
    row = entry["payload"]["refusal"]
    assert row["code"] == refusal.code.value
    assert row["team_name"] == refusal.team_name
    assert row["team_id"] == refusal.team_id
    assert row["manage_url"] == refusal.manage_url
    if refusal.resets_at is None:
        assert row["resets_at"] is None
    else:
        assert datetime.fromisoformat(row["resets_at"]) == refusal.resets_at
    # The row is JSON-serializable as stored: this is what a second reader, and
    # a reload, read back — and it reads back as the same refusal.
    stored = json.loads(json.dumps(entry))
    assert CreditRefusal.model_validate(stored["payload"]["refusal"]) == refusal


def test_a_failure_that_is_not_a_refusal_carries_no_refusal_row() -> None:
    """A provider outage must not read as a money refusal on the durable row:
    a reader told to top up over an outage tops up for nothing."""
    event = SessionStatusChanged(
        event_id="outage",
        time=_T,
        session_id="s",
        status="error",
        phase="error",
        detail="upstream error: 503 service unavailable",
    )
    entry = append_entry(event, RoleIndex())
    assert entry["payload"]["refusal"] is None
    assert entry["payload"]["detail"] == "upstream error: 503 service unavailable"


def test_a_row_written_before_refusals_were_named_still_reads() -> None:
    """A transcript row from a machine that predates the code is read by the
    current model with no refusal — the browser's sentence rules still apply."""
    old_row = {
        "schema_version": "1.2.0",
        "event_type": "session.status_changed",
        "event_id": "old",
        "time": _T.isoformat(),
        "session_id": "s",
        "status": "error",
        "phase": "error",
        "detail": "no credit available (seat or pool). Upgrade your plan at http://x",
    }
    event = SessionStatusChanged.model_validate(old_row)
    assert event.refusal is None
    assert event.detail == old_row["detail"]


async def test_the_gateway_s_402_is_raised_as_a_refusal_not_a_transport_fault() -> None:
    """A 402 is an ANSWER — the caller stops the turn on it.

    Driven through the real ``complete`` against a stubbed upstream, so the
    status→exception mapping is exercised rather than described.
    """
    transport = httpx.MockTransport(
        lambda request: httpx.Response(402, json=REFUSALS["pool"].envelope())
    )
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(GatewayInsufficientCreditError):
            await complete(
                gateway_url="http://gw.test",
                token="t",
                model="m",
                system="s",
                messages=[{"role": "user", "content": "hi"}],
                client=client,
            )


async def test_a_provider_fault_is_not_mistaken_for_a_money_refusal() -> None:
    """503 must NOT raise the credit error: the two get different cards, and a
    reader told to top up over a provider outage tops up for nothing."""
    transport = httpx.MockTransport(lambda request: httpx.Response(503, text="overloaded"))
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(Exception) as caught:
            await complete(
                gateway_url="http://gw.test",
                token="t",
                model="m",
                system="s",
                messages=[{"role": "user", "content": "hi"}],
                client=client,
            )
    assert not isinstance(caught.value, GatewayInsufficientCreditError)
