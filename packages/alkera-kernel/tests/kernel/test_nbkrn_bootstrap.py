"""Bootstrap: the token is read before site runs, sys.path[0] is the
notebook's folder, the kernel cannot be displaced by an installed package,
and only allowlisted variables reach it."""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import pytest
from alkera_notebook.kernels.launch_local import (
    LaunchSpec,
    LocalSubprocessLauncher,
    build_env,
    group_pids,
)
from alkera_notebook.rpc import frames
from nbkrn_harness import KernelFactory, step


def _venv(tmp_path: Path, name: str) -> Path:
    root = tmp_path / name
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(root)], check=True)
    return root


def _site_packages(venv: Path) -> Path:
    (sp,) = list((venv / "lib").glob("python*/site-packages"))
    return sp


async def test_nbkrn_pth_files_run_after_the_token_is_held(
    start_kernel: KernelFactory, tmp_path: Path
) -> None:
    venv = _venv(tmp_path, "env")
    sp = _site_packages(venv)
    stolen = tmp_path / "stolen.txt"
    # A .pth line starting with "import" is executed by site.
    (sp / "steal.pth").write_text(
        "import sys, os; "
        f"open({str(stolen)!r}, 'w')"
        ".write(repr(sys.stdin.read() if sys.stdin else None) + '|' + repr(os.read(0, 100)))\n"
    )
    ks = await start_kernel(interpreter=str(venv / "bin" / "python"))
    assert stolen.exists(), "site.main() never ran the environment's .pth file"
    assert stolen.read_text() == "''|b''"
    result = await ks.run(step("a", "import sys\nsys.prefix"))
    assert result.outputs("a")[0]["text/plain"] == repr(str(venv))


async def test_nbkrn_notebook_folder_is_sys_path_zero(
    start_kernel: KernelFactory, tmp_path: Path
) -> None:
    nb = tmp_path / "notebooks"
    nb.mkdir()
    (nb / "helpers.py").write_text("ANSWER = 42\n")
    ks = await start_kernel(notebook_dir=nb)
    result = await ks.run(
        step("a", "import sys, os, helpers\n(sys.path[0], helpers.ANSWER, os.getcwd())")
    )
    assert result.outputs("a")[0]["text/plain"] == repr((str(nb), 42, str(nb.resolve())))


async def test_nbkrn_kernel_is_not_importable_from_the_environment(
    start_kernel: KernelFactory,
) -> None:
    ks = await start_kernel()
    result = await ks.run(
        step(
            "a", "import importlib.util\nimportlib.util.find_spec('_alkera_kernel_v0_0_0') is None"
        )
    )
    assert result.status == "ok"


async def test_nbkrn_an_environment_pinning_an_old_alkera_still_runs(
    start_kernel: KernelFactory, tmp_path: Path
) -> None:
    venv = _venv(tmp_path, "pinned")
    sp = _site_packages(venv)
    old = sp / "alkera"
    old.mkdir()
    (old / "__init__.py").write_text("__version__ = '0.0.1-old'\n")
    (sp / "_alkera_kernel").mkdir()
    (sp / "_alkera_kernel" / "__init__.py").write_text(
        "raise RuntimeError('the installed copy must never load')\n"
    )
    ks = await start_kernel(interpreter=str(venv / "bin" / "python"))
    result = await ks.run(step("a", "import alkera\nalkera.__version__"))
    assert result.status == "ok", result.events
    assert result.outputs("a")[0]["text/plain"] == "'0.0.1-old'"


async def test_nbkrn_public_alkera_comes_from_the_mount_when_not_installed(
    start_kernel: KernelFactory, tmp_path: Path
) -> None:
    venv = _venv(tmp_path, "bare")
    ks = await start_kernel(interpreter=str(venv / "bin" / "python"))
    result = await ks.run(step("a", "import alkera\nalkera.__file__"))
    assert result.status == "ok", result.events
    assert "/public/alkera/" in result.outputs("a")[0]["text/plain"]


