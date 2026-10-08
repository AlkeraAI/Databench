"""The spawn seam: what every child inherits, and how a tree is ended.

A child gets ``/dev/null`` for stdin, no descriptor it was not handed, a
session of its own and a tie to its parent's lifetime unless its spec says
otherwise. The tests that need a real child run one; the per-platform
branches of the argument list are pinned by monkeypatching ``sys.platform``,
and the Windows Job Object runs for real only on Windows.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest
from alkera_core import process
from alkera_core.process import (
    SpawnSpec,
    SpawnSpecError,
    kill_tree,
    popen_args,
    process_alive,
    run,
    run_async,
    spawn,
)

needs_posix = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX sessions, signals and socketpairs on fd 0"
)
windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Objects")

PY = sys.executable


def _env() -> dict[str, str]:
    return dict(os.environ)


# -- what a child inherits ---------------------------------------------------------


@needs_posix
def test_spawn_default_stdin_is_eof(tmp_path: Path) -> None:
    """A child that reads stdin while its parent's fd 0 is an open socket (an
    org worker's control channel) reads end-of-file at once instead of
    consuming the parent's channel. Run in a fresh interpreter so this test's
    own fd 0 is never touched."""
    script = textwrap.dedent(
        f"""
        import json, os, socket, sys
        sys.path[:0] = {sys.path!r}
        from alkera_core.process import SpawnSpec, run
        ours, theirs = socket.socketpair()
        os.dup2(ours.fileno(), 0)
        theirs.sendall(b"a control frame the worker must read itself")
        child = [sys.executable, "-c", "import sys; print(repr(sys.stdin.buffer.read()))"]
        done = run(SpawnSpec(argv=child, env=dict(os.environ), stdout="pipe"), timeout=10)
        theirs.close()
        left = os.read(0, 100)
        print(json.dumps({{"child_read": done.stdout.decode().strip(), "left": left.decode()}}))
        """
    )
    done = subprocess.run(
        [PY, "-c", script], capture_output=True, text=True, timeout=60, check=False
    )
    assert done.returncode == 0, done.stderr
    seen = json.loads(done.stdout.strip().splitlines()[-1])
    assert seen == {
        "child_read": "b''",
        "left": "a control frame the worker must read itself",
    }


@needs_posix
def test_a_child_sees_no_descriptor_it_was_not_handed() -> None:
    read_end, write_end = os.pipe()
    os.set_inheritable(write_end, True)
    try:
        probe = f"import os; os.fstat({write_end})"
        done = run(SpawnSpec(argv=[PY, "-c", probe], env=_env(), stderr="pipe"), timeout=30)
        assert done.returncode != 0
        assert b"Bad file descriptor" in done.stderr
    finally:
        os.close(read_end)
        os.close(write_end)


@needs_posix
def test_a_descriptor_handed_over_with_a_reason_reaches_the_child() -> None:
    read_end, write_end = os.pipe()
    os.write(write_end, b"from the parent")
    os.close(write_end)
    try:
        spec = SpawnSpec(
            argv=[PY, "-c", "import sys; sys.stdout.write(sys.stdin.read())"],
            env=_env(),
            stdin=read_end,
            stdout="pipe",
            pass_fds_reason="the test hands the child its input",
        )
        assert run(spec, timeout=30).stdout == b"from the parent"
    finally:
        os.close(read_end)


@pytest.mark.parametrize(
    "spec_args",
    [
        pytest.param({"stdin": 5}, id="an-fd-for-stdin"),
        pytest.param({"pass_fds": (7,)}, id="pass-fds"),
        pytest.param({"pass_fds": (7,), "pass_fds_reason": "  "}, id="a-blank-reason"),
    ],
)
def test_a_descriptor_handed_over_without_a_reason_is_refused(spec_args: dict[str, object]) -> None:
    with pytest.raises(SpawnSpecError):
        SpawnSpec(argv=["true"], env={}, **spec_args)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "argv", [pytest.param("ls -l", id="a-shell-string"), pytest.param([], id="empty")]
)
def test_a_command_that_is_not_an_argument_list_is_refused(argv: object) -> None:
    with pytest.raises(SpawnSpecError):
        SpawnSpec(argv=argv, env={})  # type: ignore[arg-type]


def test_the_child_sees_exactly_the_environment_it_was_given() -> None:
    probe = "import json, os; print(json.dumps(sorted(os.environ)))"
    env = {"ONLY_THIS": "1"}
    if sys.platform == "win32":
        # A Windows interpreter cannot start without its system root.
        env["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
    done = run(SpawnSpec(argv=[PY, "-c", probe], env=env, stdout="pipe"), timeout=30)
    # What the platform's own loader adds to every process (macOS).
    added = {"__CF_USER_TEXT_ENCODING", "LC_CTYPE"}
    assert set(json.loads(done.stdout)) - added == set(env)


@needs_posix
def test_a_child_gets_a_session_of_its_own_unless_it_asks_not_to() -> None:
    probe = "import os; print(os.getpid() == os.getsid(0))"
    own = run(SpawnSpec(argv=[PY, "-c", probe], env=_env(), stdout="pipe"), timeout=30)
    shared = run(
        SpawnSpec(argv=[PY, "-c", probe], env=_env(), stdout="pipe", new_session=False),
        timeout=30,
    )
    assert (own.stdout.strip(), shared.stdout.strip()) == (b"True", b"False")


def test_input_needs_a_pipe() -> None:
    with pytest.raises(SpawnSpecError):
        run(SpawnSpec(argv=[PY, "-c", "pass"], env=_env()), timeout=10, input=b"x")


def test_input_reaches_a_piped_child() -> None:
    spec = SpawnSpec(
        argv=[PY, "-c", "import sys; sys.stdout.write(sys.stdin.read().upper())"],
        env=_env(),
        stdin="pipe",
        stdout="pipe",
    )
    assert run(spec, timeout=30, input=b"abc").stdout == b"ABC"


# -- the argument list each platform runs ------------------------------------------


def test_linux_binds_the_child_through_the_launcher_and_no_pre_exec_hook(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(process, "pdeathsig_launcher", lambda: "/usr/bin/setpriv")
    argv, kwargs = popen_args(SpawnSpec(argv=["/opt/agent", "serve"], env={}))
    assert argv == ["/usr/bin/setpriv", "--pdeathsig", "KILL", "--", "/opt/agent", "serve"]
    assert kwargs["start_new_session"] is True
    assert "preexec_fn" not in kwargs
    assert (kwargs["close_fds"], kwargs["stdin"]) == (True, subprocess.DEVNULL)


def test_linux_without_a_launcher_binds_in_the_child(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(process, "pdeathsig_launcher", lambda: "")
    argv, kwargs = popen_args(SpawnSpec(argv=["/opt/agent"], env={}))
    assert argv == ["/opt/agent"]
    assert callable(kwargs["preexec_fn"])


def test_a_child_not_bound_to_its_parent_has_no_launcher_or_hook(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(process, "pdeathsig_launcher", lambda: "/usr/bin/setpriv")
    argv, kwargs = popen_args(SpawnSpec(argv=["/opt/agent"], env={}, die_with_parent=False))
    assert argv == ["/opt/agent"]
    assert "preexec_fn" not in kwargs


def test_macos_detaches_and_has_no_death_signal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    argv, kwargs = popen_args(SpawnSpec(argv=["/opt/agent"], env={}))
    assert argv == ["/opt/agent"]
    assert kwargs["start_new_session"] is True
    assert "preexec_fn" not in kwargs and "creationflags" not in kwargs


def test_windows_puts_the_child_in_a_process_group_of_its_own(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    _, kwargs = popen_args(SpawnSpec(argv=["agent.exe"], env={}))
    # CREATE_NEW_PROCESS_GROUP == 0x00000200 (the stable Win32 value).
    assert kwargs["creationflags"] == 0x00000200
    assert "start_new_session" not in kwargs and "preexec_fn" not in kwargs
    _, shared = popen_args(SpawnSpec(argv=["agent.exe"], env={}, new_session=False))
    assert "creationflags" not in shared


# -- a command behind the launcher is still the caller's command -----------------------


@pytest.fixture
def stand_in_launcher(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """The Linux launcher path on any POSIX host: a ``setpriv`` stand-in that
    drops its flags and execs the command, as the real one does."""
    launcher = tmp_path / "bin" / "setpriv"
    launcher.parent.mkdir()
    launcher.write_text('#!/bin/sh\nshift 3\nexec "$@"\n', encoding="utf-8")
    launcher.chmod(0o755)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(process, "pdeathsig_launcher", lambda: str(launcher))
    return launcher


@needs_posix
def test_a_command_behind_the_launcher_runs_and_reads_as_the_callers(
    stand_in_launcher: Path,
) -> None:
    spec = SpawnSpec(argv=[PY, "-c", "print('ran')"], env=_env(), stdout="pipe")
    argv, _ = popen_args(spec)
    assert argv[0] == str(stand_in_launcher)
    assert process.unlaunched(argv) == [PY, "-c", "print('ran')"]
    assert run(spec, timeout=30).stdout.strip() == b"ran"


@pytest.mark.parametrize(
    "argv",
    [
        pytest.param(["/opt/agent", "serve"], id="no-launcher"),
        pytest.param(["/usr/bin/env", "--pdeathsig", "KILL", "--", "x"], id="another-program"),
        pytest.param(["/usr/bin/setpriv", "--pdeathsig", "KILL", "--"], id="no-command"),
    ],
)
def test_unlaunched_leaves_a_command_without_the_launcher_alone(argv: list[str]) -> None:
    assert process.unlaunched(argv) == argv


@needs_posix
@pytest.mark.parametrize(
    "program",
    [
        pytest.param("{tmp}/missing-dbt", id="absolute"),
        pytest.param("missing-dbt", id="on-the-path"),
        pytest.param("./missing-dbt", id="relative-to-its-directory"),
    ],
)
def test_a_missing_program_behind_the_launcher_names_the_callers_command(
    stand_in_launcher: Path, tmp_path: Path, program: str
) -> None:
    """The launcher itself is there, so without the check the OS would start
    it and the caller would read the launcher's own failure instead."""
    named = program.format(tmp=tmp_path)
    spec = SpawnSpec(argv=[named], env={"PATH": str(tmp_path)}, cwd=tmp_path)
    with pytest.raises(FileNotFoundError) as raised:
        spawn(spec)
    assert raised.value.filename == named


