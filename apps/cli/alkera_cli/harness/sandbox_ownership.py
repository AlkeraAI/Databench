"""Who owns the chat's trees on the host, and the steps that make it so.

Before every spawn the launch hands the chat's trees (its folder, its runtime
state) to the chat's uid and group, closes them to everyone else, keeps its
config root's and lends the uid a way through the ancestors. The steps are
composed here as data (:class:`~alkera_cli.harness.sandbox_steps.ShellStep`)
and run by :func:`~alkera_cli.harness.sandbox_steps.run_steps` with the rest
of the launch; the golden tests pin their exact argv.

None of them follows a link. The trees are the agent's to write, so a name
below them (or, on a box of an earlier build, an owned tree itself) may be a
link the agent left, and a ``mkdir -p``, ``chown -R`` or ``chmod -R`` that
followed it would act on a host tree as root. Nor may a name swapped for a link
while the step runs redirect it: the agent and the workspace's other processes
run while root repairs their trees, so the trees are walked from opened
directories and each entry changed through its own descriptor
(:class:`~alkera_cli.harness.sandbox_steps.RepairStep`), never by path.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Sequence
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Protocol, TypeVar

from alkera_cli.files.chat_fs import TreeIdentity
from alkera_cli.harness.sandbox_layout import ENVS_MOUNT, command_tmp_dir, host_path
from alkera_cli.harness.sandbox_steps import (
    RepairStep,
    SandboxRefusedError,
    ShellStep,
    Step,
    argv_text,
)

if TYPE_CHECKING:
    from alkera_cli.harness.sandbox import SandboxSpec

#: Makes each positional argument a directory unless it is a link, which is a
#: refusal: ``mkdir -p`` would make the link's target instead.
OWNED_MKDIR_SHELL = (
    'for d; do if [ -L "$d" ]; then echo "$d is a link" >&2; exit 1; fi; '
    'mkdir -p "$d" || exit 1; done'
)


#: The tree's group-shared umask: what the agent server, every command and
#: every kernel create is writable by the tree's group, since they share one
#: tree under different uids and must write each other's files.
SHARED_UMASK = 0o002
#: The umask of a workspace member on a ``none`` box, where no container keeps
#: the host's other uids out: the group still writes, everyone else gets
#: nothing.
SHARED_TREE_UMASK = 0o007
#: The name the umask wrapper's shell runs under (its ``$0``).
UMASK_ARGV0 = "alkera-umask"


def umask_shell(umask: int = SHARED_UMASK) -> str:
    """Starts the rest of the argv under ``umask``. A wrapper rather than the
    OCI ``process.user.umask``, which runsc was not shown to honour.
    Positional, never interpolated: ``"$@"`` is the command."""
    return f'umask {umask:03o}; exec "$@"'


#: The wrapper under the tree's group-shared umask (:data:`SHARED_UMASK`).
UMASK_SHELL = umask_shell()


def umask_prefix(sh: str = "/bin/sh", *, umask: int = SHARED_UMASK) -> tuple[str, ...]:
    """The argv that runs what follows it under ``umask`` (:func:`umask_shell`)."""
    return (sh, "-c", umask_shell(umask), UMASK_ARGV0)


def spec_umask(spec: SandboxSpec) -> int:
    """The umask a launch of ``spec`` runs its processes under: the member's
    stricter one on a ``none`` box (:attr:`SandboxSpec.share_gid`), the
    tree's group-shared one otherwise."""
    return SHARED_TREE_UMASK if spec.share_gid is not None else SHARED_UMASK


def tree_identity(spec: SandboxSpec) -> int:
    """The uid (and group: they are one number) that owns the workspace's
    shared trees: :attr:`SandboxSpec.share_gid` for a member on a ``none``
    box, which runs as its own uid, else the launch's own uid (a chat on its
    own, or a member under gVisor, which runs as the workspace's)."""
    return spec.share_gid if spec.share_gid is not None else spec.uid


def shared_trees(spec: SandboxSpec) -> tuple[Path, ...]:
    """The trees every process of the workspace writes, under different uids:
    the folder (the agents' root and the kernels' notebooks) and, where the
    members share them, the workspace's environments (bound at
    :data:`ENVS_MOUNT`). Owned by :func:`tree_identity`, group-shared and
    setgid, and repaired when their root is not (``RepairStep.when_wrong``). A member on a
    ``none`` box (:attr:`SandboxSpec.share_gid`) shares the folder alone: its
    environment is its own."""
    if spec.share_gid is not None:
        return (spec.folder,)
    envs = [b.source for b in spec.binds if b.kind == "owned" and b.destination == ENVS_MOUNT]
    return tuple(dict.fromkeys((spec.folder, *envs)))


