"""Two chats' trees, laid out side by side the way a box lays them out, with the
sandbox's ownership steps run over both — and what each chat's uid can then do
with its own trees and with the other chat's.

The steps are the real ``chown`` / ``chmod`` / ``setfacl`` over real users, so
this needs Linux, root, shadow-utils and the acl tools. It is a module so the
test can call :func:`run` in-process when pytest itself runs as root, and a
script so the test can run it under ``sudo`` — or a privileged container can —
when it does not: ``python _sandbox_ownership_probe.py`` prints the facts as
one JSON object (``{"skip": reason}`` where it cannot run).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from alkera_cli.harness import sandbox as sb

REQUIRED_TOOLS = ("useradd", "userdel", "setfacl")


def unavailable() -> str | None:
    """Why the probe cannot run in this process, or ``None`` when it can."""
    if not sys.platform.startswith("linux"):
        return "real users and ACLs need Linux"
    if os.geteuid() != 0:
        return "the ownership steps need root"
    missing = [tool for tool in REQUIRED_TOOLS if shutil.which(tool) is None]
    if missing:
        return f"missing {', '.join(missing)} (shadow-utils and acl)"
    return None


def _run_as(uid: int, interpreter: str, code: str) -> subprocess.CompletedProcess[str]:
    """``interpreter -c code`` as ``uid`` (its own group, no others), from ``/``."""
    return subprocess.run(
        [interpreter, "-c", code],
        user=uid,
        group=uid,
        extra_groups=[],
        capture_output=True,
        text=True,
        check=False,
        cwd="/",
    )


def interpreter_for(uid: int) -> str | None:
    """A ``python3`` the chat's uid can run, for the attempts made as that uid.

    The attempts need only the standard library, so any interpreter will do;
    what matters is that the uid can reach it. The process's own is tried
    first, but it may live under a directory closed to other users (a virtual
    environment under ``/root``, a runner's home), so the system ones follow.
    """
    candidates = [
        sys.executable,
        shutil.which("python3"),
        "/usr/bin/python3",
        "/usr/local/bin/python3",
    ]
    for candidate in candidates:
        if not candidate:
            continue
        try:
            done = _run_as(uid, candidate, "print('ready')")
        except OSError:
            continue
        if done.returncode == 0 and done.stdout.strip() == "ready":
            return candidate
    return None


def _as(uid: int, interpreter: str, code: str) -> str:
    """Run ``code`` as ``uid`` and return what it printed: ``ok`` or the
    error's class name."""
    wrapped = (
        f"import os\ntry:\n    {code}\n    print('ok')\n"
        "except OSError as exc:\n    print(type(exc).__name__)\n"
    )
    done = _run_as(uid, interpreter, wrapped)
    if done.returncode != 0:
        raise RuntimeError(f"the attempt itself failed as uid {uid}: {done.stderr.strip()}")
    return done.stdout.strip()


def _read(path: Path) -> str:
    return f"open({str(path)!r}).read()"


def _write(path: Path) -> str:
    return f"open({str(path)!r}, 'w').write('x')"


def _listdir(path: Path) -> str:
    return f"os.listdir({str(path)!r})"


@dataclass(frozen=True)
class ChatTrees:
    """One chat's trees as the box lays them out: its records and folder under
    the work root, its agent config and state under the daemon's home."""

    tag: str
    chat: Path
    folder: Path
    runtime: Path
    root: Path
    config: Path
    state: Path

    @property
    def transcript(self) -> Path:
        return self.chat / "transcript.jsonl"

    @property
    def report(self) -> Path:
        return self.folder / "report.csv"

    @property
    def policy(self) -> Path:
        return self.config / "agent" / "build.md"

    @property
    def instructions(self) -> Path:
        return self.config / "global-instructions.md"


def lay_out(base: Path, tag: str) -> ChatTrees:
    """Make the chat's trees under ``base`` with the modes the daemon's own
    writes leave (world-traversable directories, world-readable files), so
    that "another uid cannot reach it" afterwards is the steps' doing."""
    chat = base / ".alkera" / "chats" / tag
    folder, runtime = chat / "sandbox", chat / ".runtime"
    root = base / "home" / "harness" / tag
    config, state = root / "config", root / "state"
    trees = ChatTrees(
        tag=tag, chat=chat, folder=folder, runtime=runtime, root=root, config=config, state=state
    )
    for directory in (folder, runtime, config / "agent", state):
        directory.mkdir(parents=True)
    for directory in (
        base / ".alkera",
        base / ".alkera" / "chats",
        chat,
        base / "home",
        base / "home" / "harness",
        root,
    ):
        directory.chmod(0o755)
    trees.transcript.write_text("records")
    trees.report.write_text("work")
    trees.policy.write_text("policy")
    trees.instructions.write_text("instructions")
    for file in (trees.transcript, trees.report, trees.policy, trees.instructions):
        file.chmod(0o644)
    return trees


def spec_for(trees: ChatTrees, uid: int) -> sb.SandboxSpec:
    """The chat's sandbox as the adapter states it: the trees it owns, the
    config it reads, and the two directories private to it — the chat's
    records beside its folder, and its agent root beside every other chat's."""
    return sb.SandboxSpec(
        chat_id=trees.tag,
        folder=trees.folder,
        uid=uid,
        vcpu=1,
        memory_mb=256,
        home="/home/alkera",
        mode="none",
        cgroup="none",
        binds=(
            sb.Bind(trees.runtime, sb.HARNESS_DATA_MOUNT),
            sb.Bind(trees.state, sb.HARNESS_STATE_MOUNT),
            sb.Bind(trees.config, sb.HARNESS_CONFIG_MOUNT, readonly=True, kind="config"),
        ),
        private_dirs=(trees.chat, trees.root),
    )


def facts(actor: int, interpreter: str, trees: ChatTrees) -> dict[str, str]:
    """What uid ``actor`` can do with ``trees``: ``ok`` or the error's class
    name for each attempt. The same attempts are made by a chat on its own
    trees and by the other chat, so the two answers compare key for key."""

    def attempt(code: str) -> str:
        return _as(actor, interpreter, code)

    return {
        "write folder": attempt(_write(trees.folder / f"new-{actor}.txt")),
        "write runtime": attempt(_write(trees.runtime / f"agent-{actor}.db")),
        "make state dir": attempt(f"os.makedirs({str(trees.state / 'agent' / f'bin-{actor}')!r})"),
        "read config": attempt(_read(trees.policy)),
        "read instructions": attempt(_read(trees.instructions)),
        "change config": attempt(_write(trees.policy)),
        "add to config": attempt(_write(trees.config / "agent" / f"evil-{actor}.md")),
        "move config": attempt(
            f"os.rename({str(trees.config)!r}, {str(trees.root / f'moved-{actor}')!r})"
        ),
        "read transcript": attempt(_read(trees.transcript)),
        "read report": attempt(_read(trees.report)),
        "list chat": attempt(_listdir(trees.chat)),
        "list folder": attempt(_listdir(trees.folder)),
        "list state": attempt(_listdir(trees.state)),
        "list config": attempt(_listdir(trees.config)),
    }


def run() -> dict[str, Any]:
    """Lay out two chats, run each one's steps the way a box does — A's spawn,
    then B's beside it, then A again (A restarted after B appeared) — and
    report what each chat's uid can do with its own trees and the other's."""
    reason = unavailable()
    if reason is not None:
        return {"skip": reason}
    tags = [f"a{uuid.uuid4().hex[:10]}", f"b{uuid.uuid4().hex[:10]}"]
    # Under a world-traversable parent like the box's work root, not pytest's
    # owner-only tmp dir, so "another uid cannot reach it" is the steps' doing.
    base = Path(tempfile.mkdtemp(prefix="alkera-ownership-", dir="/var/tmp"))
    base.chmod(0o755)
    users: list[str] = []
    try:
        trees = [lay_out(base, tag) for tag in tags]
        uids: list[int] = []
        for tag in tags:
            users.append(sb.chat_user(tag))
            uids.append(sb.ensure_chat_uid(tag))
        a, b = trees
        uid_a, uid_b = uids
        interpreter = interpreter_for(uid_a)
        if interpreter is None:
            return {
                "skip": f"no python3 the chat uids can run ({sys.executable} is under a "
                "directory closed to them and no system python3 was found)"
            }
        for chat, uid in ((a, uid_a), (b, uid_b), (a, uid_a)):
            sb.run_steps(sb.NoneRuntime().compose_launch(spec_for(chat, uid)).before)
        return {
            "uids": [uid_a, uid_b],
            "interpreter": interpreter,
            "a_on_a": facts(uid_a, interpreter, a),
            "b_on_b": facts(uid_b, interpreter, b),
            "a_on_b": facts(uid_a, interpreter, b),
            "b_on_a": facts(uid_b, interpreter, a),
        }
    finally:
        for name in users:
            subprocess.run(["userdel", name], check=False, capture_output=True)
        shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    print(json.dumps(run()))