@needs_posix
async def test_a_missing_program_behind_the_launcher_names_it_on_the_event_loop(
    stand_in_launcher: Path, tmp_path: Path
) -> None:
    missing = str(tmp_path / "missing-dbt")
    with pytest.raises(FileNotFoundError) as raised:
        await process.spawn_async(SpawnSpec(argv=[missing], env={}))
    assert raised.value.filename == missing


@pytest.mark.skipif(sys.platform != "linux", reason="PR_SET_PDEATHSIG is Linux only")
def test_the_launcher_really_sets_the_death_signal_on_a_child() -> None:
    if not process.pdeathsig_launcher():
        pytest.skip("no setpriv with --pdeathsig on this host")
    code = (
        "import ctypes; v = ctypes.c_int(0); "
        "ctypes.CDLL(None, use_errno=True).prctl(2, ctypes.byref(v)); print(v.value)"
    )
    done = run(SpawnSpec(argv=[PY, "-c", code], env=_env(), stdout="pipe"), timeout=30)
    assert done.stdout.strip() == b"9"


@pytest.mark.parametrize(
    ("help_text", "expected"),
    [
        pytest.param(
            b"Usage: setpriv [options] <program>\n --pdeathsig keep|clear|<signal>\n",
            "/x/setpriv",
            id="capable",
        ),
        pytest.param(b"Usage: setpriv [options] <program>\n --nnp\n", "", id="too-old"),
    ],
)
def test_the_launcher_is_only_a_setpriv_that_knows_the_flag(
    monkeypatch: pytest.MonkeyPatch, help_text: bytes, expected: str
) -> None:
    class _Probe:
        stdout = help_text
        stderr = b""

    monkeypatch.setattr(process, "_PDEATHSIG_LAUNCHER", None)
    monkeypatch.setattr(
        process.shutil, "which", lambda name: "/x/setpriv" if name == "setpriv" else None
    )
    monkeypatch.setattr(process.subprocess, "run", lambda *a, **k: _Probe())
    assert process.pdeathsig_launcher() == expected


