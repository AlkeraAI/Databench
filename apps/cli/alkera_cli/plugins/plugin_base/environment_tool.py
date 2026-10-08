"""``environment.capture`` and ``environment.recreate``: the agent keeps the
workspace's environment spec current, and builds an environment from it.

Both are thin callers of :mod:`alkera_cli.environment`. What makes them safe on
a shared box is the runner: in a cloud chat every command (the probe that reads
the environment, the installs, the write of the spec into the chat's folder)
runs inside the chat's own sandbox, as the chat's own user, through the same
launch its ``bash`` commands use. The box's root never reads a file the chat
controls (a symlink planted there could otherwise point it at the box's own
credentials) and never writes one (a planted symlink could aim that write
anywhere). In a local session commands run as the person, in their project.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import os
import shlex
import tarfile
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, ClassVar

from alkera_core.project.directory import CHATS_SUBDIR
from pydantic import BaseModel, Field

from alkera_cli.contracts.tool_types import ActionDescriptor, Effect, ResourceRef
from alkera_cli.environment import (
    CAPTURE_TOOL_NAME,
    LOCAL_ENV_DIR,
    PLACEHOLDER_RE,
    RECREATE_TOOL_NAME,
    SPEC_FILENAME,
    ChatLaunch,
    Command,
    CommandResult,
    CommandRunner,
    EnvironmentService,
    EnvironmentSpec,
    Gap,
    InvalidSpecError,
    LocalRunner,
    NotPortable,
    ProbeError,
    SpecSecretError,
    StepReport,
    capture,
    env_python,
    execute_recreate,
    host_python,
    load_spec,
    local_python,
    prepare_recreate,
    probe_command,
    render_spec,
    run_probe,
    summarize_spec,
    write_spec,
)
from alkera_cli.files.chat_fs import ChatTree
from alkera_cli.harness.sandbox import RUNTIME_STATE_SUBDIR, SandboxLaunch, default_env_path
from alkera_cli.harness.sandbox_env import session_default_env
from alkera_cli.plugins.plugin_base.agent_tree import (
    SpillFactory,
    ToolOutput,
    tool_output,
    tool_tree,
)
from alkera_cli.plugins.plugin_base.bash_exec import (
    ExecLimits,
    build_child_env,
    run_command,
    run_with_abort,
    session_sandbox,
)
from alkera_cli.plugins.plugin_base.permissions import (
    binding_from_context,
    denied_error,
    gate_shell_action,
    gate_sql_action,
)
from alkera_cli.plugins.plugin_base.tool import Tool, ToolContext, ToolError, ToolSpec

#: The sandbox's interpreter when the caller names no environment: the first
#: ``python3`` on the chat's ``PATH``, which is its active environment.
SANDBOX_PYTHON = "python3"


@dataclass(frozen=True, slots=True)
class ChatEnvironment:
    """Everything this tool assumes about where a cloud chat's environment
    lives, in one place: the runtime state its sandbox launch makes the
    default environment under, the trees the chat may build an environment
    in, and the interpreter that is active. The sandbox and the fence own
    these facts; a change to where the environment lives is made there and
    read here through :func:`chat_environment` alone."""

    state_dir: Path | None
    own_trees: tuple[Path, ...]
    python: str = SANDBOX_PYTHON
    session_id: str = ""

    @property
    def default_env(self) -> Path | None:
        """The chat's default environment, the one a wake restores: its
        workspace's for a member, its own otherwise."""
        if self.state_dir is None:
            return None
        if self.session_id:
            return session_default_env(self.session_id, self.state_dir)
        return default_env_path(self.state_dir)


def chat_environment_for(alkera_dir: Any, session_id: str, fence: Any) -> ChatEnvironment:
    """The cloud chat's environment facts (see :class:`ChatEnvironment`)."""
    state_dir = (
        Path(alkera_dir) / CHATS_SUBDIR / session_id / RUNTIME_STATE_SUBDIR
        if alkera_dir is not None and session_id
        else None
    )
    own = tuple(Path(t) for t in fence.own_trees) if fence is not None else ()
    return ChatEnvironment(state_dir=state_dir, own_trees=own, session_id=session_id or "")


def chat_environment(ctx: ToolContext) -> ChatEnvironment:
    return chat_environment_for(ctx.alkera_dir, ctx.session_id, ctx.fence)


