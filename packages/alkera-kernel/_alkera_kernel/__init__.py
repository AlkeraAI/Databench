"""The Alkera notebook kernel runtime.

Loaded by ``boot.py`` from the platform mount by path, inside the kernel
process, never from the person's environment. Standard library only, and it
contains no marimo code. Every import inside the package is relative: the
package runs under a version-qualified private name.
"""

from __future__ import annotations

__version__ = "0.0.0"


def serve(token: str) -> None:
    """Connect to ``ALKERA_RPC_ENDPOINT``, say hello and run until the
    engine goes away. Never returns normally."""
    import os

    from .connection import Connection
    from .runtime import Kernel

    endpoint = os.environ.get("ALKERA_RPC_ENDPOINT", "")
    scheme, _, path = endpoint.partition(":")
    if scheme != "unix" or not path:
        raise SystemExit(f"alkera-kernel: unsupported ALKERA_RPC_ENDPOINT {endpoint!r}")
    notebook_dir = os.environ.get("ALKERA_NOTEBOOK_DIR") or os.getcwd()
    conn = Connection.connect(path, then_chdir=notebook_dir)
    kernel = Kernel(
        conn, kernel_id=os.environ.get("ALKERA_KERNEL_ID", ""), notebook_dir=notebook_dir
    )
    # The handler goes in before hello: once the engine sees the kernel, it
    # may signal it at any time.
    kernel.interrupts.install()
    kernel.hello(token)
    del token
    kernel.install()
    kernel.serve()
