"""Offering a widget library's own JavaScript to the engine.

When the kernel opens a comm for a widget whose ``_model_module`` or
``_view_module`` is not provided by the platform, it reads that library's
``share/jupyter/nbextensions/<module>/`` from the kernel's environment and
sends it once as a ``widget.asset`` notification::

    {"module": "bqplot", "version": "0.12.45",
     "files": [{"path": "index.js", "sha256": "...", "bytes": 1234,
                "data": <segment>}, ...],
     "omitted": ["index.js.map"]}

``data`` is the file's bytes as a segment (codec ``bytes``); ``path`` is
POSIX, relative to the module's folder; ``omitted`` (present only when not
empty) names files left out because the notification would have outgrown a
frame. ``version`` is the installed version when the library also ships a
labextension (its ``package.json``), else the version the widget model
declares, else ``*``. The AMD dependencies named in ``index.js`` are offered
the same way, transitively, each once per module and version.

Platform modules are never read from an environment: the engine refuses a
module named like a platform bundle, and the frame has its own copy.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from . import _frames as f

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
PLATFORM_SCOPES = ("@jupyter-widgets/",)
#: What one notification may carry: a kernel frame is at most 16 MiB, and the
#: header needs room beside the file bytes.
NOTIFICATION_BUDGET = f.FRAME_LIMIT_CLIENT - 256 * 1024
#: AMD names that are not modules to fetch.
_AMD_BUILTINS = frozenset({"require", "exports", "module"})
_MODULE_NAME = re.compile(r"^(@[a-z0-9][\w.-]*/)?[a-z0-9][\w.-]*$", re.IGNORECASE)
_DEFINE = re.compile(r"""\bdefine\(\s*(?:(["'])[^"']*\1\s*,\s*)?\[([^\]]*)\]""")
_STRING = re.compile(r"""(["'])([^"']+)\1""")


def is_platform_module(module: str) -> bool:
    return module in PLATFORM_MODULES or module.startswith(PLATFORM_SCOPES)


def amd_dependencies(code: str) -> list[str]:
    """The module names an AMD bundle's ``define`` calls depend on, in order
    of first appearance (``require``, ``exports``, ``module`` and relative
    names excluded)."""
    out: list[str] = []
    for match in _DEFINE.finditer(code):
        for _, name in _STRING.findall(match.group(2)):
            if name in _AMD_BUILTINS or name.startswith(".") or name in out:
                continue
            out.append(name)
    return out


@dataclass(frozen=True)
class LibraryFiles:
    """One module's folder as read from the environment."""

    module: str
    installed_version: str | None
    files: list[tuple[str, bytes]]
    dependencies: list[str]


def default_nbextensions_dir() -> str:
    return os.path.join(sys.prefix, "share", "jupyter", "nbextensions")


def read_library(share_jupyter: str, module: str) -> LibraryFiles | None:
    """``<share_jupyter>/nbextensions/<module>/`` and its version, or None
    when the environment has no such folder (or it leaves the environment's
    nbextensions through a link)."""
    if not _MODULE_NAME.match(module) or ".." in module:
        return None
    root = os.path.realpath(os.path.join(share_jupyter, "nbextensions"))
    folder = os.path.realpath(os.path.join(root, *module.split("/")))
    if not _inside(folder, root) or not os.path.isdir(folder):
        return None
    files: list[tuple[str, bytes]] = []
    for dirpath, dirnames, filenames in os.walk(folder):
        dirnames.sort()
        for name in sorted(filenames):
            path = os.path.join(dirpath, name)
            real = os.path.realpath(path)
            if not _inside(real, folder) or not os.path.isfile(real):
                continue
            try:
                with open(real, "rb") as fh:
                    data = fh.read()
            except OSError:
                continue
            files.append((os.path.relpath(path, folder).replace(os.sep, "/"), data))
    index = next((data for path, data in files if path == "index.js"), b"")
    deps = amd_dependencies(index.decode("utf-8", errors="replace"))
    return LibraryFiles(module, _labextension_version(share_jupyter, module), files, deps)


def _inside(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def _labextension_version(share_jupyter: str, module: str) -> str | None:
    package = os.path.join(share_jupyter, "labextensions", *module.split("/"), "package.json")
    try:
        with open(package, encoding="utf-8") as fh:
            version = json.load(fh).get("version")
    except (OSError, ValueError, AttributeError):
        return None
    return version if isinstance(version, str) and version else None


def notification_params(
    library: LibraryFiles, version: str, budget: int = NOTIFICATION_BUDGET
) -> dict[str, Any]:
    """The ``widget.asset`` params for one module: ``index.js`` first, then
    the smallest files, while they fit in ``budget``."""
    ordered = sorted(library.files, key=lambda item: (item[0] != "index.js", len(item[1]), item[0]))
    files: list[dict[str, Any]] = []
    omitted: list[str] = []
    used = 0
    for path, data in ordered:
        if used + len(data) > budget:
            omitted.append(path)
            continue
        used += len(data)
        files.append(
            {
                "path": path,
                "sha256": hashlib.sha256(data).hexdigest(),
                "bytes": len(data),
                "data": f.Segment(data),
            }
        )
    params: dict[str, Any] = {"module": library.module, "version": version, "files": files}
    if omitted:
        params["omitted"] = sorted(omitted)
    return params


class WidgetAssetOffers:
    """Per kernel: which modules and versions were already offered."""

    def __init__(
        self,
        notify: Callable[[str, dict[str, Any]], None],
        share_jupyter: Callable[[], str] | None = None,
    ) -> None:
        self._notify = notify
        self._share = share_jupyter or (lambda: os.path.dirname(default_nbextensions_dir()))
        self._sent: set[tuple[str, str]] = set()
        self._libraries: dict[str, LibraryFiles] = {}

    def offer_for_state(self, state: Any) -> None:
        """Offer the modules a widget model's state names (a comm open)."""
        if not isinstance(state, dict):
            return
        for module_key, version_key in (
            ("_model_module", "_model_module_version"),
            ("_view_module", "_view_module_version"),
        ):
            module, declared = state.get(module_key), state.get(version_key)
            if isinstance(module, str) and module:
                self._offer(module, declared if isinstance(declared, str) else None, set())

    def _offer(self, module: str, declared: str | None, seen: set[str]) -> None:
        if module in seen or is_platform_module(module):
            return
        seen.add(module)
        library = self._library(module)
        if library is None:
            return
        version = library.installed_version or declared or "*"
        if (module, version) not in self._sent:
            self._sent.add((module, version))
            self._notify(f.EVENT_WIDGET_ASSET, notification_params(library, version))
        for dep in library.dependencies:
            self._offer(dep, None, seen)

    def _library(self, module: str) -> LibraryFiles | None:
        # Only found libraries are remembered: one installed later in the
        # session is offered on its first comm after the install.
        library = self._libraries.get(module)
        if library is None:
            try:
                library = read_library(self._share(), module)
            except OSError:
                library = None
            if library is not None:
                self._libraries[module] = library
        return library
