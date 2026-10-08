"""Spawning a harness child: :mod:`alkera_core.process` owns the mechanism;
this adapter puts a sandbox launch in front of it.

A sandbox launch goes INSIDE the death-signal launcher, so the whole chain
(the ``runsc`` container under gVisor, or cgroup placement, the uid drop and
the agent with no boundary) dies with the daemon, and the child starts in the
sandbox's working directory (the bundle directory under gVisor) under the
launch's umask where it names one.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from alkera_core.process import ErrorSpec, OutputSpec, SpawnSpec, launch_argv

from alkera_cli.harness.sandbox import SandboxLaunch


def sandbox_command(argv: Sequence[str], sandbox: SandboxLaunch | None) -> list[str]:
    """The argument list the OS runs for the harness child ``argv``: the
    death-signal launcher, then the sandbox's wrapper, then ``argv``."""
    return launch_argv(argv if sandbox is None else sandbox.wrap(argv))


def sandbox_spec(
    argv: Sequence[str],
    *,
    sandbox: SandboxLaunch | None,
    env: Mapping[str, str],
    stdout: OutputSpec = "inherit",
    stderr: ErrorSpec = "inherit",
) -> SpawnSpec:
    """The spawn spec for the harness child ``argv`` inside ``sandbox``."""
    if sandbox is None:
        return SpawnSpec(argv=list(argv), env=env, stdout=stdout, stderr=stderr)
    return SpawnSpec(
        argv=sandbox.wrap(argv),
        env=env,
        cwd=sandbox.cwd.as_posix() if sandbox.cwd is not None else None,
        umask=sandbox.umask,
        stdout=stdout,
        stderr=stderr,
    )


__all__ = ["sandbox_command", "sandbox_spec"]
