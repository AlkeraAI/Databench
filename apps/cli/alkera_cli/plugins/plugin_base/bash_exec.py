"""The parent-hosted shell executor — a faithful port of OpenCode's ``shell.ts``
``run()``, in Python asyncio.

We run the user's command in the PARENT ``alkera`` process (which already holds the
user's true environment), so the vendored ``ALKERA_SHELL_ENV_RESTORE`` reverse-diff
is unnecessary; the env work inverts to a defensive SCRUB of alkera/gateway-injected
keys (:data:`_ENV_SCRUB`) — never a blanket ``ALKERA_*`` sweep, so the user's own
``ALKERA_API_URL`` / toolchain vars survive.

Faithful behaviors ported (each pinned by a test):
- spawn ``shell -c command`` in its OWN process group (``start_new_session``) so a
  timeout/cancel can kill the whole tree, with stdin closed and stdout+stderr merged;
- a rolling byte-bounded window (``KEEP_BYTES``) for the final tail + a separate
  spill of the COMPLETE output to a file once it exceeds ``MAX_BYTES``;
- the UTF-8-safe last-N-lines/bytes :func:`tail` (incl. the single-huge-line case);
- the drain runs CONCURRENTLY with the exit/timeout race (a sequential
  drain-then-wait deadlocks on a command that hangs without output);
- timeout (+100ms) → SIGTERM the group → SIGKILL after a grace; the exact
  ``...output truncated... Full output saved to: <file>`` banner, ``(no output)``,
  and the ``<shell_metadata>`` footer.

Cancellation (a backgrounded command's ``background_cancel`` / chat close) arrives as
``CancelledError``: the group is killed and the error re-raised so the registry
records the job ``cancelled``.
"""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import io
import logging
import math
import os
import shutil
import sys
import tempfile
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from alkera_core.process import SpawnSpec, kill_tree_async, spawn_async

from alkera_cli.harness.agent_root import agent_config_root
from alkera_cli.harness.sandbox import (
    HOST_ONLY_ENV,
    NO_SANDBOX,
    SandboxLaunch,
    SandboxRefusedError,
    SandboxSettings,
    SandboxSpec,
    chat_binds,
    ensure_chat_uid,
    python_ro_binds,
    run_steps,
    select_runtime,
    shell_spec,
)
from alkera_cli.harness.sandbox_env import session_default_env, session_envs_dir
from alkera_cli.harness.sandbox_probe import current_capability
from alkera_cli.harness.sandbox_processes import RunscLister, kill_tree, run_probe
from alkera_cli.harness.sandbox_scope import scope_of
from alkera_cli.harness.sandbox_uid import member_identity
from alkera_cli.harness.session_launches import cached_launch, remember_launch
from alkera_cli.plugins.plugin_base.agent_tree import SpillFactory, SpillTarget
from alkera_cli.plugins.plugin_base.bash_ids import (
    DEFAULT_TIMEOUT_MS,
    FORCE_KILL_SECONDS,
    KEEP_BYTES,
    MAX_BYTES,
    MAX_LINES,
    STUCK_CHECK_SECONDS,
    STUCK_SECONDS,
)
from alkera_cli.plugins.plugin_base.bash_progress import (
    CpuReading,
    container_tree_cpu,
    host_tree_cpu,
)
from alkera_cli.plugins.plugin_base.tool import ToolError

logger = logging.getLogger(__name__)

#: Shells OpenCode refuses (``META.deny``) — we fall back rather than run in them.
_UNACCEPTABLE_SHELLS = frozenset({"fish", "nu"})

