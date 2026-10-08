"""What a title may be when it is proposed, and what a stored one may be.

A title is display text — nothing writes it to a filesystem — so it is bounded
in characters rather than bytes and may hold a ``/``, a ``.`` and a ``CON``.
The ceiling exists so a title stays a title: a heading a row can show and a
person can read back, not a paragraph pasted into the field.

The other half is that the ceiling only ever judges an *incoming* title. Rows
predate it, and a read that refused one would take a chat away from its owner
over a heading.
"""

from __future__ import annotations

import uuid

import pytest
from alkera_core.schemas.objects.api import (
    MAX_TITLE_LENGTH,
    ChatCreate,
    ChatPromoteRequest,
    ChatTemplateUpdate,
    SaveAsTemplate,
    WorkspaceObjectCreate,
    WorkspaceObjectUpdate,
)
from pydantic import BaseModel, ValidationError

#: Every request shape that carries a title, and whether it may omit one.
_CARRIERS: tuple[tuple[type[BaseModel], dict[str, object]], ...] = (
    (ChatCreate, {}),
    (ChatPromoteRequest, {"event_id": "ev_1"}),
    (WorkspaceObjectCreate, {"type": "result", "client_id": "cl_1"}),
    (WorkspaceObjectUpdate, {"expected_version": 1}),
    (SaveAsTemplate, {"source_chat_id": uuid.uuid4()}),
    (ChatTemplateUpdate, {"expected_version": 1}),
)

_CASES = [pytest.param(model, rest, id=model.__name__) for model, rest in _CARRIERS]


def test_the_ceiling_is_a_heading_not_a_paragraph() -> None:
    """Pinned outright, because every case below is relative to it."""
    assert MAX_TITLE_LENGTH == 200


@pytest.mark.parametrize(("model", "rest"), _CASES)
def test_a_title_at_the_ceiling_is_accepted(
    model: type[BaseModel], rest: dict[str, object]
) -> None:
    built = model(title="t" * MAX_TITLE_LENGTH, **rest)
    assert built.title == "t" * MAX_TITLE_LENGTH


@pytest.mark.parametrize(("model", "rest"), _CASES)
def test_a_title_one_character_over_the_ceiling_is_refused(
    model: type[BaseModel], rest: dict[str, object]
) -> None:
    with pytest.raises(ValidationError) as raised:
        model(title="t" * (MAX_TITLE_LENGTH + 1), **rest)
    assert any(error["type"] == "string_too_long" for error in raised.value.errors())


def test_the_ceiling_counts_characters_and_not_bytes() -> None:
    """Three-byte characters are 600 bytes and still a legal title.

    The name ceiling is bytes because a name lands on a filesystem; a title
    lands in a column and on a screen, so bounding it in bytes would give a
    Japanese heading a third of the room an English one gets.
    """
    built = ChatCreate(title="あ" * MAX_TITLE_LENGTH)
    assert built.title is not None
    assert len("あ".encode()) * MAX_TITLE_LENGTH > MAX_TITLE_LENGTH
    assert len(built.title) == MAX_TITLE_LENGTH


def test_a_title_stored_before_the_ceiling_still_reads_back() -> None:
    """No read shape bounds the title, so a long row is not taken away.

    Asserted against every model in the module rather than one hand-picked
    read: the moment somebody puts ``max_length`` on a response field, a chat
    titled with a pasted paragraph stops loading for its owner, and the failure
    lands at serialization time in production rather than here.
    """
    import alkera_core.schemas.objects.api as api_module

    long_title = "t" * (MAX_TITLE_LENGTH * 3)
    bounded: list[str] = []
    for name in dir(api_module):
        candidate = getattr(api_module, name)
        if not isinstance(candidate, type) or not issubclass(candidate, BaseModel):
            continue
        field = candidate.model_fields.get("title")
        if field is None or candidate in {model for model, _ in _CARRIERS}:
            continue
        try:
            candidate.model_validate({"title": long_title}, strict=False)
        except ValidationError as exc:
            if any(error["type"] == "string_too_long" for error in exc.errors()):
                bounded.append(f"{candidate.__name__}.title")
    assert bounded == []
