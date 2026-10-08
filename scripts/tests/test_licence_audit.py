"""The licence audit refuses what may not ship and walks only what ships.

Every case runs on a small planted lock or store, so a pass means the audit
reads the same shapes the real ``uv.lock`` and ``pnpm-lock.yaml`` hold.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import textwrap
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

SCRIPT = Path(__file__).resolve().parents[1] / "licence_audit.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("licence_audit", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


audit = _load()
POLICY = audit.load_policy()


# --- verdicts ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("expression", "verdict"),
    [
        pytest.param("MIT", audit.ALLOWED, id="permissive"),
        pytest.param("GPL-3.0-only", audit.REFUSED, id="strong-copyleft"),
        pytest.param("AGPL-3.0-or-later", audit.REFUSED, id="network-copyleft"),
        pytest.param("GPL-2.0+", audit.REFUSED, id="plus-suffix-still-gpl"),
        pytest.param("SSPL-1.0", audit.REFUSED, id="sspl"),
        pytest.param("UNLICENSED", audit.REFUSED, id="npm-unlicensed"),
        pytest.param("Unlicense", audit.ALLOWED, id="the-unlicense-is-not-unlicensed"),
        pytest.param("LGPL-3.0-only", audit.WEAK, id="weak-copyleft"),
        pytest.param("MIT OR GPL-3.0-only", audit.ALLOWED, id="or-takes-the-best-branch"),
        pytest.param("MIT AND GPL-3.0-only", audit.REFUSED, id="and-takes-the-worst"),
        pytest.param("MPL-2.0 AND (Apache-2.0 OR MIT)", audit.WEAK, id="parenthesised"),
        pytest.param(
            "GPL-2.0-only WITH Classpath-exception-2.0", audit.ALLOWED, id="listed-exception"
        ),
        pytest.param(
            "GPL-2.0-only WITH Font-exception-2.0", audit.REFUSED, id="unlisted-exception"
        ),
        pytest.param("Some-Custom-1.0", audit.UNKNOWN, id="unrecognised-id"),
        pytest.param("MIT GPL-3.0-only", audit.UNKNOWN, id="junk-after-expression"),
    ],
)
def test_an_expression_gets_the_verdict_its_operators_imply(expression: str, verdict: str) -> None:
    assert audit.classify(expression, POLICY) == verdict


@pytest.mark.parametrize(
    ("text", "spdx"),
    [
        pytest.param("MIT License", "MIT", id="classifier-name"),
        pytest.param("Apache Software License", "Apache-2.0", id="apache-classifier"),
        pytest.param("3-Clause BSD License", "BSD-3-Clause", id="bsd-free-text"),
        pytest.param("Apache-2.0", "Apache-2.0", id="already-spdx"),
    ],
)
def test_licence_names_normalise_to_spdx(text: str, spdx: str) -> None:
    assert audit.normalise(text) == spdx


# --- the Python closure -----------------------------------------------------------

UV_LOCK = textwrap.dedent(
    """
    version = 1

    [[package]]
    name = "open-app"
    version = "0.0.0"
    source = { editable = "apps/open" }
    dependencies = [
        { name = "web-lib", extra = ["fast"] },
        { name = "private-pkg" },
    ]

    [package.dev-dependencies]
    dev = [{ name = "test-only" }]

    [[package]]
    name = "private-pkg"
    version = "0.0.0"
    source = { editable = "packages/private" }
    dependencies = [{ name = "private-driver" }]

    [[package]]
    name = "web-lib"
    version = "1.0.0"
    source = { registry = "https://pypi.org/simple" }
    dependencies = [{ name = "always-dep" }]

    [package.optional-dependencies]
    fast = [{ name = "speedup" }]
    slow = [{ name = "not-asked-for" }]

    [[package]]
    name = "always-dep"
    version = "2.0.0"
    source = { registry = "https://pypi.org/simple" }

    [[package]]
    name = "speedup"
    version = "3.0.0"
    source = { registry = "https://pypi.org/simple" }

    [[package]]
    name = "not-asked-for"
    version = "1.0.0"
    source = { registry = "https://pypi.org/simple" }

    [[package]]
    name = "private-driver"
    version = "1.0.0"
    source = { registry = "https://pypi.org/simple" }

    [[package]]
    name = "test-only"
    version = "1.0.0"
    source = { registry = "https://pypi.org/simple" }
    """
)


def test_the_python_closure_follows_requested_extras_and_stops_at_unshipped_members() -> None:
    import tomllib

    closure = audit.python_closure(tomllib.loads(UV_LOCK), ["open-app"])

    assert {name for name, _ in closure.packages} == {"web-lib", "always-dep", "speedup"}
    assert closure.skipped_members == {("open-app", "private-pkg")}


def test_a_name_that_is_not_a_workspace_member_is_refused() -> None:
    import tomllib

    with pytest.raises(ValueError, match="web-lib"):
        audit.python_closure(tomllib.loads(UV_LOCK), ["web-lib"])


# --- the npm closure --------------------------------------------------------------

PNPM_LOCK: dict[str, Any] = {
    "lockfileVersion": "9.0",
    "importers": {
        ".": {"devDependencies": {"prettier": {"specifier": "^3", "version": "3.0.0"}}},
        "apps/web": {
            "dependencies": {
                "@acme/ui": {"specifier": "workspace:*", "version": "link:../../packages/ui"},
                "react": {"specifier": "^18", "version": "18.3.1"},
            },
            "devDependencies": {"vite": {"specifier": "^5", "version": "5.0.0"}},
        },
        "packages/ui": {
            "dependencies": {"@scope/lib": {"specifier": "^1", "version": "1.2.0(react@18.3.1)"}},
            "devDependencies": {"vitest": {"specifier": "^1", "version": "1.0.0"}},
        },
    },
    "snapshots": {
        "react@18.3.1": {"dependencies": {"loose-envify": "1.4.0"}},
        "loose-envify@1.4.0": {},
        "@scope/lib@1.2.0(react@18.3.1)": {"optionalDependencies": {"fsevents": "2.3.3"}},
        "fsevents@2.3.3": {},
        "vite@5.0.0": {"dependencies": {"esbuild": "0.20.0"}},
        "esbuild@0.20.0": {},
        "vitest@1.0.0": {},
        "prettier@3.0.0": {},
    },
}


def test_the_npm_closure_follows_links_and_never_dev_dependencies() -> None:
    closure = audit.node_closure(PNPM_LOCK, ["apps/web"])

    assert set(closure.packages) == {
        ("react", "18.3.1"),
        ("loose-envify", "1.4.0"),
        ("@scope/lib", "1.2.0"),
        ("fsevents", "2.3.3"),
    }


def _plant_store(root: Path, packages: dict[str, dict[str, Any]]) -> Path:
    store = root / "node_modules" / ".pnpm"
    for entry, manifest in packages.items():
        directory = store / entry / "node_modules" / manifest["name"]
        directory.mkdir(parents=True)
        (directory / "package.json").write_text(json.dumps(manifest), encoding="utf-8")
    return store


def test_npm_licences_are_read_in_every_declared_form(tmp_path: Path) -> None:
    store = _plant_store(
        tmp_path,
        {
            "react@18.3.1": {"name": "react", "version": "18.3.1", "license": "MIT"},
            "@scope+lib@1.2.0_react@18.3.1": {
                "name": "@scope/lib",
                "version": "1.2.0",
                "license": {"type": "ISC"},
            },
            "old@0.1.0": {
                "name": "old",
                "version": "0.1.0",
                "licenses": [{"type": "MIT"}, {"type": "Apache-2.0"}],
            },
            "bare@1.0.0": {"name": "bare", "version": "1.0.0"},
        },
    )

    assert audit.node_licences(store) == {
        ("react", "18.3.1"): "MIT",
        ("@scope/lib", "1.2.0"): "ISC",
        ("old", "0.1.0"): "MIT OR Apache-2.0",
        ("bare", "1.0.0"): None,
    }


# --- overrides --------------------------------------------------------------------


def _policy(tmp_path: Path, override_rows: str) -> Path:
    real = audit.POLICY_PATH.read_text(encoding="utf-8")
    head = real.split("[overrides.python]")[0]
    path = tmp_path / "policy.toml"
    path.write_text(head + override_rows, encoding="utf-8")
    return path


def test_an_override_applies_only_at_the_version_that_was_checked(tmp_path: Path) -> None:
    policy = audit.load_policy(
        _policy(
            tmp_path,
            '[overrides.python]\n[overrides.node]\nbare = { version = "1.0.0", '
            'licence = "MIT", reason = "checked" }\n',
        )
    )
    checked = audit.Shipped("node", "bare", "1.0.0", via="apps/web")
    moved_on = audit.Shipped("node", "bare", "2.0.0", via="apps/web")

    by_version = {p.version: p for p in audit.judge([checked, moved_on], policy)}

    assert (by_version["1.0.0"].verdict, by_version["1.0.0"].overridden) == (audit.ALLOWED, True)
    assert (by_version["2.0.0"].verdict, by_version["2.0.0"].overridden) == (audit.UNKNOWN, False)


@pytest.mark.parametrize(
    "row",
    [
        pytest.param('x = { version = "1", licence = "MIT" }', id="no-reason"),
        pytest.param('x = { licence = "MIT", reason = "r" }', id="no-version"),
        pytest.param('x = { version = "1", reason = "r" }', id="no-licence"),
    ],
)
def test_an_incomplete_override_is_refused(tmp_path: Path, row: str) -> None:
    with pytest.raises(ValueError, match="override python:x"):
        audit.load_policy(_policy(tmp_path, f"[overrides.python]\n{row}\n[overrides.node]\n"))


def test_the_committed_policy_loads() -> None:
    assert "Apache-2.0" in POLICY.allowed
    assert not POLICY.allowed & POLICY.weak


# --- end to end -------------------------------------------------------------------

MEMBER_ONLY_LOCK = textwrap.dedent(
    """
    version = 1

    [[package]]
    name = "open-app"
    version = "0.0.0"
    source = { editable = "apps/open" }
    """
)


@pytest.mark.parametrize(
    ("licence", "exit_code"),
    [
        pytest.param("MIT", 0, id="permissive-passes"),
        pytest.param("GPL-3.0-only", 1, id="copyleft-fails"),
        pytest.param(None, 1, id="no-licence-fails"),
    ],
)
def test_the_command_fails_on_what_may_not_ship(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], licence: str | None, exit_code: int
) -> None:
    (tmp_path / "uv.lock").write_text(MEMBER_ONLY_LOCK, encoding="utf-8")
    lock = {
        "lockfileVersion": "9.0",
        "importers": {
            "apps/web": {"dependencies": {"dep": {"specifier": "1", "version": "1.0.0"}}}
        },
        "snapshots": {"dep@1.0.0": {}},
    }
    (tmp_path / "pnpm-lock.yaml").write_text(yaml.safe_dump(lock), encoding="utf-8")
    manifest: dict[str, Any] = {"name": "dep", "version": "1.0.0"}
    if licence:
        manifest["license"] = licence
    _plant_store(tmp_path, {"dep@1.0.0": manifest})
    report = tmp_path / "report.md"

    code = audit.main(["--root", str(tmp_path), "--report", str(report)])

    assert code == exit_code
    failing_section = report.read_text(encoding="utf-8").split("## Weak copyleft")[0]
    assert ("`dep`" in failing_section) is (exit_code == 1)
    if exit_code:
        assert "dep 1.0.0" in capsys.readouterr().err