def test_no_setpriv_is_no_launcher(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(process, "_PDEATHSIG_LAUNCHER", None)
    monkeypatch.setattr(process.shutil, "which", lambda name: None)
    assert process.pdeathsig_launcher() == ""


# -- ending a tree ------------------------------------------------------------------

_PARENT_OF_A_SLEEPER = textwrap.dedent(
    """
    import subprocess, sys, time
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    print(child.pid, flush=True)
    time.sleep(120)
    """
)


@needs_posix
def test_kill_tree_ends_the_child_and_what_it_started() -> None:
    proc = spawn(SpawnSpec(argv=[PY, "-c", _PARENT_OF_A_SLEEPER], env=_env(), stdout="pipe"))
    assert proc.stdout is not None
    grandchild = int(proc.stdout.readline())
    kill_tree(proc, grace=2)
    assert proc.returncode is not None
    deadline = time.monotonic() + 10
    while process_alive(grandchild) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not process_alive(grandchild)


@needs_posix
def test_a_run_past_its_time_ends_the_whole_tree(tmp_path: Path) -> None:
    marker = tmp_path / "grandchild"
    script = _PARENT_OF_A_SLEEPER.replace(
        "print(child.pid, flush=True)", f"open({str(marker)!r}, 'w').write(str(child.pid))"
    )
    with pytest.raises(subprocess.TimeoutExpired):
        run(SpawnSpec(argv=[PY, "-c", script], env=_env()), timeout=3)
    grandchild = int(marker.read_text())
    deadline = time.monotonic() + 10
    while process_alive(grandchild) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not process_alive(grandchild)


async def test_run_async_collects_what_the_child_wrote() -> None:
    spec = SpawnSpec(argv=[PY, "-c", "print('hi')"], env=_env(), stdout="pipe")
    done = await run_async(spec, time_limit=30)
    assert (done.returncode, done.stdout.strip()) == (0, b"hi")


async def test_run_async_past_its_time_raises_and_ends_the_child() -> None:
    spec = SpawnSpec(argv=[PY, "-c", "import time; time.sleep(60)"], env=_env())
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        await run_async(spec, time_limit=1)
    assert time.monotonic() - started < 20


@windows_only
def test_a_bound_child_dies_when_its_job_is_released() -> None:
    proc = spawn(SpawnSpec(argv=[PY, "-c", "import time; time.sleep(60)"], env=_env()))
    try:
        assert process_alive(proc.pid)
        process.release(proc.pid)
        deadline = time.monotonic() + 10
        while process_alive(proc.pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not process_alive(proc.pid)
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)


@windows_only
def test_kill_tree_ends_a_windows_child() -> None:
    proc = spawn(SpawnSpec(argv=[PY, "-c", "import time; time.sleep(60)"], env=_env()))
    kill_tree(proc, grace=1)
    proc.wait(timeout=10)
    assert not process_alive(proc.pid)


# -- what the remaining callers need ------------------------------------------------


def test_run_text_decodes_what_the_child_wrote_and_replaces_what_is_not_utf8() -> None:
    probe = "import sys; sys.stdout.buffer.write(b'caf\\xc3\\xa9 \\xff')"
    done = process.run_text(
        SpawnSpec(argv=[PY, "-c", probe], env=_env(), stdout="pipe", stderr="pipe"), timeout=30
    )
    assert (done.stdout, done.stderr) == ("café �", "")


def test_run_captured_has_the_shape_of_subprocess_run() -> None:
    done = process.run_captured(
        [PY, "-c", "print('ok')"], capture_output=True, text=True, timeout=30, check=False
    )
    assert (done.returncode, done.stdout.strip()) == (0, "ok")


@needs_posix
async def test_a_tree_asked_to_stop_with_no_grace_is_waited_for_and_never_killed(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "stopped"
    script = textwrap.dedent(
        f"""
        import signal, sys, time
        def stop(*_):
            open({str(marker)!r}, "w").write("asked")
            sys.exit(0)
        signal.signal(signal.SIGTERM, stop)
        print("ready", flush=True)
        time.sleep(60)
        """
    )
    proc = await process.spawn_async(SpawnSpec(argv=[PY, "-c", script], env=_env(), stdout="pipe"))
    assert proc.stdout is not None
    await proc.stdout.readline()
    await process.kill_tree_async(proc, grace=None)
    assert (proc.returncode, marker.read_text()) == (0, "asked")


async def test_a_read_limit_bounds_one_line_the_loop_will_buffer() -> None:
    long_line = "import sys; sys.stdout.write('x' * 5000 + '\\n')"
    proc = await process.spawn_async(
        SpawnSpec(argv=[PY, "-c", long_line], env=_env(), stdout="pipe", read_limit=1024)
    )
    assert proc.stdout is not None
    with pytest.raises(ValueError):
        await proc.stdout.readline()
    await proc.wait()


def test_windows_can_start_a_child_with_no_window(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    _, kwargs = popen_args(SpawnSpec(argv=["cmd"], env={}, no_window=True))
    # CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW, the stable Win32 values.
    assert kwargs["creationflags"] == 0x00000200 | 0x08000000


@needs_posix
def test_replace_process_runs_the_new_program_in_the_same_pid() -> None:
    script = (
        "import os, sys; from alkera_core.process import replace_process; "
        "print(os.getpid(), flush=True); "
        "replace_process([sys.executable, '-c', 'import os; print(os.getpid())'])"
    )
    done = run(SpawnSpec(argv=[PY, "-c", script], env=_env(), stdout="pipe"), timeout=30)
    before, after = done.stdout.split()
    assert before == after


def _square(n: int) -> int:
    return n * n


def test_a_process_pool_runs_work_in_other_processes() -> None:
    with process.process_pool(max_workers=2) as pool:
        assert list(pool.map(_square, [1, 2, 3])) == [1, 4, 9]
