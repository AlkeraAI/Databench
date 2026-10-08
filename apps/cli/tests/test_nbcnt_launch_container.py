"""The container launcher, the box transport and the build runner: the exact
command a kernel starts with, what it is given, and how it is signalled,
measured, waited for and handed its socket, against a stand-in runsc."""

from __future__ import annotations

import asyncio
import os
import signal
import socket
import stat
import sys
from pathlib import Path

import pytest
from _nbcnt_fakes import Rig, make_rig, process_alive, wait_for
from alkera_cli.harness.sandbox import CONTAINER_ENV, SandboxRefusedError, umask_prefix
from alkera_cli.notebooks import launch_container as lc
from alkera_cli.notebooks import transport_box as tb
from alkera_cli.notebooks.kernel_sandbox import KERNEL_MOUNT, KernelIdentity
from alkera_notebook.kernels.launch_local import LaunchSpec, kernel_data_dir

TOKEN = "dG9rZW4tdG9rZW4tdG9rZW4tdG9rZW4tdG9rZW4tdG9rZW4"


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Rig:
    return make_rig(tmp_path, monkeypatch)


def spec_for(rig: Rig, endpoint: str, **env: str) -> LaunchSpec:
    config = rig.config
    venv = config.envs_dir / "alkera"
    base = {
        "PATH": f"{venv / 'bin'}:/usr/bin:/bin:/host/only/bin",
        "HOME": str(config.folder),
        "LANG": "C.UTF-8",
        "VIRTUAL_ENV": str(venv),
        "PYTHONHASHSEED": "0",
    }
    base.update(env)
    return LaunchSpec(
        interpreter=str(venv / "bin" / "python"),
        notebook_dir=config.folder / "nb",
        kernel_id="krn_0001",
        endpoint=endpoint,
        token=TOKEN,
        mount=config.platform_mount,
        env=base,
        # Where the engine makes the kernel's data directory.
        data_dir=kernel_data_dir(config.runtime_dir, "krn_0001"),
    )


IDENTITY = KernelIdentity(uid=20100, gid=20001, slot=0)


# --- the command -------------------------------------------------------------------------


def test_a_kernel_starts_with_exactly_the_contract_command(rig: Rig) -> None:
    sandbox = rig.sandbox()
    socket_path = rig.config.runtime_dir / "sock" / "krn_0001" / "k.sock"
    pid_file = rig.config.state_dir / "pids" / "krn_0001.pid"
    spec = spec_for(rig, f"unix:{socket_path}")
    argv = lc.kernel_argv(sandbox, IDENTITY, spec, pid_file=pid_file)
    assert argv == [
        str(rig.runsc),
        f"--root={rig.config.trees.runsc_root.as_posix()}",
        "exec",
        "--user=20100:20001",
        "--cwd=/home/alkera/nb",
        f"--internal-pid-file={pid_file.as_posix()}",
        rig.config.container,
        CONTAINER_ENV,
        "-i",
        "PATH=/opt/alkera/envs/alkera/bin:/usr/bin:/bin",
        "HOME=/home/alkera",
        "LANG=C.UTF-8",
        "VIRTUAL_ENV=/opt/alkera/envs/alkera",
        "PYTHONHASHSEED=0",
        "ALKERA_RPC_ENDPOINT=unix:/opt/alkera/run/sock/krn_0001/k.sock",
        "ALKERA_NOTEBOOK_DIR=/home/alkera/nb",
        "ALKERA_KERNEL_ID=krn_0001",
        # The bind of the runtime directory, as the kernel sees it.
        "ALKERA_DATA_DIR=/opt/alkera/run/kernels/krn_0001",
        "prlimit",
        f"--nproc={rig.config.kernel_nproc}:{rig.config.kernel_nproc}",
        "--",
        "/bin/sh",
        "-c",
        'umask 002; unset PWD; exec "$@"',
        "alkera-kernel",
        "/opt/alkera/envs/alkera/bin/python",
        "-s",
        "-S",
        "-X",
        "utf8",
        f"{KERNEL_MOUNT}/boot.py",
    ]
    assert not any(a.startswith("--cap") for a in argv)
    assert TOKEN not in " ".join(argv)  # the token goes on stdin, never the argv


