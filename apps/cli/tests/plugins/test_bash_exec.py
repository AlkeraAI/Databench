"""The parent-hosted shell executor (``bash_exec``).

Pure helpers (``tail`` / ``build_child_env``) are exhausted in
isolation; the spawn / stream / truncate / timeout / process-group-kill behaviors
run a REAL subprocess (the repo's "prefer real collaborators" rule) since that's
exactly the surface a mock can't prove.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from pathlib import Path

import pytest
from alkera_cli.files.chat_fs import ChatTree
from alkera_cli.plugins.plugin_base.agent_tree import SpillTarget
from alkera_cli.plugins.plugin_base.bash_exec import (
    ExecLimits,
    build_child_env,
    run_command,
    select_shell,
    tail,
)
from alkera_core.process import process_alive

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX-only shell executor")


def _spill_factory(tmp_path: Path):
    counter = {"n": 0}
    tree = ChatTree(tmp_path)

    def _make() -> SpillTarget:
        counter["n"] += 1
        return SpillTarget(tree, f"spill-{counter['n']}.txt")

    return _make


def _pid_in(path: Path) -> int | None:
    """The pid the command wrote, or ``None`` while the file is absent or the
    shell has truncated it but not yet written the number."""
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return None


async def _until(predicate: Callable[[], bool], *, what: str, seconds: float = 10.0) -> None:
    """Wait for ``predicate``, or fail naming what never happened.

    The ceiling is a stop, not a budget: the events waited on here take
    milliseconds when the mechanism works, so ten seconds only decides how long
    a broken one is given before it is reported.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    pytest.fail(f"{what} did not happen within {seconds:g}s")


async def _run(command: str, tmp_path: Path, **kw: object):
    return await run_command(
        command,
        cwd=str(tmp_path),
        env=build_child_env(dict(os.environ), cwd=str(tmp_path)),
        shell=select_shell(),
        make_spill=_spill_factory(tmp_path),
        **kw,  # type: ignore[arg-type]
    )


# --------------------------------------------------------------------------- #
# tail() — UTF-8-safe last-N-lines/bytes.
# --------------------------------------------------------------------------- #


def test_tail_under_limits_is_unchanged() -> None:
    text = "a\nb\nc"
    assert tail(text, 2000, 50_000) == (text, False)


def test_tail_over_max_lines_keeps_the_last_n() -> None:
    text = "\n".join(str(i) for i in range(10))
    out, cut = tail(text, 3, 50_000)
    assert cut is True
    assert out == "7\n8\n9"


def test_tail_over_max_bytes_keeps_the_last_bytes() -> None:
    text = "\n".join(["xxxx"] * 100)  # many short lines
    out, cut = tail(text, 2000, 10)
    assert cut is True
    assert len(out.encode()) <= 10
    assert out.endswith("xxxx")


def test_tail_single_huge_line_aligns_to_utf8_boundary() -> None:
    # A single line longer than the byte budget, full of 2-byte codepoints (é =
    # 0xC3 0xA9). The cut must never split a codepoint → the result decodes cleanly
    # and every char is 'é'.
    line = "é" * 100  # 200 bytes
    out, cut = tail(line, 2000, 15)
    assert cut is True
    assert len(out.encode()) <= 15
    assert set(out) <= {"é"}  # no replacement char, no split codepoint


# --------------------------------------------------------------------------- #
# build_child_env() — the security scrub.
# --------------------------------------------------------------------------- #


def test_env_scrubs_injected_secrets_but_keeps_user_config() -> None:
    parent = {
        "PATH": "/usr/bin",
        "HOME": "/home/u",
        "ALKERA_SERVER_PASSWORD": "hunter2",
        "ALKERA_PERMISSION": "{}",
        "ALKERA_DISABLE_SHARE": "true",
        "ANTHROPIC_API_KEY": "sk-leak",
        "ALKERA_API_URL": "https://api.alkera.dev",  # user-facing config — KEEP
        "ALKERA_HOME": "/home/u/.alkera",  # KEEP
        "GITHUB_TOKEN": "ghp_users_own",  # the user's own secret — KEEP (it's their shell)
    }
    out = build_child_env(parent, cwd="/work")
    # Scrubbed:
    assert "ALKERA_SERVER_PASSWORD" not in out
    assert "ALKERA_PERMISSION" not in out
    assert "ALKERA_DISABLE_SHARE" not in out
    assert "ANTHROPIC_API_KEY" not in out
    # Kept:
    assert out["ALKERA_API_URL"] == "https://api.alkera.dev"
    assert out["ALKERA_HOME"] == "/home/u/.alkera"
    assert out["GITHUB_TOKEN"] == "ghp_users_own"
    assert out["HOME"] == "/home/u"
    assert out["PWD"] == "/work"  # set to the resolved cwd


