"""The runtime's environment service: what happens to a chat's environment
when its agent starts and when its session ends, and the record of which
spec this chat captured.

One instance per :class:`~alkera_cli.harness.runtime.HarnessRuntime`, owned
by it and handed to the tools through the session's tool registry. Running a
command in a chat's sandbox needs the shell tools' launch machinery, which
the runtime cannot import, so the registry carries a
:data:`ChatRunnerFactory` the shell tools provide and the service takes it as
an argument; nothing here is a module-global registry.

**Provenance.** A spec is restored automatically at a wake only when this
chat captured it: :meth:`EnvironmentService.record_capture` writes the spec's
digest into the chat's own records (beside its working directory, which the
agent cannot write, and outside the box-local runtime state, so it moves with
the chat). A spec that arrived any other way (committed in a cloned
repository, written by hand, written by the agent) is offered to the agent,
which can install it through ``environment.recreate`` and its permission
gate; it is never installed on its own. Even a spec this chat captured runs
a step reading a tree file (a ``requirements.txt``, the project's build
hooks) only when that file is still the one the spec recorded.

**A cut-off restore.** A restore under way is recorded in the chat's records
too, and the record goes only when the restore ran to its end. A box that
died, slept or was stopped mid-install leaves it behind, and the next wake
restores again (the plan is incremental, so only what is missing is
installed) instead of taking the half-filled environment for a whole one.

**The off switch.** ``ALKERA_CLOUD_ENV_RESTORE=0`` stops every automatic
restore on a box; a chat whose captured spec would have been restored is
offered it instead, as one of unknown origin is.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from alkera_core.project import write_text_atomic
from alkera_core.project.directory import CHATS_SUBDIR

from alkera_cli.environment.files import parse_spec, read_workspace_text
from alkera_cli.environment.recreate import RecreateReport, recreate
from alkera_cli.environment.runner import CommandRunner
from alkera_cli.environment.spec import SPEC_FILENAME
from alkera_cli.environment.wake import (
    RESTORE_DEADLINE_S,
    EnvironmentWakes,
    TreeLocks,
    environment_is_empty,
)

logger = logging.getLogger(__name__)

#: The chat's record of the spec it captured, in its records beside the
#: working directory.
PROVENANCE_FILENAME = "environment-provenance.json"
#: The chat's record of a restore under way, beside its provenance.
RESTORING_FILENAME = "environment-restoring.json"
_MAX_SPEC_BYTES = 8 * 1024 * 1024
#: The box setting that turns automatic restores off (``0``, ``false``, ``off``).
ENV_RESTORE = "ALKERA_CLOUD_ENV_RESTORE"
_OFF = frozenset({"0", "false", "no", "off"})


def auto_restore_from_env(env: Mapping[str, str] | None = None) -> bool:
    """Whether a wake restores a captured spec on its own: on unless
    :data:`ENV_RESTORE` says ``0`` / ``false`` / ``no`` / ``off``."""
    source = os.environ if env is None else env
    return source.get(ENV_RESTORE, "").strip().lower() not in _OFF


class SessionFenceLike(Protocol):
    """What the service reads off a cloud chat's fence."""

    working_dir: Path
    aliases: tuple[tuple[str, Path], ...]

    def spell(self, path: Path | str) -> str: ...


@dataclass(frozen=True, slots=True)
class ChatLaunch:
    """Where a cloud chat's environment lives and how to run in its sandbox."""

    runner: CommandRunner
    default_env: Path
    """The default environment, host path."""
    python: str
    """The interpreter the sandbox's ``PATH`` resolves first."""


ChatRunnerFactory = Callable[[str, Any, Path], "ChatLaunch | None"]
"""``(session_id, fence, alkera_dir)`` -> the chat's launch, or ``None`` when
the chat has no sandbox environment. Provided by the shell tools."""


