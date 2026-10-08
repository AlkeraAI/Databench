"""Daemon version string assembly.

Returns a single string the daemon surfaces via `ping` and `info`, and that a
node reports in its heartbeat:

    "<package-version> (build <BUILD_ID>)"   if a build id is known
    "<package-version>"                       otherwise

The build id is, in order: `ALKERA_BUILD_ID` from the environment (an explicit
override), the build id baked into a compiled binary
(`alkera_cli.host.build_profile.BUILD_ID`, written by the binary build),
or `ALKERA_RELEASE_VERSION` — the release label a provisioned node installed,
written into its `node.env` by the bootstrap. The baked id outranks the label:
the label is what whoever installed the binary SAID it was, the baked id is what
the binary IS, and a node once ran a build three labels older than the one its
rig had written down. For dev runs from source all three are unset and we just
report the package version. The result is capped so it always fits the
heartbeat's `daemon_version` field.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from alkera_cli import __version__ as _package_version
from alkera_cli.host import build_profile as _build_profile

#: The heartbeat's ``daemon_version`` is ``max_length=32``; the report must fit.
MAX_LENGTH = 32


def build_id(env: Mapping[str, str] | None = None) -> str:
    """The build this process runs, or ``""`` when nothing names one."""
    source = os.environ if env is None else env
    return (
        source.get("ALKERA_BUILD_ID")
        or _build_profile.BUILD_ID
        or source.get("ALKERA_RELEASE_VERSION")
        or ""
    )


def daemon_version() -> str:
    build = build_id()
    if not build:
        return _package_version
    room = MAX_LENGTH - len(f"{_package_version} (build )")
    if room <= 0:
        return _package_version[:MAX_LENGTH]
    return f"{_package_version} (build {build[:room]})"


__all__ = ["MAX_LENGTH", "build_id", "daemon_version"]
