"""The receipt the machine sends and the receipt the cloud stores are one shape.

The daemon assembles a receipt from the tool call it recorded and posts it to
``POST /objects/{id}/payload``; the route validates it as
``alkera_core.schemas.objects.Receipt`` before the payload is stored. Two
models, one wire format — and when they disagree the failure is invisible and
terminal: the upload 422s, the promoted result stays ``pending_upload``, and
the page shows "Saving — the workspace is uploading this result." forever.

So this is the pin, and it is deliberately built from the PRODUCER: every case
below dumps a real ``ResultReceipt`` and feeds those bytes to the real reader.
A hand-written fixture on either side would pass while the seam was broken —
which is exactly what happened before this test existed.

Importing the CLI package from an api-core test is deliberate, and follows
``test_result_blob_parity.py``: the backend may not import ``alkera_cli``
(CLAUDE.md's layering rule), so the boundary is pinned here instead.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

import pytest
from alkera_cli.cloud.receipt import ReceiptPrincipal, ResultReceipt
from alkera_core.schemas.objects import Receipt

EXECUTED_AT = datetime(2026, 9, 7, 12, 0, 0, tzinfo=UTC)


def _full() -> ResultReceipt:
    """Everything the daemon can learn about a query it ran."""
    return ResultReceipt(
        sql="SELECT day, orders FROM daily WHERE customer = %(customer)s",
        connection_id="8c4e3d6f-5031-4d5e-a08f-a4d6f2c3e444",
        connection_name="Tideline Postgres",
        role="analytics_readonly",
        engine="postgres",
        principal=ReceiptPrincipal(
            user_id="00000000-0000-4000-8000-000000000001",
            agent_id="sess-01J7Q3M8",
            chain=["00000000-0000-4000-8000-000000000001", "sess-01J7Q3M8"],
        ),
        executed_at=EXECUTED_AT,
        row_count=3,
        duration_ms=412,
        params={"customer": "acme"},
        event_id="ev-1",
    )


def _unknown() -> ResultReceipt:
    """What the daemon sends when the tool output told it nothing: the shape
    ``_receipt_for`` emits whenever the result carries no ``role`` string and
    no start time was recorded."""
    return ResultReceipt(
        sql="SELECT 1",
        connection_name="Tideline Postgres",
        role=None,
        engine="postgres",
        executed_at=None,
        duration_ms=None,
        event_id="ev-2",
    )


@pytest.mark.parametrize(
    "produce",
    [pytest.param(_full, id="everything_known"), pytest.param(_unknown, id="nothing_known")],
)
def test_the_cloud_reads_what_the_machine_writes(produce: Callable[[], ResultReceipt]) -> None:
    """The one assertion this module exists for: real producer bytes, real
    reader, no exception."""
    Receipt.model_validate(produce().model_dump(mode="json"))


def test_a_receipt_that_learned_nothing_still_arrives() -> None:
    """A bare receipt — every optional field null — is a legal upload. The
    reader renders null as "—"; refusing it would strand the object."""
    stored = Receipt.model_validate(ResultReceipt().model_dump(mode="json"))
    assert stored.role is None
    assert stored.executed_at is None
    assert stored.duration_ms is None


@pytest.mark.parametrize(
    "field", [pytest.param("role"), pytest.param("executed_at"), pytest.param("duration_ms")]
)
def test_each_field_the_daemon_may_leave_unknown_is_optional_on_the_cloud(field: str) -> None:
    """Named one by one: a field the producer types as optional and the reader
    types as required is a 422 waiting for the first query that lacks it."""
    dumped = _full().model_dump(mode="json")
    dumped[field] = None
    assert Receipt.model_validate(dumped).model_dump(mode="json")[field] is None


def test_the_values_the_machine_learned_survive_the_crossing() -> None:
    stored = Receipt.model_validate(_full().model_dump(mode="json"))
    assert stored.sql == "SELECT day, orders FROM daily WHERE customer = %(customer)s"
    assert stored.connection_name == "Tideline Postgres"
    assert stored.role == "analytics_readonly"
    assert stored.engine == "postgres"
    assert stored.executed_at == "2026-09-07T12:00:00Z"
    assert stored.row_count == 3
    assert stored.duration_ms == 412
    assert stored.params == {"customer": "acme"}


def test_the_machine_never_overwrites_the_promoter_on_the_chain() -> None:
    """The daemon spells its own actors ``principal``; the cloud's
    ``principal_chain`` is written at promote time and the upload merges into
    it (R-K). So the daemon's document rides along as an extra — it must not
    arrive as an empty ``principal_chain`` that erases the promoter."""
    stored = Receipt.model_validate(_full().model_dump(mode="json"))
    assert stored.principal_chain == {}, "the daemon does not claim this field"
    assert stored.model_dump(mode="json")["principal"]["agent_id"] == "sess-01J7Q3M8"


def test_an_older_receipt_still_reads() -> None:
    """Widening a field is only safe if what past writers emitted still loads:
    a stored 1.0.0 receipt used ``""`` and ``0`` where null is written now."""
    stored = Receipt.model_validate(
        {"schema_version": "1.0.0", "role": "", "executed_at": "", "duration_ms": 0}
    )
    assert (stored.role, stored.executed_at, stored.duration_ms) == ("", "", 0)
