"""``widget.asset``: a widget library's own nbextension files, offered by the
kernel from its environment when a comm opens for a non-platform module."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from _alkera_kernel import widget_assets as wa
from nbkrn_harness import KernelFactory, step

GRID_INDEX = (
    b'define("fakegrid", ["@jupyter-widgets/base", "fakescales", "exports", "./chunk"], '
    b"function (base, scales, exports) { exports.GridModel = 1; });\n"
)
SCALES_INDEX = b"define(['@jupyter-widgets/base', 'fakegrid'], function (b) { return {}; });\n"
LOGO = b"\x89PNG fake logo"


def make_share(root: Path) -> Path:
    """An environment's ``share/jupyter`` with two libraries (one depending
    on the other and back), a labextension version for one, and folders
    named like platform modules that must never be offered."""
    share = root / "share" / "jupyter"
    nb = share / "nbextensions"
    (nb / "fakegrid" / "static").mkdir(parents=True)
    (nb / "fakegrid" / "index.js").write_bytes(GRID_INDEX)
    (nb / "fakegrid" / "static" / "logo.png").write_bytes(LOGO)
    (nb / "fakescales").mkdir()
    (nb / "fakescales" / "index.js").write_bytes(SCALES_INDEX)
    for platform in ("@jupyter-widgets/controls", "@jupyter-widgets/base", "anywidget"):
        (nb / platform).mkdir(parents=True)
        (nb / platform / "index.js").write_text("define([], function () { return {}; });")
    (share / "labextensions" / "fakegrid").mkdir(parents=True)
    (share / "labextensions" / "fakegrid" / "package.json").write_text(
        json.dumps({"name": "fakegrid", "version": "1.2.3"})
    )
    return share


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _files(params: dict[str, Any]) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    for entry in params["files"]:
        data = entry["data"].data
        assert entry["sha256"] == _sha(data) and entry["bytes"] == len(data)
        out[entry["path"]] = data
    return out


# --------------------------------------------------------------------------- pure parts


@pytest.mark.parametrize(
    ("code", "deps"),
    [
        pytest.param(GRID_INDEX.decode(), ["@jupyter-widgets/base", "fakescales"], id="named"),
        pytest.param(SCALES_INDEX.decode(), ["@jupyter-widgets/base", "fakegrid"], id="anonymous"),
        pytest.param(
            'define(["a"],f);define("x",["b","a","require","module"],g)',
            ["a", "b"],
            id="several-defines-deduplicated",
        ),
        pytest.param("define(function () { return {}; })", [], id="no-dependency-list"),
        pytest.param("!function(){var e={};e.define=1}()", [], id="no-call"),
        pytest.param('redefine(["nope"], f)', [], id="other-identifier"),
    ],
)
def test_nbkrn_amd_dependencies(code: str, deps: list[str]) -> None:
    assert wa.amd_dependencies(code) == deps


@pytest.mark.parametrize(
    ("module", "platform"),
    [
        pytest.param("@jupyter-widgets/base", True, id="base"),
        pytest.param("@jupyter-widgets/controls", True, id="controls"),
        pytest.param("@jupyter-widgets/anything-new", True, id="jupyter-widgets-scope"),
        pytest.param("@alkera/ui-widgets", True, id="alkera-ui-widgets"),
        pytest.param("@alkera/widgets", True, id="alkera-widgets"),
        pytest.param("anywidget", True, id="anywidget"),
        pytest.param("@alkera/other", False, id="other-alkera-scope"),
        pytest.param("bqplot", False, id="library"),
        pytest.param("jupyter-widgets", False, id="lookalike"),
    ],
)
def test_nbkrn_platform_modules(module: str, platform: bool) -> None:
    assert wa.is_platform_module(module) is platform


def test_nbkrn_read_library_reads_the_whole_folder(tmp_path: Path) -> None:
    share = make_share(tmp_path)
    library = wa.read_library(str(share), "fakegrid")
    assert library is not None
    assert dict(library.files) == {"index.js": GRID_INDEX, "static/logo.png": LOGO}
    assert library.installed_version == "1.2.3"
    assert library.dependencies == ["@jupyter-widgets/base", "fakescales"]
    scales = wa.read_library(str(share), "fakescales")
    assert scales is not None and scales.installed_version is None


@pytest.mark.parametrize(
    "module",
    [
        pytest.param("missing", id="not-installed"),
        pytest.param("../fakegrid", id="parent-reference"),
        pytest.param("fake grid", id="not-a-module-name"),
        pytest.param("@scope/../../etc", id="scoped-escape"),
    ],
)
def test_nbkrn_read_library_refuses(tmp_path: Path, module: str) -> None:
    assert wa.read_library(str(make_share(tmp_path)), module) is None


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks")
def test_nbkrn_read_library_never_follows_links_out(tmp_path: Path) -> None:
    share = make_share(tmp_path / "env")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "index.js").write_text("secret")
    (outside / "key.pem").write_text("secret key")
    os.symlink(outside, share / "nbextensions" / "linked")
    os.symlink(outside / "key.pem", share / "nbextensions" / "fakegrid" / "key.pem")
    assert wa.read_library(str(share), "linked") is None
    library = wa.read_library(str(share), "fakegrid")
    assert library is not None
    assert sorted(p for p, _ in library.files) == ["index.js", "static/logo.png"]


def test_nbkrn_notification_params_index_first_within_budget() -> None:
    library = wa.LibraryFiles(
        "lib",
        None,
        [("a.map", b"m" * 50), ("big.png", b"p" * 30), ("index.js", b"i" * 40), ("s.css", b"c")],
        [],
    )
    params = wa.notification_params(library, "*", budget=75)
    assert [e["path"] for e in params["files"]] == ["index.js", "s.css", "big.png"]
    assert params["omitted"] == ["a.map"]
    whole = wa.notification_params(library, "*", budget=1000)
    assert "omitted" not in whole and len(whole["files"]) == 4


def test_nbkrn_offers_once_per_module_and_version_transitively(tmp_path: Path) -> None:
    share = make_share(tmp_path)
    sent: list[tuple[str, dict[str, Any]]] = []
    offers = wa.WidgetAssetOffers(lambda m, p: sent.append((m, p)), lambda: str(share))
    state = {
        "_model_module": "fakegrid",
        "_model_module_version": "^1.0.0",
        "_view_module": "fakegrid",
        "_view_module_version": "^1.0.0",
    }
    offers.offer_for_state(state)
    offers.offer_for_state(state)
    offers.offer_for_state({"_model_module": "fakescales", "_model_module_version": "2.0.0"})
    assert [(m, p["module"], p["version"]) for m, p in sent] == [
        ("widget.asset", "fakegrid", "1.2.3"),
        ("widget.asset", "fakescales", "*"),
        # Declared differently from the dependency's offer: a new version key.
        ("widget.asset", "fakescales", "2.0.0"),
    ]


def test_nbkrn_a_library_installed_later_is_offered(tmp_path: Path) -> None:
    sent: list[dict[str, Any]] = []
    share = tmp_path / "share" / "jupyter"
    offers = wa.WidgetAssetOffers(lambda m, p: sent.append(p), lambda: str(share))
    offers.offer_for_state({"_model_module": "fakegrid"})
    assert sent == []
    make_share(tmp_path)
    offers.offer_for_state({"_model_module": "fakegrid"})
    assert [p["module"] for p in sent] == ["fakegrid", "fakescales"]


# --------------------------------------------------------------------------- real kernels


def _purelib(python: str) -> str:
    return subprocess.run(
        [python, "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _fake_env(base_python: str, root: Path, site_of: str | None = None) -> str:
    """A virtual environment whose ``share/jupyter`` holds the fake libraries;
    the packages of ``site_of`` (another interpreter) are added to its path by
    a ``.pth`` file (the kernel runs ``site.main()``, so they load the usual
    way)."""
    env = root / "env"
    subprocess.run([base_python, "-m", "venv", "--without-pip", str(env)], check=True)
    make_share(env)
    python = str(env / "bin" / "python")
    if site_of is not None:
        Path(_purelib(python), "rich.pth").write_text(_purelib(site_of) + "\n")
    return python


def _events_of(events: list[tuple[str, dict[str, Any]]], kind: str) -> list[dict[str, Any]]:
    return [p for m, p in events if m == kind]


HOST_OPEN = (
    "import sys\nhost = sys.modules['_alkera_runtime'].host\n"
    "def opened(module, version='^1.0.0'):\n"
    "    state = {'_model_name': 'M', '_model_module': module, '_model_module_version': version,"
    " '_view_module': module, '_view_module_version': version}\n"
    "    return host.open_comm('jupyter.widget', {'state': state, 'buffer_paths': []}, {},"
    " lambda m: None).comm_id\n"
)


async def test_nbkrn_comm_open_offers_the_library_before_the_model(
    start_kernel: KernelFactory, tmp_path: Path
) -> None:
    ks = await start_kernel(interpreter=_fake_env(sys.executable, tmp_path))
    result = await ks.run(
        step("a", HOST_OPEN + "first = opened('fakegrid')\nsecond = opened('fakegrid')\nNone")
    )
    assert result.status == "ok", result.events
    assets = _events_of(ks.events, "widget.asset")
    assert [(a["module"], a["version"]) for a in assets] == [
        ("fakegrid", "1.2.3"),
        ("fakescales", "*"),
    ]
    assert _files(assets[0]) == {"index.js": GRID_INDEX, "static/logo.png": LOGO}
    assert _files(assets[1]) == {"index.js": SCALES_INDEX}
    assert "omitted" not in assets[0]
    kinds = [m for m, _ in ks.events if m in ("widget.asset", "comm.open")]
    assert kinds == ["widget.asset", "widget.asset", "comm.open", "comm.open"]


async def test_nbkrn_platform_and_missing_modules_are_never_offered(
    start_kernel: KernelFactory, tmp_path: Path
) -> None:
    ks = await start_kernel(interpreter=_fake_env(sys.executable, tmp_path))
    code = HOST_OPEN + (
        "for m in ('@jupyter-widgets/controls', '@jupyter-widgets/base', 'anywidget',"
        " '@alkera/ui-widgets', 'not-installed'):\n    opened(m)\n"
        "host.open_comm('jupyter.widget', {'state': {'value': 1}}, {}, lambda m: None)\nNone"
    )
    result = await ks.run(step("a", code))
    assert result.status == "ok", result.events
    assert len(_events_of(ks.events, "comm.open")) == 6
    assert _events_of(ks.events, "widget.asset") == []


async def test_nbkrn_ipywidgets_custom_module_is_offered_once(
    start_kernel: KernelFactory, rich_python: str, tmp_path: Path
) -> None:
    ks = await start_kernel(interpreter=_fake_env(rich_python, tmp_path, site_of=rich_python))
    code = (
        "import ipywidgets as w, traitlets as t\n"
        "class Grid(w.DOMWidget):\n"
        "    _model_name = t.Unicode('GridModel').tag(sync=True)\n"
        "    _model_module = t.Unicode('fakegrid').tag(sync=True)\n"
        "    _model_module_version = t.Unicode('^1.0.0').tag(sync=True)\n"
        "    _view_name = t.Unicode('GridView').tag(sync=True)\n"
        "    _view_module = t.Unicode('fakegrid').tag(sync=True)\n"
        "    _view_module_version = t.Unicode('^1.0.0').tag(sync=True)\n"
        "slider = w.IntSlider(value=2)\ng1 = Grid()\ng2 = Grid()\ng1"
    )
    result = await ks.run(step("a", code))
    assert result.status == "ok", result.events
    view = result.outputs("a")[0]["application/vnd.jupyter.widget-view+json"]
    assets = _events_of(ks.events, "widget.asset")
    assert [(a["module"], a["version"]) for a in assets] == [
        ("fakegrid", "1.2.3"),
        ("fakescales", "*"),
    ]
    # The asset precedes the comm of the first model that needs it.
    order = [
        (m, p.get("module") or p.get("comm_id"))
        for m, p in ks.events
        if m in ("widget.asset", "comm.open")
    ]
    first_grid = order.index(("comm.open", view["model_id"]))
    assert order.index(("widget.asset", "fakegrid")) < first_grid
