"""The container's view of a chat: where each of the chat's trees is seen.

A sandboxed chat's agent sees one root, the chat's working directory at
:data:`~alkera_cli.harness.sandbox.DEFAULT_HOME`, and one internal prefix,
:data:`INTERNAL_PREFIX`, under which everything else the agent server needs
is bound at a fixed name that says nothing about the chat: its Python
environments, its own data, config and state, its binary. The host layout
is not touched; this module spells where the container sees each host tree
(:class:`Bind`, :func:`chat_binds`, :func:`tool_binds`), spells a host path
the way the agent sees it (:func:`agent_view`), respells an environment the
host made for the path the agent runs it at (:data:`RELOCATE_SOURCE`, run
as the chat's uid by :func:`relocate_argv`), and takes down what an earlier
layout left inside the shared rootfs
(:func:`legacy_mountpoint_argv`). The launch in
:mod:`alkera_cli.harness.sandbox` mounts from these tables, and the cloud
mirror's fence takes its aliases from the same ones, so a name the agent
spells under an internal mount is judged as the host tree it reaches.

The names a launch derives from the chat (its user, slice, container and
network namespace) live here too; they are the same facts, read by the
launch, the memory reader and the box's firewall.
"""

from __future__ import annotations

import hashlib
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

#: The default Python environment every chat gets: a uv venv named ``alkera``
#: under the chat's own runtime state, made before the first spawn and active
#: (``VIRTUAL_ENV``, first on ``PATH``) for the agent and every command run on
#: its behalf. ``python``, ``pip install``, ``uv pip install`` and ``uv run``
#: all land in it without the model naming a path.
DEFAULT_ENV_NAME = "alkera"
#: Where the default environment (and uv's cache beside it) lives under the
#: chat's runtime state directory.
ENVS_SUBDIR = "envs"
#: The chat's runtime state directory, beside its folder inside the chat dir:
#: the agent server's own state (its session database under ``agent/``) and
#: the default environment (under ``envs/``). Never shown to the user and never
#: at the agent's root: under gVisor its parts are bound at fixed paths under
#: :data:`INTERNAL_PREFIX`, which the agent's tools refuse.
RUNTIME_STATE_SUBDIR = ".runtime"
#: The subdirectory of the runtime state the agent server keeps its own data in
#: (opencode's ``XDG_DATA_HOME/agent``): the session database and its logs.
AGENT_DATA_SUBDIR = "agent"

#: The one prefix every internal mount lives under inside the container. The
#: agent's root is :data:`DEFAULT_HOME`; everything else the agent server needs
#: (its Python environments, its own state, its binary) is bound under this
#: prefix and nowhere else, so the container holds the root, the OS, and this.
#: The box installs its managed interpreter under the same prefix
#: (:data:`DEFAULT_PYTHON_HOME`), on the host and in the rootfs alike.
INTERNAL_PREFIX = "/opt/alkera"
#: Where the chat's Python environments live for the agent: the default
#: environment at ``<ENVS_MOUNT>/alkera`` and the uv, pip and mamba caches
#: beside it. The one internal tree the agent's tools may read and write,
#: because ``pip install`` is the chat working in its own environment.
ENVS_MOUNT = f"{INTERNAL_PREFIX}/envs"
#: The agent server's own state: ``data`` (its session database), ``config``
#: (its agent definitions and instructions, read-only) and ``state`` (its cache
#: and the file it announces its port in). Refused to the agent's tools.
HARNESS_MOUNT = f"{INTERNAL_PREFIX}/harness"
HARNESS_DATA_MOUNT = f"{HARNESS_MOUNT}/data"
HARNESS_CONFIG_MOUNT = f"{HARNESS_MOUNT}/config"
HARNESS_STATE_MOUNT = f"{HARNESS_MOUNT}/state"
#: The agent binary's directory, and ``rg``'s when it ships apart from it.
AGENT_MOUNT = f"{INTERNAL_PREFIX}/agent"
RIPGREP_MOUNT = f"{INTERNAL_PREFIX}/rg"


# ---------------------------------------------------------------------------
# Naming
# ---------------------------------------------------------------------------