async def test_nbkrn_only_allowlisted_variables_reach_the_kernel(
    start_kernel: KernelFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WORKSPACE_SECRET", "s3cr3t")
    monkeypatch.setenv("PYTHONPATH", "/nowhere")
    ks = await start_kernel()
    result = await ks.run(step("a", "import os\nsorted(os.environ)"))
    names = eval(result.outputs("a")[0]["text/plain"])
    assert "WORKSPACE_SECRET" not in names and "PYTHONPATH" not in names
    assert set(names) <= {
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
        "MPLBACKEND",
        "__CF_USER_TEXT_ENCODING",
        "LC_CTYPE",
    }
    seed = await ks.run(step("b", "import sys\nsys.flags.hash_randomization"))
    assert seed.outputs("b")[0]["text/plain"] == "0"


@pytest.mark.parametrize(
    "env",
    [
        pytest.param({"WORKSPACE_SECRET": "x"}, id="secret"),
        pytest.param({"PYTHONPATH": "/x"}, id="pythonpath"),
        pytest.param({"LD_PRELOAD": "/x.so"}, id="ld-preload"),
    ],
)
def test_nbkrn_launcher_refuses_variables_outside_the_allowlist(
    tmp_path: Path, env: dict[str, str]
) -> None:
    spec = LaunchSpec(sys.executable, tmp_path, "k", "unix:/tmp/x", "t", tmp_path, env)
    with pytest.raises(ValueError, match="allowlist"):
        build_env(spec)


def test_nbkrn_launcher_sets_the_alkera_variables(tmp_path: Path) -> None:
    spec = LaunchSpec(
        sys.executable, tmp_path, "k9", "unix:/tmp/s", "t", tmp_path, {"PATH": "/bin"}
    )
    assert build_env(spec) == {
        "PATH": "/bin",
        "PYTHONHASHSEED": "0",
        "ALKERA_RPC_ENDPOINT": "unix:/tmp/s",
        "ALKERA_NOTEBOOK_DIR": str(tmp_path),
        "ALKERA_KERNEL_ID": "k9",
    }


async def test_nbkrn_launcher_measures_and_kills_the_process_group(
    start_kernel: KernelFactory,
) -> None:
    ks = await start_kernel()
    base = ks.kernel.rss_bytes()
    assert base > 5 * 1024 * 1024
    await ks.start_run(
        [
            step(
                "a",
                "import subprocess\nkids = [subprocess.Popen(['sleep', '60']) for _ in range(3)]"
                "\nblob = bytearray(200 * 1024 * 1024)\nfor i in range(0, len(blob), 4096):\n    "
                "blob[i] = 1\n",
            )
        ]
    )
    await ks.wait_for(lambda: any(m == "cell.finished" for m, _ in ks.events))
    assert len(ks.kernel.group_pids()) >= 4
    assert ks.kernel.rss_bytes() > base + 150 * 1024 * 1024
    pids = ks.kernel.group_pids()
    ks.kernel.kill()
    assert await ks.kernel.wait() == -9
    import asyncio

    for _ in range(100):
        if not group_pids(ks.kernel.pgid):
            break
        await asyncio.sleep(0.02)
    assert group_pids(ks.kernel.pgid) == [], pids


async def test_nbkrn_kill_all_ends_every_kernel(kernel_mount: Path, tmp_path: Path) -> None:
    import asyncio

    launcher = LocalSubprocessLauncher()
    kernels = []
    for i in range(2):
        spec = LaunchSpec(
            sys.executable,
            tmp_path,
            f"k{i}",
            "unix:/tmp/none",
            "t" * 43,
            kernel_mount,
            {"PATH": os.environ["PATH"]},
        )
        kernels.append(launcher.launch(spec))
    launcher.kill_all()
    codes = [await asyncio.wait_for(k.wait(), 5) for k in kernels]
    assert all(c != 0 for c in codes)


def test_nbkrn_launcher_needs_a_mount_with_boot_py(tmp_path: Path) -> None:
    spec = LaunchSpec(sys.executable, tmp_path, "k", "unix:/tmp/x", "t", tmp_path / "empty", {})
    with pytest.raises(FileNotFoundError, match=r"boot\.py"):
        LocalSubprocessLauncher().launch(spec)


async def test_nbkrn_kernel_files_are_group_writable(
    start_kernel: KernelFactory, tmp_path: Path
) -> None:
    ks = await start_kernel()
    await ks.run(step("a", "open('made_by_kernel.txt', 'w').write('x')"))
    mode = (tmp_path / "nb" / "made_by_kernel.txt").stat().st_mode & 0o777
    assert mode & 0o020, oct(mode)


def test_nbkrn_kernel_handles_a_request_sent_with_the_hello_answer(
    kernel_mount: Path, tmp_path: Path
) -> None:
    """The service may write its first request in the same packet as the
    hello answer; the kernel must not drop it."""
    sock_dir = Path(os.path.realpath(__import__("tempfile").mkdtemp(prefix="alknb-", dir="/tmp")))
    path = sock_dir / "k.sock"
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(str(path))
    listener.listen(1)
    spec = LaunchSpec(
        sys.executable,
        tmp_path,
        "k",
        f"unix:{path}",
        "tok",
        kernel_mount,
        {"PATH": os.environ["PATH"]},
    )
    kernel = LocalSubprocessLauncher().launch(spec)
    try:
        listener.settimeout(20)
        conn, _ = listener.accept()
        conn.settimeout(20)
        reader = frames.FrameReader(limit=frames.FRAME_LIMIT_CLIENT)
        got: list[frames.Message] = []
        while not got:
            got += [frames.parse_message(fr) for fr in reader.feed(conn.recv(65536))]
        hello = got[0]
        assert isinstance(hello, frames.Request) and hello.method == "hello"
        assert hello.params["token"] == "tok"
        both = frames.encode_message(
            frames.result_response(hello.id, {"protocol": 1, "methods": []}),
            limit=frames.FRAME_LIMIT_SERVICE,
        )
        both += frames.encode_message(
            frames.request(1, "names.delete", {"names": []}), limit=frames.FRAME_LIMIT_SERVICE
        )
        conn.sendall(both)
        answers: list[frames.Message] = []
        while not answers:
            answers += [frames.parse_message(fr) for fr in reader.feed(conn.recv(65536))]
        assert answers[0] == frames.Response(1, {})
        conn.close()
    finally:
        kernel.kill()
        listener.close()
        shutil.rmtree(sock_dir, ignore_errors=True)


async def test_nbkrn_connects_through_an_endpoint_longer_than_sun_path(
    kernel_mount: Path, tmp_path: Path
) -> None:
    """The socket's path is over 104 bytes: the kernel connects by relative
    name from inside its directory and is back in the notebook's folder
    before any cell runs."""
    import asyncio

    from alkera_notebook.rpc import MethodRegistry, RpcService, UnixEndpoint, new_token

    endpoint = UnixEndpoint.create()
    long_dir = tmp_path / ("d" * 60) / ("e" * 60)
    long_dir.mkdir(parents=True)
    (long_dir / "k.sock").symlink_to(endpoint.path)
    long_path = str(long_dir / "k.sock")
    assert len(long_path.encode()) > 104
    nb = tmp_path / "nb"
    nb.mkdir()
    token = new_token()
    events: list[tuple[str, dict]] = []

    async def on_event(method: str, params: dict) -> None:
        events.append((method, params))

    service = RpcService(endpoint, token=token, registry=MethodRegistry(), on_notification=on_event)
    await service.start()
    kernel = LocalSubprocessLauncher().launch(
        LaunchSpec(
            sys.executable,
            nb,
            "k",
            f"unix:{long_path}",
            token,
            kernel_mount,
            {"PATH": os.environ["PATH"]},
        )
    )
    try:
        session = await asyncio.wait_for(service.accept(), 20)
        service.scope.begin("r1")
        await session.peer.request(
            "run.execute", {"run_id": "r1", "steps": [step("a", "import os\nos.getcwd()")]}
        )
        for _ in range(200):
            if any(m == "run.finished" for m, _ in events):
                break
            await asyncio.sleep(0.05)
        (output,) = [p for m, p in events if m == "cell.output"]
        assert output["output"]["text/plain"] == repr(str(nb.resolve()))
    finally:
        kernel.kill()
        await service.close()
        endpoint.remove()
