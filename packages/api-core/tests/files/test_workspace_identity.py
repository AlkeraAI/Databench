"""Which workspace a chat is in, and which workspace holds a node, read in one
id space.

A chat names its workspace by the workspace OBJECT's id; a node is held by
the workspace FOLDER above it, a Files node with an id of its own. These
cases pin that the two are compared through the folder's target object, so a
chat in a workspace is answered "here" for every node in that workspace's
folder and for nothing else, whatever the folder's own node id is.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import pytest
from alkera_core.files.objects_bridge import CHAT_TYPE, WORKSPACE_TYPE
from alkera_core.files.workspace_identity import (
    chat_works_at,
    chat_workspace_id,
    workspace_holding,
    workspace_of_folder,
)
from alkera_core.models.files.tree import FileNode
from alkera_core.models.workspace_object import WorkspaceObject


def _folder(*, subtype: str | None = None, target: uuid.UUID | None = None) -> FileNode:
    return FileNode(id=uuid.uuid4(), kind="folder", subtype=subtype, target_object_id=target)


def _file() -> FileNode:
    return FileNode(id=uuid.uuid4(), kind="file")


def _chat(workspace_id: object = None) -> WorkspaceObject:
    spec: dict[str, object] = {}
    if workspace_id is not None:
        spec["workspace_id"] = str(workspace_id)
    return WorkspaceObject(id=uuid.uuid4(), type="chat", spec=spec)


@dataclass(frozen=True)
class Tree:
    """Two workspaces (Main and a project) with a notebook in each shared
    tree, and a chat folder outside any workspace with a notebook in it."""

    main_id: uuid.UUID
    project_id: uuid.UUID
    main_folder: FileNode
    project_folder: FileNode
    in_main: list[FileNode]
    in_project: list[FileNode]


def _tree() -> Tree:
    root = _folder()
    main_id, project_id = uuid.uuid4(), uuid.uuid4()
    main = _folder(subtype=WORKSPACE_TYPE, target=main_id)
    project = _folder(subtype=WORKSPACE_TYPE, target=project_id)
    return Tree(
        main_id=main_id,
        project_id=project_id,
        main_folder=main,
        project_folder=project,
        in_main=[root, main, _folder(), _file()],
        in_project=[root, project, _folder(), _file()],
    )


@pytest.mark.parametrize("where", ["main", "project"])
def test_a_chat_works_at_a_node_in_its_own_workspace(where: str) -> None:
    tree = _tree()
    workspace_id, path = (
        (tree.main_id, tree.in_main) if where == "main" else (tree.project_id, tree.in_project)
    )
    assert chat_works_at(_chat(workspace_id), path) is True


@pytest.mark.parametrize("where", ["main", "project"])
def test_a_chat_of_another_workspace_does_not(where: str) -> None:
    tree = _tree()
    other, path = (
        (tree.project_id, tree.in_main) if where == "main" else (tree.main_id, tree.in_project)
    )
    assert chat_works_at(_chat(other), path) is False


def test_naming_the_workspace_folders_node_id_is_not_naming_the_workspace() -> None:
    """The comparison that used to decide: a spec equal to the folder's own
    node id. A chat's spec never holds one, and naming it places the chat in
    no workspace."""
    tree = _tree()
    assert chat_works_at(_chat(tree.main_folder.id), tree.in_main) is False


def test_a_chat_in_no_workspace_does_not_work_in_one() -> None:
    assert chat_works_at(_chat(None), _tree().in_main) is False


def test_the_innermost_workspace_folder_decides() -> None:
    outer_id, inner_id = uuid.uuid4(), uuid.uuid4()
    path = [
        _folder(subtype=WORKSPACE_TYPE, target=outer_id),
        _folder(subtype=WORKSPACE_TYPE, target=inner_id),
        _file(),
    ]
    assert chat_works_at(_chat(inner_id), path) is True
    assert chat_works_at(_chat(outer_id), path) is False


def test_a_workspace_folder_whose_workspace_ended_holds_nobody() -> None:
    """Its object was tombstoned off it: it stands for no workspace, so no
    chat works in it, and the path does not fall through to the chat rule."""
    chat = _chat(None)
    path = [
        _folder(subtype=CHAT_TYPE, target=uuid.UUID(str(chat.id))),
        _folder(subtype=WORKSPACE_TYPE, target=None),
        _file(),
    ]
    assert workspace_of_folder(path[1]) is None
    assert chat_works_at(chat, path) is False


def test_outside_every_workspace_the_chats_own_folder_decides() -> None:
    chat = _chat(None)
    mine = [_folder(), _folder(subtype=CHAT_TYPE, target=uuid.UUID(str(chat.id))), _file()]
    theirs = [_folder(), _folder(subtype=CHAT_TYPE, target=uuid.uuid4()), _file()]
    # A folder whose own node id is the chat's id is not the chat's folder.
    lookalike = [FileNode(id=chat.id, kind="folder", subtype=CHAT_TYPE), _file()]
    assert chat_works_at(chat, mine) is True
    assert chat_works_at(chat, theirs) is False
    assert chat_works_at(chat, lookalike) is False


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        pytest.param({"workspace_id": "115d3feb-0000-4000-8000-000000000001"}, True, id="an-id"),
        pytest.param({}, False, id="none"),
        pytest.param({"workspace_id": ""}, False, id="empty"),
        pytest.param({"workspace_id": "Main"}, False, id="not-an-id"),
    ],
)
def test_the_chats_workspace_is_read_from_its_spec(spec: dict[str, str], expected: bool) -> None:
    found = chat_workspace_id(WorkspaceObject(id=uuid.uuid4(), type="chat", spec=spec))
    if expected:
        assert found == uuid.UUID("115d3feb-0000-4000-8000-000000000001")
    else:
        assert found is None


def test_a_folder_that_is_not_a_workspace_stands_for_none() -> None:
    assert workspace_of_folder(_folder(subtype=CHAT_TYPE, target=uuid.uuid4())) is None


def test_the_workspace_is_read_off_a_spec_as_the_schema_writes_it() -> None:
    """Files reads the stored spec without the object schemas, so this pins
    that it reads the key ``ChatSpec`` writes: a renamed field would otherwise
    leave every chat in no workspace."""
    from alkera_core.schemas.objects.specs import ChatSpec

    workspace_id = uuid.uuid4()
    written = ChatSpec(workspace_id=str(workspace_id)).model_dump(mode="json")
    chat = WorkspaceObject(id=uuid.uuid4(), type="chat", spec=written)

    assert chat_workspace_id(chat) == workspace_id
    assert chat_workspace_id(WorkspaceObject(id=uuid.uuid4(), type="chat", spec={})) is None


@pytest.mark.parametrize("where", ["main", "project"])
def test_the_workspace_holding_a_node_is_its_folders_object_not_the_folder(where: str) -> None:
    tree = _tree()
    workspace_id, folder, path = (
        (tree.main_id, tree.main_folder, tree.in_main)
        if where == "main"
        else (tree.project_id, tree.project_folder, tree.in_project)
    )
    assert workspace_holding(path) == workspace_id
    assert workspace_holding(path) != folder.id


def test_a_node_outside_every_workspace_folder_is_held_by_none() -> None:
    chat_folder = _folder(subtype=CHAT_TYPE, target=uuid.uuid4())
    assert workspace_holding([_folder(), chat_folder, _file()]) is None


def test_an_ended_inner_workspace_holds_nobody_not_its_outer_one() -> None:
    outer_id = uuid.uuid4()
    outer = _folder(subtype=WORKSPACE_TYPE, target=outer_id)
    ended = _folder(subtype=WORKSPACE_TYPE, target=None)
    assert workspace_holding([_folder(), outer, ended, _file()]) is None
