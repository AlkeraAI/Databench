"""Capture: turn what the probe read into a portable :class:`EnvironmentSpec`.

:func:`build_spec` is pure (probe result in, spec out); :func:`capture` runs
the probe through a :class:`CommandRunner` first. A workspace path is recorded
relative to the workspace root; the root may be seen at several spellings (a
chat's folder is both its host path and ``/home/alkera`` in its sandbox), so
every spelling is passed as an alias. A path outside every spelling is not
portable and is reported, never recorded as a package.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import PurePath, PurePosixPath, PureWindowsPath
from urllib.parse import unquote, urlsplit

from alkera_core.observability import scrub_path
from pydantic import ValidationError

from alkera_cli.environment.probe import (
    ProbeConda,
    ProbeDistribution,
    ProbeEnv,
    ProbeResult,
    run_probe,
)
from alkera_cli.environment.project import (
    ProjectFacts,
    conda_history_specs,
    parse_env_vars,
    parse_pip_conf,
    read_project,
)
from alkera_cli.environment.redact import redact_url
from alkera_cli.environment.runner import CommandRunner
from alkera_cli.environment.spec import (
    CondaPackage,
    CondaSpec,
    EnvironmentSpec,
    EnvKind,
    NotPortable,
    NotPortableKind,
    PackageSpec,
    PlatformInfo,
    PythonInfo,
    VcsRef,
    is_safe_relpath,
    normalize_name,
)

#: Installers that mean an operating-system package manager put the
#: distribution there; no Python tool recreates it.
SYSTEM_INSTALLERS = frozenset(
    {"debian", "dpkg", "apt", "rpm", "dnf", "yum", "apk", "pacman", "portage", "homebrew", "nix"}
)


class Roots:
    """The workspace root at every spelling the probe may have seen it."""

    def __init__(self, roots: Sequence[str], *, windows: bool) -> None:
        self._windows = windows
        self._roots = [self._pure(r) for r in roots if r]

    def _pure(self, text: str) -> PurePath:
        return PureWindowsPath(text) if self._windows else PurePosixPath(text)

    def relative(self, path: str) -> str | None:
        """``path`` relative to the root (``/``-separated, ``.`` for the root),
        or ``None`` when it is outside every spelling. ``..`` is folded
        lexically: the probe ran elsewhere, so nothing is resolved here."""
        target = _fold(self._pure(path))
        for root in self._roots:
            base = _fold(root)
            try:
                rel = target.relative_to(base)
            except ValueError:
                continue
            text = rel.as_posix()
            return text if text else "."
        return None


def _fold(path: PurePath) -> PurePath:
    parts: list[str] = []
    for part in path.parts:
        if part == "..":
            if len(parts) > 1:
                parts.pop()
            continue
        if part != ".":
            parts.append(part)
    return type(path)(*parts) if parts else path


def file_url_path(url: str, *, windows: bool) -> str | None:
    """The local path a ``file://`` URL names, or ``None`` for any other URL."""
    parts = urlsplit(url)
    if parts.scheme != "file":
        return None
    path = unquote(parts.path)
    if windows and len(path) > 2 and path[0] == "/" and path[2] == ":":
        path = path[1:]
    return path


#: Installers whose ``REQUESTED`` marker means nothing: uv writes it on every
#: distribution it installs, dependencies included.
_REQUESTED_UNRELIABLE = frozenset({"uv"})
#: The tools a new environment is seeded with (``ensurepip`` marks pip as
#: requested); never counted as installed for their own sake.
_BOOTSTRAP = frozenset({"pip", "setuptools", "wheel"})


def _with(base: PackageSpec, update: dict[str, object]) -> PackageSpec:
    """``base`` with ``update`` applied, validated again (``model_copy`` would
    skip validation)."""
    return PackageSpec.model_validate({**base.model_dump(), **update})


def _printable(text: str) -> str:
    return re.sub(r"[\x00-\x1f\x7f]", "?", text)[:200]


