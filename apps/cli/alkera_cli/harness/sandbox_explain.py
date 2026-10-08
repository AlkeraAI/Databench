"""What the box says about a sandbox launch it refused or that failed: the
wrapper in front of the agent's own argv, and one line naming the mode, what
the probe found and how the launch was composed. Never the environment, never
a secret."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from alkera_cli.harness.sandbox_layout import CONTAINER_ENV

if TYPE_CHECKING:
    from alkera_cli.harness.sandbox import SandboxLaunch, SandboxMode, SandboxSpec


def wrapper_argv(command: Sequence[str], agent_argv: Sequence[str]) -> tuple[str, ...]:
    """The part of a spawned ``command`` in front of the agent's own argv: the
    death-signal launcher, the cgroup placement, the alias, the uid drop. A
    command that does not end in the agent's argv (the gVisor ``runsc run``,
    whose agent argv is in the OCI config) is the wrapper whole. Never the
    environment: a launch that carries it (``runsc exec``, through ``env -i``)
    puts it after the wrapper, and the agent's argv itself is not repeated."""
    command = tuple(command)
    agent = tuple(agent_argv)
    if agent and command[len(command) - len(agent) :] == agent:
        command = command[: len(command) - len(agent)]
    for at in range(len(command) - 1):
        if command[at] == CONTAINER_ENV and command[at + 1] == "-i":
            return command[:at]
    return command


def explain_sandbox(
    *,
    mode: SandboxMode,
    host: str,
    spec: SandboxSpec | None = None,
    launch: SandboxLaunch | None = None,
    wrapper: Sequence[str] = (),
) -> str:
    """One line for the reader of a refusal or a failed start: the mode the box
    is set to, what the probe found on the host, and — once a chat's launch
    exists — its cgroup driver, its uid, the path the agent sees its folder at,
    and the wrapper argv in front of the agent binary. Never the environment,
    never a secret: the wrapper is argv the box composed from paths and ids."""
    parts = [f"sandbox mode={mode}"]
    if spec is not None:
        alias = launch.agent_home if launch is not None else None
        parts.append(f"cgroup={spec.cgroup}")
        parts.append(f"uid={spec.uid}")
        parts.append(f"alias={alias or 'none'}")
    line = ", ".join(parts) + f"; host: {host}"
    if wrapper:
        line += "; wrapper: " + " ".join(wrapper)
    elif launch is not None:
        line += "; wrapper: none"
    return line


__all__ = ["explain_sandbox", "wrapper_argv"]
