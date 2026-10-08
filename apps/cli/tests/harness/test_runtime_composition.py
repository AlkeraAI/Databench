"""Characterization of the four production ``HarnessRuntime`` builds.

The headless runner, the editor daemon and the box each build a runtime with
their own strategy set (the product's terminal chat is characterized in its own
suite). These tests capture every argument
each build hands ``HarnessRuntime`` (by identity where the strategy is a shared
object, by structure where the build makes a fresh one), so the builds can move
behind one composition root without one profile silently taking another's
strategy.
"""

from __future__ import annotations

import types
from pathlib import Path
from typing import Any

import pytest
from _helpers.runtime_builds import (
    CATALOG,
    assert_shared_tail,
    closure_values,
    fetch_qualname,
    record_builds,
)
from alkera_cli.contracts.gateway_model import GatewayModel
from alkera_cli.harness.gateway_session import (
    default_gateway_config_builder,
    remembered_catalog_config_builder,
)
from alkera_cli.harness.org_flags import machine_web_flags, web_search_org_flag
from alkera_cli.harness.safety_judge import GatewaySafetyJudge
from alkera_cli.harness.web_flags import WebToolFlags


@pytest.fixture
def builds(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, dict[str, Any]]]:
    return record_builds(monkeypatch)


async def test_the_headless_runner_wires_the_selectable_catalog_and_its_flags(
    builds: list[tuple[Any, dict[str, Any]]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from alkera_cli.chat import headless
    from alkera_cli.gateway.client import GatewayCatalog

    hidden = GatewayModel(id="m-test", display_name="T", wire="anthropic", family="test")
    catalog = GatewayCatalog(
        models=[*CATALOG, hidden], web_search_enabled=True, web_fetch_enabled=False
    )

    async def fetch(_url: str, _token: str) -> GatewayCatalog:
        return catalog

    monkeypatch.setattr("alkera_cli.chat.headless.sweep_orphaned_agents", lambda: 0)
    monkeypatch.setattr("alkera_cli.chat.headless._auth_token", lambda _root: "tok")
    monkeypatch.setattr("alkera_cli.chat.headless._fetch_runtime_catalog", fetch)
    overrides = {"identity": "be brief"}
    options = headless.ProductionRuntimeOptions(
        project_root=tmp_path,
        harness_type="alkera",
        model="m-one",
        effort=None,
        system_block_overrides=overrides,
    )
    await headless._build_production_runtime(options)

    ((project, kwargs),) = builds
    assert project.path == tmp_path / ".alkera"
    assert set(kwargs) == {
        "gateway_config_builder",
        "claude_env_builder",
        "subagent_model_resolver",
        "safety_judge",
        "web_search_enabled",
        "audit_reporter",
        "otel_exporter",
        "system_block_overrides",
    }
    builder = kwargs["gateway_config_builder"]
    assert builder.__qualname__ == "gateway_config_builder_for_catalog.<locals>.build"
    assert CATALOG in closure_values(builder)
    assert "default_subagent_model_resolver.<locals>._unbound" == fetch_qualname(
        kwargs["subagent_model_resolver"]
    )
    assert kwargs["safety_judge"] is None
    assert kwargs["web_search_enabled"] == WebToolFlags(search=True, fetch=False)
    assert kwargs["system_block_overrides"] is overrides
    assert_shared_tail(kwargs)


def test_the_daemon_wires_the_pinned_model_builder_and_the_seed_subprocess(
    builds: list[tuple[Any, dict[str, Any]]], tmp_path: Path
) -> None:
    from alkera_cli.app.runtime_pool import runtime_for as _runtime_for

    server = types.SimpleNamespace()
    first = _runtime_for(server, str(tmp_path))  # type: ignore[arg-type]
    again = _runtime_for(server, str(tmp_path))  # type: ignore[arg-type]

    assert first is again
    ((project, kwargs),) = builds
    assert project.path == tmp_path.resolve() / ".alkera"
    assert set(kwargs) == {
        "gateway_config_builder",
        "claude_env_builder",
        "subagent_model_resolver",
        "safety_judge",
        "subprocess_seed",
        "web_search_enabled",
        "audit_reporter",
        "otel_exporter",
    }
    # The whole remembered catalog, so an editor switch needs no respawn; the
    # pinned model alone when the catalog lacks it.
    assert kwargs["gateway_config_builder"] is remembered_catalog_config_builder
    assert "default_subagent_model_resolver.<locals>._unbound" == fetch_qualname(
        kwargs["subagent_model_resolver"]
    )
    assert kwargs["safety_judge"] is None
    assert kwargs["subprocess_seed"] is True
    assert kwargs["web_search_enabled"] is web_search_org_flag
    assert_shared_tail(kwargs)


@pytest.mark.parametrize(
    ("on_machine_credential", "resolver_fetch", "web_flags"),
    [
        pytest.param(
            False,
            "default_subagent_model_resolver.<locals>._unbound",
            web_search_org_flag,
            id="signed-in-box",
        ),
        pytest.param(
            True,
            "machine_subagent_model_resolver.<locals>._nothing",
            machine_web_flags,
            id="machine-credential-box",
        ),
    ],
)
def test_the_box_wires_its_credential_forms(
    builds: list[tuple[Any, dict[str, Any]]],
    tmp_path: Path,
    on_machine_credential: bool,
    resolver_fetch: str,
    web_flags: object,
) -> None:
    from alkera_cli.commands.box import SandboxProbes, build_mirror_runtime
    from alkera_cli.notebooks.box_compose import BoxNotebookSlot

    probes = SandboxProbes()
    notebooks = BoxNotebookSlot()
    build_mirror_runtime(
        tmp_path,
        on_machine_credential=on_machine_credential,
        api_url="https://api.example" if on_machine_credential else None,
        machine_credential="mc" if on_machine_credential else None,
        sandbox_probes=probes,
        notebook_host_factory=notebooks,
    )

    ((project, kwargs),) = builds
    assert project.path == tmp_path.resolve() / ".alkera"
    assert set(kwargs) == {
        "gateway_config_builder",
        "claude_env_builder",
        "subagent_model_resolver",
        "safety_judge",
        "subprocess_seed",
        "web_search_enabled",
        "audit_reporter",
        "otel_exporter",
        "connection_scope",
        "adapter_factory",
        "notebook_host_factory",
    }
    # The agents' notebooks are the box's: the slot the machine channel serves.
    assert kwargs["notebook_host_factory"] is notebooks
    # The agent servers record their sandbox probes where the service reads them.
    assert kwargs["adapter_factory"].probes is probes
    assert kwargs["gateway_config_builder"] is default_gateway_config_builder
    assert resolver_fetch == fetch_qualname(kwargs["subagent_model_resolver"])
    judge = kwargs["safety_judge"]
    if on_machine_credential:
        assert isinstance(judge, GatewaySafetyJudge)
        assert vars(judge).get("_token") is None
        assert kwargs["connection_scope"].__qualname__ == "ChatConnectionsClient.records_for_chat"
    else:
        assert judge is None
        assert kwargs["connection_scope"] is None
    assert kwargs["subprocess_seed"] is True
    assert kwargs["web_search_enabled"] is web_flags
    assert_shared_tail(kwargs)


def test_a_machine_credential_box_without_its_door_is_refused(
    builds: list[tuple[Any, dict[str, Any]]], tmp_path: Path
) -> None:
    from alkera_cli.commands.box import build_mirror_runtime

    with pytest.raises(ValueError, match="needs its API URL and credential"):
        build_mirror_runtime(tmp_path, on_machine_credential=True)
    assert builds == []
