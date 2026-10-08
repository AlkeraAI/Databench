"""``ChatBlobs``: the shared blob store as one chat may name it.

On a machine that serves many people's chats, one content-addressed store
holds every chat's spilled results. A chat may name only what it wrote or was
handed: any other hash, whether another chat's or one that never existed, gets
the same answer, and releasing a blob never removes bytes another chat still
names. These run against the real store on disk.
"""

from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path

import pytest
from alkera_core.project.chats.blobs import BlobStore, ChatBlobs
from alkera_core.project.directory import ProjectDirectory

RANDOM = hashlib.sha256(b"no chat ever wrote this").hexdigest()


def _store(tmp_path: Path) -> BlobStore:
    return ProjectDirectory(tmp_path / ".alkera").blobs()


def _refusal(view: ChatBlobs, sha: str) -> list[str]:
    """Every read a chat can make, as the exception each raised (with the hash
    itself replaced so two hashes' answers compare)."""
    out: list[str] = []
    for read in (view.read, view.size, view.path):
        try:
            read(sha)
        except Exception as exc:
            out.append(f"{type(exc).__name__}:{str(exc).replace(sha, '<sha>')}")
        else:
            out.append("answered")
    out.append(f"exists={view.exists(sha)} holds={view.holds(sha)}")
    return out


def test_a_chat_reads_what_it_wrote(tmp_path: Path) -> None:
    a = _store(tmp_path).for_chat("chat-a")
    sha, size = a.write(b"org a's revenue by region")
    assert a.read(sha) == b"org a's revenue by region"
    assert a.size(sha) == size
    assert a.exists(sha) and a.holds(sha)
    assert a.path(sha).is_file()


def test_another_chats_hash_answers_like_one_that_never_existed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    sha, _ = store.for_chat("chat-a").write(b"org a's revenue by region")
    b = store.for_chat("chat-b")
    assert _refusal(b, sha) == _refusal(b, RANDOM)
    assert _refusal(b, sha)[0] == "FileNotFoundError:<sha>"
    # The bytes are on disk all along; only the naming is refused.
    assert store.exists(sha)


def test_releasing_a_hash_it_never_held_changes_nothing(tmp_path: Path) -> None:
    store = _store(tmp_path)
    sha, _ = store.for_chat("chat-a").write(b"org a's revenue by region")
    b = store.for_chat("chat-b")
    assert b.delete(sha) is False
    assert b.delete(RANDOM) is False
    assert b.release(sha) is False
    assert store.for_chat("chat-a").read(sha) == b"org a's revenue by region"


def test_a_released_hash_is_no_longer_nameable(tmp_path: Path) -> None:
    a = _store(tmp_path).for_chat("chat-a")
    sha, _ = a.write(b"x")
    assert a.release(sha) is True
    with pytest.raises(FileNotFoundError):
        a.read(sha)
    assert not a.exists(sha)


def test_deleting_the_last_holders_copy_removes_the_bytes(tmp_path: Path) -> None:
    store = _store(tmp_path)
    a = store.for_chat("chat-a")
    sha, _ = a.write(b"only a has this")
    assert a.delete(sha) is True
    assert not store.exists(sha)


def test_shared_content_survives_one_chats_delete(tmp_path: Path) -> None:
    """Two chats that produced the same bytes share one file. One letting go
    leaves the other reading; the last letting go removes the file."""
    store = _store(tmp_path)
    a, b = store.for_chat("chat-a"), store.for_chat("chat-b")
    sha, _ = a.write(b"same rows")
    assert b.write(b"same rows")[0] == sha
    assert a.delete(sha) is True
    assert b.read(sha) == b"same rows"
    assert b.delete(sha) is True
    assert not store.exists(sha)


def test_bytes_another_record_names_are_kept(tmp_path: Path) -> None:
    store = _store(tmp_path)
    a = store.for_chat("chat-a")
    sha, _ = a.write(b"named in another transcript")
    assert a.delete(sha, kept_for={sha}) is True
    assert store.exists(sha)
    assert not a.holds(sha)


