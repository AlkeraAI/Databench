"""A real ``none``-mode launch on the container a RunPod pod is.

The pure tests pin the composition; this runs it: the probe's verdict on the
host it runs on, the launch composed from that verdict, its steps run for
real, and a command run through its wrapper that proves the uid drop, the
folder as the agent's home, the read-only trees lent to the uid, the cgroup
where the host has one and the alias where it can make one — and that a
child's exit status is read as its own, even when the process started with
``SIGCHLD`` ignored (which is how a pod once reported a working systemd). It
needs Linux and root, so it runs in the ``live`` tier inside the RunPod image
(with and without extra capabilities) and skips everywhere else.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import textwrap
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from alkera_cli.harness import sandbox as sb
from alkera_cli.harness.sandbox_probe import SandboxCapability, probe

pytestmark = [pytest.mark.live]

CGROUP_ROOT = Path("/sys/fs/cgroup")


def _host() -> SandboxCapability:
    if sys.platform != "linux":
        pytest.skip("a real uid drop needs a Linux host")
    if os.geteuid() == 0:
        _delegate_controllers()
    cap = probe(settings=sb.SandboxSettings(mode="none"))
    if not cap.root:
        pytest.skip("a real uid drop needs root (the daemon runs as root on a box)")
    if not cap.uid:
        pytest.skip(f"no chat uid on this host: {cap.reason}")
    if shutil.which("useradd") is None or shutil.which("userdel") is None:
        pytest.skip("no useradd/userdel to make the chat's user with")
    return cap


def _delegate_controllers() -> None:
    """What the prerequisites do on a cgroupfs box: hand the parent slice the
    controllers the per-chat groups set limits on. Best effort — where the
    kernel refuses (a container whose root group holds its own processes) the
    probe answers ``none`` for the cgroup and the launch applies the uid without
    one, which is what this test then proves."""
    if not os.access(CGROUP_ROOT, os.W_OK):
        return
    parent = CGROUP_ROOT / "alkera.slice"
    try:
        parent.mkdir(exist_ok=True)
    except OSError:
        return
    for ctl in ("cpu", "memory", "pids"):
        for node in (CGROUP_ROOT, parent):
            try:
                (node / "cgroup.subtree_control").write_text(f"+{ctl}")
            except OSError:
                pass


@pytest.fixture
def chat() -> Iterator[tuple[str, int]]:
    _host()
    chat_id = f"live{uuid.uuid4().hex[:12]}"
    uid = sb.ensure_chat_uid(chat_id)
    try:
        yield chat_id, uid
    finally:
        subprocess.run(["userdel", sb.chat_user(chat_id)], capture_output=True, check=False)


def test_the_probe_reads_real_exit_status_even_when_started_with_sigchld_ignored() -> None:
    """The verdict a daemon started under an ancestor that ignores ``SIGCHLD``
    reaches must be the host's real one, not 'everything works'."""
    if sys.platform != "linux":
        pytest.skip("needs a Linux host")
    script = textwrap.dedent(
        """
        import json, signal, subprocess
        signal.signal(signal.SIGCHLD, signal.SIG_IGN)
        blind = subprocess.run(["/bin/false"], capture_output=True).returncode
        from alkera_cli.harness import sandbox as sb
        from alkera_cli.harness.sandbox_probe import probe
        cap = probe(settings=sb.SandboxSettings(mode="none"))
        after = subprocess.run(["/bin/false"], capture_output=True).returncode
        print(json.dumps({"blind": blind, "after": after, "cgroup": cap.cgroup,
                          "mount_ns": cap.mount_ns, "exit_status": cap.exit_status}))
        """
    )
    done = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=False, timeout=120
    )
    assert done.returncode == 0, done.stderr
    child = json.loads(done.stdout.strip().splitlines()[-1])
    normal = probe(settings=sb.SandboxSettings(mode="none"))
    assert child["blind"] == 0 and child["after"] == 1  # the ignored state, then reclaimed
    assert child["exit_status"] is True
    assert (child["cgroup"], child["mount_ns"]) == (normal.cgroup, normal.mount_ns)


def test_a_none_launch_runs_a_command_as_the_chat_uid_with_the_controls_the_host_has(
    tmp_path: Path, chat: tuple[str, int]
) -> None:
    cap = _host()
    chat_id, uid = chat
    chat_dir = tmp_path / "chat"
    folder = chat_dir / "sandbox"
    runtime_dir = chat_dir / ".runtime"
    folder.mkdir(parents=True)
    runtime_dir.mkdir()
    (folder / "seed.txt").write_text("hi\n")
    # The agent's own tree, closed the way a 077 umask leaves what the daemon makes.
    agent_dir = tmp_path / "cache" / "runtime-x"
    agent_dir.mkdir(parents=True, mode=0o700)
    (tmp_path / "cache").chmod(0o700)
    tool = agent_dir / "tool"
    tool.write_text("#!/bin/sh\necho tool-ok\n")
    tool.chmod(0o700)
    home = str(tmp_path / "home")
    spec = sb.SandboxSpec(
        chat_id=chat_id,
        folder=folder,
        uid=uid,
        vcpu=1,
        memory_mb=256,
        home=home,
        mode="none",
        cgroup=cap.cgroup,
        binds=(
            sb.Bind(runtime_dir, sb.HARNESS_DATA_MOUNT),
            *sb.tool_binds(agent_dir),
        ),
        private_dirs=(chat_dir,),
        default_env=None,
        mount_alias=cap.mount_ns,
        setpriv=cap.setpriv or "setpriv",
        unshare=cap.unshare or "unshare",
        cgroup_root=CGROUP_ROOT,
    )
    launch = sb.NoneRuntime().compose_launch(spec)
    sb.run_steps(launch.before)
    try:
        env = launch.apply_env(dict(os.environ))
        script = (
            'id -u; cat "$HOME/seed.txt"; echo "$HOME"; '
            f"{tool.as_posix()}; "
            'touch "$HOME/written"; sleep 1; cat /proc/self/cgroup'
        )
        proc = subprocess.Popen(
            launch.wrap(["/bin/sh", "-c", script]),
            env=env,
            cwd=launch.cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        sb.run_steps(launch.after_spawn(proc.pid))
        out, err = proc.communicate(timeout=60)
        assert proc.returncode == 0, err
        lines = out.splitlines()
        assert lines[0] == str(uid)
        assert lines[1] == "hi"
        assert lines[2] == (home if cap.mount_ns else folder.as_posix())
        assert lines[3] == "tool-ok"
        if cap.cgroup == "cgroupfs":
            assert any(f"alkera.slice/chat-{sb.chat_slug(chat_id)}" in line for line in lines[4:])
        elif cap.cgroup == "systemd":
            assert any(sb.chat_slice(chat_id) in line for line in lines[4:])
        written = folder / "written"
        assert written.exists() and written.stat().st_uid == uid
        # A failing command's own status comes back, never 255.
        failed = subprocess.run(
            launch.wrap(["/bin/sh", "-c", "exit 7"]),
            env=env,
            cwd=launch.cwd,
            capture_output=True,
            check=False,
            timeout=60,
        )
        assert failed.returncode == 7
    finally:
        sb.run_steps(launch.after_exit)
    assert signal.getsignal(signal.SIGCHLD) != signal.SIG_IGN