def _unsupported(name: str, exc: ValidationError) -> NotPortable:
    """A value no recreate could use safely (a name, a path, a URL)."""
    errors = exc.errors()
    message = str(errors[0]["msg"]) if errors else ""
    return NotPortable(kind="unsupported_value", name=_printable(name), detail=_printable(message))


def _requested(dists: Sequence[ProbeDistribution]) -> set[str]:
    """Distributions installed for their own sake: required by no other
    installed distribution, or marked ``REQUESTED`` by an installer whose mark
    is meaningful (pip's)."""
    required = {normalize_name(r) for d in dists for r in d.requires}
    out: set[str] = set()
    for dist in dists:
        name = normalize_name(dist.name)
        marked = dist.requested and dist.installer.lower() not in _REQUESTED_UNRELIABLE
        if name not in _BOOTSTRAP and (marked or name not in required):
            out.add(name)
    return out


def _package(
    dist: ProbeDistribution,
    *,
    requested: bool,
    roots: Roots,
    facts: ProjectFacts,
    windows: bool,
) -> PackageSpec | NotPortable:
    """One installed distribution as a spec entry, or why it is not portable."""
    base = PackageSpec(
        name=dist.name, version=dist.version, requested=requested, installer=dist.installer
    )
    info = dist.direct_url or {}
    url = str(info.get("url", ""))
    if not url:
        base.hashes = list(facts.hashes.get((normalize_name(dist.name), dist.version), []))
        return base
    subdirectory = str(info.get("subdirectory", ""))
    local = file_url_path(url, windows=windows)
    dir_info = info.get("dir_info") or {}
    if local is not None:
        editable = bool(isinstance(dir_info, dict) and dir_info.get("editable"))
        rel = roots.relative(local)
        if rel is None:
            kind: NotPortableKind = (
                "editable_outside_workspace" if editable else "path_outside_workspace"
            )
            return NotPortable(
                kind=kind, name=_printable(dist.name), detail=_printable(scrub_path(local))
            )
        source = "editable" if editable else "path"
        return _with(base, {"source": source, "path": rel, "subdirectory": subdirectory})
    cleaned, _ = redact_url(url)
    vcs_info = info.get("vcs_info")
    if isinstance(vcs_info, dict):
        vcs = VcsRef(
            vcs=str(vcs_info.get("vcs", "git")),
            url=cleaned,
            commit=str(vcs_info.get("commit_id", "")),
            requested_revision=str(vcs_info.get("requested_revision", "")),
        )
        return _with(base, {"source": "vcs", "vcs": vcs, "subdirectory": subdirectory})
    archive = info.get("archive_info") or {}
    digests: list[str] = []
    if isinstance(archive, dict):
        hashes = archive.get("hashes")
        if isinstance(hashes, dict):
            digests = [f"{algo}:{value}" for algo, value in sorted(hashes.items())]
        elif isinstance(archive.get("hash"), str):
            digests = [archive["hash"].replace("=", ":", 1)]
    return _with(
        base, {"source": "url", "url": cleaned, "hashes": digests, "subdirectory": subdirectory}
    )


def _owned_by_editable(
    pth_file: str, path: str, editables: Sequence[PackageSpec], roots: Roots
) -> bool:
    """Whether a ``sys.path`` entry is an editable install's own mechanism
    (setuptools' ``__editable__.*.pth``, hatchling's ``_<name>.pth``, or a
    path inside an editable's source folder) rather than an entry of its own."""
    if pth_file.startswith("__editable__"):
        return True
    stem = normalize_name(pth_file.removesuffix(".pth").removesuffix(".egg-link").lstrip("_"))
    if any(normalize_name(p.name) == stem for p in editables):
        return True
    rel = roots.relative(path)
    if rel is None:
        return False
    return any(p.path != "." and (rel == p.path or rel.startswith(p.path + "/")) for p in editables)


