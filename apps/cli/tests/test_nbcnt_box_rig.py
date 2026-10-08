"""The kernel sandbox on a real gVisor box: the engine, the kernel runtime and the
box's pieces together, beside an agent's container of the same workspace.

Opt in, on a local dev box (the localdev box container), as root, with the
daemon's venv and gVisor ready::

    ALKERA_NBCNT_BOX=1 /opt/alkera-venv/bin/python -m pytest --noconftest \\
        -p no:cacheprovider -o addopts="" /opt/alkera-src/apps/cli/tests/test_nbcnt_box_rig.py

Everything is made under ``ALKERA_NBCNT_BOX_ROOT`` (``/opt/alkera-work/nbcnt-rig``)
and taken down at the end. The last case starts the same module again as an
org worker, through the supervisor's own launch chain (the org's cgroup, the
read-only mount namespace, the user namespace mapping the slot's id range, its
own network namespace and link), and requires every case to pass there too.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pytest

pytestmark = [
    pytest.mark.skipif(
        os.environ.get("ALKERA_NBCNT_BOX") != "1" or os.geteuid() != 0 or not shutil.which("runsc"),
        reason="the kernel sandbox box rig runs on a gVisor dev box as root (ALKERA_NBCNT_BOX=1)",
    ),
    pytest.mark.xdist_group("nbcnt_box_rig"),
]

ROOT = Path(os.environ.get("ALKERA_NBCNT_BOX_ROOT", "/opt/alkera-work/nbcnt-rig"))
#: Set to ``inner`` when this module runs as an org worker (the outer case
#: starts it so).
IN_WORKER = os.environ.get("ALKERA_NBCNT_IN_WORKER", "")
ORGS_ROOT = Path("/opt/alkera-work/nbcnt-orgs")
SLOT = 7
WS = "ws:5b0c0e1e-0000-4000-8000-00000000cafe"
CHAT = "chat_5b0c0e1e0000aaaa"
SECRET = "AGENT-SECRET-MARKER-5b0c"
AGENT_PORT = 4096
LIMIT_MB = 1536


@dataclass
class Box:
    tree: Path
    envs: Path
    interpreter: str
    host: Any
    parts: Any
    agent_launch: Any
    agent_proc: subprocess.Popen[bytes]
    agent_ip: str
    agent_spec: Any

    def agent_run(self, *argv: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        """A command run on the agent's behalf, in its container, as the box runs one."""
        from alkera_cli.harness.sandbox import GvisorRuntime, command_env, shell_spec

        shell = GvisorRuntime().compose_launch(shell_spec(self.agent_spec))
        env = shell.apply_env(command_env(self.agent_spec))
        done = subprocess.run(
            shell.wrap(list(argv), env=env), capture_output=True, text=True, check=False
        )
        if check and done.returncode != 0:
            raise AssertionError(f"agent command failed: {argv}: {done.stderr}")
        return done

    def kernel_run(self, *argv: str, user: tuple[int, int] | None = None) -> str:
        """A command in the kernel sandbox, as a kernel slot's uid by default."""
        from alkera_cli.notebooks.kernel_sandbox import exec_argv

        sandbox = self.parts.sandbox
        slot = sandbox.slot_identities()[-1]
        who = user or (slot.uid, slot.gid)
        env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/home/alkera"}
        done = subprocess.run(
            exec_argv(
                sandbox.target,
                who,
                ("/bin/sh", "-c", 'umask 002; exec "$@"', "k", *argv),
                env=env,
                cwd="/home/alkera",
            ),
            capture_output=True,
            text=True,
            check=False,
        )
        return done.stdout + done.stderr

    def agent_answers(self) -> bool:
        try:
            with urllib.request.urlopen(f"http://{self.agent_ip}:{AGENT_PORT}/", timeout=2) as r:
                return bool(r.status == 200)
        except OSError:
            return False


def _capability() -> Any:
    from alkera_cli.harness.sandbox_probe import probe

    cap = probe()
    assert cap.gvisor, cap.reason
    return cap


