"""npm install-time code execution is gated by an explicit allowlist.

pnpm 9 runs ``preinstall``/``install``/``postinstall`` for EVERY package in the
transitive closure by default (the opt-in allowlist only became the default in
pnpm 10). Every ``pnpm install`` in this repo — five pr-gate jobs, the e2e-live
workflow, the ECS deploy, the extension publish, and the web Dockerfile — would
therefore execute arbitrary code from any dependency in the tree. The extension
publish is the sharp end: that job holds the marketplace PATs and the Sentry
token.

``onlyBuiltDependencies`` in ``pnpm-workspace.yaml`` narrows that to the packages
that genuinely compile or link a binary. This test pins the allowlist EXACTLY, in
both directions: dropping a name silently breaks the build that needs it, and
adding one is a decision to run someone else's install script that should be made
in a diff a human reads, not slipped in with a dependency bump.

It also pins the LOCATION. The installed pnpm no longer reads the ``pnpm`` key in
``package.json`` — it warns that the key is ignored and carries on running every
install script — so the same list there is a control that enforces nothing while
looking like it does. That failure is silent, which is exactly the kind worth a
test.

Offline by construction — the expected set was derived from the registry's
``hasInstallScript`` flag for every ``name@version`` in ``pnpm-lock.yaml``. To
re-derive it after a dependency change, run ``pnpm install`` and read the
"Ignored build scripts" line pnpm prints.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
PACKAGE_JSON = REPO_ROOT / "package.json"
WORKSPACE_YAML = REPO_ROOT / "pnpm-workspace.yaml"
LOCKFILE = REPO_ROOT / "pnpm-lock.yaml"

#: Every package in the committed lockfile's closure that declares an install
#: lifecycle script. Each is a compiler or a binary linker the build genuinely
#: needs; nothing else in the tree may run code at install time.
EXPECTED_BUILT = {
    "@sentry/cli",  # links the platform sentry-cli binary
    "@vscode/vsce-sign",  # links the platform VSIX signing binary
    "esbuild",  # links the platform esbuild binary
    "fsevents",  # native macOS file-watching addon
    "keytar",  # native OS-keychain addon
}


def _root_package() -> dict:
    return json.loads(PACKAGE_JSON.read_text(encoding="utf-8"))


def _allowlist() -> list[str]:
    """The ``onlyBuiltDependencies`` entries from ``pnpm-workspace.yaml``.

    Parsed by hand rather than through a YAML library, for the same reason the
    lockfile below is: this must read exactly the bytes pnpm reads, with no
    dependency of its own."""
    entries: list[str] = []
    in_block = False
    for line in WORKSPACE_YAML.read_text(encoding="utf-8").splitlines():
        if line.startswith("onlyBuiltDependencies:"):
            in_block = True
            continue
        if in_block:
            item = re.match(r"^\s+-\s+[\"']?(?P<name>[^\"'\s]+)[\"']?\s*$", line)
            if item:
                entries.append(item.group("name"))
                continue
            if line.strip() and not line.startswith((" ", "\t", "#")):
                break  # next top-level key
    return entries


def _locked_package_names() -> set[str]:
    """Every ``name`` under the lockfile's ``packages:`` section."""
    names: set[str] = set()
    in_packages = False
    for line in LOCKFILE.read_text(encoding="utf-8").splitlines():
        if line.startswith("packages:"):
            in_packages = True
            continue
        if in_packages and line and not line.startswith(" "):
            break
        if not in_packages:
            continue
        match = re.match(r"^  '?(?P<name>@?[^@'\s]+(?:/[^@'\s]+)?)@[^:'\s]+'?:$", line)
        if match:
            names.add(match.group("name"))
    return names


def test_the_workspace_declares_an_install_script_allowlist() -> None:
    """Without the key, pnpm 9 runs every transitive install script."""
    allowlist = _allowlist()
    assert allowlist, (
        "pnpm-workspace.yaml has no onlyBuiltDependencies -- pnpm runs install "
        "scripts for the ENTIRE transitive npm closure without it"
    )


def test_the_allowlist_is_where_pnpm_actually_reads_it() -> None:
    """The same list under a `pnpm` key in package.json is inert: current pnpm
    warns that the key is ignored and runs every install script anyway. Keeping it
    there would read as a control in review while enforcing nothing, so the wrong
    location fails loudly instead of silently."""
    stale = _root_package().get("pnpm", {}).get("onlyBuiltDependencies")
    assert stale is None, (
        "package.json still carries pnpm.onlyBuiltDependencies -- pnpm ignores it "
        "there. The allowlist belongs in pnpm-workspace.yaml; two copies means the "
        "one being edited may not be the one being enforced."
    )


def test_the_allowlist_is_exactly_the_packages_that_need_to_build() -> None:
    """Pinned in both directions. A missing name is a broken build; an extra one
    is a new package granted arbitrary code execution on every install, in every
    CI job, including the one holding the marketplace credentials."""
    allowlist = set(_allowlist())
    assert allowlist == EXPECTED_BUILT, (
        "onlyBuiltDependencies drifted. Adding an entry grants that package "
        "install-time code execution -- confirm it genuinely compiles or links a "
        "binary, then update EXPECTED_BUILT here in the same change."
    )


def test_every_allowlisted_package_is_actually_in_the_tree() -> None:
    """A stale entry is a pre-approval for a package nobody reviewed the return
    of: if it ever comes back as a transitive dependency, its install script runs
    with no further discussion."""
    locked = _locked_package_names()
    assert locked, "could not parse any package name out of pnpm-lock.yaml"
    unknown = set(_allowlist()) - locked
    assert not unknown, f"allowlisted but not in the lockfile: {sorted(unknown)}"
