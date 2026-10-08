"""The Loro store answers ``editing`` by the same contract as the file store.

Both replay ``packages/alkera-notebook/tests/vectors/editing_rule.json``.
Here the backend's view route is a ``MockTransport`` whose presence is the
claims the platform keeps: each person's standing caret (gone the moment
they leave the cell) and the edits of actors that publish no caret. The
store's own clock is pinned, so a claim's age is the vector's.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from alkera_cli.notebooks.store_loro import DocSignal, Located, LoroDocumentStore
from alkera_notebook.document.editing import blocker

VECTORS = json.loads(
    (
        Path(__file__).resolve().parents[4]
        / "packages"
        / "alkera-notebook"
        / "tests"
        / "vectors"
        / "editing_rule.json"
    ).read_text(encoding="utf-8")
)
T0 = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
PATH = "analysis/weekly.alknb.py"


def _presence(case: dict[str, Any]) -> list[dict[str, Any]]:
    """What the platform holds after the case's events."""
    carets: dict[str, tuple[str | None, int]] = {}
    edits: list[dict[str, Any]] = []
    for event in case["events"]:
        if event["do"] == "caret":
            carets[event["actor"]] = (event["cell"], event["t"])
        else:
            edits.append(event)

    def entry(actor: str, cell: str, t: int, *, caret: bool) -> dict[str, Any]:
        who = VECTORS["actors"][actor]
        return {
            "who": who["name"],
            "kind": who["kind"],
            "cell_id": cell,
            "at": (T0 + timedelta(seconds=t)).isoformat(),
            "actor_id": actor,
            "caret": caret,
        }

    found = [
        entry(actor, cell, t, caret=True) for actor, (cell, t) in carets.items() if cell is not None
    ]
    found += [
        entry(e["actor"], e["cell"], e["t"], caret=False) for e in edits if e["actor"] not in carets
    ]
    return found


class _Silent:
    """A document channel nobody follows in these cases."""

    async def watch(self, item_id: str) -> AsyncIterator[DocSignal]:
        return
        yield


def _store(presence: list[dict[str, Any]], now: datetime) -> LoroDocumentStore:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"token": "v1", "cells": [], "settings": {}, "presence": presence}
        )

    async def resolve(path: str) -> Located:
        return Located(drive_id="d1", item_id="i1")

    return LoroDocumentStore(
        http=httpx.AsyncClient(base_url="http://backend", transport=httpx.MockTransport(handler)),
        resolve=resolve,
        signals=_Silent(),
        clock=lambda: now,
    )


@pytest.mark.parametrize("case", [pytest.param(c, id=c["name"]) for c in VECTORS["cases"]])
async def test_the_loro_store_answers_the_editing_contract(case: dict[str, Any]) -> None:
    store = _store(_presence(case), T0 + timedelta(seconds=case["at"]))
    found = await store.editing(PATH)
    assert {cid: [[i.actor_id, i.caret] for i in infos] for cid, infos in found.items()} == case[
        "editing"
    ]


async def test_the_requester_is_told_from_others_by_actor_id_not_by_name() -> None:
    """Two people with one display name are two actors, and a caret names
    the actor a run's requester is compared with."""
    at = T0.isoformat()
    store = _store(
        [
            {
                "who": "Sam",
                "kind": "person",
                "cell_id": "b",
                "at": at,
                "actor_id": "user:1",
                "caret": True,
            },
            {
                "who": "Sam",
                "kind": "person",
                "cell_id": "b",
                "at": at,
                "actor_id": "user:2",
                "caret": True,
            },
        ],
        T0,
    )
    found = await store.editing(PATH)
    assert sorted(i.actor_id for i in found["b"]) == ["user:1", "user:2"]


async def test_presence_from_a_backend_that_predates_the_rule_reads_as_an_edit() -> None:
    """No actor id and no caret mark: whoever is named edited the cell at
    that time, which counts for 15 s."""
    entry = {"who": "Ada", "kind": "person", "cell_id": "b", "at": T0.isoformat()}
    within = await _store([entry], T0 + timedelta(seconds=15)).editing(PATH)
    assert [(i.actor_id, i.caret) for i in within["b"]] == [("Ada", False)]
    assert await _store([entry], T0 + timedelta(seconds=16)).editing(PATH) == {}


@pytest.mark.parametrize(
    ("requester", "held_by"),
    [
        pytest.param("agent:chat-1", None, id="the-agent-that-edited"),
        pytest.param("agent:chat-2", "Claude for Ada", id="another-agent"),
        pytest.param("user:ada", "Claude for Ada", id="the-person-it-acts-for"),
    ],
)
async def test_an_agent_s_own_edit_never_holds_its_own_run(
    requester: str, held_by: str | None
) -> None:
    """The box's agent edits a cell, then runs: the backend files the edit
    under the agent's actor key, which is the id the run's requester has, so
    the edit holds anyone else's run of that cell and never the agent's own."""
    edited = {
        "who": "Claude for Ada",
        "kind": "agent",
        "cell_id": "a",
        "at": T0.isoformat(),
        "actor_id": "agent:chat-1",
        "caret": False,
    }
    found = await _store([edited], T0 + timedelta(seconds=1)).editing(PATH)

    held = blocker(found["a"], requester)

    assert (held.display_name if held is not None else None) == held_by
