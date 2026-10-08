"""The gateway components a box runs present each chat's own credential.

A box on its machine credential holds nothing the gateway accepts and no login
on its disk. Its auto-mode judge, its subagent model resolver and its org
web-tool flags are the forms that hold no credential of their own; the runtime
binds each to a chat's gateway token as it opens the chat, so every gateway
call a chat causes is made as that chat. Unbound, each fails closed — a verdict
that cannot be asked, a spawn that inherits, tools that stay off — and none of
them ever reads ``~/.alkera/auth.yml``. The local forms are the same shape:
a local chat's credential is the sign-in profile it bound when it opened, so
unbound they read no stored token either.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from _adapter_factory import FakeAdapterFactory
from alkera_cli.account import auth_file
from alkera_cli.account.auth_file import StoredAuth
from alkera_cli.contracts.gateway_model import GatewayModel
from alkera_cli.contracts.tool_types import ActionDescriptor, Effect
from alkera_cli.harness import HarnessRuntime, PermissionBroker
from alkera_cli.harness import org_flags as org_flags_module
from alkera_cli.harness import safety_judge as judge_module
from alkera_cli.harness._fake import FakeAdapter
from alkera_cli.harness.gateway_session import (
    GatewayAuthRequiredError,
    bound_to,
    chat_token,
    default_gateway_config_builder,
)
from alkera_cli.harness.org_flags import OrgWebFlags, machine_web_flags, web_search_org_flag
from alkera_cli.harness.safety_judge import (
    GatewaySafetyJudge,
    default_safety_judge,
    machine_safety_judge,
)
from alkera_cli.harness.subagent_routing import (
    TierModelResolver,
    default_subagent_model_resolver,
    machine_subagent_model_resolver,
    make_tier_model_resolver,
)
from alkera_cli.harness.web_flags import WebToolFlags
from alkera_cli.plugins.plugin_base.surfaces import AgentDefinition
from alkera_core.project.directory import ProjectDirectory

CHAT_ID = "chat-credentials"
MODEL = {"provider_id": "alkera-anthropic", "model_id": "claude-opus-4.5", "efforts": ["high"]}
CHEAP = GatewayModel(id="haiku", display_name="Haiku", wire="anthropic", tier="cheap")


class _Catalog:
    """A gateway catalog stand-in that records which credential asked."""

    def __init__(self) -> None:
        self.asked: list[str] = []

    async def models(self, *, gateway_url: str, token: str) -> Sequence[GatewayModel]:
        self.asked.append(token)
        return [CHEAP]

    async def catalog(self, *, gateway_url: str, token: str) -> Any:
        self.asked.append(token)
        return type("Catalog", (), {"web_search_enabled": True, "web_fetch_enabled": False})()


@pytest.fixture
def no_login(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """No ``auth.yml`` anywhere, and reading it is an error: what a box must
    never do is reach for one."""

    def _never() -> None:
        raise AssertionError("the device token file must not be read by a box's harness")

    monkeypatch.setattr(auth_file, "AUTH_FILE_PATH", tmp_path / "absent" / "auth.yml")
    # Every reader of the sign-in file goes through this one function.
    monkeypatch.setattr(auth_file, "_read_document", _never)


@pytest.fixture
def catalog(monkeypatch: pytest.MonkeyPatch) -> _Catalog:
    recorder = _Catalog()
    monkeypatch.setattr(judge_module, "fetch_models", recorder.models)
    monkeypatch.setattr(org_flags_module, "fetch_catalog", recorder.catalog)
    monkeypatch.setattr("alkera_cli.gateway.client.fetch_models", recorder.models)
    return recorder


def _descriptor() -> ActionDescriptor:
    return ActionDescriptor(capability="fs", effect=Effect.WRITE, operation="write")


# --- the seam itself ---------------------------------------------------------


def test_binding_leaves_alone_what_cannot_or_need_not_be_bound() -> None:
    plain = object()
    assert bound_to(plain, "gw") is plain, "a component without the seam is itself"
    assert bound_to(None, "gw") is None, "no judge stays no judge"
    judge = machine_safety_judge()
    assert bound_to(judge, None) is judge, "a local chat has no per-chat token to bind"
    assert bound_to(judge, "gw") is not judge


# --- the judge ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_machine_judge_answers_nothing_until_bound_and_never_reads_a_login(
    no_login: None, catalog: _Catalog
) -> None:
    judge = machine_safety_judge()
    with pytest.raises(GatewayAuthRequiredError):
        await judge.judge(_descriptor(), "goal")
    assert catalog.asked == [], "nothing was asked of the gateway without a credential"


@pytest.mark.asyncio
async def test_a_bound_judge_asks_the_gateway_as_the_chat(
    no_login: None, catalog: _Catalog, monkeypatch: pytest.MonkeyPatch
) -> None:
    completions: list[str] = []

    async def _complete(**kwargs: Any) -> str:
        completions.append(kwargs["token"])
        return '{"decision": "allow", "reason": "fine"}'

    monkeypatch.setattr(judge_module, "complete", _complete)
    judge = machine_safety_judge().bound_to("gw-chat-1")
    verdict = await judge.judge(_descriptor(), "goal")
    assert verdict.decision == "allow"
    assert catalog.asked == ["gw-chat-1"], "the model was resolved as the chat"
    assert completions == ["gw-chat-1"], "the verdict was asked as the chat"


@pytest.mark.asyncio
async def test_a_bound_judge_carries_the_resolved_model_and_a_second_chat_binds_afresh(
    no_login: None, catalog: _Catalog, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The runtime's judge resolves the cheapest model once; a chat bound
    from it pays no second catalog read, and two chats bound from the same
    judge present two credentials."""
    completions: list[str] = []

    async def _complete(**kwargs: Any) -> str:
        completions.append(kwargs["token"])
        return '{"decision": "block", "reason": "no"}'

    monkeypatch.setattr(judge_module, "complete", _complete)
    pinned = GatewaySafetyJudge(gateway_url="https://gw.example", token=None, model="haiku")
    first, second = pinned.bound_to("gw-a"), pinned.bound_to("gw-b")
    await first.judge(_descriptor(), "goal")
    await second.judge(_descriptor(), "goal")
    assert catalog.asked == [], "a pinned model is never re-resolved"
    assert completions == ["gw-a", "gw-b"]


