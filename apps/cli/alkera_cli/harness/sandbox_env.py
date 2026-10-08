"""The chat's default Python environment: the steps that make it and keep it
running wherever a build mounts it.

The environment is the agent's to write (``pip install`` lands there), and
nothing the daemon runs may execute or follow what the agent can write: a
``.pth`` planted in ``site-packages`` runs in every interpreter that starts
there, a replaced ``bin/python`` is whatever the agent made it, a link in the
environment's place leads wherever it points. So every step here runs under
the chat's uid (:func:`~alkera_cli.harness.sandbox_ownership.uid_prefix`),
reaching nowhere the agent's own commands could not, and every program a step
runs is one the box installed: ``uv`` or the managed interpreter, isolated
(``-I``) and without ``site`` (``-S``), never the environment's own
``bin/python``. The steps run on the host, outside the gVisor boundary, so a
program the agent planted would run there with the host's kernel and the
daemon's network in reach; :func:`~alkera_cli.harness.sandbox_ownership.
refuse_agent_programs` refuses any launch that would. The steps are composed
as data and run with the rest of the launch; :mod:`alkera_cli.harness.sandbox`
places them before the spawn and after the exit.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Final

from alkera_cli.harness.sandbox_layout import (
    DEFAULT_ENV_NAME,
    envs_dir_for,
    host_path,
    relocate_argv,
)
from alkera_cli.harness.sandbox_ownership import (
    spec_umask,
    trees_floor,
    uid_prefix,
    umask_prefix,
)
from alkera_cli.harness.sandbox_scope import scope_of
from alkera_cli.harness.sandbox_steps import ShellStep, Step
from alkera_cli.host import paths

if TYPE_CHECKING:
    from alkera_cli.harness.sandbox import SandboxSpec

#: Overrides where workspaces' environments are kept (an absolute path).
ENV_WORKSPACE_ENVS_ROOT: Final = "ALKERA_WORKSPACE_ENVS_ROOT"
#: The directory under the org's root (or under ``ALKERA_HOME`` on a box with
#: no org workers) that holds one environments directory per workspace.
WORKSPACE_ENVS_SUBDIR: Final = "envs"


def workspace_envs_root(env: Mapping[str, str] | None = None) -> Path:
    """Where this process keeps its workspaces' environments: the override when
    one is set, else ``envs/`` under the org's root (an org worker's every tree
    sits under it), else ``envs/`` under ``ALKERA_HOME``."""
    source = os.environ if env is None else env
    raw = source.get(ENV_WORKSPACE_ENVS_ROOT, "").strip()
    if raw.startswith("/"):
        return Path(raw)
    floor = trees_floor()
    if floor is not None:
        return floor / WORKSPACE_ENVS_SUBDIR
    return paths.ALKERA_HOME / WORKSPACE_ENVS_SUBDIR


def shares_workspace_envs(mode: str | None = None) -> bool:
    """Whether a workspace's members share its environments on this box: only
    where every member runs as the workspace's uid in a container of its own
    (gVisor). On a ``none`` box each member runs as its own uid and only the
    folder is shared, so a member's environment (what its agent's interpreter
    loads) stays its own and another member cannot plant code in it."""
    if mode is None:
        from alkera_cli.harness.sandbox import ENV_MODE

        mode = os.environ.get(ENV_MODE, "").strip() or "none"
    return mode != "none"


def session_envs_dir(session_id: str, state_dir: Path, *, mode: str | None = None) -> Path:
    """The environments ``session_id`` runs with, on the host: its workspace's
    when it is a member on a box whose members share them
    (:func:`shares_workspace_envs`; the one directory every member's container
    and the workspace's kernel sandbox bind), its own ``<state_dir>/envs``
    otherwise. ``mode`` is the box's sandbox mode (read from the environment
    when not given)."""
    scope = scope_of(session_id)
    shared = scope.member and shares_workspace_envs(mode)
    return envs_dir_for(
        state_dir=state_dir,
        workspace=scope.tree if shared else None,
        envs_root=workspace_envs_root(),
    )


def session_default_env(session_id: str, state_dir: Path) -> Path:
    """The default environment ``session_id`` runs with (:func:`session_envs_dir`)."""
    return session_envs_dir(session_id, state_dir) / DEFAULT_ENV_NAME


#: Seeds ``pip`` into an environment from the managed interpreter's own
#: bundled wheel, run by that interpreter isolated and without ``site`` as the
#: chat's uid (:func:`seed_pip_argv`). ``python -m ensurepip`` would have to
#: be run by the environment's interpreter, which is the agent's to replace
#: and starts every ``.pth`` the agent leaves in ``site-packages``; here
#: nothing under the environment runs or is imported. pip is imported from the
#: wheel and installs that same wheel with the environment as its prefix, so
#: the layout is the one a venv's own pip writes (``lib/pythonX.Y/
#: site-packages``, ``bin``). ``sys.executable`` is set to the environment's
#: interpreter only so the console scripts' ``#!`` names it (the relocation
#: then makes them location-independent); nothing executes it. ``--isolated``
#: keeps pip from reading configuration from the environment variables or the
#: home the step runs with (beside the environment, the agent's to write).
#: An environment that already holds pip is left alone, as ``ensurepip``
#: leaves one. Without ``ENSUREPIP_OPTIONS`` pip lays down ``pip`` as well as
#: ``pip3`` and ``pip3.X``, so a shell tool that types ``pip`` finds it.
SEED_PIP_SOURCE: Final = """\
import glob
import os
import sys

