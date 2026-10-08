"""The sandbox launch each session's shell commands reuse, kept per session.

A fenced session builds the launch its commands run in once (the uid, slice
and, under gVisor, the container of its agent server) and reuses it for every
command after. The launch names an agent server that ends with the session, so
:class:`~alkera_cli.harness.runtime.ChatSession` forgets the session's launches
when it closes. The cache lives in the harness, not beside the shell tool that
fills it, so the session can forget it without importing the tool.
"""

from __future__ import annotations

from alkera_cli.harness.sandbox import SandboxLaunch

_LAUNCHES: dict[tuple[str, bool], SandboxLaunch] = {}


def cached_launch(session_id: str, *, python: bool) -> SandboxLaunch | None:
    """The launch built for ``session_id`` (its own-interpreter variant when
    ``python``), or ``None`` when none is held."""
    return _LAUNCHES.get((session_id, python))


def remember_launch(session_id: str, launch: SandboxLaunch, *, python: bool) -> None:
    _LAUNCHES[(session_id, python)] = launch


def forget_session_launches(session_id: str) -> None:
    """Drop every launch held for ``session_id``; its agent server is gone."""
    for python in (False, True):
        _LAUNCHES.pop((session_id, python), None)


def reset_launches_for_tests() -> None:
    _LAUNCHES.clear()


__all__ = [
    "cached_launch",
    "forget_session_launches",
    "remember_launch",
    "reset_launches_for_tests",
]