def test_the_local_judge_exists_only_signed_in_and_holds_no_login_of_its_own(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Signed out, a local runtime has no judge (auto-mode writes prompt).
    Signed in, it has a judge that presents NO stored token: it answers only
    once bound to a chat's credential, so a verdict is asked as the chat's
    bound profile and never as whichever sign-in is current now."""
    monkeypatch.setattr(auth_file, "AUTH_FILE_PATH", tmp_path / "auth.yml")
    assert default_safety_judge() is None
    auth_file.save_auth(
        StoredAuth(
            api_url="http://api.test",
            token="device-jwt",
            expires_at=datetime.now(UTC) + timedelta(days=1),
        )
    )
    judge = default_safety_judge()
    assert isinstance(judge, GatewaySafetyJudge)
    with pytest.raises(GatewayAuthRequiredError):
        judge._credential()
    assert bound_to(judge, "chat-profile-jwt")._credential() == "chat-profile-jwt"


# --- the subagent model resolver ---------------------------------------------


def _cheap_agent() -> AgentDefinition:
    return AgentDefinition(name="explore", mode="explore", model_tier="cheap")


@pytest.mark.asyncio
async def test_the_machine_resolver_inherits_unbound_and_routes_as_the_chat_bound(
    no_login: None, catalog: _Catalog
) -> None:
    resolver = machine_subagent_model_resolver()
    assert isinstance(resolver, TierModelResolver)
    assert await resolver(_cheap_agent(), dict(MODEL)) is None, "unbound: inherit, no read"
    assert catalog.asked == []
    routed = await bound_to(resolver, "gw-chat-1")(_cheap_agent(), dict(MODEL))
    assert routed is not None and routed["model_id"] == "haiku"
    assert catalog.asked == ["gw-chat-1"]


@pytest.mark.asyncio
async def test_each_chat_bound_resolver_keeps_a_catalog_of_its_own(
    no_login: None, catalog: _Catalog
) -> None:
    """What one chat's token may list is that chat's catalog: two chats are
    two reads, and a chat asking twice is one."""
    resolver = machine_subagent_model_resolver()
    for_a = bound_to(resolver, "gw-a")
    for_b = bound_to(resolver, "gw-b")
    await for_a(_cheap_agent(), dict(MODEL))
    await for_a(_cheap_agent(), dict(MODEL))
    await for_b(_cheap_agent(), dict(MODEL))
    assert catalog.asked == ["gw-a", "gw-b"]


@pytest.mark.asyncio
async def test_a_resolver_over_a_fixed_lister_cannot_be_rebound() -> None:
    """A test's lister is already what it is; binding it is a no-op rather
    than a resolver that quietly reads nothing."""

    async def fetch() -> Sequence[GatewayModel]:
        return [CHEAP]

    resolver = make_tier_model_resolver(fetch)
    assert bound_to(resolver, "gw") is resolver
    routed = await resolver(_cheap_agent(), dict(MODEL))
    assert routed is not None and routed["model_id"] == "haiku"


@pytest.mark.asyncio
async def test_the_local_resolver_lists_nothing_unbound_and_routes_as_the_chat_bound(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, catalog: _Catalog
) -> None:
    """Even signed in, the unbound local resolver reads no stored token: a
    spawn's catalog is read as its chat's bound credential only."""
    monkeypatch.setattr(auth_file, "AUTH_FILE_PATH", tmp_path / "auth.yml")
    auth_file.save_auth(
        StoredAuth(
            api_url="http://api.test",
            token="device-jwt",
            expires_at=datetime.now(UTC) + timedelta(days=1),
        )
    )
    resolver = default_subagent_model_resolver()
    assert isinstance(resolver, TierModelResolver)
    assert await resolver(_cheap_agent(), dict(MODEL)) is None
    assert catalog.asked == []
    routed = await bound_to(resolver, "chat-profile-jwt")(_cheap_agent(), dict(MODEL))
    assert routed is not None and routed["model_id"] == "haiku"
    assert catalog.asked == ["chat-profile-jwt"]


# --- the org web-tool flags ----------------------------------------------------


@pytest.mark.asyncio
async def test_the_machine_flags_are_off_unbound_and_read_as_the_chat_bound(
    no_login: None, catalog: _Catalog
) -> None:
    assert await machine_web_flags() == WebToolFlags()
    assert catalog.asked == []
    flags = await bound_to(machine_web_flags, "gw-chat-1")()
    assert flags == WebToolFlags(search=True, fetch=False)
    assert catalog.asked == ["gw-chat-1"]


@pytest.mark.asyncio
async def test_the_local_flags_read_nothing_unbound_and_read_as_the_chat_bound(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, catalog: _Catalog
) -> None:
    """Signed in or not, the unbound local flags read no stored token (off);
    bound to a chat's credential they read the org as that chat."""
    monkeypatch.setattr(auth_file, "AUTH_FILE_PATH", tmp_path / "auth.yml")
    auth_file.save_auth(
        StoredAuth(
            api_url="http://api.test",
            token="device-jwt",
            expires_at=datetime.now(UTC) + timedelta(days=1),
        )
    )
    assert isinstance(web_search_org_flag, OrgWebFlags)
    assert await web_search_org_flag() == WebToolFlags()
    assert catalog.asked == []
    flags = await bound_to(web_search_org_flag, "chat-profile-jwt")()
    assert flags == WebToolFlags(search=True, fetch=False)
    assert catalog.asked == ["chat-profile-jwt"]


# --- the runtime threads the chat's token to all three -------------------------


async def _allow(_request: object) -> object:
    raise AssertionError("no permission is asked here")


@pytest.mark.asyncio
async def test_the_runtime_binds_the_judge_the_resolver_and_the_flags_to_the_chats_token(
    no_login: None, catalog: _Catalog, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A box's runtime, opening a chat on its gateway token: the tool binding's
    judge presents the token, the web-tool flags were read as the token when
    the registry was built, and the session keeps the token for the spawns
    and verdicts of its turns. Nothing here read a login."""
    runtime = HarnessRuntime(
        ProjectDirectory(tmp_path / "workspace" / ".alkera"),
        adapter_factory=FakeAdapterFactory(FakeAdapter, available=True),
        gateway_config_builder=default_gateway_config_builder,
        safety_judge=machine_safety_judge(),
        subagent_model_resolver=machine_subagent_model_resolver(),
        web_search_enabled=machine_web_flags,
    )
    runtime.project.chats().create(
        session_id=CHAT_ID, title="t", harness_type="agent", model=dict(MODEL)
    ).close()
    session = await runtime.open_chat(
        CHAT_ID,
        permission_broker=PermissionBroker(_allow, default_timeout_seconds=None),
        gateway_token="gw-chat-1",
    )
    try:
        assert chat_token(session.credential) == "gw-chat-1"
        binding = session._tool_binding
        assert binding is not None
        assert isinstance(binding.judge, GatewaySafetyJudge)
        assert binding.judge._credential() == "gw-chat-1"
        assert "gw-chat-1" in catalog.asked, "the org flags were read as the chat"
        resolver = bound_to(runtime._subagent_model_resolver, chat_token(session.credential))
        assert resolver is not runtime._subagent_model_resolver
    finally:
        await runtime.close_chat(CHAT_ID)
