"""Every published open package carries the open repository's LICENSE and NOTICE.

Apache-2.0 requires a copy of the licence, and the NOTICE, in every
distribution. A wheel or npm tarball is built from its own package directory
and cannot reach the repository root, so each package that declares Apache-2.0
holds copies. The open root's files are the one source; this test holds every
copy byte-identical to them, so a change to the root that is not copied fails.
"""

from __future__ import annotations

import json
import subprocess
import tomllib
from pathlib import Path

import pytest

pytestmark = [pytest.mark.xdist_group("open_boundary")]

REPO_ROOT = Path(__file__).resolve().parents[3]
# Before the move the open tree is a folder of this repository; after it, the
# folder is the repository.
OPEN_ROOT = REPO_ROOT / "Databench" if (REPO_ROOT / "Databench").is_dir() else REPO_ROOT
APACHE = "Apache-2.0"
CARRIED = ("LICENSE", "NOTICE")
SKIP_PARTS = frozenset({"vendor", "node_modules", "fixtures"})

# The distributions the open repository publishes. Discovery must find each.
PUBLISHED = {
    "alkera",
    "alkera-cli",
    "alkera-core",
    "alkera-kernel",
    "alkera-notebook",
    "alkera-sdk",
    "@alkera/sdk",
}


def declared_apache(manifest: Path) -> str | None:
    """The package's name when its manifest publishes it under Apache-2.0."""
    if manifest.name == "pyproject.toml":
        project = tomllib.loads(manifest.read_text(encoding="utf-8")).get("project", {})
        return project.get("name") if project.get("license") == APACHE else None
    data = json.loads(manifest.read_text(encoding="utf-8"))
    if data.get("private") or data.get("license") != APACHE:
        return None
    name = data.get("name")
    return name if isinstance(name, str) else None


def apache_packages(root: Path, tracked: list[str]) -> dict[str, Path]:
    """Package name -> its directory, for every Apache-2.0 manifest in ``tracked``."""
    found: dict[str, Path] = {}
    for rel in tracked:
        path = Path(rel)
        if path.name not in ("pyproject.toml", "package.json") or SKIP_PARTS & set(path.parts):
            continue
        name = declared_apache(root / path)
        if name:
            found[name] = (root / path).parent
    return found


def drifted(packages: dict[str, Path], source: Path) -> list[str]:
    """``<package>: <file>`` for every carried file that is missing or differs."""
    problems: list[str] = []
    for name, directory in sorted(packages.items()):
        for filename in CARRIED:
            copy = directory / filename
            if not copy.is_file():
                problems.append(f"{name}: {filename} is missing")
            elif copy.read_bytes() != (source / filename).read_bytes():
                problems.append(f"{name}: {filename} differs from the open root's")
    return problems


def _tracked(root: Path) -> list[str]:
    listing = subprocess.run(
        ["git", "ls-files", "-z"], cwd=root, capture_output=True, check=True
    ).stdout.decode("utf-8")
    return [path for path in listing.split("\0") if path]


@pytest.fixture(scope="module")
def packages() -> dict[str, Path]:
    return apache_packages(REPO_ROOT, _tracked(REPO_ROOT))


def test_every_published_package_is_found(packages: dict[str, Path]) -> None:
    assert PUBLISHED <= set(packages), sorted(PUBLISHED - set(packages))


def test_every_apache_package_carries_the_open_root_files(packages: dict[str, Path]) -> None:
    problems = drifted(packages, OPEN_ROOT)
    assert not problems, (
        "copy the open root's LICENSE and NOTICE into each package:\n  " + "\n  ".join(problems)
    )


# --- the check itself, on a planted tree ----------------------------------------


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_the_check_finds_missing_and_drifted_copies_only(tmp_path: Path) -> None:
    source = tmp_path / "open"
    _write(source / "LICENSE", "licence text\n")
    _write(source / "NOTICE", "notice text\n")
    files = {
        "py/good/pyproject.toml": '[project]\nname = "good"\nlicense = "Apache-2.0"\n',
        "py/good/LICENSE": "licence text\n",
        "py/good/NOTICE": "notice text\n",
        "py/stale/pyproject.toml": '[project]\nname = "stale"\nlicense = "Apache-2.0"\n',
        "py/stale/LICENSE": "an older licence\n",
        "py/stale/NOTICE": "notice text\n",
        "py/bare/pyproject.toml": '[project]\nname = "bare"\nlicense = "Apache-2.0"\n',
        "py/unlicensed/pyproject.toml": '[project]\nname = "unlicensed"\n',
        "js/public/package.json": '{"name": "@x/public", "license": "Apache-2.0"}',
        "js/public/LICENSE": "licence text\n",
        "js/private/package.json": (
            '{"name": "@x/private", "license": "Apache-2.0", "private": true}'
        ),
        "vendor/up/pyproject.toml": '[project]\nname = "up"\nlicense = "Apache-2.0"\n',
    }
    for rel, text in files.items():
        _write(tmp_path / rel, text)

    found = apache_packages(tmp_path, sorted(files))

    assert sorted(found) == ["@x/public", "bare", "good", "stale"]
    assert drifted(found, source) == [
        "@x/public: NOTICE is missing",
        "bare: LICENSE is missing",
        "bare: NOTICE is missing",
        "stale: LICENSE differs from the open root's",
    ]
