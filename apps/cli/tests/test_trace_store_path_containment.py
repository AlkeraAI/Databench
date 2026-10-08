"""The retention sweep only ever deletes chat directories it can prove it owns.

A chat's ``manifest.json`` names its own session id in its *contents*, and that
id used to be handed straight to ``ChatStore.delete``, which joins it onto the
chats directory and removes the result recursively. A manifest is a plain file:
it can be committed into a repository under ``.alkera/chats/<anything>/``, so a
clone plus the first chat opened in it (which sweeps on open) was enough to have an
attacker-chosen directory recursively deleted — a traversal, an absolute path, or
the name of a legitimate sibling chat that is still in use.

These tests pin the rule the sweep now follows: a session id is actionable only
when the chats directory really contains a directory of exactly that name, and
only one manifest claims it.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest
from alkera_cli.observability.trace_store import sweep_expired_traces
from alkera_core.project.chats.store import ChatStore
from alkera_core.project.directory import ProjectDirectory
from freezegun import freeze_time

STALE = "2020-01-01T00:00:00+00:00"


def _store(tmp_path: Path) -> ChatStore:
    return ProjectDirectory(tmp_path / ".alkera").chats()


def _expired_chat(store: ChatStore, **fields: object) -> str:
    """Create a real chat directory whose manifest is long past any retention
    window, then overwrite the given manifest fields. Returns the DIRECTORY name,
    which is not necessarily what the manifest now claims."""
    with freeze_time(STALE):
        session_id = store.create(harness_type="agent").manifest.session_id
    if fields:
        path = store.path / session_id / "manifest.json"
        data = json.loads(path.read_text())
        data.update(fields)
        path.write_text(json.dumps(data))
    return session_id


def _victim(tmp_path: Path, name: str = "victim") -> Path:
    """A directory OUTSIDE the chat store, with a file in it so an emptied-but-
    present directory can't pass for an intact one."""
    victim = tmp_path / name
    victim.mkdir()
    (victim / "keep.txt").write_text("not the sweep's to delete")
    return victim


@pytest.mark.parametrize(
    "escape_id",
    [
        pytest.param(lambda tmp: "../../victim", id="parent-traversal"),
        pytest.param(lambda tmp: "..", id="the-alkera-dir-itself"),
        pytest.param(lambda tmp: str(tmp / "victim"), id="absolute-path"),
        pytest.param(lambda tmp: "../../victim/../victim", id="mixed-traversal"),
    ],
)
def test_a_manifest_cannot_name_a_directory_outside_the_store(
    tmp_path: Path, escape_id: Callable[[Path], str]
) -> None:
    victim = _victim(tmp_path)
    store = _store(tmp_path)
    impostor = _expired_chat(store, session_id=escape_id(tmp_path))

    removed = sweep_expired_traces(store, retention_days=30)

    assert removed == 0
    assert (victim / "keep.txt").read_text() == "not the sweep's to delete"
    assert (tmp_path / ".alkera").is_dir()
    # The impostor is left alone too — an id we cannot trust is not a licence to
    # delete the directory that shipped it either.
    assert store.list_session_ids() == [impostor]


def test_a_manifest_cannot_name_a_sibling_chat(tmp_path: Path) -> None:
    """Two chats claiming one id is an identity conflict, not a deletion order:
    the sweep cannot tell which directory the id refers to, so it touches neither."""
    store = _store(tmp_path)
    target = _expired_chat(store)
    impostor = _expired_chat(store, session_id=target)

    removed = sweep_expired_traces(store, retention_days=30)

    assert removed == 0
    assert set(store.list_session_ids()) == {target, impostor}


def test_a_claimed_child_cannot_escape_through_the_subtree_delete(tmp_path: Path) -> None:
    """Deleting a subtree must not re-derive its members from manifest contents.

    A directory that merely *claims* an expiring chat as its parent would
    otherwise be followed into whatever path its own manifest names."""
    victim = _victim(tmp_path, "victim-child")
    store = _store(tmp_path)
    root = _expired_chat(store)
    impostor = _expired_chat(store, session_id="../../victim-child", parent_session_id=root)

    removed = sweep_expired_traces(store, retention_days=30)

    assert (victim / "keep.txt").exists()
    # The root itself is genuinely expired and genuinely its own directory, so it
    # still goes; only the forged child is refused.
    assert removed == 1
    assert store.list_session_ids() == [impostor]


def test_a_manifest_naming_an_absent_sibling_deletes_nothing(tmp_path: Path) -> None:
    """A syntactically harmless id that matches no directory is still not swept —
    the guard keys off the directory listing, not off how the string looks."""
    store = _store(tmp_path)
    impostor = _expired_chat(store, session_id="no-such-chat")

    assert sweep_expired_traces(store, retention_days=30) == 0
    assert store.list_session_ids() == [impostor]


def test_a_symlinked_chat_directory_is_not_followed(tmp_path: Path) -> None:
    """A chat directory can carry an honest name and still point elsewhere."""
    victim = _victim(tmp_path, "victim-link-target")
    store = _store(tmp_path)
    store.path.mkdir(parents=True, exist_ok=True)
    try:
        (store.path / "linked").symlink_to(victim, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("this platform does not allow creating symlinks unprivileged")
    (victim / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "1.4.0",
                "session_id": "linked",
                "created_at": STALE,
                "updated_at": STALE,
            }
        )
    )

    assert sweep_expired_traces(store, retention_days=30) == 0
    assert (victim / "keep.txt").exists()


def test_an_ordinary_expired_chat_is_still_swept(tmp_path: Path) -> None:
    """The control: the guard must not have made the sweep inert."""
    store = _store(tmp_path)
    _expired_chat(store)

    assert sweep_expired_traces(store, retention_days=30) == 1
    assert store.list_session_ids() == []


def test_an_honest_expired_subtree_still_goes_together(tmp_path: Path) -> None:
    """Doing the recursion here instead of in ``ChatStore.delete`` must not change
    what a legitimate parent/child pair does."""
    store = _store(tmp_path)
    parent = _expired_chat(store)
    _expired_chat(store, parent_session_id=parent)

    assert sweep_expired_traces(store, retention_days=30) == 1
    assert store.list_session_ids() == []