def chat_slug(chat_id: str) -> str:
    """The chat id as it appears in a user name and a unit name: lowercase
    ``[a-z0-9]``, at most 20 characters, a stable hash when the cleaned id is
    longer (so two long ids can never share a slug by truncation)."""
    cleaned = "".join(ch for ch in chat_id.lower() if ch.isalnum())
    if cleaned and len(cleaned) <= 20:
        return cleaned
    return hashlib.sha256(chat_id.encode("utf-8")).hexdigest()[:20]


def chat_user(chat_id: str) -> str:
    return f"alkera-chat-{chat_slug(chat_id)}"


def chat_slice(chat_id: str) -> str:
    return f"alkera-chat-{chat_slug(chat_id)}.slice"


def chat_container(chat_id: str) -> str:
    """The gVisor container id for a chat. The agent server's ``runsc run``
    creates it; a command run on the chat's behalf ``runsc exec``s into it, so
    the two share the container's kernel, uid and cgroup."""
    return f"alkera-chat-{chat_slug(chat_id)}"


def chat_netns(chat_id: str) -> str:
    """The chat's network namespace, as ``ip netns`` names it."""
    return f"alkera-chat-{chat_slug(chat_id)}"


def chat_cgroup_path(chat_id: str, root: Path = Path("/sys/fs/cgroup")) -> Path:
    return root / "alkera.slice" / f"chat-{chat_slug(chat_id)}"


def chat_slice_cgroup_path(chat_id: str, root: Path = Path("/sys/fs/cgroup")) -> Path:
    """Where systemd keeps the chat's slice: a dash in a slice name is a level
    of the tree, so ``alkera-chat-<slug>.slice`` sits under ``alkera-chat.slice``
    under ``alkera.slice``. The scope runsc opens for the container hangs under
    it, and the slice's ``memory.events`` counts what happened to it."""
    return root / "alkera.slice" / "alkera-chat.slice" / chat_slice(chat_id)


def chat_cgroup_dir(cgroup: str, chat_id: str, cgroup_root: Path) -> Path | None:
    """The cgroup v2 directory that holds a chat's processes under ``cgroup``
    (the box's driver): its slice under systemd, its own group under cgroupfs,
    ``None`` where the chat has no cgroup. The one answer the memory reader
    and the process probe both take."""
    if cgroup == "systemd":
        return chat_slice_cgroup_path(chat_id, cgroup_root)
    if cgroup == "cgroupfs":
        return chat_cgroup_path(chat_id, cgroup_root)
    return None


def default_env_path(state_dir: Path) -> Path:
    """Where the chat's default Python environment lives under its runtime
    state directory, on the host. Under gVisor the ``envs`` directory is bound
    at :data:`ENVS_MOUNT`, so the agent sees the same environment at
    ``<ENVS_MOUNT>/alkera``; :meth:`SandboxSpec.agent_path` does the spelling."""
    return state_dir / ENVS_SUBDIR / DEFAULT_ENV_NAME


#: The prefix of a workspace's custody key (``ws:<workspace id>``), the name a
#: member's sandbox scope carries as its tree. Spelled here because the
#: harness does not import the cloud layer that mints it; a test holds the two
#: together.
WORKSPACE_KEY_PREFIX = "ws:"
_SAFE_WORKSPACE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def workspace_dir_name(key: str) -> str:
    """The directory a workspace's own state is kept under: its id when the
    custody key carries a plain one, else a stable hash of the key (so no key,
    whatever it holds, names a path outside the root it is joined to)."""
    if key.startswith(WORKSPACE_KEY_PREFIX):
        workspace_id = key[len(WORKSPACE_KEY_PREFIX) :]
        if _SAFE_WORKSPACE_ID.match(workspace_id):
            return workspace_id
    return chat_slug(key)


def workspace_envs_dir(envs_root: Path, key: str) -> Path:
    """A workspace's environments on the host: ``<envs root>/<workspace id>``,
    holding the default environment and the caches beside it exactly as a
    chat's ``.runtime/envs`` does, and bound at :data:`ENVS_MOUNT` into every
    member's container and the workspace's kernel sandbox alike."""
    return envs_root / workspace_dir_name(key)


