"""A chat's web tools follow ITS org, on a runtime that serves several.

A pool box runs one ``HarnessRuntime`` for chats of many orgs. The runtime
builds its tool registry once, and until this file existed the org web-tools
toggle was read as part of that one build — as whichever chat opened first —
so the first chat's org decided whether every later chat, of every other org,
could search and fetch the web. These tests open two chats of two orgs on one
runtime, in both orders, and pin that each sees exactly its own org's answer;
that nothing one chat resolved survives in the runtime for the next (the
registry cache, a registry rebuild); and that the answer is re-read at the
start of every turn, as the chat, so an admin's change binds without a
restart.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from _adapter_factory import FakeAdapterFactory
from alkera_cli.account import auth_file
from alkera_cli.gateway.client import GatewayCatalog
from alkera_cli.harness import HarnessRuntime, PermissionBroker
from alkera_cli.harness import org_flags as org_flags_module
from alkera_cli.harness._fake import FakeAdapter
from alkera_cli.harness.mcp_server import WEB_MCP_MOUNT
from alkera_cli.harness.org_flags import machine_web_flags
from alkera_cli.harness.web_flags import WebToolFlags
from alkera_cli.plugins.plugin_base.mcp_entry import web_tool_descriptors
from alkera_cli.plugins.plugin_base.web_tools import WEB_TOOL_NAMES
from alkera_core.project.directory import ProjectDirectory

pytestmark = [pytest.mark.spread]

#: Two orgs on one box: A has web access off, B has it on.
ORG_A = "gw-token-org-a"
ORG_B = "gw-token-org-b"
CHAT_A = "chat-of-org-a"
CHAT_B = "chat-of-org-b"
MODEL = {"provider_id": "alkera-anthropic", "model_id": "claude-opus-4.5", "efforts": ["high"]}
#: The steering block the prompt composes only when the session serves the web tools.
WEB_STEERING = "WEB SEARCH & FETCH"


class _Gateway:
    """The gateway catalog as each org's credential reads it. A credential the
    box was never handed an answer for is refused, the way a real gateway
    refuses an unknown token, so a read as the wrong chat cannot pass as one."""

    def __init__(self) -> None:
        self.flags: dict[str, WebToolFlags] = {
            ORG_A: WebToolFlags(),
            ORG_B: WebToolFlags(search=True, fetch=True),
        }
        self.asked: list[str] = []

    async def catalog(self, *, gateway_url: str, token: str) -> GatewayCatalog:
        self.asked.append(token)
        flags = self.flags[token]
        return GatewayCatalog(
            models=[], web_search_enabled=flags.search, web_fetch_enabled=flags.fetch
        )


@pytest.fixture
def gateway(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> _Gateway:
    """The gateway behind the flags provider, and no login on the box's disk:
    a box's chat reads its org as its own token or not at all."""

    def _never() -> None:
        raise AssertionError("a box's chat must not read a device login for its org's flags")

    monkeypatch.setattr(auth_file, "AUTH_FILE_PATH", tmp_path / "absent" / "auth.yml")
    monkeypatch.setattr(auth_file, "_read_document", _never)
    recorder = _Gateway()
    monkeypatch.setattr(org_flags_module, "fetch_catalog", recorder.catalog)
    return recorder


async def _allow(_request: object) -> object:
    raise AssertionError("no permission is asked here")


@pytest.fixture
async def box(tmp_path: Path) -> Iterator[tuple[HarnessRuntime, FakeAdapterFactory]]:
    """A pool box's runtime: the machine flags provider (nothing until bound to a
    chat's token) over a fake agent that answers every prompt."""
    factory = FakeAdapterFactory(lambda: FakeAdapter(reply_text="done"), available=True)
    runtime = HarnessRuntime(
        ProjectDirectory(tmp_path / "workspace" / ".alkera"),
        adapter_factory=factory,
        web_search_enabled=machine_web_flags,
    )
    for chat_id in (CHAT_A, CHAT_B):
        runtime.project.chats().create(
            session_id=chat_id, title=chat_id, harness_type="agent", model=dict(MODEL)
        ).close()
    try:
        yield runtime, factory
    finally:
        await runtime.close_all()


