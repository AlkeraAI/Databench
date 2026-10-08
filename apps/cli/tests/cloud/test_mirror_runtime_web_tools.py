"""The cloud box's runtime serves the org's web tools.

A chat opened from the web portal runs on a provisioned box under
``alkera cloud-mirror run``, whose runtime ``build_mirror_runtime`` wires. It
must register ``web.search`` / ``web.fetch`` on the same org toggle the editor
daemon reads — read as the chat's own credential, never a login on the box's
disk, and LAZILY, because the box builds the runtime long before any chat
opens, and an admin who turns the toggle off must see the tools drop rather
than keep them for the process's lifetime.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alkera_cli.commands.box import build_mirror_runtime
from alkera_cli.gateway.client import GatewayCatalog
from alkera_cli.harness import org_flags
from alkera_cli.harness.web_flags import WebToolFlags

WEB_TOOLS = {"web.search", "web.fetch"}
CHAT_TOKEN = "chat-gateway-token"


@pytest.fixture
def gateway(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """Stand in for the gateway catalog read and record what it was asked; a
    read as anything but the chat's own credential is an error. ``signed_in``
    says whether the chat asking has a credential at all."""
    state: dict[str, object] = {
        "enabled": True,
        "fetch_enabled": True,
        "signed_in": True,
        "raises": False,
        "reads": 0,
    }

    async def _fetch_catalog(*, gateway_url: str, token: str) -> GatewayCatalog:
        assert token == CHAT_TOKEN, "the org flags are read as the chat, never a login"
        state["reads"] = int(state["reads"]) + 1  # type: ignore[call-overload]
        if state["raises"]:
            raise RuntimeError("gateway down")
        return GatewayCatalog(
            models=[],
            web_search_enabled=bool(state["enabled"]),
            web_fetch_enabled=bool(state["enabled"]) and bool(state["fetch_enabled"]),
        )

    monkeypatch.setattr(org_flags, "fetch_catalog", _fetch_catalog)
    return state


async def _tool_names(project_dir: Path, gateway: dict[str, object]) -> set[str]:
    """What a chat with (or, signed out, without) a credential is offered."""
    runtime = build_mirror_runtime(project_dir)
    credential = CHAT_TOKEN if gateway["signed_in"] else None
    return {spec.name for spec in (await runtime.tool_registry(credential=credential)).all_specs()}


async def test_the_box_registers_the_web_tools_when_the_org_allows_them(
    tmp_path: Path, gateway: dict[str, object]
) -> None:
    """The bug this file exists for: the mirror built its runtime with no web
    flag at all, so a portal chat's agent had no search and no fetch."""
    gateway["enabled"] = True
    assert WEB_TOOLS <= await _tool_names(tmp_path, gateway)


async def test_the_deployment_can_withhold_web_fetch_alone(
    tmp_path: Path, gateway: dict[str, object]
) -> None:
    """The operator kill switch: `web.fetch` reaches any public URL the model
    names, in every permission mode, so an install that wants its agents off the
    public internet turns it off with an env change and a gateway restart. It
    must remove the tool from the registry entirely — a tool that is still
    advertised costs a turn on a refusal — while leaving the key-less metasearch
    the org is still paying for."""
    gateway["enabled"] = True
    gateway["fetch_enabled"] = False
    names = await _tool_names(tmp_path, gateway)
    assert "web.search" in names
    assert "web.fetch" not in names


async def test_the_fetch_switch_cannot_grant_past_the_org_toggle(
    tmp_path: Path, gateway: dict[str, object]
) -> None:
    """The switch only ever subtracts. An org that disabled web access keeps it
    disabled however the deployment is configured."""
    gateway["enabled"] = False
    gateway["fetch_enabled"] = True
    assert WEB_TOOLS.isdisjoint(await _tool_names(tmp_path, gateway))


async def test_the_box_withholds_the_web_tools_when_the_org_forbids_them(
    tmp_path: Path, gateway: dict[str, object]
) -> None:
    gateway["enabled"] = False
    assert WEB_TOOLS.isdisjoint(await _tool_names(tmp_path, gateway))


async def test_a_gateway_failure_leaves_the_web_tools_off(
    tmp_path: Path, gateway: dict[str, object]
) -> None:
    """Fail-closed: the registry still builds (the box must serve chats with a
    sick gateway) but a capability it could not confirm is not granted."""
    gateway["raises"] = True
    assert WEB_TOOLS.isdisjoint(await _tool_names(tmp_path, gateway))


async def test_an_unsigned_in_box_asks_the_gateway_nothing_and_stays_off(
    tmp_path: Path, gateway: dict[str, object]
) -> None:
    gateway["signed_in"] = False
    assert WEB_TOOLS.isdisjoint(await _tool_names(tmp_path, gateway))
    assert gateway["reads"] == 0


async def test_the_flag_is_read_lazily_and_re_read_on_every_rebuild(
    tmp_path: Path, gateway: dict[str, object]
) -> None:
    """A pinned bool would freeze the box at whatever the gateway said when the
    process started — before it is even signed in — and would keep the tools
    live after an admin revoked them."""
    gateway["signed_in"] = False
    gateway["enabled"] = False
    runtime = build_mirror_runtime(tmp_path)
    assert gateway["reads"] == 0, "construction must not touch the gateway"

    # A chat with a credential, toggle on: the tools appear on its registry view.
    gateway["enabled"] = True
    registry = await runtime.tool_registry(credential=CHAT_TOKEN)
    assert WEB_TOOLS <= {s.name for s in registry.all_specs()}
    assert gateway["reads"] == 1

    # The admin turns the org toggle off; the very next resolution honours it.
    gateway["enabled"] = False
    assert await runtime._resolve_web_search_flag(CHAT_TOKEN) == WebToolFlags()
    assert gateway["reads"] == 2