def uid_prefix(spec: SandboxSpec, *, shared: bool = True) -> tuple[str, ...]:
    """Drop to the chat's uid with nothing kept: no capabilities, no way back
    up, and no group but, for a workspace member on a ``none`` box
    (:attr:`~alkera_cli.harness.sandbox.SandboxSpec.share_gid`), the
    workspace's, which is how it reaches the shared tree; ``shared`` False
    keeps even that out (the environment steps touch the member's own trees
    alone). ``--pdeathsig`` comes after the change because the kernel clears
    the parent-death signal on a credential change."""
    groups = (
        f"--groups={spec.share_gid}" if shared and spec.share_gid is not None else "--clear-groups"
    )
    return (
        spec.setpriv,
        f"--reuid={spec.uid}",
        f"--regid={spec.uid}",
        groups,
        "--inh-caps=-all",
        "--no-new-privs",
        "--pdeathsig",
        "KILL",
        "--",
    )


#: The directory an org worker's trees all sit under (its org root). Above it
#: nothing is the worker's to change, and the way through is the directories'
#: own modes, so no ancestor ACL is granted there.
ENV_TREES_FLOOR = "ALKERA_SANDBOX_TREES_FLOOR"


def trees_floor() -> Path | None:
    raw = os.environ.get(ENV_TREES_FLOOR, "").strip()
    return Path(raw) if raw.startswith("/") else None


def _ancestors(trees: tuple[Path, ...], floor: Path | None = None) -> list[str]:
    """Every directory above the trees, each once, the root left out, and
    with a ``floor`` only the floor and what lies beneath it."""
    found: list[str] = []
    for tree in trees:
        for parent in tree.parents:
            spelled = host_path(parent)
            if spelled == "/" or spelled in found:
                continue
            if floor is not None and parent != floor and floor not in parent.parents:
                continue
            found.append(spelled)
    return found


def uid_steps(spec: SandboxSpec) -> tuple[Step, ...]:
    """Own the chat's trees, close them to every other uid, and hand its config
    to it read-only.

    The daemon writes into the folder as root between spawns (a pull from the
    drive, a dropped file), so every step runs before every spawn. The owned
    trees (the folder, its runtime state) go to the chat's uid and group, open
    to both and closed to other (the chat tree's modes): every process in the
    sandbox shares the working tree, so the group bits are granted, never
    stripped. The config trees go to root, writable by root alone and readable
    by whoever can reach them, which through their closed ancestors is this uid
    alone (gVisor's sentry honours the mode bits it is shown, never a host
    ACL). The private directories are closed to group and other. Each ancestor
    of a bind source gets a traverse-only ACL for this one uid so a process of
    this uid can open the source on the host; no other uid gains anything, and
    nothing gains read on them.

    Order matters: a chmod rewrites an ACL's mask, so every chmod runs before
    the ACLs are granted."""
    shared = [host_path(p) for p in shared_trees(spec)]
    owned = [
        tree
        for tree in (host_path(p) for p in spec.folder_and_binds())
        if not _within(tree, shared)
    ]
    if spec.default_env is not None:
        # Where its commands keep their temporary files: made with the rest.
        owned.append(host_path(command_tmp_dir(spec.default_env)))
    config = [host_path(p) for p in spec.config_trees]
    private = [host_path(p) for p in spec.private_dirs]
    tree_owner = TreeIdentity(tree_identity(spec), tree_identity(spec))
    steps: list[Step] = [
        # A bind's source has to exist before runsc mounts it; the agent's data
        # directory is otherwise first made by the agent server itself.
        ShellStep(("/bin/sh", "-c", OWNED_MKDIR_SHELL, "mkdir-owned", *shared, *owned)),
        # The shared trees stay the workspace's, whoever this launch runs as.
        *(RepairStep(Path(tree), tree_owner, when_wrong=True) for tree in shared),
    ]
    # The launch's own trees are walked every time: the daemon writes into
    # them as root (the agent's secrets file) and hands them over here. A
    # sandbox with none (a workspace's kernel sandbox) walks nothing. A member
    # on a ``none`` box owns these alone.
    owner = TreeIdentity(spec.uid, spec.uid)
    steps += [RepairStep(Path(tree), owner) for tree in owned]
    if spec.share_gid is not None:
        # A member on a ``none`` box writes with its own primary group, so
        # every directory of the shared trees is setgid (the tree's directory
        # mode): what it makes there stays the workspace group's.
        steps += [RepairStep(Path(tree), None, files=False) for tree in shared]
    if config:
        steps.append(ShellStep(("chown", "-R", "0:0", *config)))
        steps.append(ShellStep(("chmod", "-R", "u=rwX,go=rX", *config)))
    if private:
        steps.append(ShellStep(("chmod", "go-rwx", *private)))
    ancestors = _ancestors((*spec.folder_and_binds(), *spec.config_trees), trees_floor())
    if ancestors:
        steps.append(ShellStep(("setfacl", "-m", f"u:{spec.uid}:x", *ancestors)))
    return tuple(steps)