def _start_agent(tree: Path, envs: Path, cap: Any, ensure: Any) -> tuple[Any, Any, Any]:
    """An agent's container of the workspace, composed by the box's own launch:
    the shared tree at its home, the workspace's environments, the tree's
    uid, its own network slot, and marker secrets in its environment. Its
    server answers HTTP, for the "every agent request answers" probe."""
    from alkera_cli.harness.sandbox import GvisorRuntime, SandboxSpec
    from alkera_cli.harness.sandbox_kinds import box_tools
    from alkera_cli.harness.sandbox_layout import chat_binds
    from alkera_cli.harness.sandbox_steps import run_steps

    chat = ROOT / "chats" / CHAT
    runtime = chat / ".runtime"
    config_root = chat / "agentcfg"
    for d in (runtime / "agent", config_root / "config", config_root / "state"):
        d.mkdir(parents=True, exist_ok=True)
    spec = SandboxSpec(
        chat_id=CHAT,
        folder=tree,
        uid=ensure(WS),
        net_uid=ensure(CHAT),
        vcpu=1,
        memory_mb=1024,
        home="/home/alkera",
        mode="gvisor",
        cgroup=cap.cgroup,
        binds=chat_binds(runtime_dir=runtime, agent_config_root=config_root, envs_dir=envs),
        private_dirs=(chat,),
        bundle=chat / "runsc",
        rootfs=Path(cap.rootfs),
        overlay_dir=chat / ".overlay",
        python_home=cap.python_home,
        resolvers=cap.resolvers,
        agent_env={"ALKERA_SERVER_PASSWORD": SECRET, "ALKERA_CONFIG_CONTENT": SECRET},
        **box_tools(cap),
    )
    argv = [f"{cap.python_home}/bin/python3", "-m", "http.server", str(AGENT_PORT), "-d", "/tmp"]
    launch = GvisorRuntime().compose_launch(spec, argv)
    run_steps(launch.before)
    old = os.umask(0o022)
    log = open(chat / "runsc.log", "ab")
    proc = subprocess.Popen(list(launch.command), stdout=log, stderr=log, start_new_session=True)
    os.umask(old)
    deadline = time.monotonic() + 30
    assert launch.network is not None
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(f"http://{launch.network.container_ip}:{AGENT_PORT}/", timeout=1)
            break
        except OSError:
            time.sleep(0.1)
    return spec, launch, proc


