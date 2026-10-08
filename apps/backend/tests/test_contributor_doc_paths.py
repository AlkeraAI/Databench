"""A path named in a Markdown doc must be a path that exists.

Docs are the only place in this repo where a file reference is never checked:
an import that moves breaks a test, a `-f` in the Makefile breaks a command, but
a compose file that moves leaves every doc pointing at the old location and
nothing goes red. The compose files moved to ``deploy/docker/`` and two
references in the agent guide stayed behind -- one of them a command an agent is
told to run as a sanity check, which then fails on a file that is not there.

So: every compose file a doc names must resolve, and the sanity-check command
must work in the worktree the reader is actually in. Per-worktree dev isolation
gives each branch its own compose project (``alkera-<branch-slug>``), so a
command hard-coding ``-p alkera`` reports the wrong stack -- empty on a branch,
or `main`'s containers -- which is worse than an error.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
AGENT_GUIDE = REPO_ROOT / "AGENTS.md"
CLAUDE_GUIDE = REPO_ROOT / "CLAUDE.md"
WORKSPACE_ENV = REPO_ROOT / "ops" / "scripts" / "workspace-env.sh"

#: Directories whose Markdown is not ours to keep true (vendored trees, build
#: output, dependency caches).
SKIP_PARTS = frozenset(
    {".git", ".venv", "node_modules", "vendor", "dist", "build", "site", "__pycache__"}
)
#: A compose file reference: a path ending in a `compose*.yml` / `compose*.yaml`.
COMPOSE_REF = re.compile(r"[A-Za-z0-9_./-]*compose[A-Za-z0-9_.-]*\.ya?ml")

pytestmark = pytest.mark.skipif(
    not AGENT_GUIDE.is_file(), reason="the repo docs are not part of this distribution"
)


def _tracked_markdown() -> list[str]:
    """Repo-relative paths of every Markdown file git tracks."""
    listed = subprocess.run(
        ["git", "ls-files", "-z", "--", "*.md"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert listed.returncode == 0, listed.stderr
    return [name for name in listed.stdout.split("\0") if name]


def _docs() -> list[Path]:
    """The Markdown this repo is answerable for.

    Tracked files only: a checkout also holds local-only Markdown -- scratch
    notes, generated reports -- that is gitignored, absent in CI, and not ours
    to keep true. Walking the filesystem judges those too, so the audit turns
    red on whatever text a machine happens to be carrying.
    """
    return sorted(
        REPO_ROOT / name
        for name in _tracked_markdown()
        if not SKIP_PARTS & set(Path(name).parts) and (REPO_ROOT / name).is_file()
    )


def _unresolved(doc: Path) -> list[str]:
    """Compose references in ``doc`` that name no file in this repo.

    A reference with no directory part (``compose.yml``) is the operator's own
    copy, not ours; anything with a directory must resolve either from the repo
    root or relative to the doc.
    """
    missing = []
    for ref in COMPOSE_REF.findall(doc.read_text(encoding="utf-8")):
        if "/" not in ref:
            continue
        if (REPO_ROOT / ref.lstrip("./")).is_file():
            continue
        if (doc.parent / ref).resolve().is_file():
            continue
        missing.append(ref)
    return missing


def _offenders() -> dict[str, list[str]]:
    return {
        str(doc.relative_to(REPO_ROOT)): missing for doc in _docs() if (missing := _unresolved(doc))
    }


@pytest.fixture
def scratch_doc() -> Iterator[Path]:
    """An untracked Markdown file inside the repo naming a compose file that
    moved away.

    This is the shape of the local-only text a checkout accumulates -- an
    agent's report, a scratch note -- hidden directory and all.
    """
    scratch = Path(tempfile.mkdtemp(prefix=".doc-audit-scratch-", dir=REPO_ROOT))
    try:
        doc = scratch / "note.md"
        doc.write_text("see docker/compose.local.yml\n", encoding="utf-8")
        yield doc
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def test_every_compose_file_a_doc_names_exists() -> None:
    offenders = _offenders()
    assert offenders == {}, offenders


def test_an_untracked_doc_is_not_this_repos_to_keep_true(scratch_doc: Path) -> None:
    """The audit judges the repo, not the checkout it happens to run in.

    Walking the filesystem also reads gitignored, local-only Markdown, so a
    scratch note naming a moved compose file fails the audit on a machine that
    has one and passes in CI -- a red that says nothing about the repo.
    """
    assert _unresolved(scratch_doc) == ["docker/compose.local.yml"], (
        "the fixture must genuinely name a missing compose file, or this proves nothing"
    )
    assert str(scratch_doc.relative_to(REPO_ROOT)) not in _offenders()


def test_a_tracked_doc_naming_a_missing_compose_file_is_still_an_offender(
    monkeypatch: pytest.MonkeyPatch, scratch_doc: Path
) -> None:
    """Ignoring untracked files must not defang the audit: the same doc, once
    git reports it as tracked, is caught."""
    relative = str(scratch_doc.relative_to(REPO_ROOT))
    monkeypatch.setattr(sys.modules[__name__], "_tracked_markdown", lambda: [relative])
    assert _offenders() == {relative: ["docker/compose.local.yml"]}


def test_the_sanity_check_inspects_this_worktrees_stack() -> None:
    """`docker compose ps` on the wrong project name is a silent wrong answer --
    an empty table on a branch, or `main`'s containers -- so the documented
    command must derive the project name the way the Makefile does."""
    text = AGENT_GUIDE.read_text(encoding="utf-8")
    (command,) = [
        line
        for line in text.splitlines()
        if line.startswith("docker compose") and "compose.local.yml" in line
    ]
    assert "deploy/docker/compose.local.yml" in command, command
    assert "workspace-env.sh project-name" in command, (
        "the project name must come from the workspace helper, not a literal"
    )
    assert not re.search(r"-p alkera(\s|$)", command), command


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="no POSIX shell: `bash` on the Windows runner is the WSL launcher",
)
def test_the_documented_command_runs_and_reports_this_stack() -> None:
    """Executed, not just read: the helper prints a project name and the compose
    file parses under it."""
    if not WORKSPACE_ENV.is_file():
        pytest.skip("ops scripts are not part of this distribution")
    name = subprocess.run(
        ["bash", str(WORKSPACE_ENV), "project-name"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert name.returncode == 0, name.stderr
    project = name.stdout.strip()
    assert project.startswith("alkera"), project
    compose = REPO_ROOT / "deploy" / "docker" / "compose.local.yml"
    assert compose.is_file()


def test_claude_md_imports_the_agent_guide_and_adds_nothing() -> None:
    """Claude Code reads CLAUDE.md and Codex reads AGENTS.md. There is one guide:
    CLAUDE.md only imports AGENTS.md, so a rule written into CLAUDE.md alone
    would give two agents different instructions."""
    assert AGENT_GUIDE.is_file()
    assert CLAUDE_GUIDE.read_text(encoding="utf-8").strip() == "@AGENTS.md", (
        "CLAUDE.md must hold only `@AGENTS.md`: write the rule in AGENTS.md instead"
    )
