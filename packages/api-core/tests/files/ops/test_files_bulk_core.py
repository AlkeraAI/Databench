"""The batch plan: what a batch is refused for, and what survives the row.

Two properties are worth pinning here, and neither can be satisfied by the
implementation restating itself. The first is that the *shape* refusals are
decided from the request alone — a batch is too big, or repeats a correlation
id, or omits the field its verb needs — so both the inline path and the queued
path answer them identically. The second is that a plan written onto
``file_ops.result`` and read back is the same batch: an operation whose runner
died mid-batch resumes from what the row says, so any field the round trip
drops is a field the resume silently discards.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from alkera_core.files.bulk import MAX_ITEMS, BulkItemPlan, BulkPlan, plan
from alkera_core.files.errors import InvalidRequest


def _item(**over: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"id": "a", "op": "trash", "item_id": str(uuid.uuid4())}
    body.update(over)
    return body


def test_a_batch_carries_its_items_in_the_order_they_were_sent() -> None:
    """Results are matched by correlation id, but the plan is still ordered:
    a move that depends on a folder created earlier in the same batch only
    works if the runner applies them in the order the caller wrote."""
    folder = uuid.uuid4()
    built = plan(
        [
            {"id": "one", "op": "createFolder", "parent_id": str(folder), "name": "reports"},
            {"id": "two", "op": "star", "item_id": str(folder)},
        ]
    )
    assert [one.id for one in built.items] == ["one", "two"]
    assert built.items[0].op == "createFolder"
    assert built.items[0].name == "reports"
    assert built.cursor == 0
    assert built.results == ()


@pytest.mark.parametrize(
    ("items", "code"),
    [
        pytest.param([], "files.bulk_empty", id="empty"),
        pytest.param([_item(id="a"), _item(id="a")], "files.bulk_duplicate_id", id="duplicate-id"),
        pytest.param([_item(id="  ")], "files.bulk_missing_field", id="blank-correlation-id"),
        pytest.param([_item(op="rename")], "files.bulk_bad_op", id="unknown-verb"),
        pytest.param([{"id": "a", "op": "trash"}], "files.bulk_missing_field", id="trash-no-item"),
        pytest.param(
            [{"id": "a", "op": "createFolder", "parent_id": str(uuid.uuid4())}],
            "files.bulk_missing_field",
            id="create-no-name",
        ),
        pytest.param(
            [{"id": "a", "op": "move", "item_id": str(uuid.uuid4())}],
            "files.bulk_missing_field",
            id="move-no-parent",
        ),
        pytest.param([_item(item_id="not-an-id")], "files.bad_id", id="malformed-id"),
    ],
)
def test_a_batch_the_request_alone_refuses(items: list[dict[str, Any]], code: str) -> None:
    """Each of these is a fact about the request, so it is decided with no node
    read — which is what lets the inline and the queued path agree."""
    with pytest.raises(InvalidRequest) as refused:
        plan(items)
    assert refused.value.code == code


@pytest.mark.parametrize(
    ("count", "accepted"),
    [
        pytest.param(MAX_ITEMS - 1, True, id="one-under-the-cap"),
        pytest.param(MAX_ITEMS, True, id="exactly-the-cap"),
        pytest.param(MAX_ITEMS + 1, False, id="one-over-the-cap"),
    ],
)
def test_the_cap_is_a_boundary_not_a_truncation(count: int, accepted: bool) -> None:
    """A batch at the cap is applied whole; one item more is refused outright.
    Truncating would apply a prefix and answer as though it had done all of
    it, so the over-cap case must raise rather than return a shorter plan."""
    items = [_item(id=f"i{index}") for index in range(count)]
    if not accepted:
        with pytest.raises(InvalidRequest) as refused:
            plan(items)
        assert refused.value.code == "files.bulk_too_large"
        assert refused.value.detail == {"limit": MAX_ITEMS}
        return
    assert len(plan(items).items) == count


def test_a_plan_survives_the_operation_row_unchanged() -> None:
    """Every field the runner needs comes back off ``file_ops.result``.

    Asserted through real JSON rather than the dataclass, because the row is
    ``jsonb``: a value that only survives in memory is a value the resume of a
    killed runner would lose.
    """
    parent = uuid.uuid4()
    item = uuid.uuid4()
    built = plan(
        [
            {
                "id": "one",
                "op": "createFolder",
                "parent_id": str(parent),
                "name": "Q3 plans",
                "if_match": 7,
                "conflict": "rename",
            },
            {"id": "two", "op": "move", "item_id": str(item), "parent_id": str(parent)},
        ]
    )
    read = BulkPlan.load(json.loads(json.dumps(built.dump())))
    assert read == built
    assert read.items[0].if_match == 7
    assert read.items[0].conflict == "rename"
    assert read.items[1].conflict == "fail"


def test_a_resumed_plan_reports_only_what_is_left() -> None:
    """The cursor is how many items are already applied, so a runner that
    re-read the row applies each remaining item exactly once. A cursor the
    round trip dropped would re-apply a prefix whose effects are in the tree."""
    built = plan([_item(id=f"i{index}") for index in range(4)])
    part = BulkPlan(
        items=built.items,
        cursor=2,
        results=({"id": "i0", "status": 204}, {"id": "i1", "status": 404}),
    )
    read = BulkPlan.load(json.loads(json.dumps(part.dump())))
    assert [one.id for one in read.remaining] == ["i2", "i3"]
    assert read.cursor == 2
    assert [one["status"] for one in read.results] == [204, 404]


def test_an_operation_row_with_no_plan_is_refused_not_guessed() -> None:
    """A queued batch whose row carries no items has nothing to run; treating
    that as an empty batch would report ``done`` over work never performed."""
    with pytest.raises(InvalidRequest) as refused:
        BulkPlan.load({"cursor": 0})
    assert refused.value.code == "files.bulk_plan_missing"


def test_an_item_without_optional_fields_writes_none_of_them() -> None:
    """The row holds what the item said and nothing invented: a ``name`` that
    appeared out of nowhere would make a resumed create differ from the one
    the caller asked for."""
    node = uuid.uuid4()
    body = BulkItemPlan(id="one", op="trash", item_id=node).dump()
    assert body == {"id": "one", "op": "trash", "conflict": "fail", "item_id": str(node)}
