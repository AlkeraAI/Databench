"""Bringing a chat's environment back when the chat wakes.

A cloud chat's default environment lives in box-local state that never
syncs, so a chat that sleeps (the daily backstop, a memory or slot
eviction) or moves to another box starts with a fresh, empty environment,
while its workspace still holds the spec that says what was installed. At
agent start this module decides which of three things is true and tells the
agent on its next turns:

* the environment is empty and the workspace has a spec: it is restored in
  the background (as the chat's own user, inside its sandbox, under a
  deadline) and the agent is told it is restoring until it is done, then
  told once how it went;
* the environment is empty, there is no spec, and the chat ran before: the
  agent is told once, plainly, that its environment was reset;
* otherwise nothing is said.

The registry is keyed by session id; the runtime's
:class:`~alkera_cli.environment.service.EnvironmentService` owns one. A
restore is keyed by the environment it fills as well, so a second wake into an
environment already being restored joins that restore rather than running a
second install into the same target. Each cloud chat has an environment of its
own, so two chats of one workspace restore separately; what they share is the
workspace tree, and the steps that build in it run under the tree's lock
(:class:`TreeLocks`).
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from alkera_cli.environment.instructions import CAPTURE_TOOL_NAME, RECREATE_TOOL_NAME
from alkera_cli.environment.recreate import RecreateReport
from alkera_cli.environment.spec import SPEC_FILENAME, normalize_name

logger = logging.getLogger(__name__)

WakeState = Literal["restoring", "restored", "failed", "reset", "offered"]

#: How long a restore may take before it is reported as failed.
RESTORE_DEADLINE_S = 900.0
#: How long a capture or recreate waits for a restore still running.
RESTORE_WAIT_S = 960.0
#: Distributions a fresh environment is seeded with; an environment holding
#: only these is empty.
_SEED = frozenset({"pip", "setuptools", "wheel"})

RESTORING_NOTICE = (
    "ENVIRONMENT. This chat woke with a fresh Python environment, and the packages in "
    f"{SPEC_FILENAME} are being reinstalled in the background. Until this notice is gone, "
    "a package may be missing; do not reinstall it yourself."
)
RESTORED_NOTICE = (
    f"ENVIRONMENT. The Python environment was restored from {SPEC_FILENAME} after this chat woke."
)
FAILED_NOTICE = (
    "ENVIRONMENT. This chat woke with a fresh Python environment, and restoring it from "
    f"{SPEC_FILENAME} did not finish: {{detail}}. Packages the spec lists may not be "
    f"installed. Call `{RECREATE_TOOL_NAME}` to see what is missing, or reinstall what you "
    "need."
)
OFFERED_NOTICE = (
    f"ENVIRONMENT. This chat woke with a fresh Python environment. The workspace has a "
    f"{SPEC_FILENAME} this chat did not capture, so it was not installed on its own. "
    f"Call `{RECREATE_TOOL_NAME}` to install it (it asks first), or reinstall what you need."
)
RESET_NOTICE = (
    "ENVIRONMENT. This chat woke with a fresh Python environment: packages installed "
    "earlier in this chat are gone. Reinstall what you need, then call "
    f"`{CAPTURE_TOOL_NAME}` so they come back on their own next time."
)


@dataclass
class _Wake:
    state: WakeState
    detail: str = ""
    told: bool = False
    task: asyncio.Task[None] | None = field(default=None, repr=False)
    #: The restore this session waits on, which other sessions may share.
    restore: asyncio.Future[RecreateReport] | None = field(default=None, repr=False)


class TreeLocks:
    """One lock per workspace tree, by its resolved path: the steps of a
    restore that build in the tree (an editable's build hooks, ``uv sync``)
    hold it, so two chats of a workspace restoring at once never build into
    the same folder together."""

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}

    def lock(self, root: Path | str) -> asyncio.Lock:
        key = os.path.realpath(root)
        found = self._locks.get(key)
        if found is None:
            found = self._locks[key] = asyncio.Lock()
        return found


class EnvironmentWakes:
    """The wake state of every session in this process."""

    def __init__(self) -> None:
        self._wakes: dict[str, _Wake] = {}
        #: The restore running into each target environment, by its host path.
        self._restores: dict[str, asyncio.Future[RecreateReport]] = {}

    def state(self, session_id: str) -> WakeState | None:
        wake = self._wakes.get(session_id)
        return wake.state if wake is not None else None

    def notice(self, session_id: str) -> str | None:
        """What the agent is told on this turn: the restoring notice on every
        turn until the restore ends, then its outcome (or the reset) once."""
        wake = self._wakes.get(session_id)
        if wake is None:
            return None
        if wake.state == "restoring":
            return RESTORING_NOTICE
        if wake.told:
            return None
        wake.told = True
        if wake.state == "restored":
            return RESTORED_NOTICE
        if wake.state == "failed":
            return FAILED_NOTICE.format(detail=wake.detail or "the restore failed")
        if wake.state == "offered":
            return OFFERED_NOTICE
        return RESET_NOTICE

    def mark_reset(self, session_id: str) -> None:
        self._wakes[session_id] = _Wake(state="reset")

    def mark_offered(self, session_id: str) -> None:
        self._wakes[session_id] = _Wake(state="offered")

    def restoring(self, target: str) -> bool:
        """Whether a restore into ``target`` is running."""
        running = self._restores.get(target)
        return running is not None and not running.done()

    def begin(
        self,
        session_id: str,
        restore: Callable[[], Awaitable[RecreateReport]] | None,
        *,
        target: str | None = None,
    ) -> None:
        """Start restoring in the background; the outcome lands in the state.
        A restore already running into ``target`` is joined instead of
        started again (``restore`` may then be ``None``): every session
        waiting on it reads the one outcome."""
        shared = self._restores.get(target) if target is not None else None
        if shared is None or shared.done():
            if restore is None:
                raise ValueError("nothing is restoring into this environment to join")
            shared = asyncio.ensure_future(asyncio.wait_for(restore(), timeout=RESTORE_DEADLINE_S))
            if target is not None:
                self._restores[target] = shared
                shared.add_done_callback(functools.partial(self._settled, target))
            logger.info(
                "chat %s: restoring its environment from %s",
                session_id,
                SPEC_FILENAME,
                extra={"chat_id": session_id, "target": target},
            )
        else:
            logger.info(
                "chat %s: joining the restore already running into its environment",
                session_id,
                extra={"chat_id": session_id, "target": target},
            )
        wake = _Wake(state="restoring", restore=shared)
        self._wakes[session_id] = wake
        running = shared

        async def _run() -> None:
            try:
                report = await asyncio.shield(running)
            except TimeoutError:
                wake.state, wake.detail = "failed", f"it ran past {RESTORE_DEADLINE_S:g} s"
            except asyncio.CancelledError:
                if not running.cancelled():
                    raise
                wake.state, wake.detail = "failed", "the restore was stopped"
            except Exception as exc:
                logger.warning(
                    "chat %s: the environment restore raised",
                    session_id,
                    exc_info=True,
                    extra={"chat_id": session_id},
                )
                wake.state, wake.detail = "failed", str(exc)[:300] or type(exc).__name__
            else:
                missing = [g.name or g.kind for g in report.not_recreated]
                if missing:
                    wake.state = "failed"
                    wake.detail = "not recreated: " + ", ".join(missing[:10])
                else:
                    wake.state = "restored"
            _log_outcome(session_id, wake)

        wake.task = asyncio.ensure_future(_run())

    def _settled(self, target: str, done: asyncio.Future[RecreateReport]) -> None:
        """A finished restore stops being the one a later wake joins."""
        if self._restores.get(target) is done:
            del self._restores[target]

    def _orphaned(self, wake: _Wake) -> asyncio.Future[RecreateReport] | None:
        """``wake``'s restore when no other session still waits on it."""
        shared = wake.restore
        if shared is None or shared.done():
            return None
        for other in self._wakes.values():
            if other.restore is shared and other.task is not None and not other.task.done():
                return None
        for target, running in list(self._restores.items()):
            if running is shared:
                del self._restores[target]
        return shared

    async def wait(self, session_id: str, *, limit_s: float = RESTORE_WAIT_S) -> None:
        """Wait for a restore still running (a capture would record half an
        environment; a recreate would install beside it)."""
        wake = self._wakes.get(session_id)
        if wake is None or wake.task is None or wake.task.done():
            return
        with contextlib.suppress(TimeoutError, asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(wake.task), timeout=limit_s)

    async def cancel(self, session_id: str) -> None:
        """Forget the session's wake and stop a restore still running, waiting
        until its install has been killed: a closed or sleeping chat leaves no
        install behind to hold its sandbox awake or race its next wake, which
        starts the restore again."""
        wake = self._wakes.pop(session_id, None)
        if wake is None or wake.task is None or wake.task.done():
            return
        wake.task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await wake.task
        # The install itself stops only when nobody else waits on it.
        if (shared := self._orphaned(wake)) is not None:
            shared.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await shared

    def forget(self, session_id: str) -> None:
        wake = self._wakes.pop(session_id, None)
        if wake is not None and wake.task is not None and not wake.task.done():
            wake.task.cancel()
            if (shared := self._orphaned(wake)) is not None:
                shared.cancel()


def _log_outcome(session_id: str, wake: _Wake) -> None:
    if wake.state == "restored":
        logger.info(
            "chat %s: its environment was restored",
            session_id,
            extra={"chat_id": session_id},
        )
    else:
        logger.warning(
            "chat %s: the environment restore did not finish: %s",
            session_id,
            wake.detail,
            extra={"chat_id": session_id, "detail": wake.detail},
        )


def environment_is_empty(env: Path) -> bool:
    """Whether the environment at ``env`` is missing or holds nothing beyond
    what a fresh one is seeded with. Reads directory names only, never a
    file and never through a link, since the tree is the chat's to change."""
    with contextlib.suppress(OSError):
        if env.is_symlink():
            # The chat put a link where its environment goes: nothing here is
            # read, and nothing is restored into it.
            return False
    sites = [env / "Lib" / "site-packages"]
    lib = env / "lib"
    with contextlib.suppress(OSError):
        if lib.is_dir() and not lib.is_symlink():
            sites.extend(p / "site-packages" for p in lib.iterdir() if p.name.startswith("python"))
    for site in sites:
        try:
            if site.is_symlink() or not site.is_dir():
                continue
            names = os.listdir(site)
        except OSError:
            continue
        for name in names:
            if not name.endswith(".dist-info"):
                continue
            dist = normalize_name(name[: -len(".dist-info")].rsplit("-", 1)[0])
            if dist not in _SEED:
                return False
    return True


__all__ = [
    "FAILED_NOTICE",
    "OFFERED_NOTICE",
    "RESET_NOTICE",
    "RESTORED_NOTICE",
    "RESTORE_DEADLINE_S",
    "RESTORING_NOTICE",
    "EnvironmentWakes",
    "TreeLocks",
    "WakeState",
    "environment_is_empty",
]