@pytest.mark.parametrize(
    ("change", "error", "match"),
    [
        pytest.param({"env": {"AWS_SECRET_ACCESS_KEY": "x"}}, ValueError, "allowlist", id="var"),
        pytest.param(
            {"interpreter": "/usr/bin/python3"}, SandboxRefusedError, "not in", id="interp"
        ),
        pytest.param({"notebook_dir": Path("/srv/x")}, SandboxRefusedError, "not in", id="cwd"),
        pytest.param({"mount": Path("/opt/other")}, SandboxRefusedError, "platform", id="mount"),
        pytest.param({"endpoint": "tcp:1.2.3.4:5"}, ValueError, "unix", id="endpoint"),
        pytest.param(
            {"data_dir": Path("/srv/data/krn_0001")}, SandboxRefusedError, "not in", id="data"
        ),
    ],
)
def test_a_launch_outside_what_the_sandbox_holds_is_refused(
    rig: Rig, change: dict[str, object], error: type[Exception], match: str
) -> None:
    sandbox = rig.sandbox()
    base = spec_for(rig, f"unix:{rig.config.runtime_dir}/sock/krn_0001/k.sock")
    values = {f: getattr(base, f) for f in base.__dataclass_fields__}
    if "env" in change:
        values["env"] = {**base.env, **change.pop("env")}  # type: ignore[dict-item]
    values.update(change)
    with pytest.raises(error, match=match):
        lc.kernel_argv(sandbox, IDENTITY, LaunchSpec(**values), pid_file=Path("/x"))  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("code", "status"),
    [
        pytest.param(0, 0, id="clean"),
        pytest.param(3, 3, id="its-own-code"),
        pytest.param(137, -9, id="sigkill-as-runsc-reports"),
        pytest.param(130, -2, id="sigint"),
        pytest.param(-9, -9, id="client-killed"),
        pytest.param(255, 255, id="not-a-signal"),
    ],
)
def test_exit_statuses_name_the_signal(code: int, status: int) -> None:
    assert lc.exit_status(code) == status


# --- the transport -----------------------------------------------------------------------


def test_each_kernel_gets_a_fresh_private_socket_directory(rig: Rig) -> None:
    transport = tb.BoxKernelTransport(rig.config.runtime_dir)
    stale = rig.config.runtime_dir / "sock" / "krn_0001"
    stale.mkdir(parents=True)
    (stale / "k.sock").write_text("left over")
    endpoint = transport.endpoint("krn_0001")
    assert endpoint.path == stale / "k.sock"
    assert endpoint.uri == f"unix:{stale / 'k.sock'}"
    assert not endpoint.path.exists()  # the engine binds it
    assert stat.S_IMODE(endpoint.directory.stat().st_mode) == 0o700
    with pytest.raises(ValueError, match="refusing kernel id"):
        transport.endpoint("../krn")


def test_a_socket_path_too_long_for_the_host_is_refused(tmp_path: Path) -> None:
    transport = tb.BoxKernelTransport(tmp_path / ("d" * 120))
    with pytest.raises(ValueError, match="too long"):
        transport.endpoint("krn_0001")