#: Alkera/gateway-injected env keys scrubbed from the child command's environment.
#: These are set into the opencode CHILD process (see ``opencode_http._build_env``),
#: never the parent — so this is DEFENSE IN DEPTH (it removes nothing today) that
#: fails safe if a future path ever leaks one into the parent. NOT a blanket
#: ``ALKERA_*`` sweep: the user's own ``ALKERA_API_URL`` / ``ALKERA_HOME`` /
#: ``ALKERA_OPENCODE_BIN`` etc. are legitimate and survive.
_ENV_SCRUB: frozenset[str] = frozenset(
    {
        "ALKERA_SERVER_PASSWORD",
        "ALKERA_LISTEN_FILE",
        "ALKERA_PERMISSION",
        "ALKERA_CONFIG_CONTENT",
        "ALKERA_AUTH_CONTENT",
        "ALKERA_SHELL_ENV_RESTORE",
        "ALKERA_TEST_HOME",
        "ALKERA_PURE",
        "ALKERA_DISABLE_PROJECT_CONFIG",
        "OPENCODE_CONFIG",
        "OPENCODE_CONFIG_DIR",
        "OPENCODE_CONFIG_CONTENT",
        "OPENCODE_AUTH_CONTENT",
        "OPENCODE_DB",
        # Gateway/provider creds (defensive — the gateway injects these into the
        # opencode child, not the parent; if one ever appears here it'd reroute a
        # user command through the gateway or leak a token).
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_MODEL",
        "OPENAI_API_KEY",
    }
)


@dataclass(frozen=True, slots=True)
class ExecLimits:
    #: ``None`` on either threshold means that half never cuts; the rolling
    #: window (``keep_bytes``) still bounds memory either way.
    max_lines: int | None = MAX_LINES
    max_bytes: int | None = MAX_BYTES
    keep_bytes: int = KEEP_BYTES


@dataclass(frozen=True, slots=True)
class ExecResult:
    """The outcome of one command run — mirrors OpenCode's shell tool result."""

    output: str
    """The model-visible text: the tailed output + the truncation banner +
    ``<shell_metadata>`` footer (``(no output)`` when empty)."""
    exit_code: int | None
    """The process exit code, or ``None`` if it timed out (was killed)."""
    truncated: bool
    output_path: str | None
    """Path to the FULL spilled output when truncated, else ``None``."""
    timed_out: bool
    stuck: bool = False
    """Ended because it made no progress for the stuck bound, not because a
    wall clock ran out (``timed_out`` is set too: the command was killed)."""


def tail(text: str, max_lines: int | None, max_bytes: int | None) -> tuple[str, bool]:
    """The UTF-8-safe last-``max_lines``-lines / last-``max_bytes``-bytes of ``text``.

    Returns ``(tailed_text, was_cut)``. Ported from OpenCode's ``tail`` — including
    the single-line-longer-than-``max_bytes`` case, which takes the last
    ``max_bytes`` bytes aligned to a UTF-8 boundary (never splitting a codepoint)."""
    if max_lines is None and max_bytes is None:
        return text, False
    line_cap = math.inf if max_lines is None else max_lines
    byte_cap = math.inf if max_bytes is None else max_bytes
    lines = text.split("\n")
    if len(lines) <= line_cap and len(text.encode("utf-8")) <= byte_cap:
        return text, False
    out: list[str] = []
    total = 0
    i = len(lines) - 1
    while i >= 0 and len(out) < line_cap:
        # +1 byte for the newline that will rejoin this line to the one after it.
        size = len(lines[i].encode("utf-8")) + (1 if out else 0)
        if total + size > byte_cap:
            if not out:
                # A single line longer than the whole budget: keep its last
                # max_bytes bytes, advancing past any UTF-8 continuation bytes
                # (0b10xxxxxx) so we never start mid-codepoint.
                buf = lines[i].encode("utf-8")
                start = 0 if max_bytes is None else max(0, len(buf) - max_bytes)
                while start < len(buf) and (buf[start] & 0xC0) == 0x80:
                    start += 1
                out.insert(0, buf[start:].decode("utf-8", errors="replace"))
            break
        out.insert(0, lines[i])
        total += size
        i -= 1
    return "\n".join(out), True


def select_shell() -> str:
    """Pick the shell to run commands in — ``$SHELL`` when acceptable, else the
    platform fallback (darwin ``/bin/zsh`` → ``bash`` → ``/bin/sh``), mirroring
    OpenCode's ``Shell.acceptable`` resolution order."""
    candidate = os.environ.get("SHELL", "")
    name = os.path.basename(candidate).lower()
    # Require EXECUTABLE (not merely present), like every other shell resolver in the
    # repo — a non-executable $SHELL falls back to bash/sh instead of failing to spawn.
    if candidate and name not in _UNACCEPTABLE_SHELLS and os.access(candidate, os.X_OK):
        return candidate
    if sys.platform == "darwin" and os.access("/bin/zsh", os.X_OK):
        return "/bin/zsh"
    found = shutil.which("bash")
    if found:
        return found
    return "/bin/sh"


