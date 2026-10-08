"""The portable environment spec: what a workspace's Python (and conda)
environment holds, written as ``.alkera-environment.json`` in the workspace
root beside the human-written ``ENVIRONMENT.md``.

The spec is the capture's output and the recreate planner's input. It never
carries an absolute path into the workspace (an editable install records its
source relative to the workspace root, because the tree is copied separately)
and never carries a credential (:mod:`alkera_cli.environment.redact` strips
them and refuses a spec that still holds one).

A spec on disk is untrusted input: anyone who can write the workspace can
write it, and the recreate planner turns its values into requirement-file
lines, command arguments and a ``.pth`` file. So every value is validated
where it is parsed. A name is a PEP 508 name, a version or hash a single
token, a URL has no whitespace, a path is relative with no ``..``, and
nothing may start with ``-`` (an option to the tool it reaches). A spec that
fails is refused whole.

The ``kind`` and ``source`` fields are plain strings with the known values
documented beside them, so a newer writer's new value still loads in an older
reader (which skips what it does not know) instead of making the spec
unreadable and getting it overwritten by the next capture.
"""

from __future__ import annotations

import re
from typing import Annotated, Any, ClassVar, Final, Literal, get_args

from alkera_core.versioning import VersionedModel
from pydantic import AfterValidator, BaseModel, ConfigDict, Field

#: The spec's file name in the workspace root. A dot file because it is
#: written by a tool, not edited by hand; JSON because the planner and every
#: other machine read it back exactly.
SPEC_FILENAME = ".alkera-environment.json"
#: The human-written instructions beside it.
INSTRUCTIONS_FILENAME = "ENVIRONMENT.md"

_NAME_SEPARATORS = re.compile(r"[-_.]+")

# --- the values a spec may hold ------------------------------------------------

_NAME_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$")
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._+!/=~-]*$")
_HASH_RE = re.compile(r"^[A-Za-z0-9]+:[0-9A-Fa-f]+$")
_SPACE_OR_CONTROL_RE = re.compile(r"[\s\x00-\x1f\x7f]")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_DRIVE_RE = re.compile(r"^[A-Za-z]:")


def _name(value: str) -> str:
    if not _NAME_RE.match(value):
        raise ValueError(f"not a package name: {value!r}")
    return value


def _token(value: str) -> str:
    """One word with no whitespace that is no option: a version, a build, a
    commit, an md5."""
    if value and not _TOKEN_RE.match(value):
        raise ValueError(f"not a single token: {value!r}")
    return value


def _hash(value: str) -> str:
    if not _HASH_RE.match(value):
        raise ValueError(f"not a digest: {value!r}")
    return value


def _url(value: str) -> str:
    if _SPACE_OR_CONTROL_RE.search(value) or value.startswith("-"):
        raise ValueError(f"not a URL: {value!r}")
    return value


def is_safe_relpath(value: str) -> bool:
    """A workspace-relative, ``/``-separated path a recreate may join onto the
    target root and write into a requirements or ``.pth`` file: no absolute
    or drive prefix, no ``..``, no backslash, no whitespace or control
    character, no ``#`` (a comment in a requirements file), no leading ``-``."""
    if not value or value == ".":
        return True
    if value.startswith(("/", "-")) or _DRIVE_RE.match(value):
        return False
    if "\\" in value or "#" in value or _SPACE_OR_CONTROL_RE.search(value):
        return False
    return all(part not in ("", "..") for part in value.split("/"))


def _relpath(value: str) -> str:
    if not is_safe_relpath(value):
        raise ValueError(f"not a safe workspace-relative path: {value!r}")
    return value


def _conda_spec(value: str) -> str:
    """A conda match spec or channel, passed as one argument: may hold spaces
    (``numpy >=1.2``) but no control character and no leading ``-``."""
    if not value or value.startswith("-") or _CONTROL_RE.search(value):
        raise ValueError(f"not a conda spec: {value!r}")
    return value


def _text(value: str) -> str:
    """Free text shown to a person or a model (a detail, an origin): one line."""
    if _CONTROL_RE.search(value.replace("\t", " ")):
        raise ValueError(f"control character in {value!r}")
    return value


Name = Annotated[str, AfterValidator(_name)]
Token = Annotated[str, AfterValidator(_token)]
Digest = Annotated[str, AfterValidator(_hash)]
Url = Annotated[str, AfterValidator(_url)]
RelPath = Annotated[str, AfterValidator(_relpath)]
CondaWord = Annotated[str, AfterValidator(_conda_spec)]
Text = Annotated[str, AfterValidator(_text)]

# --- the known values of the open vocabularies --------------------------------

EnvKind = Literal["venv", "conda", "system", "none"]
"""What the captured environment is: a virtual environment, a conda prefix,
an interpreter's own site-packages, or no environment at all (a capture of
the project files only)."""

PackageSource = Literal["index", "vcs", "url", "editable", "path"]
"""Where a package comes from: a package index, a VCS checkout, a direct
archive URL, an editable install of a folder in the workspace, or a plain
install of a folder or archive in the workspace."""

NotPortableKind = Literal[
    "editable_outside_workspace",
    "path_outside_workspace",
    "path_entry_outside_workspace",
    "unsupported_value",
    "system_package",
    "system_site_packages",
    "system_interpreter",
    "conda_package_without_url",
    "index_credentials_removed",
]

