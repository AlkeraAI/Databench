"""Widget JavaScript, content-addressed.

Three kinds of asset reach an output frame, always as text the parent page
posts in: platform bundles (``@alkera/widgets``, which also carries
``@alkera/ui-widgets``), environment assets (a widget library's own
``share/jupyter/nbextensions/<module>/index.js`` from the notebook's
environment, read here and kept as bytes, never served by path), and large
anywidget ``_esm`` / ``_css`` values the hub moved out of model state.

A notebook may fetch only hashes that were offered for it, plus platform
bundles; a module named like a platform bundle is never accepted from an
environment.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from alkera_notebook.tree_io import Tree

AssetKind = Literal["platform", "environment", "value"]

ASSET_REF_PREFIX = "alkera-asset:sha256:"
ASSET_VERSION = "asset"

PLATFORM_MODULES = frozenset(
    {
        "@alkera/widgets",
        "@alkera/ui-widgets",
        "@jupyter-widgets/base",
        "@jupyter-widgets/controls",
        "@jupyter-widgets/output",
        "anywidget",
    }
)

_MODULE_NAME = re.compile(r"^(@[a-z0-9][\w.-]*/)?[a-z0-9][\w.-]*$", re.I)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ASSET_PATH = re.compile(r"^[\w.-]+(/[\w.-]+)*$")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def workspace_scope(org_id: str, workspace_id: str) -> str:
    """The key environment assets are stored under on the platform."""
    return f"org:{org_id}/workspace:{workspace_id}"


def asset_ref(sha: str) -> str:
    return f"{ASSET_REF_PREFIX}{sha}"


def parse_asset_ref(value: str) -> str | None:
    if not value.startswith(ASSET_REF_PREFIX):
        return None
    sha = value[len(ASSET_REF_PREFIX) :]
    return sha if _SHA256.match(sha) else None


@dataclass(frozen=True)
class AssetEntry:
    name: str
    version: str
    sha256: str
    bytes: int
    kind: AssetKind


class BlobStore(Protocol):
    """Where asset bytes live, keyed by a scope and the SHA-256. The open core
    scopes by notebook root; the platform by (org, workspace)."""

    def put(self, scope: str, data: bytes) -> str: ...

    def get(self, scope: str, sha: str) -> bytes | None: ...


class FileBlobStore:
    """Blobs under ``<root>/<scope digest>/<sha256>``, written atomically."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._tree = Tree(root)

    @staticmethod
    def _dir(scope: str) -> str:
        return hashlib.sha256(scope.encode()).hexdigest()[:32]

    def put(self, scope: str, data: bytes) -> str:
        sha = sha256_hex(data)
        target = f"{self._dir(scope)}/{sha}"
        if not self._tree.exists(target):
            self._tree.write_atomic(target, data)
        return sha

    def get(self, scope: str, sha: str) -> bytes | None:
        if not _SHA256.match(sha):
            return None
        try:
            data = self._tree.read_bytes(f"{self._dir(scope)}/{sha}")
        except FileNotFoundError:
            return None
        # A blob that no longer matches its name is not served.
        return data if sha256_hex(data) == sha else None


class MemoryBlobStore:
    def __init__(self) -> None:
        self._blobs: dict[tuple[str, str], bytes] = {}

    def put(self, scope: str, data: bytes) -> str:
        sha = sha256_hex(data)
        self._blobs[(scope, sha)] = data
        return sha

    def get(self, scope: str, sha: str) -> bytes | None:
        return self._blobs.get((scope, sha))


class AssetRefusedError(Exception):
    pass


@dataclass(frozen=True)
class KernelAssetFile:
    """One file of a ``widget.asset`` notification: its path inside
    ``nbextensions/<module>/``, the hash the kernel named, and the bytes
    that arrived as a segment."""

    path: str
    sha256: str
    data: bytes


def _check_environment_module(module: str) -> None:
    if module in PLATFORM_MODULES:
        raise AssetRefusedError(f"{module} is provided by the platform, not by an environment")
    if not _MODULE_NAME.match(module) or ".." in module:
        raise AssetRefusedError(f"{module!r} is not a widget module name")