# --------------------------------------------------------------------------- #
# run_command() — real subprocess.
# --------------------------------------------------------------------------- #


async def test_run_echo_returns_output_and_zero_exit(tmp_path: Path) -> None:
    res = await _run("echo hello", tmp_path)
    # The trailing newline is preserved (faithful to the raw shell output, like
    # OpenCode — neither strips it).
    assert res.output == "hello\n"
    assert res.exit_code == 0
    assert res.timed_out is False
    assert res.truncated is False
    assert res.output_path is None


async def test_run_merges_stdout_and_stderr(tmp_path: Path) -> None:
    res = await _run("echo out; echo err 1>&2", tmp_path)
    assert "out" in res.output
    assert "err" in res.output  # stderr is merged into the captured stream


async def test_run_propagates_nonzero_exit_code(tmp_path: Path) -> None:
    res = await _run("exit 3", tmp_path)
    assert res.exit_code == 3


async def test_run_no_output_is_marked(tmp_path: Path) -> None:
    res = await _run("true", tmp_path)
    assert res.output == "(no output)"
    assert res.exit_code == 0


async def test_run_uses_the_given_cwd(tmp_path: Path) -> None:
    res = await _run("pwd", tmp_path)
    assert res.output.rstrip("\n") == str(tmp_path)


async def test_run_times_out_and_kills(tmp_path: Path) -> None:
    res = await _run("sleep 30", tmp_path, timeout_ms=150)
    assert res.timed_out is True
    assert res.exit_code is None
    assert "<shell_metadata>" in res.output
    assert "timeout 150 ms" in res.output


async def test_timeout_kills_the_whole_process_group(tmp_path: Path) -> None:
    # The command backgrounds a child sleep and prints its pid, then hangs. On
    # timeout we SIGTERM the GROUP — so the backgrounded grandchild dies too (not
    # just the shell). This is the core "kill the whole tree" safety property.
    res = await _run("sleep 30 & echo $!; sleep 30", tmp_path, timeout_ms=200)
    child_pid = int(res.output.strip().splitlines()[0])
    await _until(
        lambda: not process_alive(child_pid),
        what="the backgrounded child dies with the group the timeout killed",
    )


async def test_run_truncates_and_spills_full_output(tmp_path: Path) -> None:
    # Tiny limits so a cheap command overflows. The model sees the tail + a banner;
    # the FULL output is on disk.
    limits = ExecLimits(max_lines=3, max_bytes=40, keep_bytes=80)
    res = await _run("for i in $(seq 1 20); do echo line-$i; done", tmp_path, limits=limits)
    assert res.truncated is True
    assert res.output_path is not None
    assert "...output truncated..." in res.output
    assert "Full output saved to:" in res.output
    # The tail shows the LAST lines, not the first.
    assert "line-20" in res.output
    assert "line-1\n" not in res.output
    # The spill file holds the COMPLETE output.
    full = await asyncio.to_thread(Path(res.output_path).read_text)
    assert "line-1\n" in full
    assert "line-20" in full


async def test_cancellation_kills_the_process_and_propagates(tmp_path: Path) -> None:
    # A cancelled turn must take the whole GROUP with it. The result never
    # returns on this path, so the command writes its backgrounded child's pid
    # to a file instead of stdout and the case watches that pid directly:
    # without the group kill the `sleep 30` outlives the test, on every worker,
    # on every run.
    pidfile = tmp_path / "child.pid"
    factory = _spill_factory(tmp_path)
    started = f"sleep 30 & echo $! > {pidfile}; sleep 30"
    coro = run_command(
        started,
        cwd=str(tmp_path),
        env=build_child_env(dict(os.environ), cwd=str(tmp_path)),
        shell=select_shell(),
        make_spill=factory,
        timeout_ms=30_000,
    )
    task = asyncio.ensure_future(coro)
    await _until(lambda: _pid_in(pidfile) is not None, what="the command records its child's pid")
    child = _pid_in(pidfile)
    assert child is not None
    assert process_alive(child), "the backgrounded child is running before the cancel"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await _until(
        lambda: not process_alive(child),
        what="the backgrounded child dies with the group the cancel killed",
    )
