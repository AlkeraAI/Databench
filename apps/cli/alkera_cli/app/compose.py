"""The one place the CLI composes a production ``HarnessRuntime``.

Each profile is one of the four surfaces that run a production runtime, and
each keeps its own strategy set:

- ``interactive``: the terminal client. opencode spawns knowing the whole
  ``catalog`` so the in-app ``/model`` picker can switch providers mid-session.
- ``headless``: the one-shot runner. The same catalog builder, the org's web
  flags from the catalog fetch, and the caller's system block overrides.
- ``daemon``: the editor daemon. The model is pinned at chat creation, so the
  single-model gateway builder; the lineage seed runs in a subprocess; the org
  web-tools flag resolves lazily.
- ``box``: a cloud box. Like the daemon, except that on its machine credential
  the judge, the subagent resolver and the web flags are the forms that hold no
  credential of their own, and each chat's connections are read through
  ``connection_scope``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from alkera_cli.contracts.gateway_model import GatewayModel
from alkera_cli.harness.adapter_factory import AdapterFactory
from alkera_cli.harness.claude_gateway import default_claude_env_builder
from alkera_cli.harness.extension_points import prepare_shared_project
from alkera_cli.harness.gateway_session import (
    default_gateway_config_builder,
    gateway_config_builder_for_catalog,
    remembered_catalog_config_builder,
)
from alkera_cli.harness.org_flags import machine_web_flags, web_search_org_flag
from alkera_cli.harness.runtime import HarnessRuntime
from alkera_cli.harness.safety_judge import default_safety_judge, machine_safety_judge
from alkera_cli.harness.sandbox_processes import SandboxProbes
from alkera_cli.harness.subagent_routing import (
    default_subagent_model_resolver,
    machine_subagent_model_resolver,
)
from alkera_cli.harness.web_flags import WebToolFlags
from alkera_cli.host.paths import project_directory
from alkera_cli.observability.audit_report import default_reporter
from alkera_cli.observability.otel_export import default_exporter

if TYPE_CHECKING:
    from alkera_cli.notebooks.session import HostFactory

RuntimeProfile = Literal["interactive", "headless", "daemon", "box"]


def production_runtime(
    profile: RuntimeProfile,
    project_root: Path,
    *,
    catalog: Sequence[GatewayModel] = (),
    web_tools: WebToolFlags | None = None,
    system_block_overrides: Mapping[str, str] | None = None,
    on_machine_credential: bool = False,
    connection_scope: Callable[[str], Awaitable[Iterable[str]]] | None = None,
    sandbox_probes: SandboxProbes | None = None,
    notebook_host_factory: HostFactory | None = None,
) -> HarnessRuntime:
    """A ``HarnessRuntime`` rooted at ``project_root/.alkera`` with ``profile``'s
    strategies. ``catalog`` and ``web_tools`` apply to ``interactive`` and
    ``headless``; ``system_block_overrides`` to ``headless``;
    ``on_machine_credential``, ``connection_scope``, ``sandbox_probes``
    (where each agent server records how to read its sandbox) and
    ``notebook_host_factory`` (the box's notebooks, one engine per workspace
    shared with the backend's requests) to ``box``."""
    if profile == "interactive":
        return HarnessRuntime(
            project_directory(project_root),
            gateway_config_builder=gateway_config_builder_for_catalog(catalog),
            claude_env_builder=default_claude_env_builder,
            subagent_model_resolver=default_subagent_model_resolver(),
            safety_judge=default_safety_judge(),
            web_search_enabled=web_tools or WebToolFlags(),
            audit_reporter=default_reporter(),
            otel_exporter=default_exporter(),
        )
    if profile == "headless":
        return HarnessRuntime(
            project_directory(project_root),
            gateway_config_builder=gateway_config_builder_for_catalog(catalog),
            claude_env_builder=default_claude_env_builder,
            subagent_model_resolver=default_subagent_model_resolver(),
            safety_judge=default_safety_judge(),
            web_search_enabled=web_tools or WebToolFlags(),
            audit_reporter=default_reporter(),
            otel_exporter=default_exporter(),
            system_block_overrides=system_block_overrides,
        )
    if profile == "daemon":
        return HarnessRuntime(
            project_directory(project_root),
            # The catalog the editor's picker last read, so an open chat can
            # move models in place; the pinned model alone before it is read.
            gateway_config_builder=remembered_catalog_config_builder,
            claude_env_builder=default_claude_env_builder,
            subagent_model_resolver=default_subagent_model_resolver(),
            safety_judge=default_safety_judge(),
            # Run the heavy offline lineage seed in a subprocess so a
            # spellbook-scale manifest parse never blocks the daemon's event loop.
            subprocess_seed=True,
            # Org-level web-tools toggle, resolved lazily (the daemon may not be
            # signed in yet when this runtime is built).
            web_search_enabled=web_search_org_flag,
            audit_reporter=default_reporter(),
            otel_exporter=default_exporter(),
        )
    project = project_directory(project_root)
    if connection_scope is not None:
        # Several tenants' chats share this one project; a store that keeps
        # per-tenant facts in it marks itself so it never mixes them.
        prepare_shared_project(project)
    return HarnessRuntime(
        project,
        gateway_config_builder=default_gateway_config_builder,
        claude_env_builder=default_claude_env_builder,
        subagent_model_resolver=(
            machine_subagent_model_resolver()
            if on_machine_credential
            else default_subagent_model_resolver()
        ),
        safety_judge=machine_safety_judge() if on_machine_credential else default_safety_judge(),
        subprocess_seed=True,
        # Org-level web-tools toggle, resolved lazily — the box builds this
        # runtime before it has claimed its machine credential, and an admin
        # turning the org toggle off must drop the tools on the next rebuild.
        web_search_enabled=machine_web_flags if on_machine_credential else web_search_org_flag,
        audit_reporter=default_reporter(),
        otel_exporter=default_exporter(),
        connection_scope=connection_scope,
        adapter_factory=AdapterFactory(probes=sandbox_probes),
        notebook_host_factory=notebook_host_factory,
    )
