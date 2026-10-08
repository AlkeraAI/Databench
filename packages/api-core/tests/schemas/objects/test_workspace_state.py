"""The chat workspace document — what a reader's open tabs may say.

The document is written by a browser and read back by another one, so every
test here drives it the way a client does: build it from plain JSON, assert
what a reader can observe. The refusals matter as much as the acceptances —
the document is replayed in a browser, so a tab that could name something
fetchable, or a document that could grow without bound, is the bug.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import pytest
from alkera_core.schemas.objects.workspace_state import (
    MAX_WORKSPACE_STATE_BYTES,
    MAX_WORKSPACE_TABS,
    ChatWorkspaceState,
    WorkspaceTab,
)
from pydantic import ValidationError

NODE = "0f4a2f6e-7b1a-4c3d-9b5e-2a1f8c7d6e50"
OTHER_NODE = "8c1d5b90-3e42-4a77-9f10-6b2c4d8e1a33"


def _tab(**overrides: Any) -> dict[str, Any]:
    tab: dict[str, Any] = {"id": "t1", "kind": "file", "node_id": NODE, "name": "report.html"}
    tab.update(overrides)
    return tab


def test_a_never_saved_workspace_is_empty_with_nothing_active() -> None:
    """The route answers this document for a chat nobody has arranged yet, so
    a client can render it without a special case."""
    state = ChatWorkspaceState()

    assert state.tabs == []
    assert state.active_tab_id is None
    assert state.schema_version == "1.0.0"


def test_a_tab_defaults_to_an_unnamed_file_tab_with_no_node() -> None:
    tab = WorkspaceTab()

    assert (tab.id, tab.kind, tab.node_id, tab.name, tab.path, tab.params) == (
        "",
        "file",
        None,
        "",
        None,
        {},
    )


def test_the_files_browser_tab_keeps_its_folder_in_params() -> None:
    """The browser tab is a folder view; which folder is per-tab state, not a
    field on the document, so a second browser tab can sit on another folder."""
    folder = str(uuid4())
    state = ChatWorkspaceState.model_validate(
        {"tabs": [{"id": "files", "kind": "files", "params": {"folderId": folder}}]}
    )

    assert state.tabs[0].params == {"folderId": folder}


def test_two_tabs_may_not_share_an_id() -> None:
    """The id is what a close and a React key address; two tabs sharing one
    would close the wrong pane."""
    with pytest.raises(ValidationError, match="duplicate tab id 't1'"):
        ChatWorkspaceState.model_validate({"tabs": [_tab(), _tab(node_id=OTHER_NODE)]})


def test_a_tab_with_no_id_is_refused() -> None:
    with pytest.raises(ValidationError, match="every tab needs an id"):
        ChatWorkspaceState.model_validate({"tabs": [_tab(id="")]})


@pytest.mark.parametrize(
    ("count", "accepted"),
    [
        pytest.param(MAX_WORKSPACE_TABS, True, id="the_full_strip_is_allowed"),
        pytest.param(MAX_WORKSPACE_TABS + 1, False, id="one_past_the_strip_is_refused"),
    ],
)
def test_the_tab_count_is_bounded_at_the_documented_maximum(count: int, accepted: bool) -> None:
    payload = {"tabs": [_tab(id=f"t{i}") for i in range(count)]}

    if accepted:
        assert len(ChatWorkspaceState.model_validate(payload).tabs) == count
        return
    with pytest.raises(ValidationError, match=f"at most {MAX_WORKSPACE_TABS} tabs"):
        ChatWorkspaceState.model_validate(payload)


def test_the_active_tab_must_be_one_of_the_open_tabs() -> None:
    with pytest.raises(ValidationError, match="active_tab_id must name one of the tabs"):
        ChatWorkspaceState.model_validate({"tabs": [_tab()], "active_tab_id": "t9"})


@pytest.mark.parametrize(
    "active",
    [pytest.param("t1", id="an_open_tab"), pytest.param(None, id="nothing_focused")],
)
def test_an_open_tab_or_nothing_may_be_active(active: str | None) -> None:
    state = ChatWorkspaceState.model_validate({"tabs": [_tab()], "active_tab_id": active})

    assert state.active_tab_id == active


def test_an_active_tab_cannot_survive_the_tab_being_dropped() -> None:
    """Editing the document in place is how a client closes a tab; the
    invariant has to hold on assignment, not only on the first parse."""
    state = ChatWorkspaceState.model_validate({"tabs": [_tab()], "active_tab_id": "t1"})

    with pytest.raises(ValidationError, match="active_tab_id must name one of the tabs"):
        state.tabs = []


@pytest.mark.parametrize(
    "node_id",
    [
        pytest.param("https://files.example.com/c/p/tok/report.html", id="a_content_url"),
        pytest.param("//files.example.com/report.html", id="a_scheme_relative_url"),
        pytest.param("javascript:alert(1)", id="a_script_url"),
        pytest.param("data:text/html;base64,PHNjcmlwdD4=", id="a_data_url"),
        pytest.param("../../etc/passwd", id="a_relative_path"),
        pytest.param("scratch/report.html", id="a_workspace_path"),
        pytest.param("not-a-uuid", id="a_word"),
        pytest.param("", id="the_empty_string"),
        pytest.param(f" {NODE} ", id="a_padded_id"),
        pytest.param(f"{NODE}?x=1", id="an_id_with_a_query_string"),
    ],
)
def test_a_tab_may_only_name_a_node_by_its_id(node_id: str) -> None:
    """The document is ids-only on purpose: it is replayed by a browser, so a
    tab that could name something fetchable would be a stored redirect."""
    with pytest.raises(ValidationError, match="node_id must be a node id"):
        WorkspaceTab.model_validate(_tab(node_id=node_id))


def test_a_tab_naming_a_real_node_is_kept_verbatim() -> None:
    assert WorkspaceTab.model_validate(_tab()).node_id == NODE


def test_a_tab_kind_this_build_has_never_heard_of_rides_along() -> None:
    """A newer client's tab must not be silently dropped by an older one — the
    two share one document, and a drop would lose the newer reader's work."""
    state = ChatWorkspaceState.model_validate(
        {"tabs": [_tab(kind="terminal", node_id=None)], "active_tab_id": "t1"}
    )

    assert state.tabs[0].kind == "terminal"
    assert ChatWorkspaceState.model_validate(state.model_dump(mode="json")) == state


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({"tabs": [], "layout": {"split": 0.4}}, id="on_the_document"),
        pytest.param({"tabs": [_tab(pinned=True)]}, id="on_a_tab"),
    ],
)
def test_a_field_a_newer_writer_added_survives_the_round_trip(payload: dict[str, Any]) -> None:
    """The nested case is the one that bites: a plain ``BaseModel`` in the tab
    slot would eat the new field while the document round-tripped looking
    whole."""
    dumped = ChatWorkspaceState.model_validate(payload).model_dump(mode="json")

    if "layout" in payload:
        assert dumped["layout"] == {"split": 0.4}
    else:
        assert dumped["tabs"][0]["pinned"] is True