def _pip_spec(line: str) -> PackageSpec:
    """A requirement an ``environment.yml`` lists under ``pip:``: pinned when
    it says ``name==version``, any version otherwise."""
    name, sep, version = line.strip().partition("==")
    bare = name.split("[", 1)[0].strip()
    if not sep:
        bare = re.split(r"[<>=!~;\s]", bare, maxsplit=1)[0]
    return PackageSpec(name=bare, version=version.strip() if sep else "", requested=True)


_URL_UNSAFE = re.compile(r"[\s\x00-\x1f\x7f]")


def _conda_word_ok(value: str) -> bool:
    try:
        CondaSpec(specs=[value])
    except ValidationError:
        return False
    return True


def _env_kind(env: ProbeEnv) -> EnvKind:
    if env.conda is not None:
        return "conda"
    if env.pyvenv_cfg is not None or env.python.prefix != env.python.base_prefix:
        return "venv"
    return "system"


def _conda(conda: ProbeConda, facts: ProjectFacts) -> tuple[CondaSpec, list[NotPortable]]:
    packages: list[CondaPackage] = []
    channels: list[str] = []
    issues: list[NotPortable] = []
    subdirs: dict[str, int] = {}
    for pkg in conda.packages:
        if not pkg.name or pkg.channel == "pypi":
            continue
        url, _ = redact_url(pkg.url) if pkg.url else ("", False)
        channel, _ = redact_url(pkg.channel)
        if pkg.subdir and channel.endswith("/" + pkg.subdir):
            channel = channel[: -len(pkg.subdir) - 1]
        if channel and channel not in channels:
            channels.append(channel)
        if pkg.subdir and pkg.subdir != "noarch":
            subdirs[pkg.subdir] = subdirs.get(pkg.subdir, 0) + 1
        if not url:
            issues.append(NotPortable(kind="conda_package_without_url", name=pkg.name))
        try:
            packages.append(
                CondaPackage(
                    name=pkg.name,
                    version=pkg.version,
                    build=pkg.build,
                    channel=channel,
                    url=url,
                    md5=pkg.md5,
                    subdir=pkg.subdir,
                )
            )
        except ValidationError as exc:
            issues.append(_unsupported(pkg.name, exc))
    for channel in facts.conda_channels:
        if channel not in channels:
            channels.append(channel)
    specs = [
        s for s in conda_history_specs(conda.history) or facts.conda_specs if _conda_word_ok(s)
    ]
    channels = [c for c in channels if _conda_word_ok(c) and not _URL_UNSAFE.search(c)]
    subdir = max(subdirs, key=lambda k: subdirs[k]) if subdirs else ""
    spec = CondaSpec(subdir=subdir, channels=channels, specs=specs, packages=packages)
    return spec, issues


def _packages_from_env(
    env: ProbeEnv, *, roots: Roots, facts: ProjectFacts, windows: bool
) -> tuple[list[PackageSpec], list[str], list[NotPortable]]:
    issues: list[NotPortable] = []
    packages: list[PackageSpec] = []
    # A conda package's own Python distribution is conda's to recreate. Its
    # INSTALLER says ``conda``, or nothing at all when the build left it out.
    conda_names = (
        {normalize_name(p.name) for p in env.conda.packages if p.channel != "pypi"}
        if env.conda is not None
        else set()
    )
    requested = _requested(env.distributions)
    for dist in sorted(env.distributions, key=lambda d: normalize_name(d.name)):
        installer = dist.installer.lower()
        if conda_names and (
            installer == "conda" or (not installer and normalize_name(dist.name) in conda_names)
        ):
            continue
        if installer in SYSTEM_INSTALLERS:
            issues.append(NotPortable(kind="system_package", name=dist.name, detail=installer))
            continue
        try:
            entry = _package(
                dist,
                requested=normalize_name(dist.name) in requested,
                roots=roots,
                facts=facts,
                windows=windows,
            )
        except ValidationError as exc:
            entry = _unsupported(dist.name, exc)
        if isinstance(entry, NotPortable):
            issues.append(entry)
        else:
            packages.append(entry)
    editables = [p for p in packages if p.source == "editable"]
    path_entries: list[str] = []
    for pth in env.path_entries:
        if _owned_by_editable(pth.file, pth.path, editables, roots):
            continue
        rel = roots.relative(pth.path)
        if rel is None:
            issues.append(
                NotPortable(
                    kind="path_entry_outside_workspace",
                    name=pth.file,
                    detail=_printable(scrub_path(pth.path)),
                )
            )
        elif not is_safe_relpath(rel):
            issues.append(
                NotPortable(kind="unsupported_value", name=pth.file, detail=_printable(rel))
            )
        elif rel not in path_entries:
            path_entries.append(rel)
    return packages, path_entries, issues


