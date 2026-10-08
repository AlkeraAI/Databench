"""An org worker's supervisor channel: handed over on its standard input,
kept off its standard streams once it runs.

Standard library and the spawn seam only: the process entry moves the
channel before it imports the command tree (``alkera_cli.entry``), and the
worker command moves it again on its own (the move is idempotent once the
channel sits past stderr).
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence

from alkera_core.process import SpawnSpec


def worker_spec(argv: Sequence[str], *, env: Mapping[str, str], channel: int) -> SpawnSpec:
    """The spawn of an org worker whose end of the control socketpair is
    ``channel``, handed over on its standard input, its output collected for
    its org's log. The supervisor ends its workers itself, over the channel:
    they stay in its session with no death signal, so a supervisor restart
    drains them instead of killing them mid-hand-back."""
    return SpawnSpec(
        argv=list(argv),
        env=env,
        stdin=channel,
        # A reason, not a secret (the rule reads any `pass*` name as one).
        pass_fds_reason=(  # noqa: S106
            "the worker's control socketpair is its standard input; the worker "
            "moves it off fd 0 before anything it starts can inherit it"
        ),
        stdout="pipe",
        stderr="stdout",
        new_session=False,
        die_with_parent=False,
    )


def take_control_channel(fd: int) -> int:
    """The supervisor's socketpair, moved off a standard stream: a new fd no
    child inherits, with ``/dev/null`` left where it was. The supervisor
    hands it over on standard input, and every program the worker runs would
    otherwise inherit it there (``runsc exec`` shuts down a socket it gets
    as stdin, which cut the worker off from its supervisor)."""
    moved = os.dup(fd)  # non-inheritable, so no child sees the channel
    if fd <= 2:
        null = os.open(os.devnull, os.O_RDWR)
        try:
            os.dup2(null, fd)
        finally:
            os.close(null)
    else:
        os.close(fd)
    return moved


__all__ = ["take_control_channel", "worker_spec"]