def test_hand_over_makes_the_socket_and_data_the_kernels_alone(rig: Rig) -> None:
    endpoint = tb.BoxKernelTransport(rig.config.runtime_dir).endpoint("krn_0001")
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(endpoint.path))
    data = rig.config.runtime_dir / "kernels" / "krn_0001"
    data.mkdir(parents=True, mode=0o755)
    owners: dict[Path, tuple[int, int]] = {}
    tb.hand_over(endpoint.path, data, IDENTITY, chown=lambda p, u, g: owners.__setitem__(p, (u, g)))
    listener.close()
    assert owners == {
        endpoint.directory: (20100, 20001),
        data: (20100, 20001),
        endpoint.path: (20100, 20001),
    }
    assert stat.S_IMODE(endpoint.directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(data.stat().st_mode) == 0o700


# --- a kernel's life ------------------------------------------------------------------------


async def launched(rig: Rig) -> tuple[lc.ContainerLauncher, lc.ContainerKernel, Path]:
    sandbox = rig.sandbox(stop_grace=0.2, files_gid=os.getgid())
    endpoint = tb.BoxKernelTransport(rig.config.runtime_dir).endpoint("krn_0001")
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(endpoint.path))
    listener.close()
    (rig.config.runtime_dir / "kernels" / "krn_0001").mkdir(parents=True)
    me = os.getuid(), os.getgid()
    launcher = lc.ContainerLauncher(sandbox, chown=lambda p, u, g: os.chown(p, *me))
    kernel = launcher.launch(spec_for(rig, endpoint.uri))
    pid_file = rig.config.state_dir / "pids" / "krn_0001.pid"
    await asyncio.to_thread(wait_for, pid_file)
    return launcher, kernel, pid_file


@pytest.mark.asyncio
async def test_a_kernel_gets_its_token_on_stdin_and_only_the_allowlist(rig: Rig) -> None:
    launcher, kernel, _ = await launched(rig)
    try:
        pid = await asyncio.to_thread(lambda: kernel.pid)
        assert kernel.pgid == pid
        await asyncio.to_thread(wait_for, rig.state / f"token-{pid}")
        assert (rig.state / f"token-{pid}").read_text() == TOKEN
        env = rig.exec_envs()[-1]
        assert set(env) == {
            "PATH",
            "HOME",
            "LANG",
            "VIRTUAL_ENV",
            "PYTHONHASHSEED",
            "ALKERA_RPC_ENDPOINT",
            "ALKERA_NOTEBOOK_DIR",
            "ALKERA_KERNEL_ID",
            "ALKERA_DATA_DIR",
        }
        assert kernel.identity is not None
        assert launcher.sandbox.kernels() == frozenset({"krn_0001"})
    finally:
        kernel.kill()
        await kernel.wait()
        launcher.sandbox.stop()


@pytest.mark.asyncio
async def test_interrupt_reaches_the_group_and_kill_ends_it_and_frees_the_slot(rig: Rig) -> None:
    launcher, kernel, pid_file = await launched(rig)
    try:
        pid = await asyncio.to_thread(lambda: kernel.pid)
        await asyncio.to_thread(kernel.signal, signal.SIGINT)
        await asyncio.to_thread(wait_for, rig.state / f"signals-{pid}")
        assert (rig.state / f"signals-{pid}").read_text() == "INT\n"
        assert process_alive(pid)  # an interrupt is not an exit
        sent = [c for c in rig.runsc_calls() if "kill" in c and "-INT" in c]
        assert sent and sent[-1][-4:] == ["kill", "-INT", "--", f"-{pid}"]
        assert sent[-1][2] == f"--user={kernel.identity.uid}:{kernel.identity.gid}"  # type: ignore[union-attr]
        await asyncio.to_thread(kernel.kill)
        assert await asyncio.wait_for(kernel.wait(), 10) == -signal.SIGKILL
        assert launcher.sandbox.kernels() == frozenset()
        assert not pid_file.exists()
        assert kernel.rss_bytes() == 0  # nothing left to measure
    finally:
        launcher.sandbox.stop()


