"""Two workspace members on a real ``none``-mode host: apart, except the tree.

The composed launches' own steps hand each member its trees and the shared
tree to the workspace, and a command run through each launch's wrapper proves
it on a real kernel: a member cannot open the other's agent state, and both
read and write the shared tree, each seeing what the other made there. It
needs Linux, root, ``setpriv``, ``setfacl`` and ``useradd``, so it runs in the
``live`` tier and skips everywhere else.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from alkera_cli.harness import sandbox as sb

pytestmark = [pytest.mark.live]


@pytest.fixture
def users() -> Iterator[dict[str, int]]:
    if sys.platform != "linux":
        pytest.skip("a real uid drop needs a Linux host")
    if os.geteuid() != 0:
        pytest.skip("a real uid drop needs root (the daemon runs as root on a box)")
    for tool in ("setpriv", "setfacl", "useradd", "userdel"):
        if shutil.which(tool) is None:
            pytest.skip(f"no {tool} on this host")
    tag = uuid.uuid4().hex[:8]
    names = {"ws": f"ws{tag}", "a": f"a{tag}", "b": f"b{tag}"}
    made = {key: sb.ensure_chat_uid(name) for key, name in names.items()}
    try:
        yield made
    finally:
        for name in names.values():
            subprocess.run(["userdel", sb.chat_user(name)], capture_output=True, check=False)


def _member(root: Path, who: str, uid: int, ws: int) -> sb.SandboxLaunch:
    chat_dir = root / ".alkera" / "chats" / who
    runtime = chat_dir / sb.RUNTIME_STATE_SUBDIR
    config = root / "home" / "harness" / who
    for path in (runtime, config / "config", config / "state"):
        path.mkdir(parents=True, exist_ok=True)
    spec = sb.SandboxSpec(
        chat_id=who,
        folder=root / ".alkera" / "workspaces" / "ws" / "files",
        uid=uid,
        share_gid=ws,
        vcpu=1,
        memory_mb=512,
        home="/home/alkera",
        mode="none",
        cgroup="none",
        binds=sb.chat_binds(runtime_dir=runtime, agent_config_root=config, default_env=False),
        private_dirs=(chat_dir, config),
        setpriv=shutil.which("setpriv") or "setpriv",
    )
    launch = sb.NoneRuntime().compose_launch(spec, ("/bin/true",))
    sb.run_steps(launch.before)
    return launch


def _as(launch: sb.SandboxLaunch, script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        launch.wrap(["/bin/sh", "-c", script]),
        capture_output=True,
        text=True,
        check=False,
        umask=launch.umask if launch.umask is not None else 0o077,
        cwd="/",
    )


def test_members_cannot_open_each_others_state_and_share_the_tree(
    tmp_path: Path, users: dict[str, int]
) -> None:
    root = tmp_path / "work"
    shared = root / ".alkera" / "workspaces" / "ws" / "files"
    shared.mkdir(parents=True)
    a = _member(root, "a", users["a"], users["ws"])
    b = _member(root, "b", users["b"], users["ws"])
    a_state = root / ".alkera" / "chats" / "a" / sb.RUNTIME_STATE_SUBDIR / "agent" / "db"

    wrote = _as(
        a, f"echo secret > {a_state} && mkdir -p {shared}/out && echo hi > {shared}/out/a.txt"
    )
    assert wrote.returncode == 0, wrote.stderr
    # B cannot read or write A's agent state.
    peek = _as(b, f"cat {a_state}")
    assert peek.returncode != 0 and "secret" not in peek.stdout
    assert _as(b, f"echo x >> {a_state}").returncode != 0
    assert a_state.read_text() == "secret\n"
    # Both share the tree, each seeing what the other made.
    seen = _as(b, f"cat {shared}/out/a.txt && echo there > {shared}/out/b.txt")
    assert (seen.returncode, seen.stdout) == (0, "hi\n"), seen.stderr
    back = _as(a, f"cat {shared}/out/b.txt")
    assert (back.returncode, back.stdout) == (0, "there\n"), back.stderr
    # What a member made in the tree is the workspace group's.
    assert (shared / "out" / "a.txt").stat().st_gid == users["ws"]
