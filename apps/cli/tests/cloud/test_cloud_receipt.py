"""The receipt the daemon lifts off a recorded ``sql.query`` call.

A promoted result is credible because of what rides beside its rows, and the
cloud renders every field of it inline. So the receipt must be COMPLETE —
connection, role, engine, when, how many rows, how long — from the tool's own
provenance block, must still be a receipt for a result recorded before the
tool carried one, and must never hand the cloud a null where it expects a
name or a number (the cloud's receipt refuses that, and a refused upload is a
promotion that silently never happens).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from alkera_cli.cloud.receipt import ReceiptPrincipal, ResultReceipt, receipt_for_tool_result
from alkera_core.schemas.objects.specs import Receipt as CloudReceipt

AT = datetime(2026, 9, 7, 12, 0, 5, tzinfo=UTC)
STARTED = AT - timedelta(milliseconds=900)
INPUT: dict[str, Any] = {
    "connection": "pg-main",
    "sql": "select 1",
    "params": {"customer": "acme"},
}
PROVENANCE: dict[str, Any] = {
    "connection_id": "rec-42",
    "connection_name": "pg-main",
    "role": "analytics_readonly",
    "engine": "postgres",
    "executed_at": "2026-09-07T12:00:04.600+00:00",
    "duration_ms": 412,
    "row_count": 120,
    "sql": "select id from orders where customer = %(customer)s",
}


def _receipt(output: dict[str, Any], *, started: datetime | None = STARTED) -> ResultReceipt:
    return receipt_for_tool_result(
        INPUT,
        output,
        event_id="res-1",
        at=AT,
        started=started,
        user_id="u1",
        agent_id="chat-1",
    )


def test_the_receipt_copies_the_tools_provenance_whole() -> None:
    receipt = _receipt({"row_count": 50, "provenance": dict(PROVENANCE)})
    assert receipt.sql == PROVENANCE["sql"], "the statement that RAN, not the one asked for"
    assert receipt.connection_id == "rec-42"
    assert receipt.connection_name == "pg-main"
    assert receipt.role == "analytics_readonly"
    assert receipt.engine == "postgres"
    assert receipt.executed_at == datetime(2026, 9, 7, 12, 0, 4, 600_000, tzinfo=UTC)
    assert receipt.duration_ms == 412, "the connector's clock, not the transcript's"
    assert receipt.row_count == 120, "the rows the statement returned, not the preview"
    assert receipt.params == {"customer": "acme"}
    assert receipt.event_id == "res-1"
    assert receipt.principal.user_id == "u1"
    assert receipt.principal.agent_id == "chat-1"
    assert receipt.principal.chain == ["u1", "chat-1"]


def test_without_provenance_the_transcript_fills_what_it_knows() -> None:
    receipt = _receipt({"row_count": 3, "engine": "postgres"})
    assert receipt.sql == "select 1"
    assert receipt.connection_name == "pg-main"
    assert receipt.connection_id is None
    assert receipt.role == "", "unknown is an empty name, never a null"
    assert receipt.engine == "postgres"
    assert receipt.executed_at == AT
    assert receipt.duration_ms == 900, "the call's own event times"
    assert receipt.row_count == 3


def test_a_call_with_no_recorded_start_reports_a_zero_duration_not_a_null() -> None:
    receipt = _receipt({"row_count": 3}, started=None)
    assert receipt.duration_ms == 0


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        pytest.param("executed_at", "not a time", AT, id="unparseable-time-falls-back"),
        pytest.param("executed_at", "", AT, id="empty-time-falls-back-to-the-event"),
        pytest.param("duration_ms", "fast", 900, id="non-numeric-duration-falls-back"),
        pytest.param("duration_ms", True, 900, id="boolean-duration-is-not-a-number"),
        pytest.param("row_count", None, 50, id="missing-row-count-falls-back-to-the-output"),
        pytest.param("role", None, "", id="null-role-becomes-an-empty-name"),
        pytest.param("connection_id", "", None, id="empty-connection-id-is-none"),
    ],
)
def test_a_malformed_provenance_value_degrades_to_what_the_transcript_knows(
    field: str, value: Any, expected: Any
) -> None:
    provenance = {**PROVENANCE, field: value}
    receipt = _receipt({"row_count": 50, "provenance": provenance})
    assert getattr(receipt, field) == expected


def test_a_provenance_that_is_not_an_object_is_ignored() -> None:
    receipt = _receipt({"row_count": 3, "provenance": "postgres"})
    assert receipt.role == "" and receipt.row_count == 3 and receipt.executed_at == AT


def test_a_naive_timestamp_is_read_as_utc() -> None:
    receipt = _receipt({"provenance": {**PROVENANCE, "executed_at": "2026-09-07T12:00:04"}})
    assert receipt.executed_at == datetime(2026, 9, 7, 12, 0, 4, tzinfo=UTC)


@pytest.mark.parametrize(
    ("output", "started"),
    [
        pytest.param({"row_count": 50, "provenance": dict(PROVENANCE)}, STARTED, id="stamped"),
        pytest.param({"row_count": 3}, STARTED, id="unstamped"),
        pytest.param({"row_count": 3}, None, id="unstamped-without-a-start"),
        pytest.param({}, None, id="bare"),
    ],
)
def test_the_cloud_accepts_every_receipt_the_daemon_builds(
    output: dict[str, Any], started: datetime | None
) -> None:
    """The cloud's ``Receipt`` types ``role`` as a name and ``duration_ms`` as a
    number; a null in either refuses the whole upload."""
    receipt = _receipt(output, started=started)
    accepted = CloudReceipt.model_validate(receipt.model_dump(mode="json"))
    assert isinstance(accepted.role, str)
    assert isinstance(accepted.duration_ms, int)
    assert accepted.executed_at, "a receipt is always dated"
    assert accepted.connection_name == "pg-main"


def test_a_principal_the_caller_resolved_is_carried_whole() -> None:
    """The mirror knows for whom a turn ran and as which machine it publishes;
    the builder copies that chain as given rather than rebuilding it from the
    box's own ids."""
    resolved = ReceiptPrincipal(
        user_id="member-7", agent_id="machine-1", chain=["member-7", "machine-1"]
    )
    receipt = receipt_for_tool_result(
        INPUT,
        {"row_count": 1, "provenance": dict(PROVENANCE)},
        event_id="res-1",
        at=AT,
        started=STARTED,
        principal=resolved,
        user_id="operator",
        agent_id="chat-1",
    )
    assert receipt.principal == resolved
    assert receipt.principal.chain == ["member-7", "machine-1"]
    assert "operator" not in receipt.principal.chain
    assert receipt.role == "analytics_readonly", "the provenance still rides beside it"


def test_without_a_resolved_principal_the_ids_build_the_chain() -> None:
    receipt = receipt_for_tool_result(INPUT, {}, event_id="res-1", at=AT, started=None)
    assert receipt.principal == ReceiptPrincipal()
    assert receipt.principal.chain == [], "no id, no link — never an empty string in the chain"
