"""A harness child's sandbox launch rides inside the spawn seam.

The mechanism (detaching, the death-signal launcher, the Job Object) is
:mod:`alkera_core.process`'s and is tested there. What is pinned here is the
harness adapter: the sandbox's wrapper sits INSIDE the death-signal launcher,
so the whole chain dies with the daemon, and the child starts in the
sandbox's working directory under its umask.
"""

from __future__ import annotations

import sys
from pathlib import Path, PureWindowsPath

import pytest
from alkera_cli.harness.sandbox import SandboxLaunch
from alkera_cli.harness.spawn import sandbox_command, sandbox_spec
from alkera_core import process as core_process
from alkera_core.process import popen_args


def _launch() -> SandboxLaunch:
    # A none-mode launch that still applies the uid + cgroup: a prefix the
    # caller's argv is appended to.
    return SandboxLaunch(
        mode="none",
        prefix=("systemd-run", "--scope", "--", "setpriv", "--reuid=20017", "--"),
        env={"HOME": "/home/alkera"},
        cwd=Path("/opt/alkera-work/.alkera/chats/c/sandbox"),
    )


def test_the_sandbox_cwd_is_spelled_for_the_linux_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """The sandbox's working directory is a path on the Linux box; composed
    from a backslash-joining ``Path`` it must still be ``/opt/...``."""
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(core_process, "pdeathsig_launcher", lambda: "")
    launch = SandboxLaunch(
        mode="none",
        prefix=("setpriv", "--"),
        cwd=PureWindowsPath("/opt/alkera-work/.alkera/chats/c/sandbox"),
    )
    _, kwargs = popen_args(sandbox_spec(["/opt/agent", "serve"], sandbox=launch, env={}))
    assert kwargs["cwd"] == "/opt/alkera-work/.alkera/chats/c/sandbox"


def test_the_sandbox_sits_inside_the_pdeathsig_launcher(monkeypatch: pytest.MonkeyPatch) -> None:
    """Outermost is still the death signal, so the whole chain (cgroup
    placement, uid drop, agent) dies with the daemon."""
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(core_process, "pdeathsig_launcher", lambda: "/usr/bin/setpriv")
    expected = [
        "/usr/bin/setpriv",
        "--pdeathsig",
        "KILL",
        "--",
        "systemd-run",
        "--scope",
        "--",
        "setpriv",
        "--reuid=20017",
        "--",
        "/opt/agent",
        "serve",
    ]
    argv, kwargs = popen_args(sandbox_spec(["/opt/agent", "serve"], sandbox=_launch(), env={}))
    assert argv == expected
    assert sandbox_command(["/opt/agent", "serve"], _launch()) == expected
    assert kwargs["cwd"] == "/opt/alkera-work/.alkera/chats/c/sandbox"
    assert kwargs["start_new_session"] is True
    assert "preexec_fn" not in kwargs


def test_a_gvisor_baked_command_runs_inside_the_pdeathsig_launcher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A gVisor agent-server launch bakes the whole runsc command (its argv is
    in the OCI config, not appended), so the baked command runs inside the
    death-signal launcher and the passed argv is not appended."""
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(core_process, "pdeathsig_launcher", lambda: "/usr/bin/setpriv")
    launch = SandboxLaunch(
        mode="gvisor",
        command=("runsc", "--network=sandbox", "run", "--bundle=/b", "alkera-chat-c"),
        cwd=Path("/b"),
    )
    argv, kwargs = popen_args(sandbox_spec(["/opt/agent", "serve"], sandbox=launch, env={}))
    assert argv == [
        "/usr/bin/setpriv",
        "--pdeathsig",
        "KILL",
        "--",
        "runsc",
        "--network=sandbox",
        "run",
        "--bundle=/b",
        "alkera-chat-c",
    ]
    assert kwargs["cwd"] == "/b"


def test_a_sandbox_with_no_launcher_still_prefixes_and_keeps_the_hook(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(core_process, "pdeathsig_launcher", lambda: "")
    argv, kwargs = popen_args(sandbox_spec(["/opt/agent", "serve"], sandbox=_launch(), env={}))
    assert argv[:3] == ["systemd-run", "--scope", "--"] and argv[-2:] == ["/opt/agent", "serve"]
    assert kwargs["cwd"] == "/opt/alkera-work/.alkera/chats/c/sandbox"
    assert callable(kwargs["preexec_fn"])


def test_a_none_launch_is_the_command_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    spec = sandbox_spec(["/opt/agent", "serve"], sandbox=SandboxLaunch(mode="none"), env={})
    argv, kwargs = popen_args(spec)
    assert argv == ["/opt/agent", "serve"]
    assert "cwd" not in kwargs and "umask" not in kwargs
