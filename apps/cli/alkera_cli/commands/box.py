"""``alkera cloud-mirror run`` — the daemon as a cloud chat's publisher.

Runs a :class:`CloudMirrorService` against the backend the machine was signed
in to (``alkera login``; ``--api-url`` / ``ALKERA_API_URL`` override the
stored one, which is how a box reaches a backend behind a tunnel) with the
same production harness runtime the editor daemon builds: opencode through
the gateway, the audit reporter, the safety judge. It refuses to start when
the opencode harness does not resolve, with a message that names the fix.

``alkera serve --cloud-mirror`` runs the same service beside the stdio
JSON-RPC daemon; this module is the standalone form a provisioned box uses.

``alkera cloud-mirror status`` prints, per team connection, the schema cards
the mirror holds in the workspace and when they were last refreshed — the
check an operator runs before letting anyone prompt against the box.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import os
import signal
import socket
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

import typer
from alkera_core.compute.box_egress import apply_allowlist

from alkera_cli.account.auth_file import Profile, ProfileResolutionError, resolve_profile
from alkera_cli.account.device_flow import DeviceCodeResponse, DeviceFlowError
from alkera_cli.box_status import status_file_from_env
from alkera_cli.cloud import personal_box
from alkera_cli.cloud.box_auth import BearerAuth
from alkera_cli.cloud.box_data import box_data
from alkera_cli.cloud.budget import TurnBudget
from alkera_cli.cloud.folder import ChatFolders
from alkera_cli.cloud.org_worker import (
    prepare_worker,
    read_hello,
    run_worker,
    take_control_channel,
    worker_settings,
)
from alkera_cli.cloud.personal_box import PersonalBoxError, load_personal_box
from alkera_cli.cloud.publisher_identity import (
    MachineIdentity,
    MachineIdentityError,
    require_machine_identity,
)
from alkera_cli.cloud.service import (
    ENV_API_URL,
    CloudMirrorService,
    MirrorSettings,
    drain_ceiling_from_env,
    memory_cap_from_env,
    mirror_limits_from_env,
    supervision_from_env,
)
from alkera_cli.cloud.sleep_policy import SleepSettings, parked_ask_hours_from_env
from alkera_cli.cloud.worker_credential import (
    WorkerConnectionsClient,
    WorkerCredential,
    WorkerFolders,
    worker_rest_client,
)
from alkera_cli.cloud_sync.client import ChatConnectionsClient
from alkera_cli.daemon.logging_setup import configure as configure_logging
from alkera_cli.harness import HarnessRuntime, HarnessUnavailableError
from alkera_cli.harness.sandbox_firewall import open_chat_links
from alkera_cli.harness.sandbox_processes import SandboxProbes
from alkera_cli.host.config import get_settings
from alkera_cli.host.version_info import daemon_version
from alkera_cli.org_root import OrgRoot
from alkera_cli.supervisor.cli import main as supervise_main
from alkera_cli.ui import console

if TYPE_CHECKING:
    from alkera_core.project import ProjectDirectory

    from alkera_cli.notebooks.box_compose import BoxNotebookSlot
    from alkera_cli.notebooks.session import HostFactory

logger = logging.getLogger(__name__)

cloud_mirror_app: typer.Typer = typer.Typer(
    no_args_is_help=True, help="Serve cloud chats from this machine's daemon."
)


def _notebook_slot(
    runtime: Callable[[], HarnessRuntime],
    *,
    org_id: str,
    org_root: Path | None = None,
    data_home: Path | None = None,
) -> BoxNotebookSlot:
    """The box's notebooks, composed once the machine is adopted. Loaded by
    the commands that serve chats, so the command tree never loads the
    notebook engines."""
    from alkera_cli.notebooks.box_compose import box_notebook_slot

    return box_notebook_slot(runtime, org_id=org_id, org_root=org_root, data_home=data_home)


def build_mirror_runtime(
    project_dir: Path,
    *,
    on_machine_credential: bool = False,
    api_url: str | None = None,
    machine_credential: str | None = None,
    sandbox_probes: SandboxProbes | None = None,
    connections: ChatConnectionsClient | None = None,
    notebook_host_factory: HostFactory | None = None,
) -> HarnessRuntime:
    """The production runtime, rooted at ``project_dir/.alkera`` — the same
    strategies the editor daemon wires (gateway-routed opencode, the audit
    reporter, the auto-mode judge, the seed subprocess).

    A box on its own machine credential (``on_machine_credential``) holds no
    credential the gateway accepts and no login on its disk. Its judge, its
    subagent model resolver and its org web-tool flags are the forms that
    present nothing of their own: the runtime binds each to a chat's gateway
    token as it opens the chat, so every gateway call a chat causes is made
    as that chat, and a chat with no token stops rather than borrows.

    Such a box also reads its team connections per chat it holds, into ONE
    workspace store that is the union over every org it serves, so its runtime
    is given the chat-scoped read (``api_url`` + ``machine_credential``, the
    box's own door to ``GET /chats/{id}/connections``) as each chat's
    connection scope: a chat sees its owner's set and nothing of another
    org's. A machine-credential runtime without that door is refused here —
    it would serve every chat the union.
    """

    from alkera_cli.app.compose import production_runtime

    connection_scope = None
    if connections is not None:
        connection_scope = connections.records_for_chat
    elif on_machine_credential:
        if not api_url or not machine_credential:
            raise ValueError(
                "a box on its machine credential needs its API URL and credential to scope "
                "each chat's connections; refusing to build a runtime that would serve the union"
            )
        connection_scope = ChatConnectionsClient(
            api_url=api_url, token=machine_credential, chats=lambda: []
        ).records_for_chat

    return production_runtime(
        "box",
        project_dir.resolve(),
        on_machine_credential=on_machine_credential,
        connection_scope=connection_scope,
        sandbox_probes=sandbox_probes,
        notebook_host_factory=notebook_host_factory,
    )


def mirror_settings_from_auth(
    *,
    api_url: str | None,
    project_dir: Path,
    machine_name: str | None,
    provider: str | None = None,
    provider_pod_id: str | None = None,
    machine_type_code: str | None = None,
) -> MirrorSettings:
    """Settings from the stored device credential; ``None`` overrides fall
    back to the login's API URL and the host name.

    The registration facts — the pod this box runs on and the catalog code of
    its flavor — come from the flags, then ``ALKERA_MACHINE_PROVIDER_POD_ID`` /
    ``ALKERA_MACHINE_TYPE_CODE`` (provisioning writes both into the box's
    environment); the provider defaults to ``runpod``. A box missing either
    is refused here, before anything connects: it could not register, and a
    box that cannot register must not serve chats it could never publish.

    A PLATFORM box carries ``ALKERA_MACHINE_CREDENTIAL`` (provisioning writes
    it into the box's environment) and nothing else: the credential IS the
    bearer on every call the box makes, the box claims the machine the
    credential names rather than registering as an org's own, the catalog
    code is not asked for — the credential says what the box is — and the
    backend, not the box, decides whose chats it serves. No login is read,
    on that box or off it: there is no person behind a box, and a login on
    its disk is one it must never present. The API URL then comes from the
    flag or ``ALKERA_API_URL``, since there is no login to take it from.
    """
    identity = MachineIdentity.from_env(
        name=(machine_name or "").strip() or MirrorSettings.machine_name_from_env(),
        provider=provider,
        provider_pod_id=provider_pod_id,
        machine_type_code=machine_type_code,
    )
    # A person's own box registered through `cloud-mirror register` runs on the
    # machine credential it was given, exactly as a platform box does on its
    # env var: never on a login.
    personal = None if identity.is_platform else load_personal_box()
    if personal is not None:
        identity = dataclasses.replace(
            identity,
            credential=personal.credential,
            provider_pod_id=identity.provider_pod_id or personal.provider_pod_id,
        )
    if identity.is_platform:
        chosen_api = (
            (api_url or "").strip()
            or os.environ.get(ENV_API_URL, "").strip()
            or (personal.api_url if personal is not None else "")
        )
        if not chosen_api:
            raise typer.BadParameter(
                f"a box on its machine credential needs {ENV_API_URL} (or --api-url)"
            )
        token = identity.credential
        user_id = ""
    else:
        stored = operator_profile()
        # The flag, then the explicit env override, then the URL the token was
        # issued against — never the built-in default over a real login.
        chosen_api = (
            (api_url or "").strip() or os.environ.get(ENV_API_URL, "").strip() or stored.api_url
        )
        token = stored.token
        user_id = _resolve_user_id(chosen_api, stored.token)
    try:
        require_machine_identity(identity)
    except MachineIdentityError as exc:
        raise typer.BadParameter(str(exc)) from exc
    idle_minutes, max_mirrors = mirror_limits_from_env()
    supervised, final_stop_file = supervision_from_env()
    return MirrorSettings(
        api_url=chosen_api,
        token=token,
        project_dir=project_dir.resolve(),
        machine_name=identity.name,
        budget=TurnBudget.from_env(),
        user_id=user_id,
        provider=identity.provider,
        provider_pod_id=identity.provider_pod_id,
        machine_type_code=identity.machine_type_code,
        machine_credential=identity.credential,
        mirror_idle_minutes=idle_minutes,
        max_mirrors=max_mirrors,
        memory_max_mirrors=memory_cap_from_env(),
        parked_ask_hours=parked_ask_hours_from_env(),
        drain_ceiling_seconds=drain_ceiling_from_env(),
        sleep=SleepSettings.from_env(),
        supervised=supervised,
        final_stop_file=final_stop_file,
        status_file=status_file_from_env(),
    )


def operator_profile(project: ProjectDirectory | None = None) -> Profile:
    """The operator's sign-in this command acts as (``--org`` / ``ALKERA_ORG``,
    then the project's pin, then the current profile), or a usage error."""
    try:
        stored = resolve_profile(project=project)
    except ProfileResolutionError as exc:
        raise typer.BadParameter(str(exc)) from exc
    if stored is None or not stored.token:
        raise typer.BadParameter("not signed in — run `alkera login` on this machine first")
    return stored


def _resolve_user_id(api_url: str, token: str) -> str:
    from alkera_cli.account.session import resolve_user

    user = resolve_user(api_url, token)
    return user.id if user is not None else ""


def install_stop_signals(
    stop: asyncio.Event, *, on_signal: Callable[[], None] | None = None
) -> list[int]:
    """Make SIGTERM and SIGINT set ``stop`` instead of killing the process.

    A box is stopped by its supervisor with SIGTERM, and a Python process
    with no handler dies on it at once — before ``run_until``'s ``finally``
    runs — leaving every chat it served on a lease nobody beats for, with the
    last turn's files only on the box's disk. Flipping the event instead lets
    the service put each chat to sleep the way the idle sweep does: pushed,
    released, marked asleep. Returns the signals actually installed; a loop
    that cannot install one (Windows) installs none, and the process keeps
    the platform's default.

    ``on_signal`` runs on the signal itself, before anything waits. The drain
    needs it: what stops a chat being placed on a box that is leaving is the
    beat that says the box is draining, and a flip that waited for the stop
    event to be observed would let one land in between.
    """
    loop = asyncio.get_running_loop()
    installed: list[int] = []

    def fire() -> None:
        if on_signal is not None:
            on_signal()
        stop.set()

    for number in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(number, fire)
        except (NotImplementedError, RuntimeError, ValueError):
            continue
        installed.append(number)
    return installed


def box_folders(settings: MirrorSettings, runtime: HarnessRuntime) -> ChatFolders:
    """The chat-folder custody a provisioned box runs with: a Files client on
    the box's own credential, over the same API the chats are read from."""
    return ChatFolders.for_box(
        api_url=settings.api_url,
        auth=BearerAuth(lambda: settings.token),
        chats_root=runtime.project.chats().path,
    )


async def run_mirror(
    settings: MirrorSettings,
    runtime: HarnessRuntime,
    *,
    stop: asyncio.Event | None = None,
    folders: ChatFolders | None = None,
    sandbox_probes: SandboxProbes | None = None,
    notebooks: BoxNotebookSlot | None = None,
) -> str | None:
    """Run the service until ``stop`` is set, a stop signal arrives, or the
    box gives up.

    Returns why the box gave up, or ``None`` when it was asked to stop. A box
    whose socket is refused for good — a device token that was revoked or
    reseeded — will never serve another chat, and a process that stays up in
    that state reads as healthy from the outside while every chat hangs. Saying
    so lets the caller exit non-zero, which is what puts the retry where it
    belongs: the supervisor, with its backoff.
    """
    stopping = stop or asyncio.Event()
    service = CloudMirrorService(
        settings,
        runtime,
        folders=folders or box_folders(settings, runtime),
        sandbox_probes=sandbox_probes,
        notebooks=notebooks,
    )
    install_stop_signals(stopping, on_signal=service.begin_drain)
    service.require_harness()
    await service.run_until(stopping)
    with contextlib.suppress(Exception):
        await runtime.close_all()
    return service.failure if service.gave_up.is_set() else None


@cloud_mirror_app.command("run")
def run(
    api_url: str | None = typer.Option(
        None, "--api-url", help="Backend base URL (default: the login's, or ALKERA_API_URL)."
    ),
    project: Path | None = typer.Option(
        None,
        "--project",
        "-p",
        exists=True,
        file_okay=False,
        dir_okay=True,
        help="Workspace root the daemon serves (default: the current directory).",
    ),
    machine_name: str | None = typer.Option(
        None,
        "--machine-name",
        help="How this machine registers (default: ALKERA_MACHINE_NAME or the host name).",
    ),
    provider: str | None = typer.Option(
        None,
        "--provider",
        help="The compute provider this box runs on (default: ALKERA_MACHINE_PROVIDER or runpod).",
    ),
    provider_pod_id: str | None = typer.Option(
        None,
        "--provider-pod-id",
        help="The provider's id for this pod (default: ALKERA_MACHINE_PROVIDER_POD_ID). Required.",
    ),
    machine_type_code: str | None = typer.Option(
        None,
        "--machine-type-code",
        help="The catalog code of this pod's flavor (default: ALKERA_MACHINE_TYPE_CODE). Required.",
    ),
    log_level: str = typer.Option("info", "--log-level", help="debug|info|warning|error"),
) -> None:
    """Serve cloud chats bound to this machine (the publisher peer)."""
    configure_logging(log_level)
    settings = mirror_settings_from_auth(
        api_url=api_url,
        project_dir=project or Path.cwd(),
        machine_name=machine_name,
        provider=provider,
        provider_pod_id=provider_pod_id,
        machine_type_code=machine_type_code,
    )
    # Before any chat starts: the private addresses its sandbox may reach, and
    # its link let through a Docker host's forward drop.
    apply_allowlist(os.environ)
    open_chat_links(os.environ)
    # Where each agent server records how to read its sandbox, and where the
    # service reads it before it puts a chat to sleep: one registry, shared.
    probes = SandboxProbes()
    # Composed once the machine is adopted, when ``runtime`` is built.
    notebooks = _notebook_slot(lambda: runtime, org_id=str(settings.org_id or ""))
    runtime = build_mirror_runtime(
        settings.project_dir,
        on_machine_credential=bool(settings.machine_credential),
        api_url=settings.api_url,
        machine_credential=settings.machine_credential or None,
        sandbox_probes=probes,
        notebook_host_factory=notebooks,
    )
    try:
        gave_up = asyncio.run(
            run_mirror(settings, runtime, sandbox_probes=probes, notebooks=notebooks)
        )
    except HarnessUnavailableError as exc:
        console.print(f"[red]✗[/red] {exc}")
        raise typer.Exit(code=2) from exc
    except KeyboardInterrupt:
        return
    if gave_up is not None:
        console.print(f"[red]✗[/red] the cloud socket gave up: {gave_up}")
        raise typer.Exit(code=3)


@cloud_mirror_app.command("register")
def register(
    api_url: str | None = typer.Option(
        None, "--api-url", help="Backend base URL (default: ALKERA_API_URL or the configured one)."
    ),
    machine_name: str | None = typer.Option(
        None, "--machine-name", help="How this box shows (default: the host name)."
    ),
    force: bool = typer.Option(False, "--force", help="Register again over an existing record."),
) -> None:
    """Register this machine as a box.

    Approve it in the browser; the box receives a machine credential bound to
    your organization that runs only your own private chats, and keeps it in
    this machine's Alkera home. No login is stored. Then run
    ``alkera cloud-mirror run``.
    """
    if load_personal_box() is not None and not force:
        console.print("This machine is already registered as a box. Use --force to redo it.")
        raise typer.Exit(code=1)
    chosen_api = (
        (api_url or "").strip()
        or os.environ.get(ENV_API_URL, "").strip()
        or get_settings().alkera_api_url
    )

    def announce(code: DeviceCodeResponse) -> None:
        console.print("\nTo register this machine as a box, open this URL in your browser:")
        console.print(f"\n  [cyan]{code.verification_uri_complete}[/cyan]\n")
        console.print(f"and check the code matches: [bold green]{code.user_code}[/bold green]\n")

    try:
        record = personal_box.register_personal_box(
            chosen_api,
            name=(machine_name or "").strip() or MirrorSettings.machine_name_from_env(),
            daemon_version=daemon_version(),
            announce=announce,
        )
    except (DeviceFlowError, PersonalBoxError) as exc:
        console.print(f"[red]✗[/red] {exc}")
        raise typer.Exit(code=1) from exc
    console.print(f"[green]✓[/green] Registered as a box ({record.machine_id}).")


__all__ = [
    "SandboxProbes",
    "box_folders",
    "build_mirror_runtime",
    "build_worker_service",
    "cloud_mirror_app",
    "install_stop_signals",
    "mirror_settings_from_auth",
    "operator_profile",
    "run_mirror",
]


@cloud_mirror_app.command("worker", hidden=True)
def worker(
    org_root: Path = typer.Option(..., "--org-root", help="The org's data root."),
    control_fd: int = typer.Option(3, "--control-fd", help="The supervisor's socketpair."),
    log_level: str = typer.Option("info", "--log-level", help="debug|info|warning|error"),
) -> None:
    """Serve one org's chats inside its namespaces (started by the supervisor)."""
    configure_logging(log_level)
    control_fd = take_control_channel(control_fd)
    hello = read_hello(control_fd)
    root = OrgRoot(org_root)
    prepare_worker(root)
    settings = worker_settings(hello, project_dir=root.work)
    credential = WorkerCredential(hello.credential)
    service = build_worker_service(
        settings, credential, org_id=str(hello.org_id), org_root=root.path
    )
    control = socket.socket(fileno=control_fd)
    asyncio.run(run_worker(settings, service, control, credential=credential))


def build_worker_service(
    settings: MirrorSettings, credential: WorkerCredential, *, org_id: str, org_root: Path
) -> CloudMirrorService:
    """An org worker's service, every client on its one replaceable credential.

    One connections client serves the runtime's per-chat scope read, the
    schema cards' sync and every connector's credential lease (it is installed
    as the process's), and it follows each replacement of the credential. It
    reads the connections of the workspaces the worker holds as well as its
    chats', since their notebook kernels run here."""
    service: CloudMirrorService | None = None

    def held_chats() -> list[str]:
        return list(service.mirrors) if service is not None else []

    def held_workspaces() -> list[str]:
        return service.held_workspace_ids() if service is not None else []

    connections = WorkerConnectionsClient(
        api_url=settings.api_url,
        credential=credential,
        chats=held_chats,
        workspaces=held_workspaces,
    )
    probes = SandboxProbes()
    # The org's notebooks: each workspace's kernels in a kernel sandbox of its
    # own under the org's root when the box runs gVisor, local otherwise; its
    # SQL through the org's connections attached to each workspace.
    notebooks = _notebook_slot(
        lambda: runtime,
        org_id=org_id,
        org_root=org_root,
        data_home=org_root / "notebooks",
    )
    runtime = build_mirror_runtime(
        settings.project_dir,
        on_machine_credential=True,
        sandbox_probes=probes,
        connections=connections,
        notebook_host_factory=notebooks,
    )
    folders = WorkerFolders.for_worker(
        api_url=settings.api_url, credential=credential, chats_root=runtime.project.chats().path
    )
    service = CloudMirrorService(
        settings,
        runtime,
        folders=folders,
        rest=worker_rest_client(credential, settings.api_url),
        sandbox_probes=probes,
        notebooks=notebooks,
        schema_loader=box_data().schema_cards(
            api_url=settings.api_url,
            token=credential.token,
            runtime=runtime,
            chats=held_chats,
            refresh_interval=settings.schema_refresh_interval,
            connections=connections,
        ),
    )
    return service


@cloud_mirror_app.command(
    "supervise", context_settings={"allow_extra_args": True, "ignore_unknown_options": True}
)
def supervise(ctx: typer.Context) -> None:
    """Run the box supervisor: claim the machine, one worker per org.

    The `alkera` entry hands this command over before the command tree loads
    (`alkera_cli/entry.py`); this is the same command, for help and for a
    caller that built the Typer app first."""
    raise typer.Exit(code=supervise_main(list(ctx.args)))