@pytest.mark.asyncio
async def test_memory_is_read_inside_as_the_kernels_uid(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NBCNT_RSS", "123456789")
    launcher, kernel, _ = await launched(rig)
    try:
        assert await asyncio.to_thread(kernel.rss_bytes) == 123456789
        call = rig.runsc_calls()[-1]
        assert call[2].startswith("--user=") and lc.GROUP_RSS_SOURCE in call
        monkeypatch.setenv("NBCNT_RSS", "not a number")
        assert await asyncio.to_thread(kernel.rss_bytes) == 0
    finally:
        kernel.kill()
        await kernel.wait()
        launcher.sandbox.stop()


@pytest.mark.asyncio
async def test_a_refused_start_is_an_exit_the_engine_can_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = make_rig(tmp_path, monkeypatch, max_kernels=1)
    sandbox = rig.sandbox(stop_grace=0.2)
    try:
        sandbox.claim("krn_other")
        launcher = lc.ContainerLauncher(sandbox)
        endpoint = tb.BoxKernelTransport(rig.config.runtime_dir).endpoint("krn_0001")
        kernel = launcher.launch(spec_for(rig, endpoint.uri))
        assert await asyncio.wait_for(kernel.wait(), 10) == lc.START_REFUSED_STATUS
        assert "already runs 1 kernels" in kernel.log_tail()
        kernel.kill()  # harmless
        assert sandbox.kernels() == frozenset({"krn_other"})
    finally:
        sandbox.stop()


@pytest.mark.asyncio
async def test_kill_all_goes_through_the_sandboxs_cgroup(rig: Rig) -> None:
    launcher, kernel, _ = await launched(rig)
    cgroup = launcher.sandbox.cgroup_dir()
    assert cgroup is not None
    launcher.kill_all()
    assert (cgroup / "cgroup.kill").read_text() == "1"
    assert not launcher.sandbox.running()
    kernel.kill()
    await asyncio.wait_for(kernel.wait(), 10)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads /proc")
def test_the_group_memory_reader_sums_its_group_and_nothing_else() -> None:
    import subprocess

    child = subprocess.Popen(
        [sys.executable, "-c", "import time; b = bytearray(64 << 20); time.sleep(30)"],
        start_new_session=True,
    )
    try:
        import time

        time.sleep(0.5)
        out = subprocess.run(
            [sys.executable, "-I", "-S", "-c", lc.GROUP_RSS_SOURCE, str(child.pid)],
            capture_output=True,
            text=True,
            check=True,
        )
        assert int(out.stdout) >= 64 << 20
        none = subprocess.run(
            [sys.executable, "-I", "-S", "-c", lc.GROUP_RSS_SOURCE, "999999"],
            capture_output=True,
            text=True,
            check=True,
        )
        assert int(none.stdout) == 0
    finally:
        child.kill()
        child.wait()


# --- environment builds ----------------------------------------------------------------------


def test_a_build_runs_as_the_build_uid_under_the_shared_umask_with_paths_inside(rig: Rig) -> None:
    sandbox = rig.sandbox()
    runner = lc.SandboxCommandRunner(sandbox)
    envs = rig.config.envs_dir
    argv = runner.command(
        ["uv", "venv", "--python", "/opt/alkera/python/current/bin/python3", str(envs / "nb1")],
        cwd=str(rig.config.folder / "nb"),
        env={
            "PATH": "/usr/bin:/elsewhere/bin",
            "UV_CACHE_DIR": str(envs / "cache"),
            "UV_OFFLINE": "1",
        },
    )
    build = sandbox.build_identity()
    assert build.uid not in {k.uid for k in sandbox.slot_identities()} and build.gid == 20001
    at = argv.index(rig.config.container)
    assert f"--user={build.uid}:20001" in argv[:at]
    assert "--cwd=/home/alkera/nb" in argv[:at]
    assert argv[at + 1 :] == [
        CONTAINER_ENV,
        "-i",
        "PATH=/usr/bin",
        "UV_CACHE_DIR=/opt/alkera/envs/cache",
        "UV_OFFLINE=1",
        "/bin/sh",
        "-c",
        umask_prefix()[2],
        "alkera-build",
        "uv",
        "venv",
        "--python",
        "/opt/alkera/python/current/bin/python3",
        "/opt/alkera/envs/nb1",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [0, 7])
async def test_a_build_reports_what_ran_and_its_status(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    monkeypatch.setenv("NBCNT_EXEC_STATUS", str(status))
    sandbox = rig.sandbox(stop_grace=0.2)
    try:
        result = await lc.SandboxCommandRunner(sandbox).run(
            ["uv", "pip", "list"], cwd=str(rig.config.folder), env={}
        )
        assert result.returncode == status
        assert "ran /bin/sh -c" in result.stdout and "uv pip list" in result.stdout
        assert sandbox.running()  # a build starts the sandbox it runs in
    finally:
        sandbox.stop()


@pytest.mark.asyncio
async def test_a_kill_before_the_kernel_exists_lands_when_it_does(rig: Rig) -> None:
    import concurrent.futures

    sandbox = rig.sandbox(stop_grace=0.2, files_gid=os.getgid())
    endpoint = tb.BoxKernelTransport(rig.config.runtime_dir).endpoint("krn_0001")
    (rig.config.runtime_dir / "kernels" / "krn_0001").mkdir(parents=True)
    me = os.getuid(), os.getgid()
    launcher = lc.ContainerLauncher(sandbox, chown=lambda p, u, g: os.chown(p, *me))
    try:
        # The start has not run yet: the kill is remembered, not lost.
        start: concurrent.futures.Future[object] = concurrent.futures.Future()
        pid_file = rig.config.state_dir / "pids" / "krn_0001.pid"
        kernel = lc.ContainerKernel(sandbox, "krn_0001", start, pid_file)  # type: ignore[arg-type]
        kernel.kill()
        assert kernel.rss_bytes() == 0 and kernel.log_tail() == ""
        started = launcher._start(spec_for(rig, endpoint.uri), pid_file)
        start.set_result(started)
        assert await asyncio.wait_for(kernel.wait(), 10) == -signal.SIGKILL
        assert sandbox.kernels() == frozenset()
    finally:
        sandbox.stop()


@pytest.mark.asyncio
async def test_a_kernel_that_dies_before_writing_its_pid_has_none(rig: Rig) -> None:
    import concurrent.futures
    import subprocess

    sandbox = rig.sandbox()

    def exited() -> subprocess.Popen[bytes]:
        done = subprocess.Popen([sys.executable, "-c", "pass"], stdin=subprocess.PIPE)
        done.wait()
        return done

    proc = await asyncio.to_thread(exited)
    start: concurrent.futures.Future[object] = concurrent.futures.Future()
    start.set_result(lc._Started(IDENTITY, proc))
    kernel = lc.ContainerKernel(sandbox, "krn_0001", start, rig.root / "never.pid")  # type: ignore[arg-type]
    with pytest.raises(ProcessLookupError, match="has no pid"):
        _ = kernel.pid
    kernel.signal(signal.SIGINT)  # an exited kernel is not signalled
    assert await kernel.wait() == 0


@pytest.mark.asyncio
async def test_a_build_that_overruns_is_killed_in_the_sandbox(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NBCNT_EXEC_SLEEP", "3")
    sandbox = rig.sandbox(stop_grace=0.2)
    try:
        result = await lc.SandboxCommandRunner(sandbox).run(
            ["uv", "sync"], cwd=str(rig.config.folder), env={}, timeout_s=0.3
        )
        assert result.timed_out and "timed out after 0.3 s" in result.stderr
        build = sandbox.build_identity()
        kills = [c for c in rig.runsc_calls() if c[-4:] == ["kill", "-KILL", "--", "-1"]]
        assert any(c[2] == f"--user={build.uid}:{build.gid}" for c in kills)
        with pytest.raises(ValueError, match="empty"):
            await lc.SandboxCommandRunner(sandbox).run([], cwd=str(rig.config.folder))
    finally:
        sandbox.stop()