def build_child_env(
    parent_env: Mapping[str, str], *, cwd: str, home: str | None = None
) -> dict[str, str]:
    """The environment for a user command: the parent's true env minus the
    defensive :data:`_ENV_SCRUB` denylist (and the ``ALKERA_DISABLE_*`` lockdown
    prefix), with ``PWD`` set to the resolved working directory. ``HOME`` and the
    user's own vars (incl. their ``GITHUB_TOKEN`` / ``AWS_*`` — it IS their shell)
    pass through unchanged.

    ``home`` makes it a BOUNDED session's environment instead: the box's, not
    the user's, so it is built from an ALLOWLIST (:func:`agent_env.bounded_shell_env`:
    the path, the locale, the terminal) rather than by removing known secrets — a
    box can hold a secret under a name no table lists. Nothing that describes the box
    survives either (:data:`HOST_ONLY_ENV`: its hostname, the daemon's own
    directories); ``HOME`` / ``TMPDIR`` point under the chat's own working directory,
    so a command that writes "somewhere" writes there.
    """
    out = {
        k: v
        for k, v in parent_env.items()
        if k not in _ENV_SCRUB and not k.startswith("ALKERA_DISABLE_")
    }
    if home is not None:
        from alkera_cli.plugins.plugin_base.agent_env import bounded_shell_env

        out = {k: v for k, v in bounded_shell_env(out).items() if k not in HOST_ONLY_ENV}
        out["HOME"] = home
        out["TMPDIR"] = home
    out["PWD"] = cwd
    return out


async def _kill_process_group(proc: asyncio.subprocess.Process, *, grace: float | None) -> None:
    """End the command's process tree: TERM, then KILL after ``grace`` (TERM
    alone with a ``grace`` of ``None``). The seam spawned it in a session of its
    own, so its process group is the tree, and the pid cannot be recycled while
    we hold an un-reaped handle."""
    await kill_tree_async(proc, grace=grace)


