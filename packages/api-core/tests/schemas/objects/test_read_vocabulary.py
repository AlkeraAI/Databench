"""The object read shape's ``type`` is the model's vocabulary, not a copy of it.

A copy is how a chat template came to answer 500: the column and the CHECK
learned the new type, the wire shape did not, and the first read of one failed
response validation. The shape now annotates ``type`` with the model's own
alias, so this file pins the sharing itself and the two edges of it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, get_args
from uuid import uuid4

import pytest
from alkera_core.models.workspace_object import OBJECT_TYPES, ObjectType
from alkera_core.schemas.objects.api import WorkspaceObjectRead
from pydantic import ValidationError


def _row(object_type: str) -> dict[str, Any]:
    now = datetime.now(UTC)
    return {
        "id": uuid4(),
        "logical_id": "vocab",
        "namespace": "workspace",
        "type": object_type,
        "title": "Admitted",
        "version": 1,
        "status": "ready",
        "spec": {},
        "owner_user_id": uuid4(),
        "visibility_scope": "org",
        "created_at": now,
        "updated_at": now,
        "content_updated_at": now.timestamp(),
    }


def test_the_read_shape_spells_exactly_the_types_the_column_admits() -> None:
    """The alias itself, not a copy of it: an identical hand-written list would
    agree on values today and drift on the next type, which is the bug this pins."""
    annotation = WorkspaceObjectRead.model_fields["type"].annotation
    assert annotation is ObjectType
    assert get_args(annotation) == OBJECT_TYPES


@pytest.mark.parametrize("object_type", OBJECT_TYPES, ids=str)
def test_a_row_of_every_admitted_type_validates(object_type: str) -> None:
    assert WorkspaceObjectRead.model_validate(_row(object_type)).type == object_type


def test_a_type_the_column_refuses_is_refused_on_the_wire_too() -> None:
    """Sharing the vocabulary did not widen it to any string."""
    with pytest.raises(ValidationError):
        WorkspaceObjectRead.model_validate(_row("dashboard"))