@pytest.fixture(scope="module")
def box() -> Iterator[Box]:
    from alkera_cli.harness.sandbox import SandboxSettings
    from alkera_cli.harness.sandbox_steps import run_steps
    from alkera_cli.harness.sandbox_uid import ensure_chat_uid
    from alkera_cli.harness.sandbox_uid_ledger import ENV_UID_LEDGER
    from alkera_cli.notebooks.engine_host import (
        NotebookEngineHost,
        NotebookHostSettings,
        WorkspaceTenancy,
        stage_platform_mount,
    )
    from alkera_cli.notebooks.kernel_mount import kernel_sources

    if IN_WORKER == "inner":
        # What an org worker does before it serves: its own /run, sysfs and
        # cgroup view, its link to the host, the chat firewall in its netns.
        from alkera_cli.cloud.org_namespace import OrgNetwork, prepare

        net = OrgNetwork(
            worker_ip=os.environ["ALKERA_ORG_WORKER_IP"], host_ip=os.environ["ALKERA_ORG_HOST_IP"]
        )
        prepare(net, pid=os.getpid())
    shutil.rmtree(ROOT, ignore_errors=True)
    ROOT.mkdir(parents=True)
    os.environ[ENV_UID_LEDGER] = str(ROOT / "uids.json")
    cap = _capability()
    tree = ROOT / "workspaces" / "ws" / "files"
    envs = ROOT / "org" / "envs" / WS.removeprefix("ws:")
    tree.mkdir(parents=True)
    envs.mkdir(parents=True)
    # The kernel's platform files as a box finds them (the public package too).
    sources = kernel_sources()
    mount = stage_platform_mount(sources.boot, sources.package, sources.public, ROOT / "py")
    settings = NotebookHostSettings(
        sandbox=SandboxSettings(mode="gvisor"),
        capability=cap,
        platform_mount=mount,
        memory_mb=LIMIT_MB,
        vcpu=2,
    )
    host = NotebookEngineHost(
        org_root=ROOT / "org", settings=settings, ensure_uid=ensure_chat_uid, engine_factory=_engine
    )
    parts = host.parts(WorkspaceTenancy(key=WS, folder=tree, envs_dir=envs))
    spec, launch, proc = _start_agent(tree, envs, cap, ensure_chat_uid)
    venv = envs / "nb"
    built = asyncio.run(
        parts.runner.run(
            ["uv", "venv", "--python", f"{cap.python_home}/bin/python3", str(venv)],
            cwd=str(tree),
            env={"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(tree)},
            timeout_s=120,
        )
    )
    assert built.returncode == 0, built.stderr
    assert launch.network is not None
    box = Box(
        tree,
        envs,
        str(venv / "bin" / "python"),
        host,
        parts,
        launch,
        proc,
        launch.network.container_ip,
        spec,
    )
    try:
        yield box
    finally:
        parts.sandbox.stop()
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        run_steps(launch.after_exit)
        shutil.rmtree(ROOT, ignore_errors=True)


def _engine(parts: Any) -> Any:
    return None  # the rig builds its engines per test (each with its own interpreter)


@pytest.fixture
async def engine(box: Box) -> AsyncIterator[Any]:
    from alkera_notebook.document.file_store import FileDocumentStore
    from alkera_notebook.engine import EngineConfig, NotebookEngine, SequentialIds, SystemClock
    from alkera_notebook.engine.config import MemoryPolicy
    from alkera_notebook.envs.static import StaticEnvRegistry
    from alkera_notebook.sql.provider import SqlProviderRegistry

    paths = box.parts.engine_paths
    config = EngineConfig(
        workspace_root=paths["workspace_root"],
        env_root=paths["env_root"],
        data_root=paths["data_root"],
        kernel_mount=paths["kernel_mount"],
        interrupt_escalation_s=(3.0, 7.0),
        base_env={"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(box.tree), "LANG": "C.UTF-8"},
        memory=MemoryPolicy(interval_s=0.1),
    )
    eng = NotebookEngine(
        config,
        store=FileDocumentStore(str(box.tree)),
        launcher=box.parts.launcher,
        transport=box.parts.transport,
        memory=box.parts.memory,
        sql=SqlProviderRegistry([]),
        envs=StaticEnvRegistry(interpreter=box.interpreter),
        clock=SystemClock(),
        ids=SequentialIds(),
    )
    try:
        yield eng
    finally:
        await eng.close()


async def _notebook(engine: Any, name: str, *cells: str) -> tuple[Any, list[str]]:
    from alkera_notebook.document.ops import InsertCell
    from alkera_notebook.engine import Actor

    ann = Actor(kind="person", id="u-ann", display_name="Ann", can_edit=True, can_run=True)
    session = await engine.create(
        f"nb/{name}.alknb.py", [InsertCell(source=c) for c in cells], {}, ann
    )
    client = session.attach(ann)
    view = await client.read()
    return client, [c.id for c in view.cells]


async def _run(client: Any, *ids: str, timeout_s: float = 60) -> Any:
    from alkera_notebook.engine import CellsTarget

    handle = await client.run(CellsTarget(ids=list(ids)))
    return await handle.wait(timeout_s)


async def _text(client: Any, cid: str) -> str:
    return str((await client.output(cid, "text")).text).strip()


# --- the cases ----------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_kernel_runs_as_its_own_uid_with_only_the_allowlist(box: Box, engine: Any) -> None:
    client, (cid,) = await _notebook(
        engine,
        "who",
        "import json, os\n"
        "mask = os.umask(0); os.umask(mask)\n"
        "caps = [l for l in open('/proc/self/status') if l.startswith('CapEff')][0].split()[1]\n"
        "try:\n"
        "    open('/proc/self/environ', 'rb').read(); dumpable = True\n"
        "except OSError:\n"
        "    dumpable = False\n"
        "print(json.dumps({'uid': os.getuid(), 'gid': os.getgid(), 'env': sorted(os.environ),"
        " 'umask': mask, 'caps': caps, 'cwd': os.getcwd(), 'dumpable': dumpable}))",
    )
    record = await _run(client, cid)
    assert record.status == "ok", record
    seen = json.loads(await _text(client, cid))
    sandbox = box.parts.sandbox
    assert seen["uid"] in {k.uid for k in sandbox.slot_identities()}
    assert seen["gid"] == sandbox.gid
    assert seen["umask"] == 0o002
    assert int(seen["caps"], 16) == 0
    assert seen["cwd"] == "/home/alkera/nb"
    # boot.py made the kernel undumpable (its /proc entries are root's), and
    # the runtime itself adds only MPLBACKEND to what the launch gave it.
    assert seen["dumpable"] is False
    assert set(seen["env"]) - {"MPLBACKEND"} <= {
        "PATH",
        "HOME",
        "LANG",
        "TZ",
        "VIRTUAL_ENV",
        "CONDA_PREFIX",
        "PYTHONHASHSEED",
        "ALKERA_RPC_ENDPOINT",
        "ALKERA_NOTEBOOK_DIR",
        "ALKERA_KERNEL_ID",
    }


@pytest.mark.asyncio
async def test_kernel_and_agent_write_each_others_files(box: Box, engine: Any) -> None:
    client, (cid,) = await _notebook(
        engine,
        "write",
        "import os\n"
        "os.makedirs('/home/alkera/from_kernel', exist_ok=True)\n"
        "open('/home/alkera/from_kernel/k.txt', 'w').write('kernel\\n')\n"
        "print(oct(os.stat('/home/alkera/from_kernel/k.txt').st_mode & 0o777))",
    )
    assert (await _run(client, cid)).status == "ok"
    assert await _text(client, cid) == "0o664"
    # The agent appends to the kernel's file and creates in its directory.
    box.agent_run("/bin/sh", "-c", "echo agent >> from_kernel/k.txt && echo a > from_kernel/a.txt")
    assert (box.tree / "from_kernel" / "k.txt").read_text() == "kernel\nagent\n"
    # The agent's own directory and file; then the kernel writes into them.
    box.agent_run("/bin/sh", "-c", "mkdir -p from_agent && echo agent > from_agent/f.txt")
    client2, (cid2,) = await _notebook(
        engine,
        "write2",
        "open('/home/alkera/from_agent/f.txt', 'a').write('kernel\\n')\n"
        "open('/home/alkera/from_agent/k2.txt', 'w').write('kernel\\n')\n"
        "print('ok')",
    )
    assert (await _run(client2, cid2)).status == "ok"
    assert (box.tree / "from_agent" / "f.txt").read_text() == "agent\nkernel\n"
    assert (box.tree / "from_agent" / "k2.txt").stat().st_gid == box.parts.sandbox.gid


@pytest.mark.asyncio
async def test_a_kernel_sees_nothing_of_the_agent(box: Box, engine: Any) -> None:
    client, (cid,) = await _notebook(
        engine,
        "peek",
        "import os\n"
        "found, cmds = [], []\n"
        "for pid in os.listdir('/proc'):\n"
        "    if not pid.isdigit(): continue\n"
        "    try: cmds.append(open(f'/proc/{pid}/cmdline','rb').read())\n"
        "    except OSError: pass\n"
        "    try: found.append(open(f'/proc/{pid}/environ','rb').read())\n"
        f"    except OSError: pass\n"
        f"print(any(b'{SECRET}' in e for e in found), any(b'http.server' in c for c in cmds))",
    )
    assert (await _run(client, cid)).status == "ok"
    assert await _text(client, cid) == "False False"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "code",
    [
        pytest.param("import time\ntime.sleep(60)", id="sleep"),
        pytest.param(
            "import socket\ns = socket.socket(); s.bind(('127.0.0.1', 0)); s.listen(1)\ns.accept()",
            id="socket-read",
        ),
        pytest.param("import subprocess\nsubprocess.run(['sleep', '60'])", id="child-process"),
    ],
)
async def test_an_interrupt_lands_within_a_second(box: Box, engine: Any, code: str) -> None:
    from alkera_notebook.engine import CellsTarget

    client, (cid,) = await _notebook(engine, f"int{abs(hash(code)) % 1000}", code)
    handle = await client.run(CellsTarget(ids=[cid]))
    deadline = time.monotonic() + 30
    while (await client.read()).cells[0].status != "running":
        assert time.monotonic() < deadline
        await asyncio.sleep(0.05)
    await asyncio.sleep(0.3)
    sent = time.monotonic()
    await client.kernel("interrupt")
    record = await handle.wait(10)
    took = time.monotonic() - sent
    assert record.status == "interrupted", record
    assert took < 1.0, took


@pytest.mark.asyncio
async def test_a_memory_burst_kills_the_kernel_and_never_the_agent(box: Box, engine: Any) -> None:
    from alkera_notebook.engine import CellsTarget

    answers: list[bool] = []
    stop = threading.Event()

    def ping() -> None:
        while not stop.is_set():
            answers.append(box.agent_answers())
            time.sleep(0.2)

    pinger = threading.Thread(target=ping)
    pinger.start()
    client, (cid,) = await _notebook(
        engine,
        "burst",
        "import threading\n"
        "held = []\n"
        "def fill():\n"
        "    while True:\n"
        "        b = bytearray(64 << 20)\n"
        "        b[::4096] = b'x' * len(b[::4096])\n"
        "        held.append(b)\n"
        "ts = [threading.Thread(target=fill) for _ in range(4)]\n"
        "[t.start() for t in ts]\n"
        "[t.join() for t in ts]",
    )
    exited: list[Any] = []

    async def watch() -> None:
        async for event in client.events:
            if getattr(event, "type", "") == "kernel.exited":
                exited.append(event)
                return

    watcher = asyncio.create_task(watch())
    handle = await client.run(CellsTarget(ids=[cid]))
    await asyncio.wait_for(watcher, 90)
    await asyncio.wait_for(handle.wait(30), 40)
    await asyncio.sleep(1)
    stop.set()
    await asyncio.to_thread(pinger.join)
    print(  # the run's account, for the rig's record
        f"\nkernel.exited={exited[0] if exited else None} guard_kills={engine.guard_kills} "
        f"oom_events={box.parts.memory.oom_events()} agent_answers={len(answers)}"
    )
    assert exited and exited[0].reason == "out_of_memory", exited
    assert answers and all(answers), answers
    assert box.agent_proc.poll() is None


def test_uv_pip_install_works_as_a_kernel_uid(box: Box) -> None:
    venv = box.envs / "nb"
    out = box.kernel_run(
        "uv", "pip", "install", "--python", "/opt/alkera/envs/nb/bin/python", "tabulate"
    )
    assert "tabulate" in out, out
    assert (venv / "lib").exists()
    imported = box.kernel_run(
        "/opt/alkera/envs/nb/bin/python", "-c", "import tabulate; print('ok')"
    )
    assert imported.strip().endswith("ok"), imported


@pytest.mark.xfail(
    IN_WORKER == "inner",
    strict=True,
    reason=(
        "under an org worker's user namespace the staged rootfs is owned by host root, "
        "which the namespace does not map, so dpkg cannot copy up its lock in the overlay"
    ),
)
def test_a_system_package_is_the_kernels_alone_and_gone_after_sleep(box: Box) -> None:
    from alkera_cli.notebooks.system_install import SystemInstaller

    result = SystemInstaller(box.parts.sandbox).install("apt", ["tree"])
    assert result.ok, result.output
    assert "/usr/bin/tree" in box.kernel_run("/bin/sh", "-c", "command -v tree")
    agent = box.agent_run("/bin/sh", "-c", "command -v tree || echo absent", check=False)
    assert "absent" in agent.stdout
    asyncio.run(box.host.put_away(WS))  # the workspace sleeps
    box.parts.sandbox.ensure_running()
    assert "/usr/bin/tree" not in box.kernel_run("/bin/sh", "-c", "command -v tree || echo absent")


def test_only_the_kernels_own_sockets_are_reachable(box: Box) -> None:
    box.parts.sandbox.ensure_running()
    out = box.kernel_run(
        "/bin/sh", "-c", "find / \\( -path /proc -o -path /sys \\) -prune -o -type s -print"
    )
    sockets = [line for line in out.splitlines() if line.startswith("/")]
    from alkera_cli.notebooks.kernel_sandbox import RUN_MOUNT

    assert all(s.startswith(f"{RUN_MOUNT}/") for s in sockets), sockets


def test_the_sandbox_was_bounded_and_named_as_composed(box: Box) -> None:
    box.parts.sandbox.ensure_running()
    cgroup = box.parts.sandbox.cgroup_dir()
    assert cgroup is not None
    assert (cgroup / "memory.max").read_text().strip() == str(LIMIT_MB << 20)
    assert (cgroup / "memory.swap.max").read_text().strip() == "0"
    assert (cgroup / "memory.oom.group").read_text().strip() == "1"
    assert (cgroup / "memory.high").read_text().strip() == "max"
    state = subprocess.run(
        ["runsc", f"--root={box.parts.sandbox.target.root}", "list"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert box.parts.sandbox.target.container in state


def test_replacing_the_limit_is_refused_on_a_none_box(box: Box) -> None:
    from alkera_cli.harness.sandbox import SandboxRefusedError, SandboxSettings
    from alkera_cli.notebooks.kernel_sandbox import KernelSandbox

    none_box = KernelSandbox(
        box.parts.sandbox.config,
        settings=replace(SandboxSettings(), mode="none"),
        cap=_capability(),
        ensure_uid=lambda name: 20000,
    )
    with pytest.raises(SandboxRefusedError, match="only in a gVisor sandbox"):
        none_box.plan_launch()


@pytest.mark.skipif(IN_WORKER == "inner", reason="this case starts the worker")
def test_every_case_passes_under_the_org_workers_user_namespace() -> None:
    from alkera_cli.org_root import ensure_org_root
    from alkera_cli.supervisor.launch import (
        RESET_SHELL,
        WorkerLaunch,
        cgroup_steps,
        network_steps,
        spawn_argv,
    )
    from alkera_cli.supervisor.slots import Slot

    slot = Slot(index=SLOT, org_id="nbcnt-rig")
    shutil.rmtree(ORGS_ROOT, ignore_errors=True)
    org_root = ensure_org_root(ORGS_ROOT, slot)
    for sub in ("home", "tmp"):
        (org_root.path / sub).mkdir(mode=0o700)
        os.chown(org_root.path / sub, slot.uid_base, slot.uid_base)
    module = Path(__file__).resolve()
    argv = (
        sys.executable,
        "-m",
        "pytest",
        "--noconftest",
        "-p",
        "no:cacheprovider",
        "-o",
        "addopts=",
        f"--rootdir={module.parents[3]}",
        "-q",
        str(module),
    )
    launch = WorkerLaunch(slot=slot, org_root=org_root.path, argv=argv)
    env = {
        "PATH": os.environ.get("ALKERA_NBCNT_WORKER_PATH", os.environ.get("PATH", "/usr/bin:/bin")),
        "HOME": str(org_root.path / "home"),
        "TMPDIR": str(org_root.path / "tmp"),
        "ALKERA_NBCNT_BOX": "1",
        "ALKERA_NBCNT_IN_WORKER": "inner",
        "ALKERA_NBCNT_BOX_ROOT": str(org_root.path / "rig"),
        "ALKERA_ORG_WORKER_IP": slot.worker_ip,
        "ALKERA_ORG_HOST_IP": slot.host_ip,
        "ALKERA_SANDBOX_TREES_FLOOR": str(org_root.path),
    }
    for step in cgroup_steps(launch):
        assert subprocess.run(list(step), check=False).returncode == 0, step
    worker = subprocess.Popen(
        list(spawn_argv(launch)), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
    )
    try:
        mine = os.stat("/proc/self/ns/net").st_ino
        deadline = time.monotonic() + 30
        while os.stat(f"/proc/{worker.pid}/ns/net").st_ino == mine:
            assert time.monotonic() < deadline, "the worker never made its network namespace"
            time.sleep(0.05)
        first, *rest = network_steps(launch, worker.pid)
        subprocess.run(list(first), capture_output=True, check=False)
        for step in rest:
            assert subprocess.run(list(step), check=False).returncode == 0, step
        out, _ = worker.communicate(timeout=900)
    finally:
        if worker.poll() is None:
            worker.kill()
            worker.wait()
        subprocess.run(["ip", "link", "delete", slot.host_if], capture_output=True, check=False)
        # The org's cgroup goes with its worker, as the supervisor's reset
        # leaves it empty: every process killed, every child removed.
        subprocess.run(
            ["/bin/sh", "-c", RESET_SHELL, "reset", str(launch.cgroup)],
            capture_output=True,
            check=False,
        )
        with contextlib.suppress(OSError):
            launch.cgroup.rmdir()
        if os.environ.get("ALKERA_NBCNT_KEEP") != "1":
            shutil.rmtree(ORGS_ROOT, ignore_errors=True)
    text = out.decode("utf-8", "replace")
    print(text[-3000:])
    assert worker.returncode == 0, text[-3000:]
    import re

    assert re.search(r"\b11 passed\b", text) and not re.search(r"\b\d+ failed\b", text), text
