"""Version stamping for a fixture corpus — the on-disk evidence of what a
past writer emitted.

A corpus directory is immutable once committed: the day a reader stops
loading it is the day a backward-compatibility break slipped in, so the
bytes have to survive forever (see the fixture rules in ``CLAUDE.md``).
Two rules keep that true, and both live here so every generator applies
them identically:

* the stamp is the MAX ``SCHEMA_VERSION`` across every model the corpus
  embeds — the floor a corpus can carry; and
* a regenerate whose payload differs from the corpus already committed at
  that stamp is a *new* corpus, so it lands in a fresh directory above
  every version on disk instead of rewriting history.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path, PurePath

from alkera_core.versioning.base import VersionedModel

_Version = tuple[int, ...]


def _parse(version: str) -> _Version:
    return tuple(int(part) for part in version.split("."))


def _format(version: _Version) -> str:
    return ".".join(str(part) for part in version)


def version_dir_name(version: str) -> str:
    """``"1.4.0"`` → ``"v1_4_0"`` — the directory a stamp names."""
    return f"v{version.replace('.', '_')}"


def corpus_version(models: Iterable[VersionedModel | type[VersionedModel]]) -> str:
    """The MAX ``SCHEMA_VERSION`` across the models a corpus embeds.

    Accepts instances or classes; the caller lists them explicitly so a
    model that joins the corpus is counted the moment it is written.
    """
    versions = {
        _parse(model.SCHEMA_VERSION if isinstance(model, type) else type(model).SCHEMA_VERSION)
        for model in models
    }
    if not versions:
        raise ValueError("a corpus must embed at least one versioned model")
    return _format(max(versions))


def corpus_key(relative: PurePath) -> str:
    """The name a corpus file is compared under: slash-spelled on every host.

    A payload names its files with slashes; a committed file read back
    through the host's own path spelling would carry backslashes on Windows,
    match nothing, and make every regenerate there land on a phantom next
    version.
    """
    return relative.as_posix()


def _committed(directory: Path) -> dict[str, bytes]:
    if not directory.is_dir():
        return {}
    return {
        corpus_key(path.relative_to(directory)): path.read_bytes()
        for path in directory.rglob("*")
        if path.is_file()
    }


def _existing_versions(root: Path) -> set[_Version]:
    found: set[_Version] = set()
    if not root.is_dir():
        return found
    for child in root.iterdir():
        if not child.is_dir() or not child.name.startswith("v"):
            continue
        try:
            found.add(_parse(child.name[1:].replace("_", ".")))
        except ValueError:
            continue
    return found


def resolve_corpus_dir(root: Path, version: str, payload: Mapping[str, bytes]) -> Path:
    """The directory this regenerate must write, given the corpus stamp.

    ``root / v<version>`` when nothing is committed there or when what is
    committed is byte-identical to ``payload`` (a regenerate is idempotent).
    Otherwise the payload is a new writer's output that an embedded model's
    bump did not lift above the existing stamp, so it lands on the next
    minor above **every** corpus on disk and the older directories stay
    exactly as they were.
    """
    wanted = dict(payload)
    stamp = _parse(version)
    target = root / version_dir_name(version)
    committed = _committed(target)
    if not committed or committed == wanted:
        return target
    existing = _existing_versions(root)
    for candidate_version in sorted(v for v in existing if v > stamp):
        candidate = root / version_dir_name(_format(candidate_version))
        if _committed(candidate) == wanted:
            # this corpus is already committed above the stamp — regenerating
            # it a second time must land on the same directory, not a new one.
            return candidate
    highest = max({*existing, stamp})
    major = highest[0]
    minor = highest[1] if len(highest) > 1 else 0
    while True:
        minor += 1
        candidate = root / version_dir_name(_format((major, minor, 0)))
        if not candidate.is_dir():
            return candidate
