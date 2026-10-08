"""Reading a stored id that may be missing or malformed."""

from __future__ import annotations

from uuid import UUID

import pytest
from backend.utils.ids import uuid_or_none

ID = UUID("12345678-1234-5678-9abc-def012345678")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        pytest.param(str(ID), ID, id="canonical"),
        pytest.param(ID.hex, ID, id="undashed-hex"),
        pytest.param(str(ID).upper(), ID, id="upper-case"),
        pytest.param(None, None, id="missing"),
        pytest.param("", None, id="empty"),
        pytest.param("not-a-uuid", None, id="malformed"),
        pytest.param(str(ID)[:-1], None, id="truncated"),
    ],
)
def test_uuid_or_none(value: str | None, expected: UUID | None) -> None:
    assert uuid_or_none(value) == expected
