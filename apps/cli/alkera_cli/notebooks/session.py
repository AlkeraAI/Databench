"""The notebook service a runtime hands its tools, and the per-turn digest brief.

:class:`WorkspaceNotebookService` implements the tools' ``NotebookService``:

- **Which workspace.** A chat's notebooks live in the folder the chat writes
  in (its sandbox: the chat's own scratch folder locally, the workspace's shared
  folder for a workspace chat), so the workspace root is ``ctx.sandbox_dir``.
  A path the agent names is resolved by the engine for that root and refused
  outside it.
- **Who acts.** The chat's agent, as ``agent:<session id>`` (a subagent acts
  as its root chat, whose sandbox it shares), so runs and edits are attributed
  to the chat that made them.
- **The engine.** One host factory per workspace root, made by the injected
  :data:`HostFactory`; the engine itself is the open core's, or for tests the
  simulator's reference engine.
- **The notebook guide.** The first notebook tool result in each conversation
  (each session id: a subagent is its own) carries the essentials of the
  ``notebooks`` skill, remembered for the life of the runtime.
- **Digest cursors.** Per chat, in ``<chat>/notebooks/digest_cursors.json``,
  never shared between chats: each chat is told what happened since its own
  last turn.

:class:`NotebookDigestBrief` is the turn brief provider the runtime asks on
every root turn: the structure-only digest of what others did in the notebooks
this chat has touched.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from alkera_core.atomic_io import write_json_atomic
from alkera_core.project.directory import CHATS_SUBDIR
from alkera_core.versioning import VersionedModel
from alkera_notebook.actors import actor_names
from alkera_notebook.digest import turn_digest
from alkera_notebook.tools import ActorRef, GuideLedger, NotebookHost
from pydantic import BaseModel, Field

from alkera_cli.host import paths
from alkera_cli.notebooks.store_loro import agent_actor_id
from alkera_cli.notebooks.tools import register_notebook_tools
from alkera_cli.plugins.plugin_base.tool import ToolContext, ToolRegistry

if TYPE_CHECKING:
    from alkera_notebook.engine.engine import NotebookEngine

logger = logging.getLogger(__name__)

#: Builds the notebooks of one workspace root for one actor.
HostFactory = Callable[[Path, ActorRef], NotebookHost]
#: The directory a write lands in with no ask under a mode, given the session's
#: sandbox and the chat's folder (the harness's ``write_free_root``), or ``None``.
FreeRoot = Callable[[str, Any, Path], Path | None]

CURSORS_FILE = "digest_cursors.json"
NOTEBOOKS_SUBDIR = "notebooks"


class NotebookDigestCursors(VersionedModel):
    """Per notebook path, the instant up to which this chat has been told what
    others did there."""

    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    cursors: dict[str, datetime] = Field(default_factory=dict)


class FileCursorStore:
    """A chat's digest cursors in one JSON file inside the chat's folder."""

    def __init__(self, chat_folder: Path) -> None:
        self._path = chat_folder / NOTEBOOKS_SUBDIR / CURSORS_FILE
        self._lock = asyncio.Lock()

    def _load(self) -> NotebookDigestCursors:
        try:
            raw = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return NotebookDigestCursors()
        try:
            return NotebookDigestCursors.model_validate_json(raw)
        except ValueError:
            # A damaged file costs the chat one digest, not its turn.
            logger.warning("notebook digest cursors at %s unreadable; starting over", self._path)
            return NotebookDigestCursors()

    async def get(self, path: str) -> datetime | None:
        return (await asyncio.to_thread(self._load)).cursors.get(path)

    async def set(self, path: str, at: datetime) -> None:
        async with self._lock:
            state = await asyncio.to_thread(self._load)
            state.cursors[path] = at
            await asyncio.to_thread(write_json_atomic, self._path, state.model_dump(mode="json"))

    async def paths(self) -> list[str]:
        return sorted((await asyncio.to_thread(self._load)).cursors)


def chat_folder(alkera_dir: Path, session_id: str) -> Path:
    return alkera_dir / CHATS_SUBDIR / session_id


def root_session(ctx: ToolContext) -> str:
    """The chat a call acts for: the root chat for a subagent."""
    return ctx.owner_session_id or ctx.session_id


class WorkspaceNotebookService:
    """The ``NotebookService`` a runtime installs on its tool registry."""

    def __init__(self, host_factory: HostFactory, free_root: FreeRoot | None = None) -> None:
        self._host_factory = host_factory
        # Without the harness's rule, no edit is free: every one is decided.
        self._free_root = free_root
        # Which conversations this runtime has given the notebook guide.
        self._guides = GuideLedger()

    def workspace_root(self, ctx: ToolContext) -> Path | None:
        if ctx.sandbox_dir is not None:
            return Path(ctx.sandbox_dir)
        if ctx.alkera_dir is not None:
            return Path(ctx.alkera_dir).parent
        return None

    def actor(self, ctx: ToolContext) -> ActorRef:
        return ActorRef(
            kind="agent", id=agent_actor_id(root_session(ctx)), display_name=actor_names().agent
        )

    def host_for(self, ctx: ToolContext) -> NotebookHost | None:
        root = self.workspace_root(ctx)
        if root is None:
            return None
        return self._host_factory(root, self.actor(ctx))

    def notebook_path(self, ctx: ToolContext, path: str) -> str:
        """A notebook path as the agent typed it, as the engine reads it: an
        absolute path under a mount the agent sees its folder at (its
        sandbox's ``/home/alkera``) becomes the host path the engine's
        workspace root is, through the session fence's one mapping. Anything
        else is passed on as written, and the engine refuses what is outside
        the workspace."""
        fence = ctx.fence
        if fence is None or not path.startswith("/"):
            return path
        return str(fence.canonical(path))

    def file_path(self, ctx: ToolContext, path: str) -> Path:
        root = self.workspace_root(ctx)
        return (root / path) if root is not None else Path(path)

    def edit_is_free(self, ctx: ToolContext, file: Path) -> bool:
        if ctx.alkera_dir is None or self._free_root is None:
            return False
        folder = chat_folder(Path(ctx.alkera_dir), root_session(ctx))
        free = self._free_root(ctx.permission_mode, ctx.sandbox_dir, folder)
        if free is None:
            return False
        try:
            target, root = file.resolve(), Path(free).resolve()
        except (OSError, RuntimeError, ValueError):
            return False
        return target == root or root in target.parents

    def with_guide(self, ctx: ToolContext, result: BaseModel) -> BaseModel:
        return self._guides.attach(result, ctx.session_id or "")

    def cursors_for(self, ctx: ToolContext) -> FileCursorStore | None:
        if ctx.alkera_dir is None or not ctx.session_id:
            return None
        return FileCursorStore(chat_folder(Path(ctx.alkera_dir), root_session(ctx)))


class NotebookDigestBrief:
    """The turn brief: what others did in this chat's notebooks since its last turn."""

    def __init__(
        self, service: WorkspaceNotebookService, ctx_for_turn: Callable[[], ToolContext | None]
    ) -> None:
        self._service = service
        self._ctx_for_turn = ctx_for_turn

    async def __call__(self) -> str | None:
        ctx = self._ctx_for_turn()
        if ctx is None:
            return None
        cursors = self._service.cursors_for(ctx)
        host = self._service.host_for(ctx)
        if cursors is None or host is None:
            return None
        try:
            digest = await turn_digest(host, cursors)
        except Exception:
            logger.warning("notebook digest failed for this turn", exc_info=True)
            return None
        return digest.text or None


def service_for(
    factory: HostFactory | None,
    write_free_root: Callable[[Any, Any, Path], Path | None],
    parse_mode: Callable[[str], Any],
) -> WorkspaceNotebookService | None:
    """The runtime's notebook service: over ``factory`` when one is injected (a
    test, an embedding host), else over the open core's engine when this build
    carries it, else none (and then no notebook tool is served). The harness's
    own free-write rule decides which edits need no ask."""
    chosen = factory or default_host_factory()
    if chosen is None:
        return None

    def free_root(mode: str, sandbox: Any, folder: Path) -> Path | None:
        return write_free_root(parse_mode(mode) or "default", sandbox, folder)

    return WorkspaceNotebookService(chosen, free_root)


def install(registry: ToolRegistry, service: WorkspaceNotebookService | None) -> None:
    """Serve the notebook tools from ``registry`` when there is a service."""
    if service is not None:
        register_notebook_tools(registry)
        registry.notebooks = service


def serves(binding: Any) -> bool:
    """Whether a session's tool binding serves the notebook tools."""
    return binding is not None and binding.registry.tool_for("notebook.read") is not None


async def notebook_digest(binding: Any, permission_mode: str) -> str | None:
    """This turn's digest for the session bound by ``binding`` (``None`` for a
    session that keeps no cursors, or nothing to report)."""
    service = getattr(getattr(binding, "registry", None), "notebooks", None)
    if not isinstance(service, WorkspaceNotebookService):
        return None
    ctx = binding.registry.build_context(
        permission_mode=permission_mode,
        session_id=binding.session_id,
        owner_session_id=binding.owner_session_id,
        alkera_dir=binding.alkera_dir,
        sandbox_dir=binding.sandbox_dir,
    )
    return await NotebookDigestBrief(service, lambda: ctx)()


def notebooks_available() -> bool:
    """Whether this build carries a notebook engine the tools can open."""
    return default_host_factory() is not None


def default_host_factory() -> HostFactory | None:
    """The production host factory: the open core engine, when this build has
    it, with each workspace's kernels mounting the platform files this build
    has (a compiled build bundles them; it has no installed alkera-kernel)."""
    from alkera_notebook.tools.engine_adapter import engine_available

    if not engine_available():
        return None
    # Lazy: the engine is optional in a build, and these import it.
    from alkera_notebook.tools.engine_adapter import LocalEngines, local_engine

    from alkera_cli.notebooks.engine_host import stage_platform_mount
    from alkera_cli.notebooks.kernel_mount import kernel_sources

    def build(root: Path) -> NotebookEngine:
        sources = kernel_sources()
        mount = stage_platform_mount(
            sources.boot,
            sources.package,
            sources.public,
            paths.ALKERA_HOME / "notebooks" / "kernel-mount",
        )
        return local_engine(root, kernel_mount=mount)

    return LocalEngines(build=build)


__all__ = [
    "FileCursorStore",
    "HostFactory",
    "NotebookDigestBrief",
    "NotebookDigestCursors",
    "WorkspaceNotebookService",
    "chat_folder",
    "default_host_factory",
    "install",
    "notebook_digest",
    "notebooks_available",
    "root_session",
    "serves",
    "service_for",
]