async def _open(runtime: HarnessRuntime, chat_id: str, token: str) -> Any:
    return await runtime.open_chat(
        chat_id,
        permission_broker=PermissionBroker(_allow, default_timeout_seconds=None),
        gateway_token=token,
    )


def _web_names(session: Any) -> set[str]:
    """What the session's ``web`` mount would advertise."""
    binding = session.tool_binding
    assert binding is not None
    return {d.name for d in web_tool_descriptors(binding.registry, binding.tool_scope)}


def _mount_of(factory: FakeAdapterFactory, chat_id: str) -> dict[str, Any]:
    config = next(c for c in factory.configs if c.session_id == chat_id)
    mcp = config.harness_native["alkera_mcp"]
    assert isinstance(mcp, dict)
    return mcp


async def _dispatch_web_search(session: Any) -> dict[str, Any]:
    binding = session.tool_binding
    assert binding is not None
    return await binding.registry.dispatch(
        "web.search", {"query": "anything"}, session_id=binding.session_id
    )


# --- two orgs, one runtime, both orders -------------------------------------------


@pytest.mark.parametrize(
    "order",
    [
        pytest.param((CHAT_A, ORG_A, CHAT_B, ORG_B), id="web-off-org-opens-first"),
        pytest.param((CHAT_B, ORG_B, CHAT_A, ORG_A), id="web-on-org-opens-first"),
    ],
)
async def test_each_chat_gets_its_own_orgs_web_tools_whichever_org_opened_first(
    box: tuple[HarnessRuntime, FakeAdapterFactory],
    gateway: _Gateway,
    order: tuple[str, str, str, str],
) -> None:
    """The tenancy guarantee: org A (web off) and org B (web on) share a box.
    Whichever opens first, A's chat lists no web tool, is configured with no
    ``web`` mount and is refused a call by name; B's chat gets both tools and
    the mount. Before the per-chat view, the second chat inherited the first
    chat's org in every one of these."""
    runtime, factory = box
    first_chat, first_token, second_chat, second_token = order
    sessions = {
        first_chat: await _open(runtime, first_chat, first_token),
        second_chat: await _open(runtime, second_chat, second_token),
    }
    a, b = sessions[CHAT_A], sessions[CHAT_B]

    assert _web_names(a) == set()
    assert _web_names(b) == {"search", "fetch"}
    assert a.tool_binding.registry.tool_for("web.search") is None
    assert b.tool_binding.registry.tool_for("web.fetch") is not None
    assert a.tool_binding.web_tools == WebToolFlags()
    assert b.tool_binding.web_tools == WebToolFlags(search=True, fetch=True)
    assert WEB_MCP_MOUNT not in _mount_of(factory, CHAT_A)
    assert WEB_MCP_MOUNT in _mount_of(factory, CHAT_B)
    refused = await _dispatch_web_search(a)
    assert refused["error"] == "tool 'web.search' is not enabled for this chat"
    # Each org's answer was read as that org's own token, nothing else.
    assert {ORG_A, ORG_B} <= set(gateway.asked)


async def test_a_deployment_fetch_switch_binds_per_chat_too(
    box: tuple[HarnessRuntime, FakeAdapterFactory], gateway: _Gateway
) -> None:
    """The finer flag rides the same path: an org whose gateway answers
    search-on / fetch-off gets the search tool and not the fetch tool, beside
    a chat of an org that gets both."""
    runtime, _ = box
    gateway.flags[ORG_A] = WebToolFlags(search=True, fetch=False)
    a = await _open(runtime, CHAT_A, ORG_A)
    b = await _open(runtime, CHAT_B, ORG_B)
    assert _web_names(a) == {"search"}
    assert _web_names(b) == {"search", "fetch"}
    assert a.tool_binding.web_tools == WebToolFlags(search=True, fetch=False)


# --- nothing one chat resolved is kept for the next ---------------------------------


