"""The crash hook the day-one closing proof needs, and nothing else.

A1's last beat is "kill the backend between upload and commit and see no
half-version". A test can only stage that from the outside if it can choose the
instant the process dies: the parts are up, the commit has not been asked for,
and the process goes away the way a machine going down goes away — no unwind,
no cleanup, no chance for the commit path to tidy after itself.

So this module exposes one route that does exactly that, and three walls that
keep it out of any deployment that is not a developer's own:

* the module name starts with ``_``, so the package walk in ``__init__`` that
  mounts every route family SKIPS it — it can only ever be mounted by the
  explicit, gated include there;
* the gate requires ``APP_ENV=local`` **and** ``FILES_TEST_HOOKS=1``. Either
  alone mounts nothing, so a stray environment variable on a real deployment
  and a local box that never opted in are both inert;
* the gate is read when the router is built, so an app that booted without it
  has no such path at all — the answer is the router's own opaque 404, not a
  403 that would confirm the hook exists.
"""

from __future__ import annotations

import os
import signal
import sys
from collections.abc import Callable, Mapping
from typing import Final

from alkera_core.config import settings
from alkera_core.files.errors import InvalidRequest
from fastapi import APIRouter, Query

from backend.auth.dependencies import CurrentPrincipal

#: The environment variable that opts a local backend in. Spelled once.
TEST_HOOKS_ENV: Final = "FILES_TEST_HOOKS"

#: The points the hook will die at. Named rather than free-form so a typo in a
#: spec fails loudly at the call instead of arming nothing: these are the
#: library's own checkpoint names around the content commit
#: (``alkera_core.files.content``).
CRASH_POINTS: Final[frozenset[str]] = frozenset(
    {
        "content.after_store_put",
        "content.after_head",
        "content.before_commit",
        "content.after_commit_before_cleanup",
    }
)


def hooks_enabled(environ: Mapping[str, str] | None = None) -> bool:
    """Whether the hook may be mounted at all.

    ``environ`` is a parameter so the gate can be proven against a planted
    environment rather than by mutating the process's own.
    """
    env: Mapping[str, str] = os.environ if environ is None else environ
    return settings.app_env == "local" and env.get(TEST_HOOKS_ENV) == "1"


def _sigkill_self(
    *,
    platform: str = sys.platform,
    kill: Callable[[int, int], None] = os.kill,
) -> None:
    """Die the way a machine dies: no unwind, no atexit, no half-written commit.

    The signal that does that is not the same one everywhere, and naming the
    POSIX one unconditionally is not merely wrong on Windows but unrunnable:
    ``signal.SIGKILL`` does not exist there, so the hook would raise
    ``AttributeError`` instead of dying and the proof would wait for a process
    that is still serving. Windows' equivalent is ``TerminateProcess``, which is
    what ``os.kill`` routes any signal other than the two console events to —
    the process ends where it stands, with no handler and no unwind, which is
    the property the proof is actually about.

    ``platform`` and ``kill`` are parameters so both branches can be driven from
    one machine; nothing but a test passes them.
    """
    if platform == "win32":
        kill(os.getpid(), signal.SIGTERM)
        return
    kill(os.getpid(), signal.SIGKILL)


def build_test_hooks_router(
    *,
    killer: Callable[[], None] = _sigkill_self,
    environ: Mapping[str, str] | None = None,
) -> APIRouter | None:
    """The hook's router when the gate is open, and ``None`` when it is not.

    ``killer`` is injectable so the routing and the refusal can be proven by a
    test that survives them.
    """
    if not hooks_enabled(environ):
        return None

    router = APIRouter(prefix="/_test", tags=["files-test-hooks"])

    @router.post("/crash", status_code=204)
    async def crash(_principal: CurrentPrincipal, at: str = Query(...)) -> None:
        """Kill this process at the named point.

        The name is checked, not scheduled: the caller drives the ordering
        (parts uploaded, commit not yet asked for), so it is here to keep the
        beat readable and to refuse a point the library no longer has.

        The principal dependency is the fourth wall: an opted-in developer box
        still runs a browser, and a page loaded from anywhere could POST here.
        Every other Files route resolves a credential before it acts and so
        does this one, so an unauthenticated caller gets 401 rather than a dead
        backend.
        """
        if at not in CRASH_POINTS:
            raise InvalidRequest(
                code="files.unknown_crash_point",
                message="no such crash point",
            )
        killer()

    return router


__all__ = [
    "CRASH_POINTS",
    "TEST_HOOKS_ENV",
    "build_test_hooks_router",
    "hooks_enabled",
]