def envs_dir_for(*, state_dir: Path, workspace: str | None, envs_root: Path) -> Path:
    """The environments a session runs with: its workspace's when it is a
    member (``workspace`` is the workspace's custody key), its own
    ``.runtime/envs`` otherwise. A chat on its own (a workspace of one) keeps
    the layout it always had, so nothing about it moves."""
    if workspace:
        return workspace_envs_dir(envs_root, workspace)
    return state_dir / ENVS_SUBDIR


BindKind = Literal["owned", "config", "tool", "runtime"]


@dataclass(frozen=True, slots=True)
class Bind:
    """One host tree the container sees, and where.

    ``owned`` trees are the chat's: owned by its uid, closed to every other
    uid, bound read-write (its runtime state, its agent state). ``config``
    trees the agent reads and must never change: owned by root, readable,
    bound read-only, reachable by this uid alone through their closed
    ancestors; a chat that could write here could turn its own gate off.
    ``tool`` trees are the agent's own binaries (``opencode``, ``rg``), bound
    read-only and lent to the uid in ``none`` mode. ``runtime`` trees are bound
    as they are and never re-owned or re-moded by a launch: their owner keeps
    their modes (a kernel sandbox's per-kernel socket directories, each its
    kernel's alone).

    ``destination`` is the path the container sees the tree at. Under gVisor
    every internal tree lands under :data:`INTERNAL_PREFIX`, never at its host
    path, so the agent's view holds its root and that prefix and nothing of
    the box's layout. In ``none`` mode there is no container and the
    destination is informational: the agent sees the host path.
    """

    source: Path
    destination: str
    readonly: bool = False
    kind: BindKind = "owned"


def chat_binds(
    *,
    runtime_dir: Path,
    agent_config_root: Path,
    default_env: bool = True,
    envs_dir: Path | None = None,
) -> tuple[Bind, ...]:
    """The internal trees a sandboxed chat's agent server needs, and where
    the container sees each one: the one place the layout is spelled, so the
    launch that mounts them and the fence that judges their names agree.

    ``runtime_dir`` is the chat's runtime state (``<chat>/.runtime``):
    its ``agent/`` is the agent server's data, its ``envs/`` the default
    environment and the caches beside it. ``agent_config_root`` holds the
    agent's read-only ``config`` and its writable ``state``. ``default_env``
    False leaves the environments out, for a box with no interpreter to make
    one from. ``envs_dir`` names the environments when they are not the
    chat's own (a workspace member's, :func:`envs_dir_for`); they are seen at
    the same :data:`ENVS_MOUNT` either way.
    """
    binds = [
        Bind(runtime_dir / AGENT_DATA_SUBDIR, f"{HARNESS_DATA_MOUNT}/{AGENT_DATA_SUBDIR}"),
        Bind(agent_config_root / "state", HARNESS_STATE_MOUNT),
        Bind(agent_config_root / "config", HARNESS_CONFIG_MOUNT, readonly=True, kind="config"),
    ]
    if default_env:
        binds.insert(1, Bind(envs_dir or runtime_dir / ENVS_SUBDIR, ENVS_MOUNT))
    return tuple(binds)


def tool_binds(binary_dir: Path, *, ripgrep_dir: Path | None = None) -> tuple[Bind, ...]:
    """The agent's own binaries as the container sees them: the agent's
    directory at :data:`AGENT_MOUNT`, and ``rg``'s at :data:`RIPGREP_MOUNT`
    when it ships apart from the agent."""
    binds = [Bind(binary_dir, AGENT_MOUNT, readonly=True, kind="tool")]
    if ripgrep_dir is not None and ripgrep_dir != binary_dir:
        binds.append(Bind(ripgrep_dir, RIPGREP_MOUNT, readonly=True, kind="tool"))
    return tuple(binds)


def agent_binds(
    *,
    runtime_dir: Path,
    agent_config_root: Path,
    default_env: bool,
    binary_dir: Path,
    ripgrep_dir: Path | None,
    prefix_dirs: Sequence[Path] = (),
    envs_dir: Path | None = None,
) -> tuple[Bind, ...]:
    """Everything a chat's container sees beside the root: the agent server's
    data, config and state, the chat's Python environments, the agent's own
    binaries, and, for a development harness run from source, the directories
    its launcher names (those stay at their host paths, since bun cannot be
    moved under the prefix)."""
    binds = [
        *chat_binds(
            runtime_dir=runtime_dir,
            agent_config_root=agent_config_root,
            default_env=default_env,
            envs_dir=envs_dir,
        ),
        *tool_binds(binary_dir, ripgrep_dir=ripgrep_dir),
    ]
    binds += [
        Bind(d, host_path(d), readonly=True, kind="tool") for d in prefix_dirs if d != binary_dir
    ]
    return tuple(dict.fromkeys(binds))