def read_access_steps(spec: SandboxSpec) -> tuple[Step, ...]:
    """``none`` mode: lend the chat's uid the agent's own read-only trees at
    their host paths (the agent binary's directory, ``rg``'s).

    Under gVisor runsc mounts them into the container as root; with no
    container the agent opens them itself, as the chat's uid, through
    directories the daemon made under its own umask (closed to everyone else
    where that umask was 077). Each tree gets read-and-traverse for this one
    uid alone, its ancestors traverse only; no other uid gains anything."""
    if not spec.tool_trees:
        return ()
    trees = [host_path(p) for p in spec.tool_trees]
    ancestors = _ancestors(spec.tool_trees, trees_floor())
    steps: list[Step] = [ShellStep(("setfacl", "-R", "-m", f"u:{spec.uid}:rX", *trees))]
    if ancestors:
        steps.append(ShellStep(("setfacl", "-m", f"u:{spec.uid}:x", *ancestors)))
    return tuple(steps)


def agent_writable(spec: SandboxSpec) -> tuple[str, ...]:
    """Every host tree the agent can write, spelled as the host sees it: the
    folder, each owned bind (its own and those of a launch it runs inside)
    and the default environment."""
    trees: list[Path] = [
        *spec.folder_and_binds(),
        *(b.source for b in spec.inherited if b.kind == "owned"),
    ]
    if spec.default_env is not None:
        trees.append(spec.default_env)
    return tuple(dict.fromkeys(host_path(t) for t in trees))


def step_program(argv: Sequence[str], setpriv: str) -> tuple[str, ...]:
    """The program a host step runs and its arguments: the argv after the uid
    drop (``setpriv ... --``) and the ``env`` that sets its directory and
    names, when it has them."""
    rest = tuple(argv)
    if rest and rest[0] == setpriv and "--" in rest:
        rest = rest[rest.index("--") + 1 :]
    if len(rest) >= 4 and rest[1] == "-c" and rest[3] == UMASK_ARGV0:
        # The umask wrapper (:func:`umask_prefix`) execs what follows it.
        rest = rest[4:]
    if rest and PurePosixPath(rest[0]).name == "env":
        rest = rest[1:]
        while rest and (rest[0].startswith("-") or "=" in rest[0]):
            rest = rest[1:]
    return rest


def _within(path: str, trees: Iterable[str]) -> bool:
    spelled = host_path(Path(path))
    return any(spelled == tree or spelled.startswith(tree.rstrip("/") + "/") for tree in trees)


def _isolated_inline(program: Sequence[str]) -> bool:
    """Whether an interpreter's argv runs inline code isolated and without
    ``site``: ``-I`` and ``-S`` before ``-c``, so no ``.pth``, no script and
    no module under a tree the agent writes is loaded."""
    if "-c" not in program:
        return False
    flags = program[1 : program.index("-c")]
    return "-I" in flags and "-S" in flags


def refuse_agent_programs(spec: SandboxSpec, steps: Iterable[Step]) -> None:
    """Refuse a launch whose host-side steps would run what the agent can
    write. The steps run on the host, outside any gVisor boundary: a program
    under one of the chat's trees (an environment's ``bin/python``, a script
    in the folder) is whatever the agent made it, and an interpreter that is
    not isolated loads the ``.pth`` files and modules the agent left. Raises
    :class:`~alkera_cli.harness.sandbox_steps.SandboxRefusedError` naming the
    step."""
    trees = agent_writable(spec)
    for step in steps:
        if not isinstance(step, ShellStep):
            continue
        program = step_program(step.argv, spec.setpriv)
        if not program:
            continue
        if _within(program[0], trees):
            raise SandboxRefusedError(
                f"a sandbox step would run a program the agent can write: {argv_text(step.argv)}"
            )
        if PurePosixPath(program[0]).name.startswith("python") and not _isolated_inline(program):
            raise SandboxRefusedError(
                "a sandbox step would run an interpreter that loads what the agent can write: "
                f"{argv_text(step.argv)}"
            )


class _HasHostSteps(Protocol):
    @property
    def before(self) -> Any: ...

    @property
    def after_exit(self) -> Any: ...


L = TypeVar("L", bound=_HasHostSteps)


def host_steps_checked(spec: SandboxSpec, launch: L) -> L:
    """``launch``, once none of the steps it runs on the host, before the
    spawn or after the exit, runs what the agent can write
    (:func:`refuse_agent_programs`)."""
    refuse_agent_programs(spec, (*launch.before, *launch.after_exit))
    return launch


__all__ = [
    "ENV_TREES_FLOOR",
    "OWNED_MKDIR_SHELL",
    "SHARED_TREE_UMASK",
    "SHARED_UMASK",
    "UMASK_ARGV0",
    "UMASK_SHELL",
    "agent_writable",
    "host_steps_checked",
    "read_access_steps",
    "refuse_agent_programs",
    "shared_trees",
    "spec_umask",
    "step_program",
    "tree_identity",
    "trees_floor",
    "uid_prefix",
    "uid_steps",
    "umask_prefix",
    "umask_shell",
]
