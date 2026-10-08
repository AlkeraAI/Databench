"""Turning subagents off removes the agent-spawning tools everywhere.

The switch has to REMOVE, not deny: on the opencode backend a denied tool is
still advertised to the model, so denial only teaches it to keep asking. These
tests pin that the tools leave the catalog, that no backend's advertised set
carries them, that a call arriving anyway is refused with a reason, and that the
guidance stops promising a tool the session does not serve.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from alkera_cli.harness.permission_mode import mode_system_prompt
from alkera_cli.harness.system_prompt import (
    _DELEGATION_EDITS,
    compose_main_agent_guidance,
    without_delegation,
)
from alkera_cli.plugins.plugin_base.mcp_entry import alkera_tool_descriptors
from alkera_cli.plugins.plugin_base.plugin import WorkspaceEvent
from alkera_cli.plugins.plugin_base.registry import PluginRegistry
from alkera_cli.plugins.plugin_base.tool import (
    SUBAGENT_TOOL_APP,
    SUBAGENT_TOOL_NAMES,
    ToolRegistry,
)
from alkera_core.project.directory import ProjectDirectory

REPO_ROOT = Path(__file__).resolve().parents[4]
TOOL_MANIFEST = REPO_ROOT / "packages" / "shared-openapi" / "tool-manifest.json"
PROVISION_SCRIPT = REPO_ROOT / "ops" / "scripts" / "demo" / "provision_runpod_box.sh"


async def _registry(tmp_path: Path, *, subagents: bool) -> ToolRegistry:
    plugins = PluginRegistry(ProjectDirectory(tmp_path / ".alkera"), tmp_path)
    await plugins.discover()
    await plugins.evaluate_activation(WorkspaceEvent(kind="open", workspace_root=tmp_path))
    return plugins.tool_registry(subagents_enabled=subagents)


# --- what the model is offered -------------------------------------------


def test_the_manifest_names_no_agent_tool_the_switch_does_not_know() -> None:
    """A subagent tool added later must be caught by the switch, not slip past
    it. Both sides are derived: the manifest is regenerated from the real Tool
    subclasses, so a new ``app="agent"`` tool fails here until it is accounted
    for."""
    manifest = json.loads(TOOL_MANIFEST.read_text(encoding="utf-8"))["x-alkera-tools"]
    from_manifest = {
        name for name, meta in manifest.items() if meta.get("app") == SUBAGENT_TOOL_APP
    }
    assert from_manifest == set(SUBAGENT_TOOL_NAMES)


async def test_switching_subagents_off_removes_those_tools_and_only_those(tmp_path: Path) -> None:
    on = await _registry(tmp_path, subagents=True)
    off = await _registry(tmp_path, subagents=False)

    on_names = {spec.name for spec in on.hot_prefix()}
    off_names = {spec.name for spec in off.hot_prefix()}
    assert on_names - off_names == set(SUBAGENT_TOOL_NAMES)
    assert off_names - on_names == set()


@pytest.mark.parametrize("name", sorted(SUBAGENT_TOOL_NAMES))
async def test_a_disabled_agent_tool_is_not_in_the_catalog_at_all(
    tmp_path: Path, name: str
) -> None:
    """Not merely absent from the hot prefix — absent from the registry, so
    ``search_tools`` / ``call_tool`` cannot reach it either."""
    off = await _registry(tmp_path, subagents=False)
    assert off.tool_for(name) is None
    assert not off.subagents_enabled
    assert (await _registry(tmp_path, subagents=True)).tool_for(name) is not None


async def test_neither_backends_advertised_set_carries_an_agent_tool(tmp_path: Path) -> None:
    """``alkera_tool_descriptors`` is the single source both the opencode MCP
    mount and the claude adapter build their tool lists from — checking it
    covers both backends. ``allow_agent_tools=True`` is passed deliberately: a
    ROOT session would be handed them if the registry still held them."""
    off = await _registry(tmp_path, subagents=False)
    advertised = {d.name for d in alkera_tool_descriptors(off, None, allow_agent_tools=True)}
    assert advertised.isdisjoint(SUBAGENT_TOOL_NAMES)

    on = await _registry(tmp_path, subagents=True)
    root_advertised = {d.name for d in alkera_tool_descriptors(on, None, allow_agent_tools=True)}
    assert set(SUBAGENT_TOOL_NAMES) <= root_advertised


# --- a call that arrives anyway -------------------------------------------


@pytest.mark.parametrize("name", sorted(SUBAGENT_TOOL_NAMES))
async def test_a_stray_call_is_refused_with_the_reason_not_as_a_typo(
    tmp_path: Path, name: str
) -> None:
    """A resumed chat or a replayed transcript can still carry the call. It must
    read as a deployment choice, not as a misspelled tool name the model should
    try to correct."""
    off = await _registry(tmp_path, subagents=False)
    result = await off.dispatch(name, {"prompt": "go look at the warehouse"})
    assert result["error"] == (
        "Subagents are disabled on this deployment: delegation is turned off here, "
        "so do the work in this chat yourself."
    )


async def test_with_subagents_on_a_spawn_call_is_not_refused_by_the_switch(
    tmp_path: Path,
) -> None:
    """The inverse: the refusal is the switch's, not something every session
    does. With spawn wiring absent this still refuses — but as the recursion
    backstop, naming subagents as the caller, never the deployment."""
    on = await _registry(tmp_path, subagents=True)
    result = await on.dispatch("spawn_agent", {"prompt": "go look at the warehouse"})
    assert "Subagents are disabled" not in json.dumps(result)


# --- what the model is told ------------------------------------------------


def test_guidance_without_subagents_never_names_a_way_to_delegate() -> None:
    off = compose_main_agent_guidance(subagents=False, web_tools=True, analyst=True)
    for promise in ("spawn_agent", "list_agent_types", "Explore agent", "Review agent"):
        assert promise not in off


def test_guidance_with_subagents_still_teaches_delegation() -> None:
    """Without this the omission test passes on guidance that never mentioned
    delegation at all."""
    on = compose_main_agent_guidance(subagents=True, web_tools=True, analyst=True)
    assert "spawn_agent" in on
    assert "list_agent_types" in on


def test_plan_mode_steering_without_subagents_tells_the_model_to_read_widely_itself() -> None:
    """Plan mode carries the most insistent delegation instruction there is — it
    says the model ALWAYS has ``spawn_agent`` and must never work inline — and it
    is injected on every plan-mode turn, outside the always-on composition."""
    off = mode_system_prompt("plan", sandbox_dir="/tmp/chat", subagents=False)
    assert off is not None
    for promise in ("spawn_agent", "list_agent_types", "Explore agents", "delegate"):
        assert promise not in off
    assert "read widely" in off
    # The rest of the plan-mode contract is untouched.
    assert "plan_present" in off
    assert "/tmp/chat/plan.md" in off


def test_plan_mode_steering_with_subagents_still_leans_on_explore() -> None:
    on = mode_system_prompt("plan", sandbox_dir="/tmp/chat", subagents=True)
    assert on is not None
    assert "spawn_agent" in on
    assert "LEAN ON EXPLORE HEAVILY" in on


@pytest.mark.parametrize("subagents", [True, False])
async def test_the_claude_backend_picks_its_plan_steering_off_the_registry(
    tmp_path: Path, subagents: bool
) -> None:
    """The claude adapter injects its own copy of the plan steering, named with
    its own plan/question tools — a second injection point the switch has to
    reach, or that backend keeps promising delegation."""
    from alkera_cli.harness.adapters.claude_agent import ClaudeAgentAdapter

    registry = await _registry(tmp_path, subagents=subagents)
    adapter = SimpleNamespace(_config=SimpleNamespace(harness_native={"tool_registry": registry}))
    text = ClaudeAgentAdapter._plan_steering_text(adapter)  # type: ignore[arg-type]
    assert ("spawn_agent" in text) is subagents
    # Its own tool names survive either way.
    assert "present_plan" in text


async def _plan_turn_system(tmp_path: Path, *, subagents: bool) -> str:
    """The system text a real plan-mode turn hands the adapter, composed through
    ``ChatSession`` rather than by calling the steering helper directly — the
    wiring between the registry and the steering is the part the deployment
    depends on, and it is invisible to a test that calls the helper itself."""
    from _adapter_factory import FakeAdapterFactory
    from alkera_cli.harness import HarnessRuntime
    from alkera_cli.harness._fake import FakeAdapter

    factory = FakeAdapterFactory(lambda: FakeAdapter(reply_text="ok"))
    runtime = HarnessRuntime(
        ProjectDirectory(tmp_path / ".alkera"),
        adapter_factory=factory,
        subagents_enabled=subagents,
    )
    chat = runtime._chats_store.create(title="c", harness_type="agent")
    sid = chat.session_id
    chat.close()
    session = await runtime.open_chat(sid)
    try:
        session.set_permission_mode("plan")
        await session.send_prompt("plan the migration")
        return factory.adapters[-1].sent_prompts[-1].system or ""
    finally:
        await runtime.close_chat(sid)


async def test_a_real_plan_turn_with_subagents_off_names_no_spawn_tool(tmp_path: Path) -> None:
    """Drives the whole path a deployment actually runs: the runtime builds the
    registry from the flag, the session reads the spawn tool off that registry,
    and the plan-mode steering follows."""
    system = await _plan_turn_system(tmp_path, subagents=False)
    for promise in ("spawn_agent", "list_agent_types", "Explore agent", "delegate"):
        assert promise not in system
    # It IS a plan turn — otherwise the absence proves nothing.
    assert "Plan mode is READ-ONLY this turn" in system


async def test_a_real_plan_turn_with_subagents_on_still_leans_on_explore(tmp_path: Path) -> None:
    system = await _plan_turn_system(tmp_path, subagents=True)
    assert "Plan mode is READ-ONLY this turn" in system
    assert "spawn_agent" in system
    assert "LEAN ON EXPLORE HEAVILY" in system


@pytest.mark.parametrize("mode", ["auto", "default", "read_only", "bypass"])
def test_the_other_modes_steering_is_the_same_either_way(mode: str) -> None:
    """The switch touches plan mode alone — no other mode's steering names a
    subagent, so none of them may change."""
    assert mode_system_prompt(mode, subagents=False) == mode_system_prompt(  # type: ignore[arg-type]
        mode,  # type: ignore[arg-type]
        subagents=True,
    )


def test_the_self_review_discipline_survives_losing_the_review_agent() -> None:
    """Dropping the Review agent must not drop the reason it exists."""
    off = compose_main_agent_guidance(subagents=False)
    assert "no breaking impact found in the cone I checked" in off
    assert "re-read your own diff" in off


@pytest.mark.parametrize("block", sorted(_DELEGATION_EDITS))
def test_an_edited_block_cannot_silently_keep_its_delegation_promise(block: str) -> None:
    """The subagents-off variant is built by removing named sentences. If a
    block is reworded and the table is not, the removal must fail loudly rather
    than leave the promise in place."""
    with pytest.raises(ValueError, match="no longer contains the delegation text"):
        without_delegation(block, "a block someone rewrote without revisiting the table")


# --- how the deployment sets it -------------------------------------------


def test_the_setting_defaults_on_and_the_env_turns_it_off(monkeypatch: pytest.MonkeyPatch) -> None:
    from alkera_cli.host.config import CliSettings

    assert CliSettings().alkera_subagents_enabled is True
    monkeypatch.setenv("ALKERA_SUBAGENTS_ENABLED", "false")
    assert CliSettings().alkera_subagents_enabled is False


def test_the_provisioned_box_serves_chats_with_subagents_off() -> None:
    """The box behind the web portal's chats is the surface that must not offer
    them, and its env file is where that is decided."""
    script = PROVISION_SCRIPT.read_text(encoding="utf-8")
    env_block = script.split('BOX_ENV_CONTENT="', 1)[1].split('"\n', 1)[0]
    assert "ALKERA_SUBAGENTS_ENABLED=false" in env_block.splitlines()
