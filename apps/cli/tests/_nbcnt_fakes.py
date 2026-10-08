"""A stand-in ``runsc`` and a kernel sandbox over temporary directories, for the
kernel sandbox lane's tests.

The stand-in is a real executable: every call appends its argv to a log; ``run``
waits for a signal like a sandbox; ``exec`` with ``--internal-pid-file`` plays a
kernel (it leads its own process group, writes its pid, reads the token from
the first line of stdin, records every ``SIGINT`` and keeps running, and ends
on ``SIGTERM`` or ``SIGKILL``); ``exec ... kill -<SIG> -- -<pgid>`` signals a
group the stand-in itself started, and never anything else (``kill -1`` is
recorded and not run); ``exec ... true`` answers readiness; anything else exits
with ``NBCNT_EXEC_STATUS`` (0). Steps that change the host as root (``ip``,
``nft``, ``chown``, the repair script) are recorded by :class:`StepRunner` and
not run; ``mkdir``, ``rm`` and ``chmod`` on the temporary trees are run.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from alkera_cli.harness.sandbox import ROOTFS_STAMP, SandboxSettings
from alkera_cli.harness.sandbox_probe import SandboxCapability
from alkera_cli.harness.sandbox_steps import RepairStep
from alkera_cli.notebooks.kernel_sandbox import KernelSandbox, KernelSandboxConfig, KernelTrees

FAKE_RUNSC = r"""#!{python}
import json, os, signal, sys, time

state = os.environ["NBCNT_STATE"]
args = sys.argv[1:]
with open(os.path.join(state, "argv.jsonl"), "a") as fh:
    fh.write(json.dumps(args) + "\n")
words = [a for a in args if not a.startswith("--")]
sub = words[0] if words else ""

def stopped(*_):
    sys.exit(0)

if sub == "run":
    signal.signal(signal.SIGTERM, stopped)
    while True:
        time.sleep(0.05)
if sub != "exec":
    sys.exit(0)
rest = args[args.index("exec") + 1 :]
flags = {}
while rest and rest[0].startswith("--"):
    name, _, value = rest.pop(0)[2:].partition("=")
    flags.setdefault(name, []).append(value)
rest.pop(0)  # the container
env = {}
if rest[:2] == ["/usr/bin/env", "-i"]:
    rest = rest[2:]
    while rest and "=" in rest[0] and not rest[0].startswith("/"):
        k, _, v = rest.pop(0).partition("=")
        env[k] = v
with open(os.path.join(state, "env.jsonl"), "a") as fh:
    fh.write(json.dumps(env) + "\n")
if rest == ["true"]:
    sys.exit(1 if os.path.exists(os.path.join(state, "not-ready")) else 0)
if rest[:1] == ["kill"]:
    target = rest[-1]
    if target == "-1":
        sys.exit(0)
    group = int(target.lstrip("-"))
    started = open(os.path.join(state, "started")).read().split() if os.path.exists(
        os.path.join(state, "started")) else []
    if str(group) in started:
        os.killpg(group, getattr(signal, "SIG" + rest[1].lstrip("-")))
    sys.exit(0)
if rest and rest[0].endswith("python3") and "-c" in rest:
    print(os.environ.get("NBCNT_RSS", "0"))
    sys.exit(0)
if "internal-pid-file" in flags:
    # Inside a container the kernel leads a session of its own; the stand-in
    # already does when the launcher started it in one.
    try:
        os.setsid()
    except PermissionError:
        pass
    with open(os.path.join(state, "started"), "a") as fh:
        fh.write(f"{os.getpid()}\n")
    token = sys.stdin.readline().strip()
    with open(os.path.join(state, f"token-{os.getpid()}"), "w") as fh:
        fh.write(token)
    with open(flags["internal-pid-file"][0], "w") as fh:
        fh.write(str(os.getpid()))
    def interrupted(*_):
        with open(os.path.join(state, f"signals-{os.getpid()}"), "a") as fh:
            fh.write("INT\n")
    signal.signal(signal.SIGINT, interrupted)
    signal.signal(signal.SIGTERM, stopped)
    while True:
        time.sleep(0.02)
