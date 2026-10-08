"""The editing rule itself, and the file store held to the shared contract
(``vectors/editing_rule.json``, which the platform's Loro store replays too)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from alkera_notebook.document.editing import FocusClaim, blocker, editing_now, holds_for
from alkera_notebook.document.file_store import FileDocumentStore
from alkera_notebook.document.fmt import default_format
from alkera_notebook.document.ops import InsertCell, ReplaceCell
from alkera_notebook.document.store import EditingInfo
from alkera_notebook.engine.models import Actor
from nbeng_fakes import ManualClock

VECTORS = json.loads(
    (Path(__file__).parent / "vectors" / "editing_rule.json").read_text(encoding="utf-8")
)
T0 = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
PATH = "nb.alknb.py"


def _actor(key: str) -> Actor:
    who = VECTORS["actors"][key]
    return Actor(kind=who["kind"], id=key, display_name=who["name"], can_edit=True, can_run=True)


@pytest.mark.parametrize("case", [pytest.param(c, id=c["name"]) for c in VECTORS["cases"]])
async def test_the_file_store_answers_the_editing_contract(
    tmp_path: Path, case: dict[str, Any]
) -> None:
    clock = ManualClock(T0)
    store = FileDocumentStore(str(tmp_path), fmt=default_format(), clock=clock)
    made = await store.create(
        PATH, [InsertCell(source="x = 1"), InsertCell(source="y = x")], {}, _actor("ann")
    )
    cells = dict(zip(("a", "b"), [c.id for c in made.document.live_cells()], strict=True))
    names = {cid: name for name, cid in cells.items()}
    # An actor is a caret publisher from its first caret on, in any order of events.
    for event in case["events"]:
        if event["do"] == "caret":
            await store.focus(PATH, _actor(event["actor"]), None)
    elapsed = 0
    for n, event in enumerate(case["events"]):
        clock.advance(event["t"] - elapsed)
        elapsed = event["t"]
        actor = _actor(event["actor"])
        if event["do"] == "caret":
            await store.focus(PATH, actor, cells[event["cell"]] if event["cell"] else None)
        else:
            await store.apply(
                PATH,
                [ReplaceCell(cell_id=cells[event["cell"]], source=f"v = {n}")],
                None,
                actor,
                None,
            )
    clock.advance(case["at"] - elapsed)
    found = await store.editing(PATH)
    assert {
        names[cid]: [[i.actor_id, i.caret] for i in infos] for cid, infos in found.items()
    } == case["editing"]


def _info(actor: str, *, caret: bool) -> EditingInfo:
    return EditingInfo(actor_id=actor, display_name=actor, kind="person", at=T0, caret=caret)


@pytest.mark.parametrize(
    ("infos", "requester", "waits_for"),
    [
        pytest.param([], "ann", None, id="nobody there"),
        pytest.param([_info("bob", caret=True)], "ann", "bob", id="someone else's caret"),
        pytest.param([_info("bot", caret=False)], "ann", "bot", id="someone else's edit"),
        pytest.param([_info("ann", caret=False)], "ann", None, id="the requester's own edit"),
        pytest.param([_info("ann", caret=True)], "ann", "ann", id="the requester's own caret"),
        pytest.param(
            [_info("ann", caret=False), _info("bob", caret=True)],
            "ann",
            "bob",
            id="the requester's edit beside someone else's caret",
        ),
    ],
)
def test_whose_editing_makes_a_rerun_wait(
    infos: list[EditingInfo], requester: str, waits_for: str | None
) -> None:
    found = blocker(infos, requester)
    assert (found.actor_id if found else None) == waits_for


def test_an_edit_claims_nothing_for_an_actor_whose_caret_is_elsewhere() -> None:
    claims = [
        FocusClaim("bob", "Bob", "person", "a", T0 + timedelta(seconds=5), caret=False),
        FocusClaim("bob", "Bob", "person", "b", T0, caret=True),
    ]
    found = editing_now(claims, now=T0 + timedelta(seconds=6))
    assert {cid: [i.caret for i in infos] for cid, infos in found.items()} == {"b": [True]}


@pytest.mark.parametrize(
    ("caret", "age", "left"),
    [
        pytest.param(False, 0.0, 15.0, id="a fresh edit holds 15 s"),
        pytest.param(False, 6.0, 9.0, id="an edit 6 s old holds 9 s more"),
        pytest.param(False, 15.0, 0.0, id="an edit at its window's end holds no longer"),
        pytest.param(False, 40.0, 0.0, id="a lapsed edit never holds a negative time"),
        pytest.param(True, 0.0, 30.0, id="a fresh caret holds 30 s unless confirmed"),
        pytest.param(True, 12.0, 18.0, id="a caret 12 s unconfirmed holds 18 s more"),
    ],
)
def test_how_long_a_claim_still_holds(caret: bool, age: float, left: float) -> None:
    info = EditingInfo(actor_id="agent:c", display_name="A", kind="agent", at=T0, caret=caret)
    assert holds_for(info, now=T0 + timedelta(seconds=age)) == timedelta(seconds=left)
