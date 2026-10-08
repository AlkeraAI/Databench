"""Starts an Alkera notebook kernel.

Run by the launcher as ``<interpreter> -s -S -X utf8 <mount>/boot.py`` in the
notebook's folder, with the capability token as the first line of stdin.
The order matters and is part of the kernel contract:

1. read the token, then point stdin at ``/dev/null``;
2. on Linux, mark the process non-dumpable (no ptrace or core dump of the
   token by the person's own processes);
3. make the notebook's folder ``sys.path[0]``;
4. run ``site.main()``: the environment's site-packages and ``.pth`` files
   load only now, after the token is held;
5. add the mount's public ``alkera`` package at the end of ``sys.path`` (an
   ``alkera`` the environment installs wins);
6. load ``_alkera_kernel`` from this mount by file path under a
   version-qualified private name, so no installed package can replace it;
7. connect and serve.

Everything is under ``if __name__ == "__main__"`` so a ``spawn`` or
``forkserver`` child that re-imports this file does nothing.
"""

if __name__ == "__main__":
    import os
    import sys

    def _read_token() -> str:
        data = b""
        while not data.endswith(b"\n") and len(data) < 1024:
            chunk = os.read(0, 1)
            if not chunk:
                break
            data += chunk
        null = os.open(os.devnull, os.O_RDONLY)
        os.dup2(null, 0)
        os.close(null)
        return data.decode("ascii", "replace").strip()

    _token = _read_token()
    sys.stdin = open(0, encoding="utf-8", closefd=False)

    if sys.platform.startswith("linux"):
        import ctypes

        try:
            ctypes.CDLL(None, use_errno=True).prctl(4, 0, 0, 0, 0)  # PR_SET_DUMPABLE = 0
        except (OSError, AttributeError):
            pass

    _mount = os.path.dirname(os.path.abspath(__file__))
    _notebook_dir = os.environ.get("ALKERA_NOTEBOOK_DIR") or os.getcwd()
    # The interpreter put this script's folder (or, through a symlink, its
    # real folder) first; the notebook's folder takes its place.
    _script_dirs = {_mount, os.path.dirname(os.path.realpath(__file__))}
    sys.path[:] = [p for p in sys.path if os.path.abspath(p or ".") not in _script_dirs]
    sys.path.insert(0, _notebook_dir)

    import site

    site.main()

    _public = os.path.join(_mount, "public")
    if os.path.isdir(_public):
        sys.path.append(_public)

    import importlib.util
    import re

    _init = os.path.join(_mount, "_alkera_kernel", "__init__.py")
    with open(_init, encoding="utf-8") as _fh:
        _match = re.search(r'^__version__ = "([^"]+)"', _fh.read(), re.MULTILINE)
    _name = "_alkera_kernel_v" + re.sub(r"\W", "_", _match.group(1) if _match else "0")
    _spec = importlib.util.spec_from_file_location(
        _name, _init, submodule_search_locations=[os.path.dirname(_init)]
    )
    assert _spec is not None and _spec.loader is not None
    _kernel = importlib.util.module_from_spec(_spec)
    sys.modules[_name] = _kernel
    _spec.loader.exec_module(_kernel)
    _kernel.serve(_token)