ProjectFileKind = Literal[
    "pyproject", "uv_lock", "requirements", "conda_env", "pixi", "python_version", "other"
]

IndexKind = Literal["index", "extra_index", "find_links"]

#: The values this reader acts on; any other is kept and skipped.
KNOWN_PACKAGE_SOURCES: Final[frozenset[str]] = frozenset(get_args(PackageSource))
KNOWN_INDEX_KINDS: Final[frozenset[str]] = frozenset(get_args(IndexKind))


class SpecPart(BaseModel):
    """Every embedded part keeps fields a newer writer added, so an older
    reader's round trip does not drop them."""

    model_config = ConfigDict(extra="allow")


class PythonInfo(SpecPart):
    version: Token = ""
    """``platform.python_version()``: ``3.12.4``."""
    implementation: Token = ""
    """``sys.implementation.name``: ``cpython``, ``pypy``."""


class PlatformInfo(SpecPart):
    sys_platform: Token = ""
    machine: Token = ""
    tag: Token = ""
    """``sysconfig.get_platform()``: ``linux-x86_64``, ``macosx-14.0-arm64``."""


class VcsRef(SpecPart):
    vcs: Token = "git"
    url: Url = ""
    commit: Token = ""
    requested_revision: Token = ""


class PackageSpec(SpecPart):
    name: Name
    version: Token = ""
    source: str = "index"
    """One of :data:`PackageSource`; a recreate skips (and reports) any other."""
    requested: bool = False
    """Installed for its own sake rather than as another package's dependency."""
    url: Url = ""
    """The archive URL for ``url``, credentials removed."""
    vcs: VcsRef | None = None
    subdirectory: RelPath = ""
    path: RelPath = ""
    """For ``editable`` and ``path``: the source, relative to the workspace
    root, ``/``-separated (``.`` for the root itself)."""
    hashes: list[Digest] = Field(default_factory=list)
    """``sha256:<hex>`` digests of the artifacts this version may be installed
    from, when a hashed requirements file or ``uv.lock`` named them."""
    installer: Token = ""


class CondaPackage(SpecPart):
    name: Token
    """A conda name (``_openmp_mutex`` is one; PEP 508 does not apply)."""
    version: Token = ""
    build: Token = ""
    channel: Url = ""
    url: Url = ""
    md5: Token = ""
    subdir: Token = ""


class CondaSpec(SpecPart):
    subdir: Token = ""
    """The conda platform the packages were built for: ``linux-64``, ``osx-arm64``."""
    channels: list[CondaWord] = Field(default_factory=list)
    specs: list[CondaWord] = Field(default_factory=list)
    """What the user asked for (``conda env export --from-history``), which
    recreates across platforms where the explicit list does not."""
    packages: list[CondaPackage] = Field(default_factory=list)
    """Every installed conda package (``conda list --explicit``)."""


class ProjectFile(SpecPart):
    path: RelPath
    kind: str = "other"
    """One of :data:`ProjectFileKind`."""
    sha256: Token = ""


class IndexRef(SpecPart):
    url: Url
    kind: str = "index"
    """One of :data:`IndexKind`; a recreate skips any other."""
    origin: Text = ""
    """Where it was found: ``requirements.txt``, ``pyproject.toml``,
    ``env:PIP_INDEX_URL``, ``pip.conf``."""
    credentials_removed: bool = False
    """A credential was in the URL and is not in the spec; the target needs it
    supplied another way (an environment variable reference, a keyring)."""


class NotPortable(SpecPart):
    kind: str
    """One of :data:`NotPortableKind`."""
    name: Text = ""
    detail: Text = ""


class EnvironmentSpec(VersionedModel):
    """A workspace's environment, portable to another machine."""

    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    captured_at: Text = ""
    """ISO 8601, UTC."""
    env_kind: str = "none"
    """One of :data:`EnvKind`."""
    python: PythonInfo | None = None
    platform: PlatformInfo | None = None
    packages: list[PackageSpec] = Field(default_factory=list)
    conda: CondaSpec | None = None
    project_files: list[ProjectFile] = Field(default_factory=list)
    indexes: list[IndexRef] = Field(default_factory=list)
    path_entries: list[RelPath] = Field(default_factory=list)
    """Workspace folders a ``.pth`` file put on ``sys.path`` (``conda develop``),
    relative to the workspace root."""
    not_portable: list[NotPortable] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    def package(self, name: str) -> PackageSpec | None:
        key = normalize_name(name)
        return next((p for p in self.packages if normalize_name(p.name) == key), None)

    def project_file(self, kind: str) -> ProjectFile | None:
        return next((f for f in self.project_files if f.kind == kind), None)


def normalize_name(name: str) -> str:
    """The PEP 503 normalized form of a distribution name."""
    return _NAME_SEPARATORS.sub("-", name).lower()


__all__ = [
    "INSTRUCTIONS_FILENAME",
    "KNOWN_INDEX_KINDS",
    "KNOWN_PACKAGE_SOURCES",
    "SPEC_FILENAME",
    "CondaPackage",
    "CondaSpec",
    "EnvKind",
    "EnvironmentSpec",
    "IndexKind",
    "IndexRef",
    "NotPortable",
    "NotPortableKind",
    "PackageSource",
    "PackageSpec",
    "PlatformInfo",
    "ProjectFile",
    "ProjectFileKind",
    "PythonInfo",
    "VcsRef",
    "is_safe_relpath",
    "normalize_name",
]
