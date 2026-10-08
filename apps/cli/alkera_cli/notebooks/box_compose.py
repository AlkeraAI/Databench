"""The box's notebooks composed for production, and the slot that holds them.

The runtime is built before the box registers, and what its notebooks need
(the folder custody, the credential, the API) comes with registration, so the
runtime and the machine channel hold a :class:`BoxNotebookSlot` that composes
:class:`~alkera_cli.notebooks.box.BoxNotebooks` once the box can serve them:
each workspace's engine in its own kernel sandbox where the box runs gVisor,
local kernels where it does not, and its SQL from the providers registered
on :data:`~alkera_cli.notebooks.sql_slot.NOTEBOOK_SQL_PROVIDERS` (a
workspace's Alkera connections, in the product).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import httpx
from alkera_core.schemas.realtime.machine import NotebookMachineRequest
from alkera_notebook.envs.models import EnvRegistry
from alkera_notebook.tools.port import ActorRef, NotebookHost, NotebookToolError

from alkera_cli.notebooks.engine_host import WorkspaceTenancy, sql_workspace_id

if TYPE_CHECKING:
    from alkera_notebook.engine.engine import NotebookEngine
    from alkera_notebook.sql.provider import SqlEngineProvider


from alkera_notebook.envs.python import default_python
from alkera_notebook.envs.template import default_env_template
from alkera_notebook.kernels.launch_local import LocalSubprocessLauncher
from alkera_notebook.sql.provider import SqlProviderRegistry
from alkera_notebook.tools.engine_adapter import local_engine

from alkera_cli.cloud.rest import backend_client, ws_url_for
from alkera_cli.files.folder_fence import Fenced, register_fence_holder
from alkera_cli.harness.sandbox import SandboxSettings, select_runtime
from alkera_cli.harness.sandbox_env import shares_workspace_envs
from alkera_cli.harness.sandbox_layout import workspace_dir_name
from alkera_cli.harness.sandbox_probe import current_capability
from alkera_cli.harness.sandbox_steps import SandboxRefusedError
from alkera_cli.harness.sandbox_uid import ensure_chat_uid
from alkera_cli.harness.web_flags import WebToolFlags
from alkera_cli.harness.workspace_sandbox import register_teardown
from alkera_cli.host import paths
from alkera_cli.notebooks.box import (
    BoxNotebooks,
    BoxStores,
    EngineFor,
    FolderCustody,
    HeldFolders,
    PersonOf,
    StoreSource,
)
from alkera_cli.notebooks.box_events import HttpKernelEvents
from alkera_cli.notebooks.engine_host import (
    NotebookEngineHost,
    NotebookHostSettings,
    notebook_engine_factory,
    stage_platform_mount,
)
from alkera_cli.notebooks.kernel_mount import kernel_sources
from alkera_cli.notebooks.sql_slot import NotebookSqlContext, sql_providers
from alkera_cli.notebooks.store_loro import RealtimeDocSignals

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Composition: the slot the runtime and the machine channel hold
# ---------------------------------------------------------------------------

#: Builds the box's notebooks once the box can serve them: its folder custody,
#: the headers every request carries (the box's own credential, read at each
#: request so a rotated bearer is used at once) and the API's URL.
Composer = Callable[[FolderCustody, Callable[[], Mapping[str, str]], str], BoxNotebooks]


class BoxNotebookSlot:
    """The box's notebooks as the runtime and the machine channel hold them.

    The runtime is built before the box has registered, and the folder
    custody and credential the notebooks need come with registration, so
    both the agents' host factory and the channel's handler are this slot,
    which serves once :meth:`start` composed the notebooks and refuses
    plainly until then."""

    def __init__(self, compose: Composer | None = None) -> None:
        self._compose = compose or compose_box_notebooks
        self._box: BoxNotebooks | None = None

    @property
    def box(self) -> BoxNotebooks | None:
        return self._box

    def start(
        self,
        custody: FolderCustody,
        headers: Callable[[], Mapping[str, str]],
        api_url: str,
        *,
        person_of: PersonOf | None = None,
    ) -> BoxNotebooks:
        """Compose the notebooks once; ``person_of`` names the person each
        chat's agent acts for (the box's chats are the service's)."""
        if self._box is None:
            self._box = self._compose(custody, headers, api_url)
        if person_of is not None:
            self._box.use_people(person_of)
        return self._box

    def __call__(self, root: Path, actor: ActorRef) -> NotebookHost:
        if self._box is None:
            raise NotebookToolError(
                "unavailable", "Notebooks are not served on this box until it has registered."
            )
        return self._box.host_factory(root, actor)

    def dispatch(self, frame: Mapping[str, Any]) -> None:
        """A notebook ``machine.request`` from the channel, served off it."""
        try:
            request = NotebookMachineRequest.model_validate(frame)
        except ValueError:
            logger.warning("machine channel: a notebook request is malformed; left unanswered")
            return
        if self._box is None:
            logger.warning(
                "machine channel: notebook request %s arrived before notebooks are served",
                request.request_id,
            )
            return
        self._box.dispatch(request)

    async def close(self) -> None:
        if self._box is not None:
            await self._box.close()


