"""What the backend and a box build may assume of each other.

The backend and the box ship separately: a box runs whatever release its
bootstrap installed, which can be older or newer than the backend it talks to.
This module is the one place both sides read the contract between them from.

:class:`BoxCapability` is the vocabulary a box uses to say, on every
heartbeat, what it can do beyond running a chat. A backend feature gate names
a capability, never a version, and both sides import the same enum: a new
capability is a new member here, used by the box that reports it and the gate
that reads it.

:class:`BoxBuild` is what one released build can do: the commands its CLI has,
the capabilities it can report, and the version of the environment contract
(the names and meanings of the variables the bootstrap writes for it) it
speaks. A release build prints it and the release
pipeline publishes it beside the build's tarball, so the backend can ask a
build what it has before it tells a machine to run it.

The bootstrap renderer resolves the build a machine will install (the pinned
version, else the environment's node channel, else the stable release:
:func:`resolve_release`), reads its manifest, and asks :func:`bootstrap_plan`
how the box starts. Every box starts with :data:`START_COMMAND`, which every
build has, and the mode it runs in (:class:`StartMode`) rides in the node's
environment: a build with the supervisor hands over to it, and a build without
one runs the single daemon. So a build swapped in place, newer or older, is
never asked for a command it lacks, and an upgrade reaches the supervisor
without the unit being rewritten. A build published before manifests existed is read as
:data:`MIN_SUPPORTED_BOX`'s committed manifest, the oldest build the backend
still serves.

:func:`admit_claim` is the other half: a box whose reported version is below
:data:`MIN_SUPPORTED_BOX` is refused at claim and heartbeat with a stable
code, so a machine that cannot be served fails at once instead of timing out.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Final, TypeGuard


class BoxCapability(StrEnum):
    """What a box says it can do, restated on every heartbeat.

    A box that does not name a capability is treated as unable to do it, so a
    box on an older build is never asked for something it cannot do.
    """

    #: It runs every chat of a shared workspace in one sandbox under one lease
    #: on the workspace's folder, with the shared tree as their home.
    WORKSPACES = "workspaces"
    #: It answers the machine channel's ``flush``: it pushes everything it
    #: holds of a folder and says when the push ended.
    FOLDER_FLUSH_V1 = "folder_flush_v1"
    #: It runs each org's chats in a worker process of that org's own, on a
    #: credential bound to that org.
    ORG_WORKERS = "org_workers"
    #: It keeps each org's worker apart from every other's (its own user and
    #: namespaces), so a second org may be placed on it. A box says this only
    #: from what its isolation probe found, never from a list its build carries.
    ORG_ISOLATION = "org_isolation"
    #: It moves a running agent to a new model on the next turn rather than
    #: after the agent restarts.
    MODEL_SWITCH_V1 = "model_switch_v1"
    #: It grows its volume's filesystem, and each org's share of it, when the
    #: provider grows the volume under it while it runs.
    DISK_GROW_ONLINE = "disk_grow_online"
    #: It has at least one GPU.
    GPU = "gpu"
    #: Its sandbox can hand a GPU to a chat or a kernel.
    GPU_PASSTHROUGH = "gpu_passthrough"


#: The version of the environment contract between the bootstrap and the box:
#: the variables the bootstrap writes into ``node.env`` and the unit, and what
#: each means. Raised when one of them is renamed or changes meaning, so a
#: build that reads the old meaning is never booted with the new one.
ENV_CONTRACT: Final = 1

#: The schema of the manifest document a build prints and the release
#: publishes.
MANIFEST_SCHEMA: Final = 1
#: Where the release publishes a build's manifest, under the release base:
#: beside the Linux tarball every node can install (the command tree is the
#: same on every target).
BOX_MANIFEST_PATH: Final = "cli/v{version}/linux-x64/box-manifest.json"


#: The commands the bootstrap may start a box with, best first: the
#: supervisor (one worker process per org), else the single daemon every build
#: since the first release has.
SUPERVISE_COMMAND: Final = "cloud-mirror supervise"
RUN_COMMAND: Final = "cloud-mirror run"
BOX_COMMANDS: Final = (SUPERVISE_COMMAND, RUN_COMMAND)
#: The one command a bootstrap starts a box with: every build has it.
START_COMMAND: Final = RUN_COMMAND
#: Where the node's environment says which :class:`StartMode` the box runs.
START_MODE_ENV: Final = "ALKERA_BOX_START_MODE"


class StartMode(StrEnum):
    """How a box runs once :data:`START_COMMAND` starts it."""

    #: A worker process per org under the supervisor, on a build that has
    #: it; a build that does not runs the single daemon.
    SUPERVISE = "supervise"
    #: The single daemon, one process for every chat on the box.
    SINGLE = "single"


#: The oldest box build the backend serves. A claim or heartbeat from an older
#: one is refused (:func:`admit_claim`), and a build published without a
#: manifest is read as this one's. Raising it means committing that version's
#: manifest under ``box_manifests/`` (a test refuses the constant without it).
MIN_SUPPORTED_BOX: Final = "0.5.0"
#: The committed manifests of builds published before builds printed their
#: own, beside this module (a test pins them to the release tooling's copies).
BASELINE_MANIFESTS: Final = Path(__file__).with_name("box_manifests")

#: What the bootstrap installs for a box that runs the developer's source tree
#: rather than a release (the ``localdev`` provider).
SOURCE_VERSION: Final = "source"

#: The stable pointer and an environment's node channel, under the release
#: base, both in ``{"version": ...}`` shape. The deploy writes the channel
#: (the release tooling's box publish step); a test pins the two spellings.
STABLE_CHANNEL_PATH: Final = "cli/stable.json"
NODE_CHANNEL_PATH: Final = "cli/nodes/{channel}.json"

#: The refusal code and the words a machine's row carries when its box is too
#: old to serve.
BOX_TOO_OLD: Final = "box_too_old"
BOX_TOO_OLD_MESSAGE: Final = "This machine's software is too old. Replace it to update."

_VERSION_RE = re.compile(r"^[0-9A-Za-z.-]{1,64}$")
_CHANNEL_RE = re.compile(r"^[a-z0-9-]+$")
_LEADING_VERSION_RE = re.compile(r"^(\d+)(?:\.(\d+))?(?:\.(\d+))?")


class BoxContractError(ValueError):
    """The backend cannot tell a machine to run this build."""


class BoxManifestError(BoxContractError):
    """A manifest document that does not describe a build."""


class ReleaseHostError(Exception):
    """The release host could not be read (unreachable, or an error answer).
    Transient: the same read may succeed later."""


@dataclass(frozen=True)
class BoxBuild:
    """What one build of the box can do."""

    #: The package version the build was made from (``alkera --version``).
    version: str
    #: Every command path its CLI has, space-joined (``"cloud-mirror run"``).
    commands: frozenset[str]
    #: The capabilities the build can report on a heartbeat.
    capabilities: frozenset[BoxCapability]
    #: The environment contract the build reads (:data:`ENV_CONTRACT`).
    env_contract: int

    def to_manifest(self) -> dict[str, Any]:
        """The manifest document, with every list sorted so it diffs cleanly."""
        return {
            "schema": MANIFEST_SCHEMA,
            "version": self.version,
            "commands": sorted(self.commands),
            "capabilities": sorted(cap.value for cap in self.capabilities),
            "env_contract": self.env_contract,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_manifest(), indent=2) + "\n"

    @classmethod
    def from_manifest(cls, doc: Mapping[str, Any]) -> BoxBuild:
        """Read a manifest document. A capability this backend does not know
        (a newer build's) is left out: nothing here can gate on it."""
        version = doc.get("version")
        commands = doc.get("commands")
        capabilities = doc.get("capabilities")
        env_contract = doc.get("env_contract")
        if not isinstance(version, str) or not version:
            raise BoxManifestError("a box manifest names no version")
        if not _strings(commands) or not _strings(capabilities):
            raise BoxManifestError(
                f"the {version} manifest's commands or capabilities are not names"
            )
        if isinstance(env_contract, bool) or not isinstance(env_contract, int):
            raise BoxManifestError(f"the {version} manifest names no environment contract")
        known = {cap.value for cap in BoxCapability}
        return cls(
            version=version,
            commands=frozenset(commands),
            capabilities=frozenset(BoxCapability(c) for c in capabilities if c in known),
            env_contract=env_contract,
        )


def _strings(value: object) -> TypeGuard[list[str]]:
    return isinstance(value, list) and all(isinstance(item, str) and item for item in value)


def source_build() -> BoxBuild:
    """The build a box running the developer's source tree has: the same
    checkout as this backend, so every command and capability it knows. The
    CLI's manifest test proves the tree has every command in
    :data:`BOX_COMMANDS`."""
    return BoxBuild(
        version=SOURCE_VERSION,
        commands=frozenset(BOX_COMMANDS),
        capabilities=frozenset(BoxCapability),
        env_contract=ENV_CONTRACT,
    )


def baseline_build(version: str = MIN_SUPPORTED_BOX) -> BoxBuild:
    """The committed manifest of a build published before builds printed one."""
    path = BASELINE_MANIFESTS / f"{version}.json"
    if not path.is_file():
        raise BoxManifestError(f"no committed box manifest for {version}")
    return BoxBuild.from_manifest(json.loads(path.read_text(encoding="utf-8")))


@dataclass(frozen=True)
class BootstrapPlan:
    """What the bootstrap tells a machine to install and run."""

    #: The release label it installs (``cli/v<version>/``), or
    #: :data:`SOURCE_VERSION`.
    version: str
    #: How the box runs once :data:`START_COMMAND` starts it.
    start_mode: StartMode


def bootstrap_plan(version: str, build: BoxBuild, *, wanted: StartMode) -> BootstrapPlan:
    """Install ``version`` (whose manifest is ``build``) and run it ``wanted``,
    or as the single daemon when the build has no supervisor. Refuses a build
    that reads another environment contract or lacks :data:`START_COMMAND`."""
    if not _VERSION_RE.match(version):
        raise BoxContractError(f"refusing box version {version!r}")
    if build.env_contract != ENV_CONTRACT:
        raise BoxContractError(
            f"the {version} build reads environment contract {build.env_contract}, "
            f"this backend writes {ENV_CONTRACT}"
        )
    if START_COMMAND not in build.commands:
        raise BoxContractError(f"the {version} build has no command a box starts with")
    supervises = wanted is StartMode.SUPERVISE and SUPERVISE_COMMAND in build.commands
    return BootstrapPlan(
        version=version, start_mode=StartMode.SUPERVISE if supervises else StartMode.SINGLE
    )


#: Read one JSON document under the release base: the document, ``None`` when
#: the host has none at that path, or :class:`ReleaseHostError`.
FetchJson = Callable[[str], Awaitable[Mapping[str, Any] | None]]


async def resolve_version(fetch: FetchJson, *, pinned: str, channel: str) -> str:
    """The release a machine installs: ``pinned``, else the build the
    environment's node channel names, else the stable release. A channel no
    deploy has written yet (no document, or one naming no version) falls to
    the stable release; a host that cannot be read is an error, never a
    reason to install a different build."""
    if pinned:
        return _checked_version(pinned)
    if channel:
        if not _CHANNEL_RE.match(channel):
            raise BoxContractError(f"refusing release channel {channel!r}")
        named = _named_version(await fetch(NODE_CHANNEL_PATH.format(channel=channel)))
        if named:
            return _checked_version(named)
    named = _named_version(await fetch(STABLE_CHANNEL_PATH))
    if not named:
        raise BoxContractError("the stable release pointer names no version")
    return _checked_version(named)


async def resolve_release(
    fetch: FetchJson, *, pinned: str, channel: str, wanted: StartMode
) -> BootstrapPlan:
    """The plan for a machine that installs a released build."""
    version = await resolve_version(fetch, pinned=pinned, channel=channel)
    doc = await fetch(BOX_MANIFEST_PATH.format(version=version))
    build = BoxBuild.from_manifest(doc) if doc is not None else baseline_build()
    return bootstrap_plan(version, build, wanted=wanted)


def _named_version(doc: Mapping[str, Any] | None) -> str:
    version = (doc or {}).get("version")
    return version if isinstance(version, str) else ""


def _checked_version(version: str) -> str:
    if not _VERSION_RE.match(version):
        raise BoxContractError(f"refusing box version {version!r}")
    return version


def version_key(reported: str) -> tuple[int, int, int] | None:
    """The leading ``major.minor.patch`` of a reported daemon version
    (``"0.5.0 (build 951dac943d65)"`` is ``(0, 5, 0)``), or ``None`` when it
    does not start with a number."""
    match = _LEADING_VERSION_RE.match(reported.strip())
    if match is None:
        return None
    major, minor, patch = (int(part) if part else 0 for part in match.groups())
    return (major, minor, patch)


@dataclass(frozen=True)
class Admission:
    """Whether a box may claim or keep beating, and why not."""

    admitted: bool
    code: str = ""
    message: str = ""


def admit_claim(reported_version: str | None) -> Admission:
    """Admit a box whose reported version is :data:`MIN_SUPPORTED_BOX` or
    newer. A box that reports no version (an empty claim field, a heartbeat
    that leaves it out) or one that does not start with a number is admitted:
    nothing says it is too old, and every build reports its package version."""
    key = version_key(reported_version or "")
    minimum = version_key(MIN_SUPPORTED_BOX)
    if key is None or minimum is None or key >= minimum:
        return Admission(admitted=True)
    return Admission(admitted=False, code=BOX_TOO_OLD, message=BOX_TOO_OLD_MESSAGE)


__all__ = [
    "BASELINE_MANIFESTS",
    "BOX_COMMANDS",
    "BOX_MANIFEST_PATH",
    "BOX_TOO_OLD",
    "BOX_TOO_OLD_MESSAGE",
    "ENV_CONTRACT",
    "MANIFEST_SCHEMA",
    "MIN_SUPPORTED_BOX",
    "NODE_CHANNEL_PATH",
    "RUN_COMMAND",
    "SOURCE_VERSION",
    "STABLE_CHANNEL_PATH",
    "START_COMMAND",
    "START_MODE_ENV",
    "SUPERVISE_COMMAND",
    "Admission",
    "BootstrapPlan",
    "BoxBuild",
    "BoxCapability",
    "BoxContractError",
    "BoxManifestError",
    "FetchJson",
    "ReleaseHostError",
    "StartMode",
    "admit_claim",
    "baseline_build",
    "bootstrap_plan",
    "resolve_release",
    "resolve_version",
    "source_build",
    "version_key",
]
