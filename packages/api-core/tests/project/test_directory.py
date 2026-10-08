"""Tests for `ProjectDirectory`."""

from __future__ import annotations

from pathlib import Path

import pytest
from alkera_core.project.directory import (
    BLOBS_SUBDIR,
    CHATS_SUBDIR,
    LINEAGE_SUBDIR,
    PLUGINS_SUBDIR,
    SCHEDULER_SUBDIR,
    ProjectDirectory,
)


def test_initializes_directory_tree(tmp_path: Path) -> None:
    root = tmp_path / ".alkera"
    pd = ProjectDirectory(root)
    assert root.is_dir()
    assert (root / CHATS_SUBDIR).is_dir()
    assert (root / BLOBS_SUBDIR).is_dir()
    assert (root / PLUGINS_SUBDIR).is_dir()
    assert (root / SCHEDULER_SUBDIR).is_dir()
    assert pd.path == root
    assert pd.chats_path == root / CHATS_SUBDIR
    assert pd.blobs_path == root / BLOBS_SUBDIR
    assert pd.plugins_path == root / PLUGINS_SUBDIR
    assert pd.scheduler_path == root / SCHEDULER_SUBDIR


def test_init_is_idempotent(tmp_path: Path) -> None:
    root = tmp_path / ".alkera"
    (root / "chats" / "existing").mkdir(parents=True)
    (root / "chats" / "existing" / "chat.jsonl").write_text("preserved\n")
    # Constructing again shouldn't wipe anything.
    ProjectDirectory(root)
    assert (root / "chats" / "existing" / "chat.jsonl").read_text() == "preserved\n"


def test_create_if_missing_false_requires_existing(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        ProjectDirectory(tmp_path / "nope", create_if_missing=False)


def test_blobs_returns_handles(tmp_path: Path) -> None:
    pd = ProjectDirectory(tmp_path / ".alkera")
    bs1 = pd.blobs()
    bs2 = pd.blobs()
    # Fresh handles — not the same instance — but pointing at same root.
    assert bs1 is not bs2
    assert bs1.root == bs2.root


def test_chats_returns_handles(tmp_path: Path) -> None:
    """ChatStore handle round-trip. Lands once ChatStore is implemented
    in the next task."""
    pd = ProjectDirectory(tmp_path / ".alkera")
    cs1 = pd.chats()
    cs2 = pd.chats()
    assert cs1 is not cs2
    assert cs1.path == cs2.path


def test_default_workspace_layout_after_init(tmp_path: Path) -> None:
    pd = ProjectDirectory(tmp_path / ".alkera")
    children = sorted(p.name for p in pd.path.iterdir())
    # `lineage/`, `plugins/`, `scheduler/` are created eagerly alongside
    # chats/blobs; `runs/` and `traces/` are created lazily by the per-run writers.
    assert children == sorted(
        [BLOBS_SUBDIR, CHATS_SUBDIR, LINEAGE_SUBDIR, PLUGINS_SUBDIR, SCHEDULER_SUBDIR]
    )


def test_cloud_mirror_path_is_a_named_subdir_not_pre_created(tmp_path: Path) -> None:
    """The cloud mirror's own state (its schema-card ledger) lives under one
    named subdirectory. It is created by its writer, not by init, so a workspace
    no mirror ever served carries no trace of one."""
    root = tmp_path / ".alkera"
    pd = ProjectDirectory(root)
    assert pd.cloud_mirror_path == root / "cloud-mirror"
    assert not pd.cloud_mirror_path.exists()
    assert pd.cloud_mirror_path.parent == root
