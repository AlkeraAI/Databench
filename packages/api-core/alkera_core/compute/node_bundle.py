"""The node bundles this deployment holds: built from source by
``scripts/build-node-bundle.sh`` into ``NODE_BUNDLE_DIR``, served to nodes by
the backend and uploaded to SSH-attached hosts by their provider.

The directory holds ``manifest.json`` (``{"version", "targets": {target:
{"file", "sha256", "size"}}}``) and the files it names. Nothing here trusts a
file the manifest does not name: a target's file must sit directly in the
directory, and its digest is the manifest's.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

#: The targets a bundle is built for, and the machine names (``uname -m``)
#: that select each.
TARGETS: tuple[str, ...] = ("linux-arm64", "linux-x64")
_MACHINES: dict[str, str] = {
    "x86_64": "linux-x64",
    "amd64": "linux-x64",
    "aarch64": "linux-arm64",
    "arm64": "linux-arm64",
}
MANIFEST = "manifest.json"
#: Each target by the architecture name an image build takes
#: (``NODE_BUNDLE_ARCHES``), which is what an admin rebuilds with.
_BUILD_ARCH: dict[str, str] = {"linux-x64": "amd64", "linux-arm64": "arm64"}


class NodeBundleError(Exception):
    """The deployment holds no usable bundle for what was asked."""


@dataclass(frozen=True)
class BundleFile:
    target: str
    path: Path
    sha256: str
    size: int
    version: str

    @property
    def sidecar(self) -> str:
        """The ``.sha256`` sidecar's contents, as ``sha256sum`` writes it."""
        return f"{self.sha256}  {self.path.name}\n"


def target_for_machine(machine: str) -> str:
    """The bundle target for a host's ``uname -m``; refuses one with none."""
    target = _MACHINES.get(machine.strip().lower())
    if target is None:
        raise NodeBundleError(f"no node bundle is built for {machine!r}")
    return target


def bundle_file(directory: str, target: str) -> BundleFile:
    """The bundle for ``target`` in ``directory``, as its manifest names it."""
    if target not in TARGETS:
        raise NodeBundleError(f"no node bundle target {target!r}")
    if not directory:
        raise NodeBundleError("this deployment holds no node bundle (NODE_BUNDLE_DIR is unset)")
    root = Path(directory)
    try:
        manifest = json.loads((root / MANIFEST).read_text(encoding="utf-8"))
        entry = manifest["targets"][target]
        name = str(entry["file"])
        sha = str(entry["sha256"]).lower()
        size = int(entry["size"])
        version = str(manifest["version"])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise NodeBundleError(f"no node bundle for {target} in {directory}") from exc
    if Path(name).name != name or len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
        raise NodeBundleError(f"the node bundle manifest in {directory} is malformed")
    path = root / name
    if not path.is_file():
        raise NodeBundleError(f"the node bundle {name} is missing from {directory}")
    return BundleFile(target=target, path=path, sha256=sha, size=size, version=version)


def held_targets(directory: str) -> list[str]:
    """The targets ``directory`` holds a usable bundle for, in :data:`TARGETS` order."""
    held: list[str] = []
    for target in TARGETS:
        try:
            bundle_file(directory, target)
        except NodeBundleError:
            continue
        held.append(target)
    return held


def install_refusal(directory: str, machine: str) -> str | None:
    """Why a host whose ``uname -m`` is ``machine`` cannot be installed from the
    bundles in ``directory``, in words for the admin; ``None`` when it can.

    Judged only where the deployment serves its own bundles (``directory`` is
    set). A deployment that installs from a release has no directory to
    consult, and gets ``None``."""
    if not directory:
        return None
    shown = machine.strip() or "an unknown architecture"
    try:
        target = target_for_machine(machine)
    except NodeBundleError:
        return f"This server is {shown}. Machines run on x86_64 or arm64 (aarch64) only."
    held = held_targets(directory)
    if target in held:
        return None
    rebuild = (
        "Rebuild the backend and worker images with NODE_BUNDLE_ARCHES=all, "
        f"or build the {target} bundle into NODE_BUNDLE_DIR on an {_BUILD_ARCH[target]} computer."
    )
    if not held:
        return f"This server is {shown}, but this install has no machine bundle. {rebuild}"
    only = " and ".join(_BUILD_ARCH[t] for t in held)
    return f"This server is {shown}, but this install only has the {only} machine bundle. {rebuild}"


__all__ = [
    "MANIFEST",
    "TARGETS",
    "BundleFile",
    "NodeBundleError",
    "bundle_file",
    "held_targets",
    "install_refusal",
    "target_for_machine",
]