def build_spec(
    probe: ProbeResult, *, roots: Sequence[str], captured_at: datetime
) -> EnvironmentSpec:
    """The portable spec for what ``probe`` read. ``roots`` are the workspace
    root's spellings as the probe saw them (``probe.root`` is always one)."""
    windows = probe.host.sys_platform == "win32"
    root_set = Roots([probe.root, *roots], windows=windows)
    facts = read_project(probe.files)
    parse_env_vars(probe.env_vars, facts)
    spec = EnvironmentSpec(
        captured_at=captured_at.astimezone(UTC).isoformat(timespec="seconds"),
        platform=PlatformInfo(
            sys_platform=probe.host.sys_platform,
            machine=probe.host.machine,
            tag=probe.host.platform_tag,
        ),
        project_files=facts.files,
    )
    env = probe.env
    if env is None:
        if facts.python_version:
            spec.python = PythonInfo(version=facts.python_version)
        if facts.conda_specs:
            spec.conda = CondaSpec(channels=facts.conda_channels, specs=facts.conda_specs)
        spec.packages = [p for p in map(_pip_spec, facts.pip_specs) if p.name]
        spec.indexes = facts.indexes
        return spec
    if env.pip_conf:
        parse_pip_conf(env.pip_conf, facts)
    spec.env_kind = _env_kind(env)
    spec.python = PythonInfo(version=env.python.version, implementation=env.python.implementation)
    packages, path_entries, issues = _packages_from_env(
        env, roots=root_set, facts=facts, windows=windows
    )
    spec.packages = packages
    spec.path_entries = path_entries
    if env.conda is not None:
        spec.conda, conda_issues = _conda(env.conda, facts)
        issues.extend(conda_issues)
    if spec.env_kind == "system":
        issues.insert(
            0, NotPortable(kind="system_interpreter", detail=scrub_path(env.python.prefix))
        )
    cfg = env.pyvenv_cfg or {}
    if cfg.get("include-system-site-packages", "").lower() == "true":
        issues.insert(
            0,
            NotPortable(
                kind="system_site_packages", detail=_printable(scrub_path(cfg.get("home", "")))
            ),
        )
    spec.indexes = facts.indexes
    issues.extend(
        NotPortable(kind="index_credentials_removed", name=i.origin, detail=i.url)
        for i in facts.indexes
        if i.credentials_removed
    )
    spec.not_portable = issues
    return spec


async def capture(
    runner: CommandRunner,
    *,
    root: str,
    python: str | None,
    aliases: Sequence[str] = (),
    now: datetime | None = None,
) -> EnvironmentSpec:
    """Capture the environment ``python`` belongs to, and the workspace at
    ``root``. ``python=None`` captures the workspace's files alone."""
    probe = await run_probe(runner, python=python, root=root, inspect_env=python is not None)
    return build_spec(probe, roots=aliases, captured_at=now or datetime.now(UTC))


__all__ = ["SYSTEM_INSTALLERS", "Roots", "build_spec", "capture", "file_url_path"]