def workspace_aliases(fence: Any) -> tuple[str, ...]:
    """Every spelling of the workspace root: its host path and the alias the
    agent sees the working directory at. The fence's other aliases (the
    environments, the agent's own trees) are not the workspace, so a path
    under them is never recorded as workspace-relative."""
    root = Path(fence.working_dir)
    own = tuple(alias for alias, real in getattr(fence, "aliases", ()) if Path(real) == root)
    return (str(root), *own)


def _chat_runner(
    session_id: str,
    root: Path,
    chat: ChatEnvironment,
    spill: SpillFactory,
    abort: Any = None,
    spell: Callable[[Path], str] | None = None,
    owner_session_id: str = "",
) -> SandboxRunner:
    """A runner for commands on the chat's behalf: its sandbox, its uid,
    entered where the agent sees its root. The launch's own ``HOME`` (and the
    rest of its environment) overrides the host ``home`` given here
    (``SandboxLaunch.apply_env``)."""
    launch = session_sandbox(
        session_id,
        folder=root,
        fenced=True,
        state_dir=chat.state_dir,
        owner_session_id=owner_session_id,
    )
    env = build_child_env(dict(os.environ), cwd=str(root), home=str(root))
    return SandboxRunner(
        sandbox=launch,
        cwd=str(root),
        env=env,
        spill=spill,
        abort=abort,
        agent_cwd=launch.agent_home,
        spell=spell,
    )


def chat_launch(session_id: str, fence: Any, alkera_dir: Path) -> ChatLaunch | None:
    """The cloud chat's launch for the runtime's environment service
    (:data:`~alkera_cli.environment.ChatRunnerFactory`): a runner in its
    sandbox as its user, where its default environment lives, and the
    interpreter its ``PATH`` resolves first. ``None`` without a fence or a
    default environment."""
    if fence is None:
        return None
    chat = chat_environment_for(alkera_dir, session_id, fence)
    if chat.default_env is None:
        return None
    root = Path(fence.working_dir)
    output = ToolOutput(ChatTree.for_chat(root, session_id))
    runner = _chat_runner(
        session_id, root, chat, lambda: output.spill("environment"), spell=fence.spell
    )
    return ChatLaunch(runner=runner, default_env=chat.default_env, python=chat.python)


#: Output a command run in the sandbox keeps (the probe prints the whole
#: environment as one line).
_KEEP_BYTES = 32 * 1024 * 1024
#: The shell every sandboxed command runs under: the container's own.
_SANDBOX_SHELL = "/bin/sh"


def _bundle(files: Mapping[str, str]) -> bytes:
    """The files as one tar stream, unpacked inside the sandbox."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, content in files.items():
            data = content.encode("utf-8")
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o600
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _shell_word(arg: str, files: Mapping[str, str]) -> str:
    """One argv element as shell text: literal pieces quoted, each ``{name}``
    the unpacked file's path, ``"$d"/name``."""
    out: list[str] = []
    pos = 0
    for match in PLACEHOLDER_RE.finditer(arg):
        name = match.group(1)
        if name not in files:
            raise KeyError(f"command names a file it does not carry: {name}")
        if match.start() > pos:
            out.append(shlex.quote(arg[pos : match.start()]))
        out.append(f'"$d"/{name}')
        pos = match.end()
    if pos < len(arg) or not out:
        out.append(shlex.quote(arg[pos:]))
    return "".join(out)


def pid_file(token: str) -> str:
    """Where, inside the sandbox, a running command's process group is named:
    its ``/tmp`` (the container's own under gVisor), under a fresh random
    token per command, written by the chat's user."""
    return f"/tmp/alkera-env-{token}.pid"  # noqa: S108 - the sandbox's /tmp, random name


def cancel_file(token: str) -> str:
    """Where, inside the sandbox, a cancel of the command under ``token`` is
    recorded before the command may have named its process group: the
    command reads it once it has, and stops itself."""
    return f"/tmp/alkera-env-{token}.cancel"  # noqa: S108 - the sandbox's /tmp, random name