def test_a_hash_written_again_after_release_is_held_again(tmp_path: Path) -> None:
    a = _store(tmp_path).for_chat("chat-a")
    sha, _ = a.write(b"x")
    a.release(sha)
    a.write(b"x")
    assert a.read(sha) == b"x"


@pytest.mark.parametrize(
    "bad",
    [pytest.param("../../etc/passwd", id="traversal"), pytest.param("ABC", id="short")],
)
def test_a_malformed_hash_is_refused_as_malformed(tmp_path: Path, bad: str) -> None:
    a = _store(tmp_path).for_chat("chat-a")
    with pytest.raises(ValueError, match="not a sha256"):
        a.holds(bad)
    with pytest.raises(ValueError, match="not a sha256"):
        a.read(bad)


@pytest.mark.parametrize(
    "key",
    [pytest.param("../escape", id="traversal"), pytest.param("a/b", id="separator")],
)
def test_a_chat_key_never_reaches_the_index_path(tmp_path: Path, key: str) -> None:
    store = _store(tmp_path)
    store.for_chat(key).write(b"x")
    refs = store.root / ".refs"
    assert [p.parent for p in refs.iterdir()] == [refs]


def test_a_torn_index_line_is_skipped_not_fatal(tmp_path: Path) -> None:
    """A writer killed mid-append leaves a fragment; the next admit starts a
    clean line and the fragment admits nothing."""
    store = _store(tmp_path)
    a = store.for_chat("chat-a")
    first, _ = a.write(b"first")
    (refs,) = (store.root / ".refs").iterdir()
    with refs.open("a", encoding="utf-8") as f:
        f.write("+" + RANDOM[:20])
    second, _ = a.write(b"second")
    assert a.read(first) == b"first" and a.read(second) == b"second"
    assert not a.holds(RANDOM)


def test_the_sweep_never_walks_the_index_as_blobs(tmp_path: Path) -> None:
    store = _store(tmp_path)
    sha, _ = store.for_chat("chat-a").write(b"x")
    old = time.time() - 10 * 24 * 3600
    os.utime(store.path(sha), (old, old))
    report = store.sweep(set())
    assert report.scanned == 1 and report.deleted == 1
    assert (store.root / ".refs").is_dir()


def test_a_view_cannot_sweep_or_become_another_chat(tmp_path: Path) -> None:
    a = _store(tmp_path).for_chat("chat-a")
    with pytest.raises(TypeError):
        a.sweep(set())
    assert a.for_chat("chat-b") is a


def test_a_chats_attachment_and_its_subagents_spills_are_its_own(tmp_path: Path) -> None:
    """An attachment a chat is handed is nameable by it; a subagent names blobs
    as its root chat, so a handle either one makes is one the other may follow,
    and another chat follows neither."""
    project = ProjectDirectory(tmp_path / ".alkera")
    chats = project.chats()
    root = chats.create(session_id="root", title="t", harness_type="agent")
    child = chats.create(session_id="child", title="t", harness_type="agent")
    child.manifest.parent_session_id = "root"  # how the harness links a subagent's chat
    other = chats.create(session_id="other", title="t", harness_type="agent")
    try:
        part = root.add_blob(b"the attached csv", filename="a.csv")
        spilled, _ = child.blobs.write(b"the subagent's result")
        assert root.blobs.read(part.sha256) == b"the attached csv"
        assert root.blobs.read(spilled) == b"the subagent's result"
        assert child.blobs.read(part.sha256) == b"the attached csv"
        assert _refusal(other.blobs, part.sha256) == _refusal(other.blobs, RANDOM)
    finally:
        for chat in (root, child, other):
            chat.close()


def test_deleting_a_chat_drops_its_index(tmp_path: Path) -> None:
    project = ProjectDirectory(tmp_path / ".alkera")
    chats = project.chats()
    chat = chats.create(session_id="gone", title="t", harness_type="agent")
    chat.blobs.write(b"x")
    chat.close()
    assert list((project.blobs_path / ".refs").iterdir())
    chats.delete("gone")
    assert list((project.blobs_path / ".refs").iterdir()) == []