async def test_the_registry_cache_carries_no_orgs_answer(
    box: tuple[HarnessRuntime, FakeAdapterFactory], gateway: _Gateway
) -> None:
    """The runtime caches ONE build of the registry. Asking for the registry as
    org A, then as org B, then as A again must answer each as itself: the
    cached build encodes no org, only the view does."""
    runtime, _ = box
    as_a = await runtime.tool_registry(credential=ORG_A)
    as_b = await runtime.tool_registry(credential=ORG_B)
    as_a_again = await runtime.tool_registry(credential=ORG_A)
    assert WEB_TOOL_NAMES.isdisjoint({s.name for s in as_a.all_specs()})
    assert WEB_TOOL_NAMES <= {s.name for s in as_b.all_specs()}
    assert WEB_TOOL_NAMES.isdisjoint({s.name for s in as_a_again.all_specs()})
    # The views share the build: the same connections, stores and hot tools.
    assert as_a.tool_for("search_tools") is as_b.tool_for("search_tools")


async def test_a_registry_rebuild_rebinds_every_chat_to_its_own_org(
    box: tuple[HarnessRuntime, FakeAdapterFactory], gateway: _Gateway
) -> None:
    """A team-connection sync rebuilds the project's registry and repoints
    every open session at it. The repointing must re-derive each session's
    view from what that session resolved as its chat — a rebuild that read the
    flags once, as the machine, handed every chat the machine's answer (on a
    box: nothing), and chat B lost its web tools to chat A's sync."""
    runtime, _ = box
    a = await _open(runtime, CHAT_A, ORG_A)
    b = await _open(runtime, CHAT_B, ORG_B)
    before_a, before_b = a.tool_binding.registry, b.tool_binding.registry

    await runtime.team_connections_changed()

    assert a.tool_binding.registry is not before_a, "the session was repointed at the rebuild"
    assert b.tool_binding.registry is not before_b
    assert _web_names(a) == set()
    assert _web_names(b) == {"search", "fetch"}
    assert (await _dispatch_web_search(a))[
        "error"
    ] == "tool 'web.search' is not enabled for this chat"


# --- re-read at the start of every turn, as the chat --------------------------------


def _system_of(factory: FakeAdapterFactory, chat_id: str, turn: int) -> str:
    adapter = factory.adapters[[c.session_id for c in factory.configs].index(chat_id)]
    return adapter._sent_prompts[turn].system or ""


async def test_an_org_toggle_turned_off_binds_at_the_chats_next_turn(
    box: tuple[HarnessRuntime, FakeAdapterFactory], gateway: _Gateway
) -> None:
    """The flags are a policy an admin changes while chats are open. Chat B
    runs a turn with the web tools (the steering names them); the admin turns
    B's org off; B's very next turn lists no web tool, composes no web steering
    and refuses a call by name — no reopen, no restart. Chat A, open beside it
    the whole time, is untouched."""
    runtime, factory = box
    a = await _open(runtime, CHAT_A, ORG_A)
    b = await _open(runtime, CHAT_B, ORG_B)

    await b.send_prompt("first")
    assert WEB_STEERING in _system_of(factory, CHAT_B, 0)
    assert _web_names(b) == {"search", "fetch"}

    gateway.flags[ORG_B] = WebToolFlags()
    await b.send_prompt("second")

    assert b.tool_binding.web_tools == WebToolFlags()
    assert _web_names(b) == set()
    assert WEB_STEERING not in _system_of(factory, CHAT_B, 1)
    assert (await _dispatch_web_search(b))[
        "error"
    ] == "tool 'web.search' is not enabled for this chat"
    assert _web_names(a) == set()
    # Each turn asked the gateway as the chat's own token.
    assert gateway.asked[-2:] == [ORG_B, ORG_B]


async def test_a_gateway_that_cannot_answer_at_turn_start_withholds_for_that_turn_only(
    box: tuple[HarnessRuntime, FakeAdapterFactory], gateway: _Gateway
) -> None:
    """Fail-closed per turn: a read that fails as the chat opens its turn runs
    that turn without the web tools; the next read that succeeds restores them.
    Nothing is remembered across the failure in either direction."""
    runtime, _ = box
    b = await _open(runtime, CHAT_B, ORG_B)
    assert _web_names(b) == {"search", "fetch"}

    working = gateway.flags
    gateway.flags = {}  # every read raises KeyError inside the provider
    await b.send_prompt("while the gateway is down")
    assert _web_names(b) == set()
    assert b.tool_binding.web_tools == WebToolFlags()

    gateway.flags = working
    await b.send_prompt("once it is back")
    assert _web_names(b) == {"search", "fetch"}
    assert b.tool_binding.web_tools == WebToolFlags(search=True, fetch=True)