def sandbox_script(command: Command, *, token: str = "") -> str:
    """The shell text that runs ``command`` inside the sandbox: its files are
    unpacked from standard input into a private ``/tmp`` directory (never the
    chat's synced folder) that is removed on exit, and each ``{name}`` in the
    argv names one of them.

    With a ``token`` the command runs in a process group of its own whose id
    is written to :func:`pid_file`, so a cancel can stop it (and everything it
    started, an installer's builds included) from inside the sandbox: killing
    the host side of a ``runsc exec`` leaves the container's processes alive.
    A cancel that lands before the group is named leaves :func:`cancel_file`
    behind instead, and the script stops the group itself once it has
    written its id (each side writes its file before reading the other's, so
    one of them always sees the other)."""
    lines = ["set -u"]
    if command.files:
        lines.append('d="$(mktemp -d /tmp/alkera-env.XXXXXX)" || exit 125')
        lines.append("trap 'rm -rf \"$d\"' EXIT HUP INT TERM")
        lines.append('tar -xf - -C "$d" || exit 125')
    if command.cwd:
        lines.append(f"cd {shlex.quote(command.cwd)} || exit 125")
    env = [f"{k}={v}" for k, v in sorted(command.env.items())]
    argv = ["env", *env, *command.argv] if env else list(command.argv)
    head = [shlex.quote(a) for a in argv[: len(argv) - len(command.argv)]]
    tail = [_shell_word(a, command.files) for a in command.argv]
    payload = " ".join([*head, *tail])
    if not token:
        lines.append(payload)
        return "\n".join(lines) + "\n"
    pid = shlex.quote(pid_file(token))
    cancel = shlex.quote(cancel_file(token))
    lines.append(f"if [ -e {cancel} ]; then rm -f {cancel}; exit 137; fi")
    # A process group of its own: setsid where it exists (every Linux box;
    # dash has no job control without a terminal), job control elsewhere.
    lines.append("if command -v setsid >/dev/null 2>&1; then")
    lines.append(f"  setsid {payload} &")
    lines.append("else")
    lines.append(f"  set -m 2>/dev/null; {payload} &")
    lines.append("fi")
    lines.append(f'p=$!; set +m 2>/dev/null; echo "$p" > {pid}')
    lines.append(f'if [ -e {cancel} ]; then kill -s KILL -- "-$p" 2>/dev/null; fi')
    lines.append(f'wait "$p"; rc=$?; rm -f {pid} {cancel}; exit "$rc"')
    return "\n".join(lines) + "\n"


def kill_script(token: str) -> str:
    """Stop the process group a :func:`sandbox_script` recorded, from inside
    the sandbox, and forget it. The cancel is recorded first, so a command
    that has not named its group yet stops itself when it does."""
    pid = shlex.quote(pid_file(token))
    cancel = shlex.quote(cancel_file(token))
    return (
        f": > {cancel}; "
        f'p="$(cat {pid} 2>/dev/null)"; '
        # ``-s KILL --``: dash's builtin reads ``-KILL -- -<pgid>`` as a bad
        # number, and a negative pid needs the ``--`` to name a process group.
        f'if [ -n "$p" ]; then kill -s KILL -- "-$p" 2>/dev/null; fi; rm -f {pid}\n'
    )


class SandboxRunner:
    """Runs commands inside one chat's sandbox, as the chat's user."""

    def __init__(
        self,
        *,
        sandbox: SandboxLaunch,
        cwd: str,
        env: Mapping[str, str],
        spill: SpillFactory,
        abort: Any = None,
        agent_cwd: str | None = None,
        spell: Callable[[Path], str] | None = None,
    ) -> None:
        self._sandbox = sandbox
        self._cwd = cwd
        self._agent_cwd = agent_cwd
        self._spell = spell
        self._env = dict(env)
        self._spill = spill
        self._abort = abort

    async def _exec(self, script: str, timeout_s: float, stdin: bytes | None) -> Any:
        return await run_command(
            script,
            cwd=self._cwd,
            env=self._env,
            shell=_SANDBOX_SHELL,
            timeout_ms=int(timeout_s * 1000),
            make_spill=self._spill,
            limits=ExecLimits(max_lines=None, max_bytes=None, keep_bytes=_KEEP_BYTES),
            sandbox=self._sandbox,
            agent_cwd=self._agent_cwd,
            spell=self._spell,
            stdin=stdin,
        )

    async def run(self, command: Command) -> CommandResult:
        token = uuid.uuid4().hex
        script = sandbox_script(command, token=token)
        stdin = _bundle(command.files) if command.files else None
        try:
            ex = await run_with_abort(
                lambda: self._exec(script, command.timeout_s, stdin), self._abort
            )
        except BaseException:
            # A cancel (a closed chat, a turn abort) kills only this side of the
            # launch; stop the command where it runs, then let the cancel go on.
            with contextlib.suppress(Exception):
                await asyncio.shield(self._exec(kill_script(token), 30, None))
            raise
        if ex.exit_code is None:
            with contextlib.suppress(Exception):
                await self._exec(kill_script(token), 30, None)
        output = "" if ex.output == "(no output)" else ex.output
        return CommandResult(exit_code=ex.exit_code, output=output)

    async def which(self, name: str) -> str | None:
        result = await self.run(
            Command(argv=("sh", "-c", 'command -v "$1"', "sh", name), timeout_s=30)
        )
        found = result.output.strip().splitlines()
        return found[-1] if result.ok and found else None


