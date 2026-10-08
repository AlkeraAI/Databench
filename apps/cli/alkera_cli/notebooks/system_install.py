"""``system.install``: a system package for a workspace's notebook kernels.

The installer runs as root in the workspace's **kernel sandbox**, with exactly
the capabilities a package manager needs (``CAP_SETUID``, ``CAP_SETGID``,
``CAP_CHOWN``, ``CAP_DAC_OVERRIDE``, ``CAP_FOWNER``) and no other. It never
runs in a container that holds an agent server: ``CAP_SETUID`` alone is
enough to become another uid and read a same-container agent's environment.
That is a property of the type here: the installer is given a
:class:`~alkera_cli.notebooks.kernel_sandbox.KernelSandbox` and nothing else.

What it installs lands in the sandbox's own root overlay: visible to every
kernel of the workspace, invisible to its agents (each container has its own
overlay), and gone when the sandbox stops, which the workspace's sleep does.
Results say so.

Package names are checked against the managers' own rule for a name
(``^[a-z0-9][a-z0-9+.-]*$``): no option, no path, no version expression.
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Final, Literal

from alkera_core.process import SpawnSpec, run

from alkera_cli.notebooks.kernel_sandbox import KernelSandbox, exec_argv

Manager = Literal["apt", "apk"]
MANAGERS: Final[tuple[Manager, ...]] = ("apt", "apk")

PACKAGE_NAME: Final = re.compile(r"^[a-z0-9][a-z0-9+.-]*$")
#: At most this many packages in one request.
MAX_PACKAGES: Final = 50
#: Exactly the capabilities an installer is given.
INSTALL_CAPS: Final = (
    "CAP_SETUID",
    "CAP_SETGID",
    "CAP_CHOWN",
    "CAP_DAC_OVERRIDE",
    "CAP_FOWNER",
)
#: The installer's whole environment.
INSTALL_ENV: Final = {
    "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    "HOME": "/root",
    "LANG": "C.UTF-8",
    "DEBIAN_FRONTEND": "noninteractive",
}
#: What a result says about where the packages are.
INSTALLED_NOTE: Final = "Installed until this workspace sleeps, visible to notebook kernels."
#: How long one install may run.
INSTALL_TIMEOUT_SECONDS: Final = 600.0


class InstallRefusedError(ValueError):
    """The request names no manager this build knows, or a name that is not a
    package name."""


def checked_packages(packages: Sequence[str]) -> tuple[str, ...]:
    """``packages`` when every one is a package name; the first that is not
    is refused by name. Duplicates are dropped, order kept."""
    if not packages:
        raise InstallRefusedError("name at least one package")
    if len(packages) > MAX_PACKAGES:
        raise InstallRefusedError(f"at most {MAX_PACKAGES} packages in one install")
    for name in packages:
        if not isinstance(name, str) or not PACKAGE_NAME.match(name):
            raise InstallRefusedError(f"{name!r} is not a package name")
    return tuple(dict.fromkeys(packages))


def install_commands(manager: str, packages: Sequence[str]) -> tuple[tuple[str, ...], ...]:
    """The package manager's commands, in order."""
    names = checked_packages(packages)
    if manager == "apt":
        return (
            ("apt-get", "update", "-qq"),
            ("apt-get", "install", "-y", "-qq", "--no-install-recommends", *names),
        )
    if manager == "apk":
        return (("apk", "add", "--no-cache", *names),)
    raise InstallRefusedError(f"{manager!r} is not a package manager this box runs")


def install_argvs(sandbox: KernelSandbox, manager: str, packages: Sequence[str]) -> list[list[str]]:
    """Each command as the ``runsc exec`` that runs it in the kernel sandbox:
    as root, with exactly :data:`INSTALL_CAPS`, from ``/``, with exactly
    :data:`INSTALL_ENV`."""
    return [
        exec_argv(sandbox.target, (0, 0), command, env=INSTALL_ENV, cwd="/", caps=INSTALL_CAPS)
        for command in install_commands(manager, packages)
    ]


@dataclass(frozen=True, slots=True)
class InstallResult:
    ok: bool
    manager: str
    packages: tuple[str, ...]
    note: str
    output: str


Run = Callable[[Sequence[str]], subprocess.CompletedProcess[bytes]]


def _run(argv: Sequence[str]) -> subprocess.CompletedProcess[bytes]:
    return run(
        SpawnSpec(argv=list(argv), env=os.environ, stdout="pipe", stderr="pipe"),
        timeout=INSTALL_TIMEOUT_SECONDS,
    )


class SystemInstaller:
    """``system.install`` for one workspace's kernel sandbox."""

    def __init__(self, sandbox: KernelSandbox, *, run: Run = _run) -> None:
        self.sandbox = sandbox
        self._run = run

    def install(self, manager: str, packages: Sequence[str]) -> InstallResult:
        """Install ``packages`` with ``manager``, starting the sandbox if it is
        not up. Refuses a bad request before anything runs."""
        argvs = install_argvs(self.sandbox, manager, packages)
        names = checked_packages(packages)
        self.sandbox.ensure_running()
        output: list[str] = []
        for argv, command in zip(argvs, install_commands(manager, names), strict=True):
            done = self._run(argv)
            text = (done.stdout + done.stderr).decode("utf-8", "replace")
            output.append(text[-4000:])
            if done.returncode != 0:
                output.append(f"\n{' '.join(command[:2])} exited {done.returncode}\n")
                return InstallResult(False, manager, names, INSTALLED_NOTE, "".join(output))
        return InstallResult(True, manager, names, INSTALLED_NOTE, "".join(output))


__all__ = [
    "INSTALLED_NOTE",
    "INSTALL_CAPS",
    "INSTALL_ENV",
    "MANAGERS",
    "MAX_PACKAGES",
    "PACKAGE_NAME",
    "InstallRefusedError",
    "InstallResult",
    "Manager",
    "SystemInstaller",
    "checked_packages",
    "install_argvs",
    "install_commands",
]