def host_path(path: Path) -> str:
    """``path`` spelled for the Linux host the sandbox runs on. The argv is
    composed from ``Path`` values; ``str`` of one would take the composing
    platform's separator, which is never the one runsc, setfacl or chown read."""
    return path.as_posix()


def relative_to_container(path: Path, *, folder: Path, home: str) -> str | None:
    """``path`` spelled as the agent sees it, under ``home`` when it lies in
    the folder; ``None`` when it does not. Pure path arithmetic."""
    try:
        rest = PurePosixPath(host_path(path)).relative_to(PurePosixPath(host_path(folder)))
    except ValueError:
        return None
    return str(PurePosixPath(home) / rest)


def agent_view(
    host: Path,
    *,
    folder: Path,
    home: str,
    binds: Sequence[Bind],
    aliased: bool,
    relocates: bool,
) -> str:
    """``host`` spelled as the agent sees it: under ``home`` when it lies in
    the folder and the launch mounts the folder there (``aliased``), under a
    bind's destination when it lies in a bound tree and this is a container
    (``relocates``), and the host path itself everywhere else. The folder wins
    over a bind that contains it, and a deeper bind over a shallower one."""
    if aliased:
        inside = relative_to_container(host, folder=folder, home=home)
        if inside is not None:
            return inside
    if relocates:
        deepest = sorted(binds, key=lambda b: len(b.source.parts), reverse=True)
        for bind in deepest:
            spelled = relative_to_container(host, folder=bind.source, home=bind.destination)
            if spelled is not None:
                return spelled
    return host_path(host)