class _Session:
    """Where this session's commands run and what its workspace is."""

    def __init__(self, ctx: ToolContext, spill: SpillFactory) -> None:
        fence = ctx.fence
        self.fenced = fence is not None
        if fence is not None:
            self.root = Path(fence.working_dir)
            # Commands run in the sandbox and name paths as the agent sees them;
            # the daemon reads and checks by host path (fence.spell / canonical).
            self.agent_root = fence.spell(self.root)
            self.spell: Callable[[Path], str] = fence.spell
            self.canonical: Callable[[str], str] = fence.canonical
            self.aliases: tuple[str, ...] = workspace_aliases(fence)
            chat = chat_environment(ctx)
            self.own_trees: tuple[Path, ...] = chat.own_trees
            self.sandbox_python = chat.python
            self.runner: CommandRunner = _chat_runner(
                ctx.session_id,
                self.root,
                chat,
                spill,
                ctx.abort,
                spell=fence.spell,
                owner_session_id=ctx.owner_session_id,
            )
            self.tree: ChatTree | None = tool_tree(ctx, self.root)
        else:
            self.root = Path(ctx.alkera_dir).parent if ctx.alkera_dir is not None else Path.cwd()
            self.agent_root = str(self.root)
            self.spell = str
            self.canonical = str
            self.aliases = ()
            self.own_trees = ()
            self.sandbox_python = SANDBOX_PYTHON
            self.runner = LocalRunner()
            self.tree = None

    def python_for(self, env_path: str) -> str | None:
        """The interpreter to read: the named environment's, else the active
        one in a sandbox, else the project's ``.venv`` locally (``None``: the
        project files alone)."""
        if env_path:
            return env_python(env_path, windows=os.name == "nt" and not self.fenced)
        if self.fenced:
            return self.sandbox_python
        return local_python(self.root, windows=os.name == "nt")

    async def write_spec(self, spec: EnvironmentSpec) -> str:
        """Write the spec into the workspace and return the text written."""
        text = render_spec(spec)
        if self.tree is None:
            await asyncio.to_thread(write_spec, self.root, spec)
            return text
        # Into the chat's tree through its one write seam: no link followed,
        # the file handed to the chat's identity so the agent can change it.
        try:
            await asyncio.to_thread(self.tree.write, SPEC_FILENAME, text.encode("utf-8"))
        except OSError as exc:
            raise ToolError(f"could not write {SPEC_FILENAME}: {exc}") from exc
        return text

    def admits_env(self, target: str) -> bool:
        """A cloud chat builds environments only in trees that are its own
        (its default environment's), never in its synced folder or elsewhere."""
        if not self.fenced:
            return True
        path = PurePosixPath(os.path.normpath(self.canonical(target)))
        return any(
            path == PurePosixPath(t) or PurePosixPath(t) in path.parents for t in self.own_trees
        )


def _service(ctx: ToolContext) -> EnvironmentService | None:
    """The runtime's environment service, handed to the session's registry."""
    service = getattr(ctx.registry, "environment", None)
    return service if isinstance(service, EnvironmentService) else None


async def _spill_factory(ctx: ToolContext) -> SpillFactory:
    output = await asyncio.to_thread(tool_output, ctx, fallback="alkera-environment")
    return lambda: output.spill("environment")


#: The modes whose contract is no change to the workspace at all.
_NO_WRITE_MODES = frozenset({"read_only", "plan"})


async def _gate_command(ctx: ToolContext, text: str, cwd: Path) -> None:
    gate = await gate_shell_action(
        text, mode=ctx.permission_mode, binding=binding_from_context(ctx), cwd=cwd
    )
    if not gate.allowed:
        raise ToolError(denied_error(gate.reason or "the command was not approved"))