def box_notebook_slot(
    runtime: Callable[[], Any],
    *,
    org_id: str = "",
    org_root: Path | None = None,
    data_home: Path | None = None,
) -> BoxNotebookSlot:
    """The slot a box's commands hand its runtime and its service: composed,
    once the machine is adopted, with ``runtime()``'s connections as each
    workspace's SQL (a box that serves one org names it; without one, a
    notebook resolves no platform connection)."""

    def compose(
        custody: FolderCustody, headers: Callable[[], Mapping[str, str]], api_url: str
    ) -> BoxNotebooks:

        built = runtime()

        async def tools() -> Any:
            return await built.tool_registry_for(WebToolFlags())

        return compose_box_notebooks(
            custody,
            headers,
            api_url,
            org_root=org_root,
            data_home=data_home,
            org_id=org_id,
            sql=sql_providers(NotebookSqlContext(runtime=built, tools=tools)),
        )

    return BoxNotebookSlot(compose=compose)


class _HeaderAuth(httpx.Auth):
    """The box's headers, read at each request."""

    def __init__(self, headers: Callable[[], Mapping[str, str]]) -> None:
        self._headers = headers

    def auth_flow(self, request: httpx.Request) -> Any:
        for name, value in self._headers().items():
            request.headers[name] = value
        yield request


