"""At run time the kernel imports only the standard library and itself.

A meta-path hook planted through a ``.pth`` file (it loads with
``site.main()``, before the kernel package) records every import of a module
that is neither in the standard library nor the kernel's own, and every
extension module loaded from outside the standard library. Everything the
kernel imports to start and serve a run is recorded before a marker the
first cell writes; the person's own imports after it prove the hook sees
them."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from nbkrn_harness import KernelFactory, step

HOOK = r"""
import sys, sysconfig, importlib.machinery as _m
_LOG = {log!r}
_STD = set(sys.stdlib_module_names) | {{"__main__", "_alkera_runtime"}}
_DIRS = {{sysconfig.get_path("stdlib"), sysconfig.get_path("platstdlib")}}
_STDLIB_DIRS = tuple(p for p in _DIRS if p)
class _Watch:
    def find_spec(self, name, path=None, target=None):
        top = name.partition(".")[0]
        if top.startswith("_alkera_kernel_v"):
            return None
        for finder in sys.meta_path:
            if finder is self or not hasattr(finder, "find_spec"):
                continue
            spec = finder.find_spec(name, path, target)
            if spec is not None:
                break
        else:
            return None
        origin = spec.origin or ""
        foreign = top not in _STD
        ext = isinstance(spec.loader, _m.ExtensionFileLoader)
        ext = ext and not origin.startswith(_STDLIB_DIRS)
        if foreign or ext:
            with open(_LOG, "a") as fh:
                fh.write(name + "\n")
        return None
sys.meta_path.insert(0, _Watch())
"""


def _make_venv(root: Path) -> Path:
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(root)], check=True)
    return root


async def test_nbkrn_kernel_imports_only_the_standard_library(
    start_kernel: KernelFactory, tmp_path: Path
) -> None:
    venv = _make_venv(tmp_path / "env")
    (sp,) = list((venv / "lib").glob("python*/site-packages"))
    log = tmp_path / "imports.log"
    (sp / "_watch_imports.py").write_text(HOOK.format(log=str(log)))
    (sp / "aaa_watch.pth").write_text("import _watch_imports\n")
    (sp / "thirdparty_probe.py").write_text("VALUE = 1\n")
    ks = await start_kernel(interpreter=str(venv / "bin" / "python"))
    result = await ks.run(
        step(
            "a", f"open({str(log)!r}, 'a').write('--user--\\n')\nprint('hello')\nx = {{'a': 1}}\nx"
        ),
        step("b", "import thirdparty_probe"),
    )
    assert result.status == "ok", result.events
    lines = log.read_text().splitlines()
    kernel_part = lines[: lines.index("--user--")]
    user_part = lines[lines.index("--user--") + 1 :]
    # Anything the .pth machinery itself loads before the kernel is excluded by name.
    assert [m for m in kernel_part if m != "_watch_imports"] == []
    assert user_part == ["thirdparty_probe"]  # the hook does see third-party imports