async def _gate_spec_write(ctx: ToolContext, path: Path) -> None:
    """Writing the spec. Refused in a mode that changes nothing; free in the
    session's own working directory (a cloud chat's folder), like every other
    write there; asked like any file write anywhere else (a local project)."""
    if ctx.permission_mode in _NO_WRITE_MODES:
        raise ToolError(denied_error(f"{ctx.permission_mode} mode does not write {path.name}"))
    sandbox = Path(ctx.sandbox_dir) if ctx.sandbox_dir is not None else None
    if sandbox is not None and path.parent == sandbox:
        return
    descriptor = ActionDescriptor(
        capability="file",
        effect=Effect.WRITE,
        operation="environment_capture",
        targets=[ResourceRef(kind="file", name=str(path))],
        raw=f"write {path.name}",
        classifier="alkera_environment",
        confidence="exact",
    )
    gate = await gate_sql_action(
        descriptor, mode=ctx.permission_mode, binding=binding_from_context(ctx)
    )
    if gate.cap_token is None:
        raise ToolError(denied_error(gate.reason or f"writing {path.name} was not approved"))


# --- environment.capture -----------------------------------------------------


class EnvironmentCaptureInput(BaseModel):
    env_path: str = Field(
        default="",
        description=(
            "The environment to capture. Leave empty for the active one (in a cloud chat, the "
            "environment `python` runs in)."
        ),
    )


class EnvironmentCaptureResult(BaseModel):
    path: str = SPEC_FILENAME
    """The spec, relative to the workspace root."""
    summary: str = ""
    packages: int = 0
    editable: list[str] = Field(default_factory=list)
    not_portable: list[NotPortable] = Field(default_factory=list)


_CAPTURE_DESCRIPTION = (
    "Record the workspace's Python environment (packages and versions, editable installs "
    "from the workspace, conda packages, indexes with credentials removed) in "
    f"`{SPEC_FILENAME}` at the workspace root, so the next agent or machine can recreate it. "
    "Call it after you install or remove packages. It reads the environment; it changes nothing "
    "in it."
)


class EnvironmentCaptureTool(Tool[EnvironmentCaptureInput, EnvironmentCaptureResult]):
    spec: ClassVar[ToolSpec] = ToolSpec(
        name=CAPTURE_TOOL_NAME,
        title="Capture the environment",
        description=_CAPTURE_DESCRIPTION,
        app="environment",
        hot=False,
        effect_hint=Effect.WRITE,
    )
    Input: ClassVar[type[BaseModel]] = EnvironmentCaptureInput
    Output: ClassVar[type[BaseModel]] = EnvironmentCaptureResult

    async def run(
        self, args: EnvironmentCaptureInput, ctx: ToolContext
    ) -> EnvironmentCaptureResult:
        session = _Session(ctx, await _spill_factory(ctx))
        await _gate_spec_write(ctx, session.root / SPEC_FILENAME)
        service = _service(ctx)
        # A capture during a wake's restore would record half an environment.
        if service is not None:
            await service.wait(ctx.session_id)
        python = session.python_for(args.env_path)
        if python is not None and not session.fenced:
            # Reading an environment runs its interpreter, and its .pth files run
            # with it: on this machine, unsandboxed, that is code execution, so it
            # is judged like the same command typed into the shell.
            await _gate_command(
                ctx, probe_command(python, session.agent_root).display(), session.root
            )
        try:
            spec = await capture(
                session.runner,
                root=session.agent_root,
                python=python,
                aliases=session.aliases,
            )
            text = await session.write_spec(spec)
            if service is not None and session.fenced and ctx.alkera_dir is not None:
                # This chat captured this spec: a later wake may restore it.
                await asyncio.to_thread(
                    service.record_capture, Path(ctx.alkera_dir), ctx.session_id, text
                )
        except ProbeError as exc:
            raise ToolError(str(exc)) from exc
        except SpecSecretError as exc:
            raise ToolError(str(exc)) from exc
        return EnvironmentCaptureResult(
            summary=summarize_spec(spec),
            packages=len(spec.packages),
            editable=[f"{p.name} ({p.path})" for p in spec.packages if p.source == "editable"],
            not_portable=spec.not_portable,
        )


# --- environment.recreate ----------------------------------------------------


class EnvironmentRecreateInput(BaseModel):
    dry_run: bool = Field(
        default=False, description="Only plan: list the commands without running any."
    )
    env_path: str = Field(
        default="",
        description=(
            "The environment to build or update. Leave empty for the active one (in a cloud "
            "chat) or the project's `.venv` (locally)."
        ),
    )