print("ran", " ".join(rest), flush=True)
time.sleep(float(os.environ.get("NBCNT_EXEC_SLEEP", "0")))
sys.exit(int(os.environ.get("NBCNT_EXEC_STATUS", "0")))
"""


def write_fake_runsc(directory: Path) -> Path:
    path = directory / "runsc"
    path.write_text(FAKE_RUNSC.replace("{python}", sys.executable))
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


#: Programs whose steps are run for real in the tests (on temporary paths).
RUN_FOR_REAL = frozenset({"mkdir", "rm", "chmod"})


@dataclass
class StepRunner:
    """The launch's step runner: the stand-in runsc and the harmless tools run;
    every other step is recorded and answers 0."""

    runsc: Path
    recorded: list[tuple[str, ...]] = field(default_factory=list)
    repaired: list[RepairStep] = field(default_factory=list)

    def repair(self, step: RepairStep) -> None:
        """Root's walk of a tree, recorded: handing it to a kernel's uid is
        root's alone."""
        self.repaired.append(step)

    def __call__(self, argv: Sequence[str]) -> int:
        self.recorded.append(tuple(argv))
        if argv[0] == str(self.runsc) or argv[0] in RUN_FOR_REAL:
            return subprocess.run(list(argv), capture_output=True, check=False).returncode
        return 0


@dataclass
class Rig:
    root: Path
    short: Path
    state: Path
    runsc: Path
    config: KernelSandboxConfig
    runner: StepRunner
    uids: dict[str, int]
    cap: SandboxCapability
    settings: SandboxSettings

    def runsc_calls(self) -> list[list[str]]:
        log = self.state / "argv.jsonl"
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text().splitlines()]

    def exec_envs(self) -> list[dict[str, str]]:
        log = self.state / "env.jsonl"
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text().splitlines()]

    def ensure_uid(self, name: str) -> int:
        return self.uids.setdefault(name, 20100 + len(self.uids))

    def sandbox(self, **kwargs: object) -> KernelSandbox:
        return KernelSandbox(
            self.config,
            settings=self.settings,
            cap=self.cap,
            ensure_uid=self.ensure_uid,
            run=self.runner,
            repair=self.runner.repair,
            **kwargs,  # type: ignore[arg-type]
        )


def make_rig(tmp_path: Path, monkeypatch: object, **overrides: object) -> Rig:
    """A kernel sandbox config over temporary trees, a staged rootfs (with its
    stamp, behind a ``current`` link), a platform mount and the stand-in runsc.
    The runtime directory sits under a short ``/tmp`` path so socket paths fit."""
    root = tmp_path
    short = Path(tempfile.mkdtemp(prefix="nbcnt-", dir="/tmp"))
    state = root / "runsc-state"
    state.mkdir()
    monkeypatch.setenv("NBCNT_STATE", str(state))
    runsc = write_fake_runsc(root)
    staged = root / "rootfs" / "abc123"
    staged.mkdir(parents=True)
    (staged / ROOTFS_STAMP).write_text("abc123\n")
    (root / "rootfs" / "current").symlink_to(staged)
    mount = root / "py"
    mount.mkdir()
    (mount / "boot.py").write_text("# boot\n")
    folder = root / "ws" / "files"
    (folder / "nb").mkdir(parents=True)
    envs = root / "envs" / "ws1"
    (envs / "alkera" / "bin").mkdir(parents=True)
    (envs / "alkera" / "bin" / "python").write_text("")
    trees = KernelTrees(
        envs_dir=envs,
        platform_mount=mount,
        runtime_dir=short / "run",
        cgroup_root=root / "cg",
        runsc_root=root / "runsc-root",
    )
    values: dict[str, object] = {
        "workspace": "ws:ws1",
        "folder": folder,
        "trees": trees,
        "state_dir": root / "kernel-state",
        "memory_mb": 1536,
        "vcpu": 2,
    }
    values.update(overrides)
    config = KernelSandboxConfig(**values)  # type: ignore[arg-type]
    cap = SandboxCapability(
        platform="linux",
        root=True,
        setpriv="/usr/bin/setpriv",
        setfacl="/usr/bin/setfacl",
        runsc=str(runsc),
        cgroup="cgroupfs",
        reason="injected: a stand-in runsc",
        rootfs=str(staged),
        ip="ip",
        nft="nft",
        unshare="unshare",
        uv="uv",
        python_home="/opt/alkera/python/current",
        resolvers=("1.1.1.1",),
    )
    settings = SandboxSettings(mode="gvisor")
    uids = {"ws:ws1": 20001}
    return Rig(root, short, state, runsc, config, StepRunner(runsc), uids, cap, settings)


def wait_for(path: Path, timeout: float = 5.0) -> None:
    import time

    deadline = time.monotonic() + timeout
    while not path.exists():
        if time.monotonic() > deadline:
            raise TimeoutError(f"{path} never appeared")
        time.sleep(0.01)


def process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
