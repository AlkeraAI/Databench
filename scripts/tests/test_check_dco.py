"""The DCO check passes a range only when every authored commit is signed off by its author."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "check_dco.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_dco", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


dco = _load()

ADA = ("Ada Lovelace", "ada@example.com")


def _git(repo: Path, *args: str, author: tuple[str, str] = ADA) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": author[0],
        "GIT_AUTHOR_EMAIL": author[1],
        "GIT_COMMITTER_NAME": author[0],
        "GIT_COMMITTER_EMAIL": author[1],
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    return subprocess.run(
        ["git", *args], cwd=repo, env=env, capture_output=True, check=True, text=True
    ).stdout.strip()


def _commit(repo: Path, message: str, author: tuple[str, str] = ADA) -> str:
    _git(repo, "commit", "--allow-empty", "-q", "-m", message, author=author)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q", "-b", "main")
    _commit(tmp_path, "base")
    return tmp_path


def _run(repo: Path, base: str, monkeypatch: pytest.MonkeyPatch) -> int:
    monkeypatch.chdir(repo)
    return int(dco.main([base, "HEAD"]))


@pytest.mark.parametrize(
    ("trailer", "author", "passes"),
    [
        pytest.param("Signed-off-by: Ada Lovelace <ada@example.com>", ADA, True, id="author"),
        pytest.param(
            "Signed-off-by: Ada Lovelace <ADA@Example.com>", ADA, True, id="email-case-free"
        ),
        pytest.param("", ADA, False, id="no-trailer"),
        pytest.param(
            "Signed-off-by: Grace Hopper <grace@example.com>", ADA, False, id="someone-else"
        ),
        pytest.param(
            "Signed-off-by: Ada Lovelace <ada@other.example>", ADA, False, id="wrong-email"
        ),
        pytest.param("signed off by Ada Lovelace", ADA, False, id="not-a-trailer"),
    ],
)
def test_a_commit_passes_only_when_its_author_signed_it_off(
    repo: Path, monkeypatch: pytest.MonkeyPatch, trailer: str, author: tuple[str, str], passes: bool
) -> None:
    base = _git(repo, "rev-parse", "HEAD")
    _commit(repo, f"feature\n\n{trailer}\n", author=author)

    assert _run(repo, base, monkeypatch) == (0 if passes else 1)


def test_one_unsigned_commit_fails_the_range_and_is_named(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    base = _git(repo, "rev-parse", "HEAD")
    _commit(repo, "first\n\nSigned-off-by: Ada Lovelace <ada@example.com>")
    unsigned = _commit(repo, "second, forgot")
    _commit(repo, "third\n\nSigned-off-by: Ada Lovelace <ada@example.com>")

    assert _run(repo, base, monkeypatch) == 1
    err = capsys.readouterr().err
    assert unsigned[:12] in err
    assert "needs: Signed-off-by: Ada Lovelace <ada@example.com>" in err


def test_commits_before_the_base_are_not_checked(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _commit(repo, "old history, never signed")
    base = _git(repo, "rev-parse", "HEAD")
    _commit(repo, "new\n\nSigned-off-by: Ada Lovelace <ada@example.com>")

    assert _run(repo, base, monkeypatch) == 0


def test_a_merge_commit_needs_no_sign_off(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "-b", "topic")
    _commit(repo, "topic work\n\nSigned-off-by: Ada Lovelace <ada@example.com>")
    _git(repo, "checkout", "-q", "main")
    _commit(repo, "main work\n\nSigned-off-by: Ada Lovelace <ada@example.com>")
    _git(repo, "merge", "-q", "--no-ff", "-m", "Merge topic", "topic")

    assert _run(repo, base, monkeypatch) == 0


def test_wrong_arguments_are_a_usage_error() -> None:
    assert dco.main(["only-one"]) == 2