env = sys.argv[1]
version = "python%d.%d" % sys.version_info[:2]
site = os.path.join(env, "lib", version, "site-packages")
if glob.glob(os.path.join(glob.escape(site), "pip-*.dist-info")):
    sys.exit(0)
import ensurepip

bundled = os.path.join(os.path.dirname(ensurepip.__file__), "_bundled")
wheels = sorted(glob.glob(os.path.join(glob.escape(bundled), "pip-*.whl")))
if not wheels:
    print("the interpreter bundles no pip wheel", file=sys.stderr)
    sys.exit(1)
sys.path.insert(0, wheels[-1])
os.environ.pop("ENSUREPIP_OPTIONS", None)
sys.executable = os.path.join(env, "bin", "python")
from pip._internal.cli.main import main

sys.exit(
    main(
        [
            "--isolated",
            "--disable-pip-version-check",
            "install",
            "--no-index",
            "--no-cache-dir",
            "--no-deps",
            "--ignore-installed",
            "--no-warn-script-location",
            "--prefix",
            env,
            wheels[-1],
        ]
    )
)
"""


def seed_pip_argv(python: str, env: str) -> tuple[str, ...]:
    """The argv that runs :data:`SEED_PIP_SOURCE` over ``env`` with the
    box's managed ``python``: isolated and without ``site``, so nothing under
    the environment is run or imported."""
    return (python, "-I", "-S", "-c", SEED_PIP_SOURCE, env)


def env_steps(spec: SandboxSpec) -> tuple[Step, ...]:
    """Make the chat's default Python environment, as the chat's uid, once the
    trees are its (the ownership steps run first).

    ``uv venv --allow-existing`` is idempotent: a second spawn keeps what the
    first installed. ``--relocatable`` makes what uv itself writes into the
    environment independent of its path, since the agent runs the environment
    at :data:`~alkera_cli.harness.sandbox_layout.ENVS_MOUNT` and not where the
    host made it. ``pip`` is seeded from the managed interpreter's bundled
    wheel (:data:`SEED_PIP_SOURCE`), so it needs no network and runs nothing
    of the environment's. Neither is checked (a chat without its default
    environment still runs, and the prompt names the environment only when it
    exists), but both run on every spawn so a wiped one comes back. The
    scripts pip (this seed, or an earlier spawn's ``pip install``) wrote with
    the host path are made relocatable last (:func:`env_exit_steps`)."""
    if spec.default_env is None:
        return ()
    env = host_path(spec.default_env)
    python = f"{spec.python_home}/bin/python3"
    as_uid = _env_prefix(spec)
    return (
        ShellStep(
            (
                *as_uid,
                spec.uv,
                "venv",
                "--quiet",
                "--allow-existing",
                "--relocatable",
                "--prompt",
                DEFAULT_ENV_NAME,
                "--python",
                python,
                env,
            ),
            check=False,
        ),
        ShellStep((*as_uid, *seed_pip_argv(python, env)), check=False),
        *env_exit_steps(spec),
    )


def _env_prefix(spec: SandboxSpec) -> tuple[str, ...]:
    """The uid drop an environment step runs under, entered beside the
    environment: the daemon's working directory, home and cache are closed to
    the uid (pip lists its working directory and fails on one it cannot read),
    so the step runs from there, with a home and uv's cache there too."""
    beside = host_path(spec.default_env.parent) if spec.default_env is not None else "/"
    return (
        # A member on a ``none`` box keeps no group: its environment is its
        # own. Elsewhere the environment may be the workspace's, so what a
        # step installs stays writable by the group every member and kernel
        # shares.
        *uid_prefix(spec, shared=False),
        *umask_prefix(umask=spec_umask(spec)),
        "/usr/bin/env",
        f"--chdir={beside}",
        f"HOME={beside}",
        f"UV_CACHE_DIR={beside}/.uv-cache",
    )


def env_exit_steps(spec: SandboxSpec) -> tuple[Step, ...]:
    """The relocation (:data:`~alkera_cli.harness.sandbox_layout.RELOCATE_SOURCE`),
    run as the chat's uid by the managed interpreter, isolated and without
    ``site`` so nothing in the environment runs in it: on every spawn, so an
    environment any build made works on this one, and again after the agent
    server exits, so a script ``pip`` installed during the session (with the
    absolute shebang pip always writes) is made relocatable before a box could
    be rolled back to a build that mounts the environment elsewhere. The pairs
    name what moved: the environment, where the agent runs it at another path
    than the host made it, and the folder an earlier build also showed at its
    host path (the root alone now); an environment that did not move still gets
    its scripts rewritten into the location-independent form."""
    if spec.default_env is None:
        return ()
    env = host_path(spec.default_env)
    python = f"{spec.python_home}/bin/python3"
    pairs: list[tuple[str, str]] = []
    spelled = spec.agent_path(spec.default_env)
    if spelled != env:
        pairs.append((env, spelled))
    folder, seen = host_path(spec.folder), spec.agent_path(spec.folder)
    if seen != folder:
        pairs.append((folder, seen))
    return (ShellStep((*_env_prefix(spec), *relocate_argv(python, env, pairs)), check=False),)


__all__ = [
    "ENV_WORKSPACE_ENVS_ROOT",
    "SEED_PIP_SOURCE",
    "WORKSPACE_ENVS_SUBDIR",
    "env_exit_steps",
    "env_steps",
    "seed_pip_argv",
    "session_default_env",
    "session_envs_dir",
    "shares_workspace_envs",
    "workspace_envs_root",
]