class EnvironmentRecreateResult(BaseModel):
    target_env: str = ""
    dry_run: bool = False
    already_satisfied: bool = False
    steps: list[StepReport] = Field(default_factory=list)
    not_recreated: list[Gap] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


_RECREATE_DESCRIPTION = (
    f"Build or update a Python environment to match the workspace's `{SPEC_FILENAME}`: create "
    "it if missing, install what is missing (uv for pip packages, micromamba for conda), and "
    "reinstall editable packages from the workspace. Removes nothing. Re-running when the "
    "environment already matches does nothing. Use `dry_run` to see the plan first; the result "
    "lists anything that could not be recreated."
)


class EnvironmentRecreateTool(Tool[EnvironmentRecreateInput, EnvironmentRecreateResult]):
    spec: ClassVar[ToolSpec] = ToolSpec(
        name=RECREATE_TOOL_NAME,
        title="Recreate the environment",
        description=_RECREATE_DESCRIPTION,
        app="environment",
        hot=False,
        effect_hint=Effect.WRITE,
    )
    Input: ClassVar[type[BaseModel]] = EnvironmentRecreateInput
    Output: ClassVar[type[BaseModel]] = EnvironmentRecreateResult

    async def run(
        self, args: EnvironmentRecreateInput, ctx: ToolContext
    ) -> EnvironmentRecreateResult:
        session = _Session(ctx, await _spill_factory(ctx))
        service = _service(ctx)
        if service is not None:
            await service.wait(ctx.session_id)
        try:
            spec = await asyncio.to_thread(load_spec, session.root)
        except InvalidSpecError as exc:
            raise ToolError(f"{exc}; call {CAPTURE_TOOL_NAME} to write one") from exc
        target = await self._target(session, args.env_path)
        if not session.admits_env(target):
            raise ToolError(f"{target} is not one of this chat's own environment folders")
        windows = os.name == "nt" and not session.fenced
        fallback = session.sandbox_python if session.fenced else host_python()
        if not session.fenced:
            # Reading the target runs its interpreter on this machine.
            probe = probe_command(env_python(target, windows=windows), str(session.root))
            await _gate_command(ctx, probe.display(), session.root)
        prepared = await prepare_recreate(
            spec,
            session.runner,
            target_env=target,
            target_root=session.agent_root,
            aliases=session.aliases,
            dry_run=args.dry_run,
            fallback_python=fallback,
            windows=windows,
        )
        if args.dry_run or not prepared.plan.steps:
            return EnvironmentRecreateResult(
                **{**prepared.report.model_dump(), "dry_run": args.dry_run}
            )
        # The approval covers exactly these steps, and exactly these run.
        await _gate_command(ctx, approval_text(prepared.report.steps), session.root)
        report = await execute_recreate(prepared, session.runner)
        return EnvironmentRecreateResult(**report.model_dump())

    @staticmethod
    async def _target(session: _Session, env_path: str) -> str:
        if env_path:
            return env_path
        if not session.fenced:
            return str(session.root / LOCAL_ENV_DIR)
        try:
            probe = await run_probe(
                session.runner,
                python=session.sandbox_python,
                root=session.agent_root,
                inspect_env=True,
            )
        except ProbeError as exc:
            raise ToolError(f"could not find the active environment: {exc}") from exc
        if probe.env is None or not probe.env.python.prefix:
            raise ToolError("could not find the active environment; name one with env_path")
        return probe.env.python.prefix


def approval_text(steps: list[StepReport]) -> str:
    """What the shell gate judges and a person approves: each command as it
    will run, with the files it reads shown beneath it as comments."""
    lines: list[str] = []
    for step in steps:
        lines.append(step.command)
        for name, content in step.inputs.items():
            lines.append(f"# <{name}>:")
            lines.extend(f"#   {line}" for line in content.splitlines())
    return "\n".join(lines)


ENVIRONMENT_TOOLS: tuple[type[Tool[Any, Any]], ...] = (
    EnvironmentCaptureTool,
    EnvironmentRecreateTool,
)

__all__ = [
    "ENVIRONMENT_TOOLS",
    "EnvironmentCaptureInput",
    "EnvironmentCaptureResult",
    "EnvironmentCaptureTool",
    "EnvironmentRecreateInput",
    "EnvironmentRecreateResult",
    "EnvironmentRecreateTool",
    "SandboxRunner",
    "approval_text",
    "chat_environment",
    "chat_environment_for",
    "chat_launch",
    "kill_script",
    "pid_file",
    "sandbox_script",
    "workspace_aliases",
]