def spec_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class EnvironmentService:
    """The environment side of a runtime's chats (see the module docstring)."""

    def __init__(self, *, auto_restore: bool | None = None) -> None:
        self.wakes = EnvironmentWakes()
        self.tree_locks = TreeLocks()
        self.auto_restore = auto_restore_from_env() if auto_restore is None else auto_restore

    # -- provenance -------------------------------------------------------

    @staticmethod
    def _provenance_path(alkera_dir: Path, session_id: str) -> Path:
        return Path(alkera_dir) / CHATS_SUBDIR / session_id / PROVENANCE_FILENAME

    def record_capture(self, alkera_dir: Path, session_id: str, spec_text: str) -> None:
        """Remember that this chat captured exactly ``spec_text``."""
        record = {
            "sha256": spec_digest(spec_text),
            "captured_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "by": "environment.capture",
        }
        path = self._provenance_path(alkera_dir, session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        write_text_atomic(path, json.dumps(record) + "\n")

    @staticmethod
    def _restoring_path(alkera_dir: Path, session_id: str) -> Path:
        return Path(alkera_dir) / CHATS_SUBDIR / session_id / RESTORING_FILENAME

    def _mark_restoring(self, alkera_dir: Path, session_id: str, target: str) -> None:
        record = {
            "target": target,
            "started_at": datetime.now(UTC).isoformat(timespec="seconds"),
        }
        path = self._restoring_path(alkera_dir, session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        write_text_atomic(path, json.dumps(record) + "\n")

    def _clear_restoring(self, alkera_dir: Path, session_id: str) -> None:
        with contextlib.suppress(FileNotFoundError):
            self._restoring_path(alkera_dir, session_id).unlink()

    def restore_was_cut_off(self, alkera_dir: Path, session_id: str) -> bool:
        """Whether a restore of this chat's environment started and never ran
        to its end."""
        return self._restoring_path(alkera_dir, session_id).is_file()

    def captured_here(self, alkera_dir: Path, session_id: str, spec_text: str) -> bool:
        path = self._provenance_path(alkera_dir, session_id)
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        return isinstance(record, dict) and record.get("sha256") == spec_digest(spec_text)

    # -- the session's life -----------------------------------------------

    def notice(self, session_id: str) -> str | None:
        return self.wakes.notice(session_id)

    async def wait(self, session_id: str) -> None:
        await self.wakes.wait(session_id)

    async def start(
        self,
        *,
        session_id: str,
        fence: SessionFenceLike | None,
        alkera_dir: Path,
        resumed: bool,
        launch_for: ChatRunnerFactory | None,
    ) -> None:
        """At a cloud chat's agent start: restore its environment when it came
        up empty and the workspace holds a spec this chat captured; offer a
        spec of unknown origin; say the environment was reset when there is
        no spec. Returns at once; a restore runs in the background, in the
        chat's sandbox as its user, under :data:`RESTORE_DEADLINE_S`."""
        if fence is None or launch_for is None:
            return
        launch = launch_for(session_id, fence, alkera_dir)
        if launch is None:
            return
        cut_off = await asyncio.to_thread(self.restore_was_cut_off, alkera_dir, session_id)
        empty = await asyncio.to_thread(environment_is_empty, launch.default_env)
        if not empty and not cut_off:
            return
        target = str(launch.default_env)
        if self.wakes.restoring(target):
            # A restore into this very environment is running: this wake
            # waits on it rather than installing a second time beside it.
            self.wakes.begin(session_id, None, target=target)
            return
        root = Path(fence.working_dir)
        text = await asyncio.to_thread(
            read_workspace_text, root, SPEC_FILENAME, max_bytes=_MAX_SPEC_BYTES
        )
        # Parsed from the very text whose digest is checked below: a second
        # read could find a spec swapped in between.
        spec = parse_spec(text) if text is not None else None
        if spec is None or text is None:
            await asyncio.to_thread(self._clear_restoring, alkera_dir, session_id)
            if resumed and empty:
                self.wakes.mark_reset(session_id)
            return
        if not await asyncio.to_thread(self.captured_here, alkera_dir, session_id, text):
            await asyncio.to_thread(self._clear_restoring, alkera_dir, session_id)
            self.wakes.mark_offered(session_id)
            return
        if not self.auto_restore:
            logger.info(
                "chat %s: automatic environment restores are off on this box; offering the spec",
                session_id,
                extra={"chat_id": session_id},
            )
            self.wakes.mark_offered(session_id)
            return
        if cut_off and not empty:
            logger.info(
                "chat %s: its last environment restore was cut off; restoring again",
                session_id,
                extra={"chat_id": session_id},
            )
        aliases = (str(root), *(a for a, real in fence.aliases if Path(real) == root))
        tree_lock = self.tree_locks.lock(root)
        await asyncio.to_thread(self._mark_restoring, alkera_dir, session_id, target)

        async def _restore() -> RecreateReport:
            report = await recreate(
                spec,
                launch.runner,
                target_env=fence.spell(launch.default_env),
                target_root=fence.spell(root),
                aliases=aliases,
                fallback_python=launch.python,
                deadline_s=RESTORE_DEADLINE_S,
                pinned_inputs=True,
                tree_lock=lambda: tree_lock,
            )
            # Ran to its end (whatever it could not install is in the report):
            # the next wake reads the environment as it is.
            await asyncio.to_thread(self._clear_restoring, alkera_dir, session_id)
            return report

        self.wakes.begin(session_id, _restore, target=target)

    async def start_session(
        self, chat: Any, path_fence: Any, alkera_dir: Path, registry: Any
    ) -> None:
        """The runtime's one call after a chat's agent starts. Hands this
        service to the session's tools (through its tool registry), then, for a
        root chat bounded by a fence, runs :meth:`start`; a local session or a
        subagent (which shares its root's environment) starts nothing."""
        registry.environment = self
        manifest = chat.manifest
        if path_fence is None or manifest.parent_session_id:
            return
        resumed = manifest.last_message_preview is not None or manifest.tokens_total.input > 0
        await self.start(
            session_id=chat.session_id,
            fence=path_fence.session,
            alkera_dir=alkera_dir,
            resumed=resumed,
            launch_for=getattr(registry, "chat_launch_factory", None),
        )

    async def close(self, session_id: str) -> None:
        """A closed or sleeping chat leaves no restore running."""
        await self.wakes.cancel(session_id)


__all__ = [
    "ENV_RESTORE",
    "PROVENANCE_FILENAME",
    "RESTORING_FILENAME",
    "ChatLaunch",
    "ChatRunnerFactory",
    "EnvironmentService",
    "auto_restore_from_env",
    "spec_digest",
]
