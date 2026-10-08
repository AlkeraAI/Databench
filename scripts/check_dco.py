"""Every commit in a range is signed off by its author (Developer Certificate of Origin).

A commit passes when its message has a ``Signed-off-by: Name <email>`` trailer
whose name and email equal the commit's author. Merge commits are skipped: they
carry no authored change of their own.

Usage::

    python scripts/check_dco.py <base> <head>    # checks base..head; exit 1 on a miss
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

_SIGN_OFF = re.compile(r"^Signed-off-by:\s*(?P<name>.+?)\s*<(?P<email>[^<>]+)>\s*$", re.MULTILINE)
_FIELD = "\x1f"
_RECORD = "\x1e"


@dataclass(frozen=True)
class Commit:
    sha: str
    author_name: str
    author_email: str
    parents: int
    message: str


def commits(base: str, head: str, cwd: Path | None = None) -> list[Commit]:
    git = shutil.which("git")
    if git is None:
        raise RuntimeError("git is not on PATH")
    fmt = _FIELD.join(["%H", "%an", "%ae", "%P", "%B"]) + _RECORD
    # An argument list with no shell; base and head are only ever revision names.
    out = subprocess.run(  # noqa: S603
        [git, "log", f"--format={fmt}", f"{base}..{head}"],
        cwd=cwd,
        capture_output=True,
        check=True,
        text=True,
        encoding="utf-8",
    ).stdout
    found: list[Commit] = []
    for record in out.split(_RECORD):
        record = record.lstrip("\n")
        if not record:
            continue
        sha, name, email, parents, message = record.split(_FIELD, 4)
        found.append(Commit(sha, name, email, len(parents.split()), message))
    return found


def signed_off_by_author(commit: Commit) -> bool:
    for match in _SIGN_OFF.finditer(commit.message):
        same_name = match["name"].strip() == commit.author_name.strip()
        same_email = match["email"].strip().lower() == commit.author_email.strip().lower()
        if same_name and same_email:
            return True
    return False


def unsigned(found: list[Commit]) -> list[Commit]:
    return [c for c in found if c.parents < 2 and not signed_off_by_author(c)]


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2:
        print("usage: check_dco.py <base> <head>", file=sys.stderr)
        return 2
    missing = unsigned(commits(args[0], args[1]))
    for commit in missing:
        subject = commit.message.splitlines()[0] if commit.message else ""
        print(
            f"{commit.sha[:12]} {subject}\n"
            f"  needs: Signed-off-by: {commit.author_name} <{commit.author_email}>",
            file=sys.stderr,
        )
    if missing:
        print(
            f"\n{len(missing)} commit(s) are not signed off by their author. "
            "Run `git rebase --signoff <base>` and force-push. See CONTRIBUTING.md.",
            file=sys.stderr,
        )
        return 1
    print("every commit is signed off by its author")
    return 0


if __name__ == "__main__":
    sys.exit(main())