def box_http_client(
    headers: Callable[[], Mapping[str, str]],
    api_url: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> httpx.AsyncClient:
    """The client the box's notebooks speak to the backend on: every request
    signed with ``headers()`` as they are at that moment (the worker's
    credential is replaced every few minutes)."""
    return backend_client(api_url, auth=_HeaderAuth(headers), timeout=30.0, transport=transport)


def compose_box_notebooks(
    custody: FolderCustody,
    headers: Callable[[], Mapping[str, str]],
    api_url: str,
    *,
    org_root: Path | None = None,
    data_home: Path | None = None,
    sql: Sequence[SqlEngineProvider] = (),
    org_id: str = "",
) -> BoxNotebooks:
    """The production box notebooks: the held folders, the platform's live
    document, the backend's events route, and each workspace's engine in its
    own kernel sandbox where the box runs gVisor (an org worker, under its
    org's root), or with local kernels where it does not (a developer's box).
    """

    http = box_http_client(headers, api_url)
    folders = HeldFolders(custody)
    stores = BoxStores(
        http=http,
        folders=folders,
        signals=RealtimeDocSignals(http=http, ws_url=ws_url_for(api_url)),
    )
    engine_for = sandboxed_engines(stores, sql, org_root=org_root, org_id=org_id) or local_engines(
        stores, sql, data_home=data_home, org_id=org_id
    )
    box = BoxNotebooks(
        folders=folders,
        engine_for=engine_for,
        events=HttpKernelEvents(http),
        locate=stores.locate,
    )
    register_teardown(box.put_away)
    return box


def sandboxed_engines(
    stores: StoreSource,
    sql: Sequence[SqlEngineProvider],
    *,
    org_root: Path | None,
    org_id: str = "",
) -> EngineFor | None:
    """Each workspace's engine with its kernels in the workspace's kernel
    sandbox (:class:`~alkera_cli.notebooks.engine_host.NotebookEngineHost`),
    or ``None`` when this box runs no sandbox: no org root to keep the
    sandboxes under, or a box not set to gVisor.

    An org box set to gVisor whose host cannot run it refuses every kernel,
    as it refuses every chat (:func:`select_runtime`): a local kernel there
    would run as the org worker, which reaches every member's files."""
    if org_root is None:
        return None

    settings = SandboxSettings.from_env()
    capability = current_capability()
    if settings.mode != "gvisor":
        return None
    if not capability.gvisor:
        return _refused_engines(settings)
    sources = kernel_sources()
    python_home = capability.python_home or settings.python_home
    host = NotebookEngineHost(
        org_root=org_root,
        settings=NotebookHostSettings(
            sandbox=settings,
            capability=capability,
            platform_mount=stage_platform_mount(
                sources.boot, sources.package, sources.public, org_root / "notebook-mount"
            ),
            memory_mb=settings.pool_memory_mb,
            vcpu=settings.pool_vcpu,
        ),
        ensure_uid=ensure_chat_uid,
        engine_factory=notebook_engine_factory(
            store_for=stores.store_for,
            sql=SqlProviderRegistry(list(sql)),
            python_home=python_home,
            default_template=default_env_template(org_root / "notebook-env-template"),
            org_id=org_id,
        ),
    )
    host.register()

    def engine_for(tenancy: WorkspaceTenancy) -> NotebookEngine:
        return cast("NotebookEngine", host.engine_for(tenancy))

    return engine_for


def _refused_engines(settings: SandboxSettings) -> EngineFor:
    """An engine source that refuses every workspace, for a box that must
    sandbox kernels and cannot."""

    def refuse(tenancy: WorkspaceTenancy) -> NotebookEngine:
        del tenancy
        select_runtime(settings.mode, gvisor_ready=current_capability().gvisor)
        raise SandboxRefusedError("the kernel sandbox is unavailable on this node")

    return refuse


def local_engines(
    stores: StoreSource,
    sql: Sequence[SqlEngineProvider],
    *,
    data_home: Path | None,
    org_id: str = "",
    shared_envs: bool | None = None,
    envs: Callable[[WorkspaceTenancy], EnvRegistry] | None = None,
) -> EngineFor:
    """Each workspace's engine with local kernels (a box without gVisor),
    its kernels' data kept under ``data_home`` per workspace. ``shared_envs``
    is whether the workspace's members share its environments here, which
    the engine reports on its listing (the box's sandbox mode decides it
    when not given: never on a ``none`` box)."""

    home = data_home or paths.ALKERA_HOME / "notebooks"
    shared = shares_workspace_envs() if shared_envs is None else shared_envs
    # The box's own Python (the one its sandboxes run), so a compiled build
    # never needs uv to download one.
    python = default_python(SandboxSettings.from_env().python_home)
    launchers: dict[str, LocalSubprocessLauncher] = {}

    def engine_for(tenancy: WorkspaceTenancy) -> NotebookEngine:
        launcher = launchers.setdefault(tenancy.key, LocalSubprocessLauncher())
        # The kernel's platform files as this box has them (a compiled build
        # bundles them; it has no installed alkera-kernel to link to).
        sources = kernel_sources()
        mount = stage_platform_mount(
            sources.boot, sources.package, sources.public, home / "kernel-mount"
        )
        return local_engine(
            tenancy.folder,
            data_root=home / workspace_dir_name(tenancy.key),
            sql=list(sql),
            store=stores.store_for(tenancy),
            workspace_id=sql_workspace_id(tenancy.key),
            org_id=org_id,
            shared_envs=shared,
            launcher=launcher,
            python=python,
            kernel_mount=mount,
            # The workspace's own environments unless a caller names others.
            envs=None if envs is None else envs(tenancy),
        )

    async def follow_fences(fenced: Fenced) -> None:
        """No cgroup to freeze here: each workspace's kernel process groups are
        stopped while its folder lease is in doubt and go on after."""
        for key, launcher in list(launchers.items()):
            doubt = fenced(key)
            if doubt == launcher.fenced:
                continue
            logger.warning("workspace %s: kernels %s", key, "stopped" if doubt else "resumed")
            await asyncio.to_thread(launcher.fence if doubt else launcher.unfence)

    register_fence_holder(follow_fences)
    return engine_for


__all__ = [
    "BoxNotebookSlot",
    "Composer",
    "box_http_client",
    "box_notebook_slot",
    "compose_box_notebooks",
    "local_engines",
    "sandboxed_engines",
]
