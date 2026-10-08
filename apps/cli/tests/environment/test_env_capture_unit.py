"""``build_spec`` over probe results written by hand: every source a
distribution can come from, every way a path can fall outside the workspace,
conda, and the project files."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from alkera_cli.environment.capture import build_spec
from alkera_cli.environment.probe import ProbeFile, ProbeResult
from alkera_cli.environment.project import conda_history_specs, read_project

ROOT = "/work/ws"
WHEN = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)


def _dist(name: str, version: str = "1.0", **kw: Any) -> dict[str, Any]:
    return {"name": name, "version": version, "installer": "pip", "requires": [], **kw}


def _probe(
    dists: list[dict[str, Any]] | None = None,
    *,
    env: dict[str, Any] | None = None,
    files: dict[str, str] | None = None,
    host: str = "linux",
    root: str = ROOT,
    env_vars: dict[str, str] | None = None,
) -> ProbeResult:
    environment = {
        "python": {
            "version": "3.12.4",
            "implementation": "cpython",
            "prefix": "/e",
            "base_prefix": "/usr",
        },
        "pyvenv_cfg": {"home": "/usr/bin"},
        "distributions": dists or [],
        "path_entries": [],
        **(env or {}),
    }
    return ProbeResult.model_validate(
        {
            "root": root,
            "host": {"sys_platform": host, "machine": "x86_64", "platform_tag": "linux-x86_64"},
            "files": {k: {"sha256": "s-" + k, "text": v} for k, v in (files or {}).items()},
            "env_vars": env_vars or {},
            "env": environment,
        }
    )


def _spec(probe: ProbeResult, roots: tuple[str, ...] = ()):  # type: ignore[no-untyped-def]
    return build_spec(probe, roots=roots, captured_at=WHEN)


def test_a_vcs_install_keeps_its_commit_and_loses_its_token() -> None:
    url = "https://x-access-token:ghp_abcdefghijklmnopqrstuvwxyz0123@github.com/o/tool.git"
    dist = _dist(
        "tool",
        direct_url={
            "url": url,
            "vcs_info": {"vcs": "git", "commit_id": "abc", "requested_revision": "main"},
        },
    )
    pkg = _spec(_probe([dist])).package("tool")
    assert pkg.source == "vcs"
    assert pkg.vcs.commit == "abc"
    assert pkg.vcs.url == "https://github.com/o/tool.git"


@pytest.mark.parametrize(
    "archive,expected",
    [
        pytest.param({"hashes": {"sha256": "ab"}}, ["sha256:ab"], id="hashes"),
        pytest.param({"hash": "sha256=cd"}, ["sha256:cd"], id="legacy-hash"),
        pytest.param({}, [], id="none"),
    ],
)
def test_an_archive_url_keeps_its_hash(archive: dict[str, Any], expected: list[str]) -> None:
    dist = _dist(
        "w", direct_url={"url": "https://files.example/w-1.0.whl", "archive_info": archive}
    )
    pkg = _spec(_probe([dist])).package("w")
    assert (pkg.source, pkg.url, pkg.hashes) == ("url", "https://files.example/w-1.0.whl", expected)


@pytest.mark.parametrize(
    "url,editable,source,path",
    [
        pytest.param(f"file://{ROOT}/libs/a", True, "editable", "libs/a", id="editable-inside"),
        pytest.param(f"file://{ROOT}", True, "editable", ".", id="editable-root"),
        pytest.param(
            f"file://{ROOT}/vendor/a-1.0.whl",
            False,
            "path",
            "vendor/a-1.0.whl",
            id="archive-inside",
        ),
        pytest.param(
            f"file://{ROOT}/libs/../libs/a", True, "editable", "libs/a", id="dotdot-folded"
        ),
        pytest.param("file:///home/alkera/libs/a", True, "editable", "libs/a", id="sandbox-alias"),
    ],
)
def test_a_local_install_inside_the_workspace_is_relative(
    url: str, editable: bool, source: str, path: str
) -> None:
    dist = _dist("a", direct_url={"url": url, "dir_info": {"editable": editable}})
    pkg = _spec(_probe([dist]), roots=("/home/alkera",)).package("a")
    assert (pkg.source, pkg.path) == (source, path)


@pytest.mark.parametrize(
    "url,editable,kind",
    [
        pytest.param("file:///opt/other/a", True, "editable_outside_workspace", id="editable"),
        pytest.param("file:///opt/other/a.whl", False, "path_outside_workspace", id="archive"),
        pytest.param(
            f"file://{ROOT}/../ws-sibling/a", True, "editable_outside_workspace", id="dotdot-out"
        ),
        pytest.param(
            f"file://{ROOT}x/a", True, "editable_outside_workspace", id="prefix-not-parent"
        ),
    ],
)
def test_a_local_install_outside_the_workspace_is_reported(
    url: str, editable: bool, kind: str
) -> None:
    spec = _spec(_probe([_dist("a", direct_url={"url": url, "dir_info": {"editable": editable}})]))
    assert spec.package("a") is None
    assert [(i.kind, i.name) for i in spec.not_portable] == [(kind, "a")]


def test_an_outside_path_reaches_the_report_without_the_username() -> None:
    dist = _dist(
        "a", direct_url={"url": "file:///Users/someone/code/a", "dir_info": {"editable": True}}
    )
    issue = _spec(_probe([dist])).not_portable[0]
    assert "someone" not in issue.detail


def test_windows_paths_are_compared_case_insensitively_as_windows_paths() -> None:
    dist = _dist(
        "a", direct_url={"url": "file:///C:/Work/WS/libs/a", "dir_info": {"editable": True}}
    )
    pkg = _spec(_probe([dist], host="win32", root="C:\\work\\ws")).package("a")
    assert (pkg.source, pkg.path) == ("editable", "libs/a")


def test_os_installed_distributions_are_not_portable() -> None:
    spec = _spec(_probe([_dist("apt-thing", installer="debian"), _dist("ok")]))
    assert spec.package("apt-thing") is None
    assert spec.package("ok") is not None
    assert [(i.kind, i.name) for i in spec.not_portable] == [("system_package", "apt-thing")]


def test_a_system_interpreter_and_system_site_packages_are_reported() -> None:
    system = _probe(
        env={
            "pyvenv_cfg": None,
            "python": {"version": "3.12.4", "prefix": "/usr", "base_prefix": "/usr"},
        }
    )
    assert _spec(system).env_kind == "system"
    assert [i.kind for i in _spec(system).not_portable] == ["system_interpreter"]
    leaky = _probe(env={"pyvenv_cfg": {"include-system-site-packages": "true", "home": "/usr/bin"}})
    assert [i.kind for i in _spec(leaky).not_portable] == ["system_site_packages"]


@pytest.mark.parametrize(
    "dists,requested",
    [
        pytest.param([_dist("app", requires=["lib"]), _dist("lib")], {"app"}, id="leaves-only"),
        pytest.param(
            [_dist("app", requires=["lib"]), _dist("lib", requested=True)],
            {"app", "lib"},
            id="pip-marked-dependency",
        ),
        pytest.param(
            [
                _dist("app", requires=["lib"], installer="uv"),
                _dist("lib", requested=True, installer="uv"),
            ],
            {"app"},
            id="uv-marks-everything",
        ),
        pytest.param(
            [_dist("pip", requested=True), _dist("setuptools"), _dist("app")],
            {"app"},
            id="bootstrap",
        ),
    ],
)
def test_requested_means_installed_for_its_own_sake(
    dists: list[dict[str, Any]], requested: set[str]
) -> None:
    spec = _spec(_probe(dists))
    assert {p.name for p in spec.packages if p.requested} == requested


def test_pth_entries_belong_to_editables_or_are_recorded() -> None:
    editable = _dist(
        "mylib", direct_url={"url": f"file://{ROOT}/libs/mylib", "dir_info": {"editable": True}}
    )
    entries = [
        {"kind": "pth", "file": "__editable__.mylib-0.1.pth", "path": f"{ROOT}/libs/mylib/src"},
        {"kind": "pth", "file": "_mylib.pth", "path": f"{ROOT}/libs/mylib/src"},
        {"kind": "pth", "file": "conda.pth", "path": f"{ROOT}/tools"},
        {"kind": "pth", "file": "conda.pth", "path": f"{ROOT}/libs/mylib/src"},
        {"kind": "pth", "file": "site.pth", "path": "/opt/shared"},
        {"kind": "egg-link", "file": "legacy.egg-link", "path": f"{ROOT}/legacy"},
    ]
    spec = _spec(_probe([editable], env={"path_entries": entries}))
    assert spec.path_entries == ["tools", "legacy"]
    assert [(i.kind, i.name) for i in spec.not_portable] == [
        ("path_entry_outside_workspace", "site.pth")
    ]


CONDA_META = {
    "packages": [
        {
            "name": "numpy",
            "version": "2.1.0",
            "build": "py312h1",
            "channel": "https://conda.anaconda.org/t/tk-123456/conda-forge/linux-64",
            "url": "https://conda.anaconda.org/t/tk-123456/conda-forge/linux-64/numpy-2.1.0-py312h1.conda",
            "md5": "m1",
            "subdir": "linux-64",
        },
        {
            "name": "tzdata",
            "version": "2024a",
            "build": "h0",
            "channel": "conda-forge",
            "url": "",
            "subdir": "noarch",
        },
        {"name": "requests", "version": "2.32", "channel": "pypi", "subdir": "pypi"},
    ],
    "history": (
        "==> 2026-01-01 <==\n# cmd: micromamba create\n"
        "# update specs: ['numpy', 'tzdata', 'conda-forge::scipy']\n"
        "==> 2026-01-02 <==\n# remove specs: ['scipy']\n"
    ),
}


def test_a_conda_prefix_is_captured_with_tokens_removed() -> None:
    dists = [_dist("numpy", "2.1.0", installer="conda"), _dist("extra", installer="pip")]
    spec = _spec(_probe(dists, env={"conda": CONDA_META}))
    assert spec.env_kind == "conda"
    conda = spec.conda
    assert conda.subdir == "linux-64"
    assert conda.channels == ["https://conda.anaconda.org/conda-forge", "conda-forge"]
    assert conda.specs == ["numpy", "tzdata"]
    assert [p.name for p in conda.packages] == ["numpy", "tzdata"]
    assert "tk-123456" not in spec.model_dump_json()
    # conda's own python packages are conda's; pip's are pip's.
    assert [p.name for p in spec.packages] == ["extra"]
    assert [(i.kind, i.name) for i in spec.not_portable] == [
        ("conda_package_without_url", "tzdata")
    ]


@pytest.mark.parametrize(
    "history,expected",
    [
        pytest.param('# update specs: ["a", "b>=1"]\n', ["a", "b>=1"], id="json-quotes"),
        pytest.param("# update specs: ['a']\n# update specs: ['a=2']\n", ["a=2"], id="re-pinned"),
        pytest.param("# update specs: ['a', 'b']\n# remove specs: ['a']\n", ["b"], id="removed"),
        pytest.param("# update specs: not a list\n", [], id="garbage"),
        pytest.param("", [], id="empty"),
    ],
)
def test_conda_history_specs(history: str, expected: list[str]) -> None:
    assert conda_history_specs(history) == expected


def test_project_files_are_classified_and_parsed() -> None:
    files = {
        "requirements.txt": (
            "--index-url https://pkgs.example/simple\n"
            "-f /srv/wheels\n"
            "alpha==1.0 \\\n    --hash=sha256:aa \\\n    --hash=sha256:bb  # pinned\n"
            "beta>=2\n"
        ),
        "requirements/dev.txt": "--extra-index-url=https://dev.example/simple\n",
        "uv.lock": (
            'version = 1\n[[package]]\nname = "gamma"\nversion = "3.0"\n'
            'source = { registry = "https://private.example/simple" }\n'
            'sdist = { url = "https://x/g.tar.gz", hash = "sha256:cc" }\n'
            'wheels = [{ url = "https://x/g.whl", hash = "sha256:dd" }]\n'
        ),
        "pyproject.toml": (
            '[[tool.uv.index]]\nurl = "https://uv-index.example/simple"\ndefault = true\n'
        ),
        ".python-version": "3.11\n",
        "notes.toml": "x = 1\n",
    }
    facts = read_project({k: ProbeFile(sha256="s", text=v) for k, v in files.items()})
    kinds = {f.path: f.kind for f in facts.files}
    assert kinds["requirements/dev.txt"] == "requirements"
    assert kinds[".python-version"] == "python_version"
    assert facts.hashes[("alpha", "1.0")] == ["sha256:aa", "sha256:bb"]
    assert facts.hashes[("gamma", "3.0")] == ["sha256:cc", "sha256:dd"]
    assert ("beta", "2") not in facts.hashes
    assert {(i.kind, i.url) for i in facts.indexes} == {
        ("index", "https://pkgs.example/simple"),
        ("find_links", "/srv/wheels"),
        ("extra_index", "https://dev.example/simple"),
        ("extra_index", "https://private.example/simple"),
        ("index", "https://uv-index.example/simple"),
    }
    assert facts.python_version == "3.11"


def test_a_project_only_capture_reads_python_and_conda_from_the_files() -> None:
    files = {
        "runtime.txt": "python-3.10.14\n",
        "environment.yml": (
            "channels: [conda-forge]\ndependencies:\n  - numpy=2.1\n  - pip:\n"
            "    - requests==2.32.3\n    - rich>=13\n"
        ),
    }
    probe = _probe(files=files).model_copy(update={"env": None})
    spec = _spec(probe)
    assert spec.env_kind == "none"
    assert spec.python.version == "3.10.14"
    assert spec.conda.specs == ["numpy=2.1"]
    assert [(p.name, p.version) for p in spec.packages] == [("requests", "2.32.3"), ("rich", "")]


def test_index_credentials_in_settings_are_removed_and_reported() -> None:
    probe = _probe(
        files={"requirements.txt": "--extra-index-url https://bob:hunter22@pkgs.example/simple\n"},
        env_vars={
            "PIP_INDEX_URL": "https://__token__:pypi-AgEIcHlwaS5vcmcCJGFiYw1234567890abcdefgh@upload.example/simple"
        },
        env={
            "pip_conf": "[global]\nextra-index-url = https://${CORP_USER}:${CORP_TOKEN}@corp.example/simple\n"
        },
    )
    spec = _spec(probe)
    text = spec.model_dump_json()
    assert "hunter22" not in text and "pypi-AgE" not in text
    by_url = {i.url: i for i in spec.indexes}
    assert by_url["https://pkgs.example/simple"].credentials_removed is True
    assert by_url["https://upload.example/simple"].origin == "env:PIP_INDEX_URL"
    # A reference to a variable is kept: the target supplies the value.
    assert (
        by_url["https://${CORP_USER}:${CORP_TOKEN}@corp.example/simple"].credentials_removed
        is False
    )
    assert sorted(i.kind for i in spec.not_portable) == ["index_credentials_removed"] * 2


@pytest.mark.parametrize(
    "dist,kept",
    [
        pytest.param(_dist("setuptools", installer=""), False, id="conda-built-without-installer"),
        pytest.param(_dist("numpy", installer="conda"), False, id="conda-installer"),
        pytest.param(_dist("extra", installer=""), True, id="not-a-conda-package"),
        pytest.param(_dist("setuptools", installer="pip"), True, id="pip-over-conda"),
    ],
)
def test_conda_owns_its_own_python_distributions(dist: dict[str, Any], kept: bool) -> None:
    meta = {
        "packages": [
            {"name": "setuptools", "version": "1", "channel": "conda-forge"},
            {"name": "numpy", "channel": "conda-forge"},
        ]
    }
    spec = _spec(_probe([dist], env={"conda": meta}))
    assert (spec.package(dist["name"]) is not None) is kept
