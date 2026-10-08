"""Licence audit of what the open repository ships.

Reads the uv lock and the pnpm lock at ``--root``, walks the runtime closure of
the shipped packages (development groups and devDependencies never ship), finds
each package's declared licence in its installed metadata, and refuses a
strong copyleft or an unknown licence. Weak copyleft (MPL, LGPL, EPL) is
allowed and listed, because a library under it can be shipped unmodified.

Which licences pass and the hand-checked answers for packages whose metadata
names no licence live in ``licence_policy.toml`` beside this script. Every
override carries the reason it is safe.

Usage::

    uv run python scripts/licence_audit.py                       # every workspace member
    uv run python scripts/licence_audit.py --python alkera-core  # one member's closure
    uv run python scripts/licence_audit.py --report audit.md     # also write a report

Run it through ``uv run`` from the repository root after ``uv sync`` and
``pnpm install``: Python licences come from the environment the script runs
in, and npm licences from ``node_modules/.pnpm``. Exit status 1 means a refused
or unknown licence in what ships.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path
from typing import Any

import yaml

POLICY_PATH = Path(__file__).resolve().with_name("licence_policy.toml")

ALLOWED = "allowed"
WEAK = "weak copyleft"
REFUSED = "refused"
UNKNOWN = "unknown"

# Trove classifiers and free-text licence fields seen in the wild, mapped to
# SPDX identifiers. Keys are compared case-insensitively.
_ALIASES: dict[str, str] = {
    "mit": "MIT",
    "mit license": "MIT",
    "the mit license": "MIT",
    "mit-0": "MIT-0",
    "apache": "Apache-2.0",
    "apache 2": "Apache-2.0",
    "apache 2.0": "Apache-2.0",
    "apache-2": "Apache-2.0",
    "apache license 2.0": "Apache-2.0",
    "apache license, version 2.0": "Apache-2.0",
    "apache software license": "Apache-2.0",
    "apache license version 2.0": "Apache-2.0",
    "bsd": "BSD-3-Clause",
    "bsd license": "BSD-3-Clause",
    "new bsd": "BSD-3-Clause",
    "new bsd license": "BSD-3-Clause",
    "3-clause bsd": "BSD-3-Clause",
    "3-clause bsd license": "BSD-3-Clause",
    "bsd 3-clause": "BSD-3-Clause",
    "bsd-3": "BSD-3-Clause",
    "bsd 2-clause": "BSD-2-Clause",
    "simplified bsd": "BSD-2-Clause",
    "isc license": "ISC",
    "isc license (iscl)": "ISC",
    "python software foundation license": "PSF-2.0",
    "psf": "PSF-2.0",
    "psf license": "PSF-2.0",
    "mozilla public license 2.0 (mpl 2.0)": "MPL-2.0",
    "mpl 2.0": "MPL-2.0",
    "mpl-2.0": "MPL-2.0",
    "the unlicense (unlicense)": "Unlicense",
    "cc0 1.0 universal (cc0 1.0) public domain dedication": "CC0-1.0",
    "zlib/libpng license": "Zlib",
    "historical permission notice and disclaimer (hpnd)": "HPND",
    "gnu lesser general public license v2 or later (lgplv2+)": "LGPL-2.1-or-later",
    "gnu lesser general public license v3 (lgplv3)": "LGPL-3.0-only",
    "gnu lesser general public license v3 or later (lgplv3+)": "LGPL-3.0-or-later",
    "gnu library or lesser general public license (lgpl)": "LGPL-2.1-or-later",
    "gnu general public license v2 (gplv2)": "GPL-2.0-only",
    "gnu general public license v2 or later (gplv2+)": "GPL-2.0-or-later",
    "gnu general public license v3 (gplv3)": "GPL-3.0-only",
    "gnu general public license v3 or later (gplv3+)": "GPL-3.0-or-later",
    "gnu affero general public license v3": "AGPL-3.0-only",
    "gnu affero general public license v3 or later (agplv3+)": "AGPL-3.0-or-later",
    "eclipse public license 2.0 (epl-2.0)": "EPL-2.0",
}


@dataclass(frozen=True)
class Policy:
    allowed: frozenset[str]
    weak: frozenset[str]
    refused_prefixes: tuple[str, ...]
    allowed_exceptions: frozenset[str]
    # ecosystem -> (package, version) -> SPDX expression checked by hand
    overrides: Mapping[str, Mapping[tuple[str, str], str]]


def load_policy(path: Path = POLICY_PATH) -> Policy:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    overrides: dict[str, dict[tuple[str, str], str]] = {}
    for ecosystem in ("python", "node"):
        rows = data.get("overrides", {}).get(ecosystem, {})
        for name, row in rows.items():
            missing = [key for key in ("version", "licence", "reason") if not row.get(key)]
            if missing:
                raise ValueError(f"override {ecosystem}:{name} has no {', '.join(missing)}")
        overrides[ecosystem] = {
            (name, row["version"]): row["licence"] for name, row in rows.items()
        }
    return Policy(
        allowed=frozenset(data["licences"]["allowed"]),
        weak=frozenset(data["licences"]["weak_copyleft"]),
        refused_prefixes=tuple(data["licences"]["refused_prefixes"]),
        allowed_exceptions=frozenset(data["licences"]["allowed_exceptions"]),
        overrides=overrides,
    )


# --- SPDX expressions ----------------------------------------------------------


def normalise(text: str) -> str:
    """One licence name or SPDX id, as an SPDX id where an alias is known."""
    cleaned = text.strip().strip(".")
    return _ALIASES.get(cleaned.lower(), cleaned)


_TOKEN = re.compile(r"\(|\)|[^\s()]+")


def classify(expression: str, policy: Policy) -> str:
    """The verdict for an SPDX expression: OR takes the best branch, AND the worst.

    ``A WITH exception`` is judged as ``A``, unless the exception is one the
    policy lists as turning a copyleft into a linking-safe licence.
    """
    tokens = _TOKEN.findall(expression)
    position = 0
    rank = {ALLOWED: 0, WEAK: 1, UNKNOWN: 2, REFUSED: 3}

    def parse_or() -> str:
        nonlocal position
        verdict = parse_and()
        while position < len(tokens) and tokens[position].upper() == "OR":
            position += 1
            other = parse_and()
            verdict = min(verdict, other, key=rank.__getitem__)
        return verdict

    def parse_and() -> str:
        nonlocal position
        verdict = parse_atom()
        while position < len(tokens) and tokens[position].upper() == "AND":
            position += 1
            other = parse_atom()
            verdict = max(verdict, other, key=rank.__getitem__)
        return verdict

    def parse_atom() -> str:
        nonlocal position
        if position >= len(tokens):
            return UNKNOWN
        item = tokens[position]
        position += 1
        if item == "(":
            verdict = parse_or()
            if position < len(tokens) and tokens[position] == ")":
                position += 1
            return verdict
        if position < len(tokens) and tokens[position].upper() == "WITH":
            exception = tokens[position + 1] if position + 1 < len(tokens) else ""
            position += 2
            if exception in policy.allowed_exceptions:
                return ALLOWED
        return classify_one(item, policy)

    verdict = parse_or()
    return verdict if position == len(tokens) else UNKNOWN


def classify_one(licence: str, policy: Policy) -> str:
    spdx = licence.removesuffix("+")
    if spdx in policy.allowed:
        return ALLOWED
    if spdx in policy.weak:
        return WEAK
    if spdx.startswith(policy.refused_prefixes):
        return REFUSED
    return UNKNOWN


# --- Python --------------------------------------------------------------------


@dataclass
class Shipped:
    """One third-party package in the shipped closure."""

    ecosystem: str
    name: str
    version: str
    via: str  # the shipped package whose closure first reached it
    licence: str | None = None
    verdict: str = UNKNOWN
    overridden: bool = False


@dataclass
class Closure:
    packages: dict[tuple[str, str], Shipped] = field(default_factory=dict)
    skipped_members: set[tuple[str, str]] = field(default_factory=set)  # (member, edge)


def _canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def python_closure(lock: Mapping[str, Any], shipped: Iterable[str]) -> Closure:
    """Every registry package the shipped members need at run time.

    A workspace member that is not shipped is not entered: an edge into it is
    recorded in ``skipped_members`` so the report can name it.
    """
    by_name = {_canonical(p["name"]): p for p in lock["package"]}
    members = {
        name
        for name, package in by_name.items()
        if "editable" in package.get("source", {}) or "virtual" in package.get("source", {})
    }
    shipped_set = {_canonical(name) for name in shipped}
    unknown = shipped_set - members
    if unknown:
        raise ValueError(f"not workspace members of this lock: {sorted(unknown)}")

    closure = Closure()
    seen: set[tuple[str, str]] = set()
    stack: list[tuple[str, frozenset[str], str]] = [
        (name, frozenset(), name) for name in sorted(shipped_set)
    ]
    while stack:
        name, extras, root = stack.pop()
        key = (name, ",".join(sorted(extras)))
        if key in seen:
            continue
        seen.add(key)
        package = by_name[name]
        if name not in members:
            closure.packages.setdefault(
                (name, package["version"]),
                Shipped("python", package["name"], package["version"], via=root),
            )
        edges = list(package.get("dependencies", []))
        for extra in extras:
            edges += package.get("optional-dependencies", {}).get(extra, [])
        for edge in edges:
            target = _canonical(edge["name"])
            if target in members and target not in shipped_set:
                closure.skipped_members.add((name, target))
                continue
            stack.append((target, frozenset(edge.get("extra", [])), root))
    return closure


def python_licence(name: str, version: str) -> str | None:
    """The licence an installed distribution declares, at exactly the locked version."""
    try:
        dist = metadata.distribution(name)
    except metadata.PackageNotFoundError:
        return None
    if dist.version != version:
        return None
    meta = dist.metadata

    def first(key: str) -> str | None:
        values = meta.get_all(key)
        return str(values[0]) if values else None

    expression = first("License-Expression")
    if expression:
        return expression.strip()
    from_classifiers = [
        normalise(classifier.split("::")[-1])
        for classifier in meta.get_all("Classifier") or []
        if classifier.startswith("License ::") and classifier.count("::") >= 2
    ]
    from_classifiers = [c for c in from_classifiers if c.lower() != "osi approved"]
    if from_classifiers:
        return " OR ".join(sorted(set(from_classifiers)))
    free_text = (first("License") or "").strip()
    if not free_text:
        return None
    if "\n" not in free_text and len(free_text) <= 60:
        return normalise(free_text)
    # Some projects paste the whole licence text; its title line names it.
    title = free_text.splitlines()[0].strip().strip(".").lower()
    return _ALIASES.get(title)


# --- Node ----------------------------------------------------------------------


def _split_key(key: str) -> tuple[str, str]:
    """``@scope/name@1.2.3(peer@4)`` -> (``@scope/name``, ``1.2.3``)."""
    base = key.split("(", 1)[0]
    at = base.rindex("@")
    return base[:at], base[at + 1 :]


def node_closure(lock: Mapping[str, Any], shipped: Iterable[str]) -> Closure:
    """Every registry package the shipped importers need at run time.

    ``dependencies``, ``optionalDependencies`` and ``peerDependencies`` ship;
    ``devDependencies`` do not. A ``link:`` edge enters the linked importer.
    """
    importers: Mapping[str, Any] = lock.get("importers", {})
    snapshots: Mapping[str, Any] = lock.get("snapshots", {}) or {}
    unknown = set(shipped) - set(importers)
    if unknown:
        raise ValueError(f"not pnpm importers of this lock: {sorted(unknown)}")

    closure = Closure()
    seen_importers: set[str] = set()
    seen_snapshots: set[str] = set()

    def runtime_edges(entry: Mapping[str, Any]) -> Iterable[tuple[str, str]]:
        for section in ("dependencies", "optionalDependencies", "peerDependencies"):
            for dep_name, spec in (entry.get(section) or {}).items():
                version = spec["version"] if isinstance(spec, Mapping) else spec
                yield dep_name, str(version)

    def visit_importer(path: str, root: str) -> None:
        if path in seen_importers:
            return
        seen_importers.add(path)
        for dep_name, version in runtime_edges(importers[path]):
            follow(path, dep_name, version, root)

    def follow(base: str, dep_name: str, version: str, root: str) -> None:
        if version.startswith("link:"):
            target = str(Path(base, version.removeprefix("link:")).as_posix())
            target = _normpath(target)
            if target in importers:
                visit_importer(target, root)
            return
        stack = [f"{dep_name}@{version}"]
        while stack:
            key = stack.pop()
            if key in seen_snapshots:
                continue
            seen_snapshots.add(key)
            name, plain_version = _split_key(key)
            closure.packages.setdefault(
                (name, plain_version), Shipped("node", name, plain_version, via=root)
            )
            for child, child_version in runtime_edges(snapshots.get(key) or {}):
                if not child_version.startswith("link:"):
                    stack.append(f"{child}@{child_version}")

    for importer in sorted(shipped):
        visit_importer(importer, importer)
    return closure


def _normpath(path: str) -> str:
    parts: list[str] = []
    for part in path.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if parts:
                parts.pop()
            continue
        parts.append(part)
    return "/".join(parts) or "."


def node_licences(store: Path) -> dict[tuple[str, str], str | None]:
    """(name, version) -> declared licence, from every package in the pnpm store."""
    found: dict[tuple[str, str], str | None] = {}
    if not store.is_dir():
        return found
    for manifest in store.glob("*/node_modules/**/package.json"):
        relative = manifest.relative_to(store).parts
        # <entry>/node_modules/<name>/package.json or <entry>/node_modules/@s/<name>/package.json
        depth = len(relative)
        if depth not in (4, 5) or (depth == 5 and not relative[2].startswith("@")):
            continue
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        name, version = data.get("name"), data.get("version")
        if not isinstance(name, str) or not isinstance(version, str):
            continue
        found.setdefault((name, version), node_licence_field(data))
    return found


def node_licence_field(data: Mapping[str, Any]) -> str | None:
    licence = data.get("license")
    if isinstance(licence, Mapping):
        licence = licence.get("type")
    if isinstance(licence, str) and licence.strip():
        return normalise(licence)
    legacy = data.get("licenses")
    if isinstance(legacy, list):
        names = [
            normalise(str(item.get("type") if isinstance(item, Mapping) else item))
            for item in legacy
        ]
        if names:
            return " OR ".join(names)
    return None


# --- the audit -----------------------------------------------------------------


def judge(packages: Iterable[Shipped], policy: Policy) -> list[Shipped]:
    judged: list[Shipped] = []
    for package in packages:
        override = policy.overrides.get(package.ecosystem, {}).get((package.name, package.version))
        if override is not None:
            package.licence, package.overridden = override, True
        package.verdict = classify(package.licence, policy) if package.licence else UNKNOWN
        judged.append(package)
    return sorted(judged, key=lambda p: (p.ecosystem, p.name.lower(), p.version))


def report(
    judged: list[Shipped], skipped: Iterable[tuple[str, str]], python: list[str], node: list[str]
) -> str:
    failing = [p for p in judged if p.verdict in (REFUSED, UNKNOWN)]
    weak = [p for p in judged if p.verdict == WEAK]
    overridden = [p for p in judged if p.overridden]
    counts: dict[str, int] = {}
    for package in judged:
        counts[package.licence or "(none found)"] = (
            counts.get(package.licence or "(none found)", 0) + 1
        )

    lines = [
        "# Licence audit",
        "",
        f"Python members: {', '.join(python) or 'none'}.",
        f"pnpm importers: {', '.join(node) or 'none'}.",
        "",
        f"{len(judged)} third-party packages ship: "
        f"{sum(p.ecosystem == 'python' for p in judged)} Python, "
        f"{sum(p.ecosystem == 'node' for p in judged)} npm. "
        f"{len(failing)} refused or unknown, {len(weak)} weak copyleft, "
        f"{len(overridden)} decided by hand in the policy.",
        "",
    ]
    lines += _table("Refused or unknown", failing)
    lines += _table("Weak copyleft (allowed, shipped unmodified)", weak)
    lines += _table("Decided by hand (licence_policy.toml)", overridden)
    if skipped:
        lines += ["## Edges into workspace members that do not ship", ""]
        lines += [f"- `{a}` requires `{b}`" for a, b in sorted(set(skipped))]
        lines.append("")
    lines += ["## Licences", "", "| Licence | Packages |", "| --- | --- |"]
    lines += [f"| {name} | {n} |" for name, n in sorted(counts.items(), key=lambda kv: -kv[1])]
    return "\n".join(lines) + "\n"


def _table(title: str, rows: list[Shipped]) -> list[str]:
    lines = [f"## {title}", ""]
    if not rows:
        return [*lines, "None.", ""]
    lines += ["| Ecosystem | Package | Version | Licence | Verdict | Reached from |"]
    lines += ["| --- | --- | --- | --- | --- | --- |"]
    lines += [
        f"| {p.ecosystem} | `{p.name}` | {p.version} | {p.licence or '(none found)'} "
        f"| {p.verdict} | `{p.via}` |"
        for p in rows
    ]
    return [*lines, ""]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument(
        "--python", action="append", default=None, help="a shipped workspace member (repeat)"
    )
    parser.add_argument(
        "--node", action="append", default=None, help="a shipped pnpm importer path (repeat)"
    )
    parser.add_argument("--report", type=Path, help="write the Markdown report here")
    parser.add_argument("--policy", type=Path, default=POLICY_PATH)
    args = parser.parse_args(argv)

    policy = load_policy(args.policy)
    uv_lock = tomllib.loads((args.root / "uv.lock").read_text(encoding="utf-8"))
    pnpm_lock = yaml.safe_load((args.root / "pnpm-lock.yaml").read_text(encoding="utf-8"))

    python = args.python or sorted(
        p["name"] for p in uv_lock["package"] if "editable" in p.get("source", {})
    )
    node = args.node or sorted(path for path in pnpm_lock.get("importers", {}) if path != ".")

    py = python_closure(uv_lock, python)
    for package in py.packages.values():
        package.licence = python_licence(package.name, package.version)
    js = node_closure(pnpm_lock, node)
    store = node_licences(args.root / "node_modules" / ".pnpm")
    for package in js.packages.values():
        package.licence = store.get((package.name, package.version))

    judged = judge([*py.packages.values(), *js.packages.values()], policy)
    text = report(judged, py.skipped_members, python, node)
    if args.report:
        args.report.write_text(text, encoding="utf-8")
    failing = [p for p in judged if p.verdict in (REFUSED, UNKNOWN)]
    for package in failing:
        print(
            f"{package.verdict}: {package.ecosystem} {package.name} {package.version} "
            f"({package.licence or 'no licence found'}) via {package.via}",
            file=sys.stderr,
        )
    print(f"{len(judged)} packages, {len(failing)} refused or unknown")
    return 1 if failing else 0


if __name__ == "__main__":
    sys.exit(main())
