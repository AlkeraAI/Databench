"""``ChatSessionRead.org_id`` is additive on the wire in both directions.

Boxes and servers roll out independently: a new box reads rows from an older
server that sends no org, and an older box reads rows from a new server that
sends one. Neither may fail to parse the row.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from alkera_core.schemas.objects.api import ChatSessionRead

_T = datetime(2026, 10, 5, tzinfo=UTC)


def _wire(**extra: Any) -> dict[str, Any]:
    """A chat row as a server serialized it before the field existed."""
    return {
        "id": str(uuid4()),
        "title": "Ops",
        "owner_user_id": str(uuid4()),
        "machine_id": None,
        "machine_status": "ready",
        "created_at": _T.isoformat(),
        "updated_at": _T.isoformat(),
        "last_seq": 0,
        **extra,
    }


def test_a_row_from_a_server_that_predates_the_field_parses_with_no_org() -> None:
    assert ChatSessionRead.model_validate(_wire()).org_id is None


def test_the_published_schema_does_not_require_the_org() -> None:
    """Every client generated from the schema (the Python and TypeScript SDKs)
    must keep accepting a row from an older server."""
    assert "org_id" not in ChatSessionRead.model_json_schema().get("required", [])


def test_a_reader_that_does_not_know_a_field_still_parses_the_row() -> None:
    """An older box's reader has no ``org_id``; it accepts the new row only
    because a chat read ignores fields it does not know. A newer field stands in
    for ``org_id`` here."""
    read = ChatSessionRead.model_validate(_wire(org_id=str(uuid4()), a_later_field=[1, 2]))
    assert "a_later_field" not in read.model_dump()


@pytest.mark.parametrize("spelled", [str, lambda u: u.hex, lambda u: str(u).upper()])
def test_the_org_round_trips_as_its_canonical_uuid(spelled: Any) -> None:
    org = uuid4()
    read = ChatSessionRead.model_validate(_wire(org_id=spelled(org)))
    assert read.org_id == org
    assert read.model_dump(mode="json")["org_id"] == str(org)


def test_an_org_that_is_not_a_uuid_is_refused() -> None:
    with pytest.raises(ValueError, match="org_id"):
        ChatSessionRead.model_validate(_wire(org_id="not-an-org"))
