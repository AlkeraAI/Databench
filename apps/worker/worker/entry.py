"""The worker's dispatch, shared by the open entry and the product's.

``health`` is the container liveness probe ECS runs every 30 s; it is answered
by ``worker.health_probe`` without loading the worker runtime (see that module
for why). Every other command is ``worker.cli``, after ``install`` has
registered whatever extensions the caller ships. Keep this module free of any
other import: whatever is imported here is paid on every probe.
"""

from __future__ import annotations

import sys
from collections.abc import Callable


def main(argv: list[str] | None = None, install: Callable[[], None] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["health"]:
        from worker.health_probe import main as probe_main

        return probe_main(argv[1:])
    if install is not None:
        install()
    from worker.cli import main as cli_main

    return cli_main(argv)