def _exec_pid(pid_file: Path) -> int | None:
    """The pid ``runsc exec`` wrote for the command inside its container, or
    ``None`` when it wrote none yet (the command was ended before it started)
    or the file does not read as one. The file is the daemon's own, in its
    temp directory, never a name in the chat's tree."""
    try:
        return int(pid_file.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return None


def _kill_inside(sandbox: SandboxLaunch, pid_file: Path, *, grace: float | None) -> None:
    """End the command's tree inside its container (sync; run off the loop).

    Under gVisor the command's processes live in the agent server's container,
    children of a ``runsc exec`` the host side can only abandon: killing the
    client here ends nothing in there, and a timed-out ``sh`` with a ``sleep``
    under it would stay in the container's table, counted as the chat's work
    and holding it awake. Every process below the command's own pid gets
    ``TERM``, then ``KILL`` after ``grace`` (never, like the host-side kill,
    when the grace is ``None``); the agent server (the container's PID 1) is
    never below it."""
    if sandbox.container is None or sandbox.runsc is None or sandbox.runsc_root is None:
        return
    pid = _exec_pid(pid_file)
    if pid is None or pid <= 1:
        return
    lister = RunscLister(
        runsc=sandbox.runsc,
        root=sandbox.runsc_root,
        container=sandbox.container,
        uid=sandbox.uid if sandbox.uid is not None else 0,
    )
    left = kill_tree(lister, pid, grace=grace)
    if left:
        logger.warning(
            "%d process(es) of a killed command survived in %s: %s",
            len(left),
            sandbox.container,
            " ".join(str(p) for p in left),
        )


async def _end_command(
    proc: asyncio.subprocess.Process,
    *,
    sandbox: SandboxLaunch | None,
    pid_file: Path | None,
    grace: float | None,
) -> None:
    """End a command and everything it started: inside its container first,
    where the processes are, then the host-side process group."""
    if sandbox is not None and pid_file is not None:
        await asyncio.to_thread(_kill_inside, sandbox, pid_file, grace=grace)
    await _kill_process_group(proc, grace=grace)


def _spill_refused(path: Path, exc: OSError) -> ToolError:
    return ToolError(f"cannot keep the output in {path.name!r}: {exc.strerror or exc}")


def _open_append(stack: contextlib.ExitStack, target: SpillTarget) -> TextIO:
    """Open a spill file for append (sync; run off the loop via ``to_thread``).

    The target opens through its chat tree: neither the directory nor the name
    is followed as a link, only a plain file is written, and the file is the
    chat's. Entered on ``stack``, which the command closes with the sink."""
    try:
        handle = stack.enter_context(target.open_append())
    except OSError as exc:
        raise _spill_refused(target.path, exc) from exc
    return io.TextIOWrapper(handle, encoding="utf-8", write_through=True)


def _write_text(target: SpillTarget, text: str) -> None:
    """Write the full output to a spill file (sync; run off the loop)."""
    try:
        target.write_text(text)
    except OSError as exc:
        raise _spill_refused(target.path, exc) from exc


async def run_command(
    command: str,
    *,
    cwd: str,
    env: Mapping[str, str],
    shell: str,
    timeout_ms: int | None = DEFAULT_TIMEOUT_MS,
    make_spill: SpillFactory,
    limits: ExecLimits | None = None,
    on_output: Callable[[str], None] | None = None,
    sandbox: SandboxLaunch | None = None,
    agent_cwd: str | None = None,
    spell: Callable[[Path], str] | None = None,
    stdin: bytes | None = None,
    stuck_seconds: float | None = STUCK_SECONDS,
    cpu_reading: CpuReading | None = None,
    clock: Callable[[], float] = time.monotonic,
    check_seconds: float = STUCK_CHECK_SECONDS,
) -> ExecResult:
    """Run ``command`` to completion (or timeout) and assemble the model-visible
    result. The drain runs concurrently with the exit/timeout race; on
    ``CancelledError`` the group is killed and the error re-raised. ``on_output``
    is handed each decoded chunk as it arrives, for a caller that shows the
    output of a command still running.

    ``sandbox`` is the chat's own sandbox launch (see :func:`session_sandbox`):
    the command runs behind the same sandbox, uid, cgroup and ``HOME`` as the
    chat's agent server (under gVisor, it ``exec``s into the agent's container).
    ``None`` and the bare no-sandbox launch run the command exactly as before,
    byte for byte. ``agent_cwd`` is ``cwd`` as the agent sees it, for a sandbox
    that mounts the working directory somewhere other than its host path: the
    container is entered there and ``PWD`` says so. ``spell`` spells a spilled
    output's path for the agent the same way; ``None`` names the host path.
    ``make_spill`` names the file the output goes to when it is too long to
    hand back whole; nothing is made unless it is.
    ``stdin`` is written to the command's standard input and closed; without
    it the command reads an empty input.

    ``timeout_ms`` is a wall clock somebody chose (the model, or an operator's
    default). With none, the command runs until it finishes unless it is
    stuck: no output and no CPU used (``cpu_reading``, by default its process
    tree) for ``stuck_seconds``, checked every ``check_seconds`` on ``clock``."""
    lim = limits or ExecLimits()
    argv: list[str] = [shell, "-c", command]
    child_env = dict(env)
    pid_file: Path | None = None
    if sandbox is not None:
        if sandbox.before:
            await asyncio.to_thread(run_steps, sandbox.before)
        child_env = sandbox.apply_env(child_env)
        if agent_cwd is not None:
            child_env["PWD"] = agent_cwd
        if sandbox.container is not None:
            # Where runsc writes the command's pid inside the container, for
            # the kill of its tree there; the daemon's own file, not the chat's.
            pid_file = Path(tempfile.gettempdir()) / f"alkera-exec-{uuid.uuid4().hex}.pid"
        # Under gVisor the wrapper carries the environment into the container
        # itself (``runsc exec --env``); elsewhere the spawn's env is the env.
        argv = sandbox.wrap(argv, env=child_env, cwd=agent_cwd, pid_file=pid_file)
    try:
        proc = await spawn_async(
            SpawnSpec(
                argv=argv,
                env=child_env,
                cwd=cwd,
                stdin="devnull" if stdin is None else "pipe",
                stdout="pipe",
                stderr="stdout",
            )
        )
    except OSError as exc:
        # Spawn itself failed (cwd vanished after the pre-check, shell not
        # executable, fork limit) — surface a purpose-built message, not a raw errno.
        raise RuntimeError(
            f"could not start the command (shell {shell!r}, cwd {cwd!r}): {exc}"
        ) from exc
    if sandbox is not None:
        placed = sandbox.after_spawn(proc.pid)
        if placed:
            await asyncio.to_thread(run_steps, placed)

    chunks: deque[tuple[str, int]] = deque()
    used = 0
    emitted = 0  # chunks seen so far: output is progress for the stuck check
    cut = False
    full = ""  # the complete output, in memory until it spills
    spill: SpillTarget | None = None
    sink: TextIO | None = None
    spill_stack = contextlib.ExitStack()
    #: Why the rest of the output could not be kept, when the tool-output
    #: directory stopped being a plain directory under a running command.
    spill_refused: str | None = None
    decoder = codecs.getincrementaldecoder("utf-8")("replace")

    async def _emit(text: str) -> None:
        nonlocal used, cut, full, spill, sink, spill_refused, emitted
        if not text:
            return
        emitted += 1
        if on_output is not None:
            on_output(text)
        size = len(text.encode("utf-8"))
        chunks.append((text, size))
        used += size
        while used > lim.keep_bytes and len(chunks) > 1:
            _, sz = chunks.popleft()
            used -= sz
            cut = True
        if sink is not None:
            sink.write(text)  # a buffered file-object write — cheap, not real disk I/O
        elif spill_refused is None:
            full += text
            if lim.max_bytes is not None and len(full.encode("utf-8")) > lim.max_bytes:
                # Spill the COMPLETE output to disk so memory stays bounded; the
                # one-time open runs off the loop.
                spill = make_spill()
                try:
                    sink = await asyncio.to_thread(_open_append, spill_stack, spill)
                except ToolError as exc:
                    # The directory was swapped under the running command; the
                    # window is all that is kept, and the banner says so.
                    spill_refused, spill = str(exc), None
                else:
                    sink.write(full)
                full = ""
                cut = True

    async def _drain() -> None:
        assert proc.stdout is not None
        while True:
            data = await proc.stdout.read(65536)
            if not data:
                break
            await _emit(decoder.decode(data))
        await _emit(decoder.decode(b"", final=True))

    async def _feed() -> None:
        assert proc.stdin is not None
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            proc.stdin.write(stdin or b"")
            await proc.stdin.drain()
        proc.stdin.close()

    drain_task: asyncio.Task[None] = asyncio.ensure_future(_drain())
    feed_task = asyncio.ensure_future(_feed()) if stdin is not None else None
    timed_out = False
    stuck = False
    reading = cpu_reading or _default_cpu_reading(proc.pid, sandbox, pid_file)
    try:
        try:
            if timeout_ms is None and stuck_seconds is not None:
                await _wait_unless_stuck(
                    proc,
                    output_seen=lambda: emitted,
                    cpu_reading=reading,
                    stuck_seconds=stuck_seconds,
                    clock=clock,
                    check_seconds=check_seconds,
                )
            else:
                # Race the EXIT against the timeout — NOT the drain (which runs
                # concurrently). A command that hangs producing no output still
                # hits this timeout, because we wait on proc.wait(), not on a read().
                await asyncio.wait_for(
                    proc.wait(),
                    timeout=None if timeout_ms is None else (timeout_ms + 100) / 1000,
                )
        except _CommandStuckError:
            timed_out = stuck = True
            await _end_command(
                proc, sandbox=sandbox, pid_file=pid_file, grace=FORCE_KILL_SECONDS or 0.0
            )
        except TimeoutError:
            timed_out = True
            await _end_command(
                proc, sandbox=sandbox, pid_file=pid_file, grace=FORCE_KILL_SECONDS or 0.0
            )
        # The process is gone (exited or killed) → stdout hits EOF → the drain
        # finishes flushing the buffered tail. Bounded so a stuck pipe can't hang us.
        with contextlib.suppress(TimeoutError, asyncio.TimeoutError, Exception):
            await asyncio.wait_for(asyncio.shield(drain_task), timeout=5.0)
    except asyncio.CancelledError:
        # background_cancel / chat close — kill the whole tree, then propagate so
        # the registry records the job as cancelled.
        await _end_command(
            proc, sandbox=sandbox, pid_file=pid_file, grace=FORCE_KILL_SECONDS or 0.0
        )
        drain_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await drain_task
        raise
    finally:
        if feed_task is not None and not feed_task.done():
            feed_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await feed_task
        if sink is not None:
            with contextlib.suppress(Exception):
                sink.close()
        with contextlib.suppress(Exception):
            spill_stack.close()
        if not drain_task.done():
            drain_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await drain_task
        if pid_file is not None:
            with contextlib.suppress(OSError):
                pid_file.unlink()

    raw = "".join(t for t, _ in chunks)
    tail_text, tail_cut = tail(raw, lim.max_lines, lim.max_bytes)
    if tail_cut:
        cut = True
    if spill is None and tail_cut and spill_refused is None:
        # Tailed but never spilled the running output — persist the window so the
        # model can Read/Grep the full text the banner points at.
        spill = make_spill()
        try:
            await asyncio.to_thread(_write_text, spill, raw)
        except ToolError as exc:
            spill_refused, spill = str(exc), None

    output = tail_text or "(no output)"
    spill_path = spill.path if spill is not None else None
    shown = (spell(spill_path) if spell is not None else str(spill_path)) if spill_path else None
    if cut and shown is not None:
        output = f"...output truncated...\n\nFull output saved to: {shown}\n\n" + output
    elif cut and spill_refused is not None:
        output = f"...output truncated...\n\nThe rest was not kept: {spill_refused}\n\n" + output
    if stuck:
        output += (
            "\n\n<shell_metadata>\n"
            f"shell tool stopped the command: it printed nothing and used no CPU for "
            f"{_minutes(stuck_seconds)}, so it looked stuck (waiting for input, a lock or a "
            "network peer that never answers). If it is meant to sit idle that long, run "
            "it again with an explicit timeout in milliseconds.\n"
            "</shell_metadata>"
        )
    elif timed_out:
        output += (
            "\n\n<shell_metadata>\n"
            f"shell tool terminated command after exceeding timeout {timeout_ms} ms. "
            "If this command is expected to take longer and is not waiting for "
            "interactive input, retry with a larger timeout value in milliseconds.\n"
            "</shell_metadata>"
        )
    return ExecResult(
        output=output,
        exit_code=None if timed_out else proc.returncode,
        truncated=cut,
        output_path=shown if (cut and shown is not None) else None,
        timed_out=timed_out,
        stuck=stuck,
    )


class _CommandStuckError(Exception):
    """The command made no progress for the stuck bound."""


def _minutes(seconds: float | None) -> str:
    """``seconds`` as the reader would say it."""
    if seconds is None:
        return "a long time"
    if seconds < 120:
        return f"{seconds:g} seconds"
    return f"{seconds / 60:g} minutes"


def _default_cpu_reading(
    pid: int, sandbox: SandboxLaunch | None, pid_file: Path | None
) -> CpuReading:
    """How a command's CPU is read: inside its gVisor container when it runs
    in one (the host sees only the ``runsc exec`` wrapper), else its host
    process tree."""
    if (
        sandbox is not None
        and sandbox.container is not None
        and pid_file is not None
        and sandbox.runsc is not None
        and sandbox.runsc_root is not None
        and sandbox.uid is not None
    ):
        container, runsc, root, uid = (
            sandbox.container,
            sandbox.runsc,
            sandbox.runsc_root,
            sandbox.uid,
        )
        return lambda: container_tree_cpu(
            runner=run_probe,
            runsc=runsc,
            root=root,
            container=container,
            uid=uid,
            pid_file=pid_file,
        )
    return lambda: host_tree_cpu(pid)


async def _wait_unless_stuck(
    proc: asyncio.subprocess.Process,
    *,
    output_seen: Callable[[], int],
    cpu_reading: CpuReading,
    stuck_seconds: float,
    clock: Callable[[], float],
    check_seconds: float,
) -> None:
    """Wait for ``proc`` to exit with no wall clock; raise
    :class:`_CommandStuckError` once it has shown neither output nor CPU use
    for ``stuck_seconds``. A CPU reading that cannot be taken is no evidence
    either way, so the bound then runs on output alone."""
    exited = asyncio.ensure_future(proc.wait())
    try:
        last_output = output_seen()
        last_cpu = await asyncio.to_thread(cpu_reading)
        progressed_at = clock()
        while True:
            done, _ = await asyncio.wait({exited}, timeout=check_seconds)
            if done:
                return
            output = output_seen()
            cpu = await asyncio.to_thread(cpu_reading)
            if output != last_output or (cpu is not None and cpu != last_cpu):
                progressed_at = clock()
            last_output = output
            if cpu is not None:
                last_cpu = cpu
            if clock() - progressed_at >= stuck_seconds:
                raise _CommandStuckError
    finally:
        if not exited.done():
            exited.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await exited


def confine_under_root(rel: str, root: Path, *, label: str) -> Path:
    """Resolve ``rel`` under the workspace ``root``, refusing any escape: an absolute path
    outside the root, a ``..`` that climbs above it, or a symlink under the root that points
    out of it. The path is judged twice — lexically, then with every link followed — and the
    LEXICAL spelling is returned, so a caller reports the name it was given. ``label`` names
    the confined thing for the error (e.g. ``"workdir"`` / ``"file"``). The single
    workspace-escape guard both the bash tool and the integration-SDK tool call."""
    resolved = Path(os.path.normpath(os.path.join(str(root), rel)))
    try:
        resolved.relative_to(root)
        Path(os.path.realpath(resolved)).relative_to(os.path.realpath(root))
    except ValueError as exc:
        raise ToolError(
            f"{label} {rel!r} is outside the workspace; keep it under the workspace root"
        ) from exc
    return resolved


async def run_with_abort(
    run: Callable[[], Awaitable[ExecResult]], abort: object | None
) -> ExecResult:
    """Run a foreground command, racing it against the turn's ``abort`` Event so a
    Ctrl-C reaps the process group.

    A turn cancel does NOT cancel the in-flight loopback-MCP dispatch (stateless
    server → detached task), so we can't rely on a ``CancelledError`` reaching
    ``run_command``. Instead the session SETS ``abort`` on cancel; we cancel the
    command task ourselves, which raises ``CancelledError`` inside ``run_command``
    where the process group is killed, then surface a model-visible cancellation.

    Without an ``abort`` (the editor / no-session path) this is just ``await run()``.
    If OUR OWN task is cancelled (a genuine loop teardown) we still reap the group
    via the command task, then re-raise — never swallow it as a tool cancel."""
    if abort is None or not isinstance(abort, asyncio.Event):
        return await run()

    run_task: asyncio.Task[ExecResult] = asyncio.ensure_future(run())
    abort_waiter: asyncio.Task[bool] = asyncio.ensure_future(abort.wait())
    try:
        done, _pending = await asyncio.wait(
            {run_task, abort_waiter}, return_when=asyncio.FIRST_COMPLETED
        )
    except asyncio.CancelledError:
        # OUR dispatch task was cancelled (a genuine teardown). Reap the command's
        # process group (run_command kills it on CancelledError), then propagate —
        # this is NOT a user "command cancelled", so don't convert it to a ToolError.
        run_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await run_task
        raise
    finally:
        # The loser never resolves on the normal path — always reap the abort waiter
        # so a completed foreground command never leaks a pending Event-wait task.
        if not abort_waiter.done():
            abort_waiter.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await abort_waiter

    if run_task in done:
        # Command finished first (success or its own RuntimeError/timeout) — return
        # its result; .result() re-raises a genuine spawn failure exactly as before.
        return run_task.result()

    # Abort won the race → cancel the command (run_command kills the group + re-raises
    # CancelledError), then surface a clean model-visible cancellation.
    run_task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await run_task
    raise ToolError("command cancelled")


__all__ = [
    "ExecLimits",
    "ExecResult",
    "build_child_env",
    "confine_under_root",
    "run_command",
    "run_with_abort",
    "select_shell",
    "tail",
]


# ---------------------------------------------------------------------------
# The chat's sandbox, for every command run on its behalf
# ---------------------------------------------------------------------------


def session_sandbox(
    session_id: str,
    *,
    folder: Path | None,
    fenced: bool,
    python: bool = False,
    state_dir: Path | None = None,
    owner_session_id: str = "",
) -> SandboxLaunch:
    """The launch a command on this session's behalf runs in, built once per
    session (and once more for commands that run our own interpreter, which
    need its trees bound). ``owner_session_id`` is the session whose identity
    the command runs as when it is not this one's: a subagent child runs in
    its own container as its parent's uid, since the two share one tree.

    A local session — no fence — is never sandboxed: the person's own shell in
    the person's own project. A fenced session with a working directory runs in
    the box's mode with the same uid and slice as its agent server (the user and
    the slice are named from the session id, so they cannot differ); under gVisor
    it ``exec``s into the agent server's live container. The command joins the
    cgroup (or container) the agent server's launch made and never sets its
    limits: only that launch knows the chat's own limits, and a command that
    set the box default would shrink (or grow) the whole chat. A box set to
    ``gvisor`` that cannot run runsc refuses the command, the same as the agent
    server; a box with no uid/cgroup to apply runs the command unsandboxed.

    ``state_dir`` is the chat's runtime state directory, where the agent
    server's launch made the default Python environment; the command runs with
    that environment active, the same as the agent.
    """
    if not fenced or folder is None:
        return NO_SANDBOX
    cached = cached_launch(session_id, python=python)
    if cached is not None:
        return cached
    settings = SandboxSettings.from_env()
    cap = current_capability()
    if settings.mode == "none" and not cap.controls:
        remember_launch(session_id, NO_SANDBOX, python=python)
        return NO_SANDBOX
    try:
        runtime = select_runtime(settings.mode, gvisor_ready=cap.gvisor)
    except SandboxRefusedError as exc:
        raise ToolError(str(exc)) from exc
    # A workspace member's command runs as its agent server does: as the
    # workspace's uid in its container under gVisor, as its own uid sharing
    # only the workspace's tree on a ``none`` box.
    scope = scope_of(session_id)
    owner = owner_session_id or session_id
    try:
        uid, share_gid = member_identity(
            ensure_chat_uid(scope.tree if scope.member else scope_of(owner).tree),
            owner,
            member=scope.member,
            mode=settings.mode,
            ensure=ensure_chat_uid,
        )
    except SandboxRefusedError as exc:
        raise ToolError(str(exc)) from exc
    # The shell spec never applies these; the agent server's launch set the
    # chat's real limits on the slice (or container) the command joins.
    vcpu, memory_mb = settings.default_limits()
    agent = SandboxSpec(
        chat_id=scope.container,
        folder=folder,
        uid=uid,
        share_gid=share_gid,
        vcpu=vcpu,
        memory_mb=memory_mb,
        home=settings.home,
        mode=settings.mode,
        cgroup=cap.cgroup,
        default_env=(
            session_default_env(session_id, state_dir)
            if state_dir is not None and cap.default_env
            else None
        ),
        # The same bind table the agent server's launch mounts from, so the
        # command's environment names the chat's trees where the container
        # holds them (the environment at its internal path, not the host's).
        binds=(
            chat_binds(
                runtime_dir=state_dir,
                agent_config_root=agent_config_root(session_id),
                default_env=cap.default_env,
                envs_dir=session_envs_dir(session_id, state_dir),
            )
            if state_dir is not None
            else ()
        ),
        python_home=cap.python_home or settings.python_home,
        chat_net=settings.chat_net,
        mount_alias=settings.mode == "none" and cap.mount_ns,
        runsc=cap.runsc or "runsc",
        setpriv=cap.setpriv or "setpriv",
        uv=cap.uv or "uv",
        unshare=cap.unshare or "unshare",
    )
    binds = python_ro_binds(packages=_own_package_files()) if python else ()
    launch = runtime.compose_launch(shell_spec(agent, python=binds))
    remember_launch(session_id, launch, python=python)
    return launch


def _own_package_files() -> tuple[str, ...]:
    import alkera_core

    import alkera_cli

    return tuple(f for f in (alkera_cli.__file__, alkera_core.__file__) if f)