class WidgetAssets:
    """The asset side of one engine: platform bundles shared by every
    notebook, environment and value assets offered per notebook."""

    def __init__(self, store: BlobStore, *, platform_scope: str = "platform") -> None:
        self.store = store
        self._platform_scope = platform_scope
        self._platform: dict[str, AssetEntry] = {}
        # notebook scope -> sha -> entry
        self._offered: dict[str, dict[str, AssetEntry]] = {}
        # notebook scope -> module name -> entry (environment assets)
        self._modules: dict[str, dict[str, AssetEntry]] = {}
        # (scope, sha) -> the store scope holding it, when not the scope itself
        self._stored_in: dict[tuple[str, str], str] = {}
        # scope -> module name -> version -> entry (assets the kernel sent)
        self._versions: dict[str, dict[str, dict[str, AssetEntry]]] = {}

    # ---------------------------------------------------------------- platform

    def add_platform_bundle(self, name: str, version: str, data: bytes) -> AssetEntry:
        if name not in PLATFORM_MODULES:
            raise AssetRefusedError(f"{name} is not a platform bundle")
        sha = self.store.put(self._platform_scope, data)
        entry = AssetEntry(name, version, sha, len(data), "platform")
        self._platform[name] = entry
        return entry

    def load_platform_dist(self, dist: Path) -> AssetEntry:
        """Registers the built ``@alkera/widgets`` bundle from its dist folder,
        checking the manifest's hash against the file."""
        manifest = json.loads((dist / "manifest.json").read_text(encoding="utf-8"))
        data = (dist / manifest["file"]).read_bytes()
        if sha256_hex(data) != manifest["sha256"]:
            raise AssetRefusedError("the widget bundle does not match its manifest")
        return self.add_platform_bundle(manifest["name"], manifest["version"], data)

    def platform(self, name: str) -> AssetEntry | None:
        return self._platform.get(name)

    # ---------------------------------------------------------------- per notebook

    def offer_value(self, scope: str, name: str, data: bytes) -> AssetEntry:
        """A large model value (anywidget ``_esm`` or ``_css``) moved out of
        state; the frame asks for it by its reference."""
        sha = self.store.put(scope, data)
        entry = AssetEntry(name, ASSET_VERSION, sha, len(data), "value")
        self._offered.setdefault(scope, {})[sha] = entry
        return entry

    def offer_environment_module(
        self, scope: str, env_prefix: Path, module: str
    ) -> AssetEntry | None:
        """Reads ``<env>/share/jupyter/nbextensions/<module>/index.js`` once
        and offers it to the notebook as bytes. Returns ``None`` when the
        environment has no such file."""
        _check_environment_module(module)
        known = self._modules.get(scope, {}).get(module)
        if known is not None:
            return known
        share = env_prefix / "share" / "jupyter"
        index = share / "nbextensions" / module / "index.js"
        try:
            resolved = index.resolve(strict=True)
        except FileNotFoundError:
            return None
        if not resolved.is_relative_to((share / "nbextensions").resolve()):
            raise AssetRefusedError(f"{module} resolves outside the environment's nbextensions")
        data = resolved.read_bytes()
        version = _labextension_version(share, module)
        sha = self.store.put(scope, data)
        entry = AssetEntry(module, version, sha, len(data), "environment")
        self._offered.setdefault(scope, {})[sha] = entry
        self._modules.setdefault(scope, {})[module] = entry
        return entry

    def resolve_module(
        self, scope: str, name: str, env_prefix: Path | None
    ) -> tuple[AssetEntry, bytes] | None:
        """Answers a frame's ``need_module``: a value reference, a platform
        bundle, or an environment module (offered on first ask)."""
        sha = parse_asset_ref(name)
        if sha is not None:
            entry = self._offered.get(scope, {}).get(sha)
            data = self._get(scope, sha) if entry else None
            return (entry, data) if entry is not None and data is not None else None
        platform = self._platform.get(name)
        if platform is not None:
            data = self.store.get(self._platform_scope, platform.sha256)
            return (platform, data) if data is not None else None
        if name in PLATFORM_MODULES:
            return None
        entry = self._modules.get(scope, {}).get(name)
        if entry is None and env_prefix is not None:
            entry = self.offer_environment_module(scope, env_prefix, name)
        if entry is None:
            return None
        data = self._get(scope, entry.sha256)
        return (entry, data) if data is not None else None

    # ---------------------------------------------------------------- from the kernel

    def offer_kernel_asset(
        self,
        scope: str,
        module: str,
        version: str,
        files: Sequence[KernelAssetFile],
        *,
        store_scope: str | None = None,
    ) -> AssetEntry:
        """Stores what the kernel's ``widget.asset`` notification carried: a
        library's ``nbextensions/<module>/`` files, each checked against the
        hash the kernel named. The entry point is ``index.js``; the other
        files (AMD chunks) are offered beside it, fetchable by hash only.
        ``scope`` is the notebook whose kernel offered them (only it may
        fetch them); the bytes are stored under ``store_scope``, the
        (org, workspace) key of :func:`workspace_scope`, so notebooks of
        one workspace share one copy."""
        _check_environment_module(module)
        if not version or len(version) > 128:
            raise AssetRefusedError("a widget asset needs a version")
        entry_sha: str | None = None
        checked: list[tuple[str, bytes]] = []
        for file in files:
            if not _ASSET_PATH.match(file.path) or ".." in file.path.split("/"):
                raise AssetRefusedError(f"{file.path!r} is not a path inside the module")
            if not _SHA256.match(file.sha256) or sha256_hex(file.data) != file.sha256:
                raise AssetRefusedError(f"{file.path} does not match its hash")
            checked.append((file.path, file.data))
            if file.path == "index.js":
                entry_sha = file.sha256
        if entry_sha is None:
            raise AssetRefusedError(f"{module} {version} has no index.js")
        offered = self._offered.setdefault(scope, {})
        stored_in = store_scope or scope
        for path, data in checked:
            sha = self.store.put(stored_in, data)
            self._stored_in[(scope, sha)] = stored_in
            if path != "index.js":
                offered.setdefault(
                    sha, AssetEntry(f"{module}/{path}", version, sha, len(data), "environment")
                )
        entry = AssetEntry(
            module, version, entry_sha, len(dict(checked)["index.js"]), "environment"
        )
        offered[entry_sha] = entry
        self._versions.setdefault(scope, {}).setdefault(module, {})[version] = entry
        self._modules.setdefault(scope, {})[module] = entry
        return entry

    def resolve(self, scope: str, module: str, version: str | None = None) -> str | None:
        """The ``GET .../widget-assets/resolve`` answer: the hash of a
        platform bundle, or of a module this scope's kernel offered. The
        frame's version is a semver range from the model, so an exact
        offered version wins and otherwise the one most recently offered."""
        platform = self._platform.get(module)
        if platform is not None:
            return platform.sha256
        if module in PLATFORM_MODULES:
            return None
        versions = self._versions.get(scope, {}).get(module)
        if not versions:
            known = self._modules.get(scope, {}).get(module)
            return known.sha256 if known is not None else None
        exact = versions.get(version) if version else None
        return (exact or self._modules[scope][module]).sha256

    def fetch(self, scope: str, sha: str) -> bytes | None:
        """The bytes behind a hash, only if this notebook was offered it or it
        is a platform bundle (the asset route's check)."""
        if any(e.sha256 == sha for e in self._platform.values()):
            return self.store.get(self._platform_scope, sha)
        if sha not in self._offered.get(scope, {}):
            return None
        return self._get(scope, sha)

    def _get(self, scope: str, sha: str) -> bytes | None:
        return self.store.get(self._stored_in.get((scope, sha), scope), sha)

    def offered(self, scope: str) -> Iterable[AssetEntry]:
        return list(self._offered.get(scope, {}).values())


def _labextension_version(share: Path, module: str) -> str:
    """The installed version, when the library also ships a labextension
    (its package.json names it); ``env`` otherwise."""
    try:
        meta = json.loads(
            (share / "labextensions" / module / "package.json").read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return "env"
    version = meta.get("version")
    return version if isinstance(version, str) else "env"
