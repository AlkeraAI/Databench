"""What one org's process can reach of another org's on a box.

Run as a process of org A (its worker, or the agent inside one of its
sandboxes), handed what belongs to org B, it reports every way org A reaches
it: a path outside org A's own roots that it may write, org B's data root or
socket that it may open, a process of org B's uid range that it may see in
``/proc``, an org B listening port that accepts its connection, a machine
secret in its environment or a secret file it may read.

The script is standard library only and reads its spec from ``argv[1]`` (a
JSON object, see :class:`Spec`) so a driver can pipe it to ``python3 -`` in
any namespace, as any uid, over ssh, with nothing installed. It prints one
JSON report. Every check is a plain function, so a test drives each against a
directory or a socket it set up itself.
"""

from __future__ import annotations

import errno
import json
import os
import socket
import stat
import sys
from collections.abc import Iterable, Iterator
from dataclasses import asdict, dataclass, field
from pathlib import Path

#: Filesystems a writable sweep never descends into: kernel views whose writable
#: entries are per process and say nothing about another org's data.
_VIRTUAL = ("/proc", "/sys", "/dev")


@dataclass(frozen=True)
class Spec:
    """What the probing process owns, and what belongs to the other org."""

    own_roots: list[str]
    """Paths the probing org may write: its data root, its private tmp."""
    foreign_roots: list[str] = field(default_factory=list)
    """The other org's data root (and anything under it worth naming)."""
    foreign_uid_ranges: list[list[int]] = field(default_factory=list)
    """``[first, last]`` host uid ranges of the other org's processes."""
    foreign_endpoints: list[list[object]] = field(default_factory=list)
    """``[host, port]`` TCP listeners of the other org (its tool servers)."""
    foreign_sockets: list[str] = field(default_factory=list)
    """Unix socket paths of the other org."""
    secret_files: list[str] = field(default_factory=list)
    """Files no org process may read (``/etc/alkera/node.env``)."""
    secret_env: list[str] = field(default_factory=list)
    """Environment names no org process may carry (the machine credential)."""
    sweep_roots: list[str] = field(default_factory=lambda: ["/"])
    """Where the writable sweep starts; from each it stays on that filesystem,
    so a data volume mounted apart from ``/`` is named here too."""
    allowed_writable: list[str] = field(default_factory=list)
    """Writable paths outside ``own_roots`` that hold nothing of any org, each
    named on purpose (a world-writable sticky dir the image ships)."""


@dataclass(frozen=True)
class Violation:
    check: str
    target: str
    detail: str


def _under(path: str, roots: Iterable[str]) -> bool:
    return any(path == r or path.startswith(r.rstrip("/") + "/") for r in roots)


def _walk_same_device(start: str) -> Iterator[tuple[str, os.stat_result]]:
    """Every entry beneath ``start`` on its filesystem, without following links."""
    try:
        device = os.lstat(start).st_dev
    except OSError:
        return
    stack = [start]
    while stack:
        directory = stack.pop()
        try:
            entries = list(os.scandir(directory))
        except OSError:
            continue
        for entry in entries:
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if info.st_dev != device or stat.S_ISLNK(info.st_mode):
                continue
            yield entry.path, info
            if stat.S_ISDIR(info.st_mode) and not _under(entry.path, _VIRTUAL):
                stack.append(entry.path)


def writable_outside(spec: Spec) -> list[Violation]:
    """Every file or directory the process may write outside its own roots."""
    allowed = [*spec.own_roots, *spec.allowed_writable]
    found: list[Violation] = []
    walked = (entry for start in spec.sweep_roots for entry in _walk_same_device(start))
    for path, info in walked:
        if _under(path, allowed) or _under(path, _VIRTUAL):
            continue
        if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
            continue
        if os.access(path, os.W_OK, follow_symlinks=False):
            kind = "directory" if stat.S_ISDIR(info.st_mode) else "file"
            found.append(Violation("writable", path, f"{kind} writable outside the org's roots"))
    return found


def foreign_roots_open(spec: Spec) -> list[Violation]:
    """The other org's roots must not open: listing a directory or reading a
    file is refused (``EACCES``), or the path does not exist for this process."""
    found: list[Violation] = []
    for root in spec.foreign_roots:
        try:
            if os.path.isdir(root):
                os.listdir(root)
            else:
                with open(root, "rb") as handle:
                    handle.read(1)
        except PermissionError:
            continue
        except FileNotFoundError:
            continue
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EPERM, errno.ENOENT):
                continue
            found.append(Violation("foreign-root", root, f"unexpected error {exc!r}"))
            continue
        found.append(Violation("foreign-root", root, "opened"))
    return found


def foreign_processes(spec: Spec, proc: str = "/proc") -> list[Violation]:
    """No process of the other org's uid range is visible in ``/proc``."""
    ranges = [(int(r[0]), int(r[1])) for r in spec.foreign_uid_ranges]
    found: list[Violation] = []
    try:
        pids = [name for name in os.listdir(proc) if name.isdigit()]
    except OSError:
        return found
    for pid in pids:
        try:
            uid = os.stat(Path(proc) / pid).st_uid
        except OSError:
            continue
        if any(first <= uid <= last for first, last in ranges):
            found.append(Violation("foreign-process", pid, f"pid of uid {uid} is visible"))
    return found


def foreign_endpoints_connect(spec: Spec, timeout: float = 1.0) -> list[Violation]:
    """No TCP listener or Unix socket of the other org accepts a connection."""
    found: list[Violation] = []
    for host, port in spec.foreign_endpoints:
        try:
            with socket.create_connection((str(host), int(str(port))), timeout=timeout):
                found.append(Violation("foreign-endpoint", f"{host}:{port}", "connected"))
        except OSError:
            continue
    for path in spec.foreign_sockets:
        unix = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        unix.settimeout(timeout)
        try:
            unix.connect(path)
            found.append(Violation("foreign-socket", path, "connected"))
        except OSError:
            pass
        finally:
            unix.close()
    return found


def secrets_reachable(spec: Spec, environ: dict[str, str] | None = None) -> list[Violation]:
    """No machine secret in the environment, and no secret file readable."""
    env = os.environ if environ is None else environ
    found = [
        Violation("secret-env", name, "set in this process's environment")
        for name in spec.secret_env
        if env.get(name)
    ]
    for path in spec.secret_files:
        try:
            with open(path, "rb") as handle:
                handle.read(1)
        except OSError:
            continue
        found.append(Violation("secret-file", path, "readable"))
    return found


def probe(spec: Spec) -> list[Violation]:
    return [
        *secrets_reachable(spec),
        *foreign_roots_open(spec),
        *foreign_endpoints_connect(spec),
        *foreign_processes(spec),
        *writable_outside(spec),
    ]


def main(argv: list[str]) -> int:
    spec = Spec(**json.loads(argv[1]))
    report = {
        "uid": os.getuid() if hasattr(os, "getuid") else -1,
        "violations": [asdict(v) for v in probe(spec)],
    }
    json.dump(report, sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