#: The program that respells an environment for the path the agent sees it at,
#: run by the managed interpreter isolated (``-I``) and without ``site``
#: (``-S``) as the chat's uid (:func:`relocate_argv`), never by the daemon in
#: its own process: the environment is the agent's to write, so nothing in it
#: (a ``.pth`` in ``site-packages``, a replaced script, a link in a file's
#: place) may run in, or be followed by, anything of the daemon's.
#:
#: A virtual environment is not relocatable by itself: ``ensurepip`` and
#: ``pip`` write each console script with a ``#!`` naming the interpreter by
#: the absolute path it ran at, ``activate`` writes the environment's path into
#: ``VIRTUAL_ENV``, and an editable install writes the project's path into a
#: ``.pth`` or an ``__editable__`` finder. The host makes the environment at
#: its host path and the agent runs it at :data:`ENVS_MOUNT`; the previous
#: build mounted it at its host path alone, and a ``none`` box runs it there
#: still. So a console script is not respelled from one path to another (a
#: box rolled back to the previous binary would find the new one missing and
#: every installed CLI failing with "bad interpreter") but rewritten into a
#: location-independent trampoline, the form ``uv venv --relocatable`` writes
#: itself: ``/bin/sh`` ``exec``s the interpreter beside the script, whatever
#: the environment is mounted at. The program takes ``env`` and ``old new``
#: pairs and rewrites: a console script's ``#!`` line when it names an
#: interpreter under the environment by any of its spellings (its own and the
#: pairs'), every spelling in an ``activate`` script, a ``.pth`` or
#: ``__editable__`` file in ``site-packages`` (the folder an earlier build
#: also showed at its host path is the root alone now, and the previous build
#: mounts the root's spelling too). Idempotent, so it runs on every spawn and
#: after every exit, and an environment any build made keeps working on the
#: next, whichever way the build moved. Only plain files are read or written,
#: by their own name with no link followed; a link (the environment itself
#: included), a directory in a file's place, or anything unreadable is left
#: alone. Prints how many files changed.
RELOCATE_SOURCE = """\
import os
import stat
import sys

NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


def plain(path):
    try:
        return stat.S_ISREG(os.lstat(path).st_mode)
    except OSError:
        return False


def rewrite(path, change):
    if not plain(path):
        return 0
    try:
        with os.fdopen(os.open(path, os.O_RDONLY | NOFOLLOW), "rb") as f:
            data = f.read()
        new = change(data)
        if new == data:
            return 0
        with os.fdopen(os.open(path, os.O_WRONLY | os.O_TRUNC | NOFOLLOW), "wb") as f:
            f.write(new)
    except OSError:
        return 0
    return 1


def entries(directory):
    if os.path.islink(directory):
        return []
    try:
        return sorted(os.listdir(directory))
    except OSError:
        return []


def spellings_of(env, pairs):
    found = [env.encode()]
    for old, new in pairs:
        for other, mine in ((new, old), (old, new)):
            if mine == env and other.encode() not in found:
                found.append(other.encode())
    return found


def trampoline(interpreter, flags):
    where = '"$(dirname -- "$(realpath -- "$0" 2>/dev/null || printf %s "$0")")"'
    line = "'''exec' " + where + "/" + interpreter + flags + ' "$0" "$@"\\n'
    return ("#!/bin/sh\\n" + line + "' '''\\n").encode()


def relocate(env, pairs):
    if os.path.islink(env):
        return 0
    names = spellings_of(env, pairs)
    pairs = [(o.encode(), n.encode()) for o, n in pairs if o != n]

    def everywhere(data):
        for old, new in pairs:
            data = data.replace(old, new)
        return data

    def shebang(data):
        if not data.startswith(b"#!"):
            return data
        cut = data.find(b"\\n")
        first, rest = (data, b"") if cut < 0 else (data[:cut], data[cut + 1 :])
        words = first[2:].split()
        for spelling in names:
            prefix = spelling + b"/bin/"
            if words and words[0].startswith(prefix):
                interpreter = words[0][len(prefix) :]
                if not interpreter or b"/" in interpreter:
                    return data
                flags = b"".join(b" " + word for word in words[1:])
                return trampoline(interpreter.decode(), flags.decode()) + rest
        return data

    changed = 0
    bin_dir = os.path.join(env, "bin")
    for name in entries(bin_dir):
        change = everywhere if name.startswith("activate") else shebang
        changed += rewrite(os.path.join(bin_dir, name), change)
    lib = os.path.join(env, "lib")
    for version in entries(lib):
        site = os.path.join(lib, version, "site-packages")
        for name in entries(site):
            if name.endswith(".pth") or name.startswith("__editable__"):
                changed += rewrite(os.path.join(site, name), everywhere)
    return changed


if __name__ == "__main__":
    rest = sys.argv[2:]
    print(relocate(sys.argv[1], list(zip(rest[0::2], rest[1::2]))))
"""


def relocate_argv(python: str, env: str, pairs: Sequence[tuple[str, str]]) -> tuple[str, ...]:
    """The argv that runs :data:`RELOCATE_SOURCE` over ``env`` with ``python``:
    isolated and without ``site``, so nothing under the environment runs in
    it, and with each ``(old, new)`` pair as two arguments after the
    environment."""
    flat = tuple(spelling for pair in pairs for spelling in pair)
    return (python, "-I", "-S", "-c", RELOCATE_SOURCE, env, *flat)


#: The container's own temporary directory: a private tmpfs the agent server's
#: scratch goes to (the runtime it is built with unpacks its native pieces
#: into ``TMPDIR`` at start, and in the root those would sync to the drive).
#: coreutils' ``env`` in the rootfs: an exec'd command starts from ``env -i``.
CONTAINER_ENV = "/usr/bin/env"
CONTAINER_TMP = "/tmp"  # noqa: S108 -- the container's private tmpfs, not a host path
#: Where a command run on the chat's behalf keeps its temporary files: beside
#: the default environment and its caches, on disk, outside the working tree.
#: In the working tree an installer's scratch (pip's build and wheel cache
#: folders) synced to the drive and then landed in the owner's Trash.
COMMAND_TMP_SUBDIR = ".tmp"


def command_tmp_dir(default_env: Path) -> Path:
    """The host directory a command's temporary files go to, beside
    ``default_env``."""
    return default_env.parent / COMMAND_TMP_SUBDIR


