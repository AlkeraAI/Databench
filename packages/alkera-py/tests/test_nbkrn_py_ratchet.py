"""``alkera`` imports only the standard library and itself, at run time, on
the running Python and on the oldest one it supports (``requires-python``).

A subprocess starts without ``site`` (so no ``.pth`` hook loads anything),
installs a meta-path finder that refuses every top-level module outside the
standard library and ``alkera``, imports every ``alkera`` submodule and
drives the API in plain Python. The source-level scan lives in
``packages/alkera-notebook/tests/test_notebook_import_ratchet.py``.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import alkera
import pytest
from alkera_py_floor import floor_version

PACKAGE_ROOT = Path(alkera.__file__).resolve().parents[1]

PROBE = textwrap.dedent(
    """
    import importlib, importlib.abc, os, sys
    sys.path.insert(0, {root!r})
    ALLOWED = set({stdlib!r}) | {{"alkera", "__future__", "__main__"}}

    class Refuse(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path, target=None):
            if name.partition(".")[0] not in ALLOWED:
                raise ImportError("refused " + name)
            return None

    sys.meta_path.insert(0, Refuse())
    {plant}
    import pkgutil
    import alkera
    for info in pkgutil.walk_packages(alkera.__path__, "alkera."):
        importlib.import_module(info.name)

    import tempfile
    os.chdir(tempfile.mkdtemp())
    out = alkera.vstack([alkera.md("# t"), alkera.html("<b>h</b>"), alkera.image(b"GIF89a....")])
    alkera.output.append(alkera.callout(out, "info"))
    for _ in alkera.status.progress_bar(range(3)):
        pass
    with alkera.status.spinner("s"):
        pass

    @alkera.persistent_cache
    def f(x):
        return x + 1

    assert f(1) == 2 and f(1) == 2
    try:
        alkera.chart
    except ImportError:
        pass
    try:
        alkera.stop(True)
    except alkera.StopCell:
        pass
    third_party = sorted(
        m for m in sys.modules if m.partition(".")[0] not in ALLOWED
    )
    assert not third_party, third_party
    print("ratchet ok", sys.version_info[:2])
    """
)


def _pythons() -> list[object]:
    params: list[object] = [pytest.param(sys.executable, id="current")]
    floor = floor_version()
    uv = shutil.which("uv")
    found = None
    if uv is not None:
        done = subprocess.run(
            [uv, "python", "find", "--no-project", floor],
            capture_output=True,
            text=True,
            check=False,
        )
        found = done.stdout.strip() if done.returncode == 0 and done.stdout.strip() else None
    params.append(
        pytest.param(
            found or "",
            id="floor",
            marks=pytest.mark.skipif(
                found is None, reason=f"no Python {floor} interpreter found by uv"
            ),
        )
    )
    return params


def _probe(python: str, plant: str = "") -> subprocess.CompletedProcess[str]:
    # The standard library list comes from this interpreter: the floor's own
    # sys.stdlib_module_names only exists from 3.10.
    script = PROBE.format(
        root=str(PACKAGE_ROOT), stdlib=sorted(sys.stdlib_module_names), plant=plant
    )
    return subprocess.run(
        [python, "-S", "-c", script], capture_output=True, text=True, check=False, timeout=60
    )


@pytest.mark.parametrize("python", _pythons())
def test_alkera_imports_only_the_standard_library(python: str) -> None:
    done = _probe(python)
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines()[-1].startswith("ratchet ok"), done.stdout


def test_the_probe_refuses_a_third_party_import() -> None:
    # The hook is what makes the green above mean something.
    done = _probe(sys.executable, plant="import pytest")
    assert done.returncode != 0
    assert "refused pytest" in done.stderr
