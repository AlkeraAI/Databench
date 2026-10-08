"""Reading a workspace's environment files: what each one is, the indexes it
names, the hashes it pins, the Python it asks for and the conda specs it lists.

Pure: the probe already read the files, so everything here is text in, data
out. A file that does not parse contributes nothing; it never fails a capture.
"""

from __future__ import annotations

import ast
import configparser
import contextlib
import re
import tomllib
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any

import yaml
from pydantic import ValidationError

from alkera_cli.environment.probe import ProbeFile
from alkera_cli.environment.redact import redact_url
from alkera_cli.environment.spec import (
    IndexKind,
    IndexRef,
    ProjectFile,
    ProjectFileKind,
    is_safe_relpath,
    normalize_name,
)

#: pip's index options, in requirement files and on a command line.
_INDEX_OPTIONS: Mapping[str, IndexKind] = {
    "-i": "index",
    "--index-url": "index",
    "--extra-index-url": "extra_index",
    "-f": "find_links",
    "--find-links": "find_links",
}
_PIP_CONF_KEYS: tuple[tuple[str, IndexKind], ...] = (
    ("index-url", "index"),
    ("extra-index-url", "extra_index"),
    ("find-links", "find_links"),
)
_PIN_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^\]]*\])?\s*===?\s*([^\s;,]+)")
_HASH_RE = re.compile(r"--hash[=\s]+(\w+:[0-9a-fA-F]+)")
_CONDA_NAME_RE = re.compile(r"^(?:[\w.-]+::)?([A-Za-z0-9_.-]+)")
_PUBLIC_REGISTRIES = frozenset({"https://pypi.org/simple", "https://pypi.org/simple/"})
#: The indexes a setting names, keyed by its pip/uv variable.
ENV_INDEX_KINDS: Mapping[str, IndexKind] = {
    "PIP_INDEX_URL": "index",
    "UV_INDEX_URL": "index",
    "UV_DEFAULT_INDEX": "index",
    "PIP_EXTRA_INDEX_URL": "extra_index",
    "UV_EXTRA_INDEX_URL": "extra_index",
    "UV_INDEX": "extra_index",
    "PIP_FIND_LINKS": "find_links",
    "UV_FIND_LINKS": "find_links",
}
#: The settings whose entries uv lets name the index (``name=url``).
_NAMED_INDEX_VARS = frozenset({"UV_INDEX", "UV_DEFAULT_INDEX"})
#: An index name uv accepts before ``=`` in those settings.
_INDEX_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*=(?=[A-Za-z][A-Za-z0-9+.-]*://|/|\.)")


def file_kind(rel: str) -> ProjectFileKind:
    name = rel.rsplit("/", 1)[-1]
    if name == "pyproject.toml":
        return "pyproject"
    if name == "uv.lock":
        return "uv_lock"
    if name.startswith("requirements") or rel.startswith("requirements/"):
        return "requirements"
    if name in ("environment.yml", "environment.yaml"):
        return "conda_env"
    if name.startswith("pixi."):
        return "pixi"
    if name in (".python-version", "runtime.txt"):
        return "python_version"
    return "other"


@dataclass
class ProjectFacts:
    """What the workspace's files say, merged."""

    files: list[ProjectFile] = field(default_factory=list)
    indexes: list[IndexRef] = field(default_factory=list)
    hashes: dict[tuple[str, str], list[str]] = field(default_factory=dict)
    """``sha256:<hex>`` digests by (normalized name, version)."""
    python_version: str = ""
    conda_channels: list[str] = field(default_factory=list)
    conda_specs: list[str] = field(default_factory=list)
    pip_specs: list[str] = field(default_factory=list)
    """Requirements an ``environment.yml`` lists under ``pip:``."""

    def add_index(self, url: str, kind: IndexKind, origin: str) -> None:
        cleaned, removed = redact_url(url.strip())
        if not cleaned or any(i.url == cleaned and i.kind == kind for i in self.indexes):
            return
        try:
            ref = IndexRef(url=cleaned, kind=kind, origin=origin, credentials_removed=removed)
        except ValidationError:
            return
        self.indexes.append(ref)

    def add_hashes(self, name: str, version: str, digests: list[str]) -> None:
        slot = self.hashes.setdefault((normalize_name(name), version), [])
        slot.extend(d for d in digests if d not in slot)


def _logical_lines(text: str) -> Iterator[str]:
    """Requirement-file lines with continuations joined and comments dropped."""
    pending = ""
    for raw in text.splitlines():
        line = raw.split(" #", 1)[0].rstrip() if not raw.lstrip().startswith("#") else ""
        if line.endswith("\\"):
            pending += line[:-1] + " "
            continue
        line = (pending + line).strip()
        pending = ""
        if line:
            yield line
    if pending.strip():
        yield pending.strip()


def parse_requirements(text: str, origin: str, facts: ProjectFacts) -> None:
    for line in _logical_lines(text):
        if line.startswith("-"):
            option, _, value = line.partition(" ")
            if "=" in option:
                option, _, value = option.partition("=")
            kind = _INDEX_OPTIONS.get(option)
            if kind is not None and value.strip():
                facts.add_index(value.strip(), kind, origin)
            continue
        pin = _PIN_RE.match(line)
        digests = _HASH_RE.findall(line)
        if pin and digests:
            facts.add_hashes(pin.group(1), pin.group(2), digests)


def parse_uv_lock(text: str, origin: str, facts: ProjectFacts) -> None:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return
    for pkg in data.get("package", []):
        if not isinstance(pkg, dict):
            continue
        name, version = str(pkg.get("name", "")), str(pkg.get("version", ""))
        artifacts = [pkg.get("sdist") or {}, *(pkg.get("wheels") or [])]
        digests = [str(a["hash"]) for a in artifacts if isinstance(a, dict) and a.get("hash")]
        if name and version and digests:
            facts.add_hashes(name, version, digests)
        source = pkg.get("source") or {}
        registry = source.get("registry") if isinstance(source, dict) else None
        if isinstance(registry, str) and registry not in _PUBLIC_REGISTRIES:
            # A local folder (uv's spelling of a --find-links source) is not an index.
            kind: IndexKind = "extra_index" if "://" in registry else "find_links"
            facts.add_index(registry, kind, origin)


def parse_pyproject(text: str, origin: str, facts: ProjectFacts) -> None:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return
    uv = data.get("tool", {}).get("uv", {})
    if not isinstance(uv, dict):
        return
    for entry in uv.get("index", []) or []:
        if isinstance(entry, dict) and isinstance(entry.get("url"), str):
            kind: IndexKind = "index" if entry.get("default") else "extra_index"
            facts.add_index(entry["url"], kind, origin)
    if isinstance(uv.get("index-url"), str):
        facts.add_index(uv["index-url"], "index", origin)
    for url in uv.get("extra-index-url", []) or []:
        if isinstance(url, str):
            facts.add_index(url, "extra_index", origin)


def parse_environment_yml(text: str, facts: ProjectFacts) -> None:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        return
    if not isinstance(data, dict):
        return
    for channel in data.get("channels") or []:
        cleaned, _ = redact_url(str(channel))
        if cleaned not in facts.conda_channels:
            facts.conda_channels.append(cleaned)
    for dep in data.get("dependencies") or []:
        if isinstance(dep, str):
            facts.conda_specs.append(dep)
            match = _CONDA_NAME_RE.match(dep)
            if match and match.group(1) == "python" and not facts.python_version:
                facts.python_version = dep[match.end() :].strip().lstrip("=")
        elif isinstance(dep, dict):
            facts.pip_specs.extend(str(p) for p in dep.get("pip") or [])


def parse_python_version(rel: str, text: str) -> str:
    first = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    if rel.endswith("runtime.txt"):
        return first.removeprefix("python-")
    return first


def parse_pip_conf(text: str, facts: ProjectFacts) -> None:
    parser = configparser.ConfigParser()
    try:
        parser.read_string(text)
    except configparser.Error:
        return
    for section in parser.sections():
        for key, kind in _PIP_CONF_KEYS:
            for url in parser.get(section, key, fallback="").split():
                facts.add_index(url, kind, "pip.conf")


def parse_env_vars(env_vars: Mapping[str, str], facts: ProjectFacts) -> None:
    for name, value in env_vars.items():
        kind = ENV_INDEX_KINDS.get(name)
        if kind is None:
            continue
        for entry in value.split():
            # ``UV_INDEX=internal=https://...``: the URL is what follows the name.
            url = _INDEX_NAME_RE.sub("", entry, count=1) if name in _NAMED_INDEX_VARS else entry
            facts.add_index(url, kind, f"env:{name}")


def conda_history_specs(history: str) -> list[str]:
    """What the user asked conda for, in order, with removals applied: the
    ``# update specs: [...]`` and ``# remove specs: [...]`` lines conda and
    micromamba write to ``conda-meta/history``."""
    specs: dict[str, str] = {}
    for line in history.splitlines():
        text = line.strip()
        for prefix, remove in (("# update specs:", False), ("# remove specs:", True)):
            if not text.startswith(prefix):
                continue
            items: Any = []
            with contextlib.suppress(ValueError, SyntaxError):
                items = ast.literal_eval(text[len(prefix) :].strip())
            for item in items if isinstance(items, list) else []:
                match = _CONDA_NAME_RE.match(str(item))
                if not match:
                    continue
                if remove:
                    specs.pop(match.group(1), None)
                else:
                    specs[match.group(1)] = str(item)
    return list(specs.values())


def read_project(files: Mapping[str, ProbeFile]) -> ProjectFacts:
    """Merge every environment file the probe read."""
    facts = ProjectFacts()
    for rel in sorted(files):
        if not is_safe_relpath(rel):
            continue
        entry = files[rel]
        kind = file_kind(rel)
        facts.files.append(ProjectFile(path=rel, kind=kind, sha256=entry.sha256))
        text = entry.text
        if text is None:
            continue
        if kind == "requirements":
            parse_requirements(text, rel, facts)
        elif kind == "uv_lock":
            parse_uv_lock(text, rel, facts)
        elif kind == "pyproject":
            parse_pyproject(text, rel, facts)
        elif kind == "conda_env":
            parse_environment_yml(text, facts)
        elif kind == "python_version" and not facts.python_version:
            facts.python_version = parse_python_version(rel, text)
    return facts


__all__ = [
    "ENV_INDEX_KINDS",
    "ProjectFacts",
    "conda_history_specs",
    "file_kind",
    "parse_env_vars",
    "parse_pip_conf",
    "parse_requirements",
    "read_project",
]