#: The shell line that takes down an earlier build's mountpoints inside the
#: shared rootfs: each positional argument that still exists as a directory is
#: removed with its empty parents, and the line always exits 0, because nothing
#: to take down is the usual case and not a failed step. Positional, never
#: interpolated.
_LEGACY_MOUNTPOINTS_SHELL = (
    'for d; do [ -d "$d" ] && rmdir -p --ignore-fail-on-non-empty "$d"; done; exit 0'
)


def legacy_mountpoint_argv(
    rootfs: Path | None, binds: Sequence[Bind], folder: Path
) -> tuple[str, ...] | None:
    """The command that takes down what an earlier build's mounts left inside
    the shared ``rootfs``, or ``None`` when there is nothing to run.

    runsc makes a bind's mountpoint in the rootfs on the host, and the staged
    rootfs is shared by every chat on the box, so the build that bound each
    tree at its host path left the chat folder's and the agent root's names
    there as empty directories, one set per chat ever run, readable by every
    later chat. The new layout binds under fixed names that say nothing about
    a chat; this takes the old ones down: each moved tree and its parent (the
    earlier build bound the runtime state whole, where its parts are bound
    now, and ``rmdir -p`` climbs only from an operand that exists), empty
    directories only, since ``rmdir`` can never remove a file or a directory
    with anything in it, and never when the rootfs is the host's own root.
    """
    if rootfs is None or rootfs == Path(rootfs.anchor):
        return None
    inside = PurePosixPath(host_path(rootfs))
    moved = [bind.source for bind in binds if bind.destination != host_path(bind.source)]
    moved.append(folder)
    relocated: list[str] = []
    for tree in moved:
        for candidate in (tree, tree.parent):
            spelled = host_path(candidate)
            if spelled != "/" and spelled not in relocated:
                relocated.append(spelled)
    operands = [str(inside / path.lstrip("/")) for path in relocated]
    return ("/bin/sh", "-c", _LEGACY_MOUNTPOINTS_SHELL, "alkera-legacy-mountpoints", *operands)


def python_ro_binds(
    *,
    executable: str = sys.executable,
    prefixes: Sequence[str] = (sys.prefix, sys.base_prefix),
    packages: Sequence[str] = (),
) -> tuple[Path, ...]:
    """The host trees a command that runs THIS interpreter needs read-only: the
    interpreter's directory, its prefixes, and the roots of the packages the
    child imports. A tool that runs our own Python on the chat's behalf (the
    graph snippet, the integration SDK) is wrapped like the shell and needs
    these; a plain shell command never reaches them for anything else."""
    roots: list[Path] = [Path(executable).resolve().parent]
    roots += [Path(prefix) for prefix in prefixes if prefix]
    roots += [Path(pkg).resolve().parent for pkg in packages if pkg]
    unique: list[Path] = []
    for root in roots:
        if str(root) == "/" or root in unique:
            continue
        unique.append(root)
    return tuple(unique)


__all__ = [
    "AGENT_DATA_SUBDIR",
    "AGENT_MOUNT",
    "CONTAINER_ENV",
    "CONTAINER_TMP",
    "DEFAULT_ENV_NAME",
    "ENVS_MOUNT",
    "ENVS_SUBDIR",
    "HARNESS_CONFIG_MOUNT",
    "HARNESS_DATA_MOUNT",
    "HARNESS_MOUNT",
    "HARNESS_STATE_MOUNT",
    "INTERNAL_PREFIX",
    "RELOCATE_SOURCE",
    "RIPGREP_MOUNT",
    "RUNTIME_STATE_SUBDIR",
    "WORKSPACE_KEY_PREFIX",
    "Bind",
    "BindKind",
    "agent_binds",
    "agent_view",
    "chat_binds",
    "chat_cgroup_dir",
    "chat_cgroup_path",
    "chat_container",
    "chat_netns",
    "chat_slice",
    "chat_slice_cgroup_path",
    "chat_slug",
    "chat_user",
    "default_env_path",
    "envs_dir_for",
    "host_path",
    "legacy_mountpoint_argv",
    "python_ro_binds",
    "relative_to_container",
    "relocate_argv",
    "tool_binds",
    "workspace_dir_name",
    "workspace_envs_dir",
]
