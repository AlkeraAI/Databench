"""Auto-discovering lineage test for the persisted chat workspace document.

The document is written by one browser and read back by another — often an
older one, after a deploy — so today's reader must keep loading whatever any
past writer emitted. Every fixture under
``packages/api-core/tests/fixtures/objects/workspace_state/`` is a past writer's output; this
walks them all with today's reader, and the day it fails is the day a
backward-compat break slipped in. The fix is a ``MIGRATIONS`` entry and a new
fixture, never an edit to a committed one.

The corpus sits beside — not inside — the ``v<version>/`` directories the
other object fixtures use, because those are stamped by ONE writer version for
the whole group and regenerated together from their generator. This document
versions on its own, so its corpus is one file per version of THIS model, and
the writer's output it is checked against is built here rather than in that
generator.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from alkera_core.schemas.objects.workspace_state import ChatWorkspaceState

FIXTURE_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "objects" / "workspace_state"

#: The chat folder the browser tab sits on and the two nodes the file tabs
#: name. Fixed rather than generated so the committed corpus is stable bytes.
FOLDER = "3d7b0c21-9f45-4e68-8a02-5c9d1e3f7b44"
REPORT = "0f4a2f6e-7b1a-4c3d-9b5e-2a1f8c7d6e50"
CHART = "8c1d5b90-3e42-4a77-9f10-6b2c4d8e1a33"


def _current_writer_document() -> ChatWorkspaceState:
    """What a client saves for a chat with the browser and two files open."""
    return ChatWorkspaceState.model_validate(
        {
            "tabs": [
                {"id": "files", "kind": "files", "name": "Files", "params": {"folderId": FOLDER}},
                {
                    "id": "tab-report",
                    "kind": "file",
                    "node_id": REPORT,
                    "name": "report.html",
                    "path": "scratch/report.html",
                },
                {
                    "id": "tab-chart",
                    "kind": "file",
                    "node_id": CHART,
                    "name": "revenue.png",
                    "path": "scratch/figures/revenue.png",
                },
            ],
            "active_tab_id": "tab-report",
        }
    )


def _fixture_bytes(state: ChatWorkspaceState) -> bytes:
    return (json.dumps(state.model_dump(mode="json"), indent=2, sort_keys=True) + "\n").encode()


def _discover() -> list[pytest.param]:
    if not FIXTURE_DIR.is_dir():
        return []
    return [pytest.param(path, id=path.stem) for path in sorted(FIXTURE_DIR.glob("v*.json"))]


def test_the_corpus_is_not_empty() -> None:
    """An empty corpus would make every walk below pass vacuously."""
    assert _discover(), f"no fixtures under {FIXTURE_DIR}"


@pytest.mark.parametrize("fixture_path", _discover())
def test_a_past_writers_document_loads_and_re_dumps(fixture_path: Path) -> None:
    payload: dict[str, Any] = json.loads(fixture_path.read_text())

    state = ChatWorkspaceState.model_validate(payload)

    assert state.schema_version == ChatWorkspaceState.SCHEMA_VERSION
    assert ChatWorkspaceState.model_validate(state.model_dump(mode="json")) == state
    assert [tab.id for tab in state.tabs], "a fixture with no tabs proves nothing"


@pytest.mark.parametrize("fixture_path", _discover())
def test_a_field_a_newer_writer_added_rides_along(fixture_path: Path) -> None:
    """The one rule the base class exists for: an older reader hands a newer
    writer's field back untouched instead of raising or eating it."""
    payload: dict[str, Any] = json.loads(fixture_path.read_text())
    payload["future_field"] = {"x": 1}
    payload["tabs"][0]["future_tab_field"] = "kept"

    dumped = ChatWorkspaceState.model_validate(payload).model_dump(mode="json")

    assert dumped["future_field"] == {"x": 1}
    assert dumped["tabs"][0]["future_tab_field"] == "kept"


def test_todays_writer_output_is_committed_under_its_own_version() -> None:
    """A shape change that forgets the version bump and the new fixture fails
    here rather than quietly rewriting the evidence of what shipped."""
    stamp = ChatWorkspaceState.SCHEMA_VERSION.replace(".", "_")
    current = FIXTURE_DIR / f"v{stamp}.json"

    assert current.is_file(), f"the writer emits {ChatWorkspaceState.SCHEMA_VERSION}; add {current}"
    assert current.read_bytes() == _fixture_bytes(_current_writer_document())
