"""This process's own peak resident set, on every platform the suite runs on.

Two proofs in this directory are about memory rather than time — the range read
that must not pull a whole gibibyte in, and the ZIP stream that must not hold a
member — and both answer it the only honest way, by measuring a *fresh*
interpreter. That means the measurement runs as source text in a child that has
none of this suite importable, so the function lives here and is embedded into
each probe with :func:`inspect.getsource` rather than imported by it.

Every platform reports the number differently, and two of the three answers are
wrong on the others, so the branch is on the platform and not on what imports:

* **Linux**: ``ru_maxrss`` will not do. CPython starts a child with fork/vfork,
  so the child inherits the parent's mm *and its peak*, and a pytest worker
  holding a gibibyte of fixtures makes every probe it starts report a gibibyte.
  ``VmHWM`` belongs to the mm ``execve`` made, so it counts this read and
  nothing before it.
* **Windows** has no ``resource`` module at all. Importing it at the top of a
  probe — which both probes used to do — ends the child with a
  ``ModuleNotFoundError`` before it measures anything, and the assertion that
  fails is the one about the subprocess's exit status, which says nothing about
  memory. ``peak_wset`` is the same high-water mark, in bytes.
* **macOS** spawns with ``posix_spawn`` and has no ``/proc``; there
  ``ru_maxrss`` is already this process's own, and already in bytes.

The platform is a parameter so all three branches are reachable from one
machine, and each branch imports what only it needs, so a platform's module
being absent can never break another platform's measurement.
"""

from __future__ import annotations

import inspect


def peak_rss(platform: str) -> int:
    """Peak resident bytes for the calling process under ``platform``."""
    if platform.startswith("linux"):
        with open("/proc/self/status", encoding="utf-8") as status:
            return next(int(line.split()[1]) * 1024 for line in status if line.startswith("VmHWM:"))
    if platform == "win32":
        import psutil

        return int(psutil.Process().memory_info().peak_wset)
    import resource

    return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)


#: ``peak_rss``'s own source, for a probe to paste into the child it runs.
PEAK_RSS_SOURCE: str = inspect.getsource(peak_rss)

__all__ = ["PEAK_RSS_SOURCE", "peak_rss"]