def test_a_document_stamped_by_an_older_writer_reads_at_todays_version() -> None:
    state = ChatWorkspaceState.model_validate({"schema_version": "0.9.0", "tabs": [_tab()]})

    assert state.schema_version == ChatWorkspaceState.SCHEMA_VERSION
    assert state.model_dump(mode="json")["schema_version"] == "1.0.0"


def test_the_size_cap_is_sixteen_kibibytes_and_a_full_strip_fits_inside_it() -> None:
    """The two bounds have to agree: a document the tab count allows must be
    one the server's byte cap can still accept, or a client could build a
    legal workspace it can never save."""
    assert MAX_WORKSPACE_STATE_BYTES == 16 * 1024

    full = ChatWorkspaceState.model_validate(
        {
            "tabs": [
                {
                    "id": str(uuid4()),
                    "kind": "file",
                    "node_id": str(uuid4()),
                    "name": f"{'quarterly-revenue-by-region-' * 2}{i}.parquet",
                    "path": f"scratch/{'analysis/' * 6}{i}.parquet",
                }
                for i in range(MAX_WORKSPACE_TABS)
            ],
        }
    )

    serialized = json.dumps(full.model_dump(mode="json")).encode()
    assert len(serialized) < MAX_WORKSPACE_STATE_BYTES
