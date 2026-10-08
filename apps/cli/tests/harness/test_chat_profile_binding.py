"""A local chat acts as the sign-in profile it bound when it opened.

Switching the current profile (another terminal, another editor window) must
change what NEW chats open as, never a running one: the next gateway call a
running chat makes, its org's web-tool flags, its judge and its spawns all
present the profile it opened with. Logging that profile out stops the chat
on the auth-required path; it never falls through to whichever profile is
current now. A project pinned to another org refuses to open a chat at all.

Before profiles existed the local providers re-read ``auth.yml`` on every
call, so a switch mid-chat moved the rest of the chat into the other org:
these tests fail on that code.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from _adapter_factory import FakeAdapterFactory
from _profiles import API, ORG_A, ORG_B, make_jwt, store
from alkera_cli.account import auth_file
from alkera_cli.account.auth_file import ProjectOrgMismatchError
from alkera_cli.account.binding import ProfileBinding, bind_session
from alkera_cli.gateway.client import GatewayCatalog
from alkera_cli.harness import HarnessRuntime, PermissionBroker
from alkera_cli.harness import org_flags as org_flags_module
from alkera_cli.harness._fake import FakeAdapter
from alkera_cli.harness.gateway_session import (
    GatewayAuthRequiredError,
    chat_token,
    default_gateway_config_builder,
)
from alkera_cli.harness.org_flags import web_search_org_flag
from alkera_cli.harness.web_flags import WebToolFlags
from alkera_cli.observability.audit_report import AuditReporter
from alkera_core.project.cloud_binding import CloudBinding
from alkera_core.project.directory import ProjectDirectory

CHAT = "chat-bound"
OTHER = "chat-opened-later"
MODEL = {"provider_id": "alkera-anthropic", "model_id": "claude-opus-4.5", "efforts": ["high"]}
TOKEN_A = make_jwt(org=ORG_A, exp=4102444800)
TOKEN_B = make_jwt(org=ORG_B, exp=4102444800)


class _Gateway:
    """The gateway's catalog as each token reads it. A token it does not know
    is refused, so a read as the wrong profile cannot pass for the right one."""

    def __init__(self) -> None:
        self.asked: list[str] = []

    async def catalog(self, *, gateway_url: str, token: str) -> GatewayCatalog:
        self.asked.append(token)
        if token not in (TOKEN_A, TOKEN_B):
            raise AssertionError(f"unknown token {token!r}")
        return GatewayCatalog(models=[], web_search_enabled=True, web_fetch_enabled=False)


@pytest.fixture
def gateway(monkeypatch: pytest.MonkeyPatch) -> _Gateway:
    recorder = _Gateway()
    monkeypatch.setattr(org_flags_module, "fetch_catalog", recorder.catalog)
    return recorder


async def _allow(_request: object) -> object:
    raise AssertionError("no permission is asked here")


@pytest.fixture
async def runtime(tmp_path: Path) -> AsyncIterator[tuple[HarnessRuntime, FakeAdapterFactory]]:
    """A local runtime: the local gateway builder and the local web flags."""
    factory = FakeAdapterFactory(lambda: FakeAdapter(reply_text="done"), available=True)
    rt = HarnessRuntime(
        ProjectDirectory(tmp_path / "workspace" / ".alkera"),
        adapter_factory=factory,
        gateway_config_builder=default_gateway_config_builder,
        web_search_enabled=web_search_org_flag,
    )
    for chat_id in (CHAT, OTHER):
        rt.project.chats().create(
            session_id=chat_id, title=chat_id, harness_type="agent", model=dict(MODEL)
        ).close()
    try:
        yield rt, factory
    finally:
        await rt.close_all()


async def _open(rt: HarnessRuntime, chat_id: str, **kwargs: Any) -> Any:
    return await rt.open_chat(
        chat_id, permission_broker=PermissionBroker(_allow, default_timeout_seconds=None), **kwargs
    )


def _agent_config_token(factory: FakeAdapterFactory, chat_id: str) -> str:
    config = next(c for c in factory.configs if c.session_id == chat_id)
    rendered = repr(config.harness_native["agent_config"])
    hits = [t for t in (TOKEN_A, TOKEN_B) if t in rendered]
    assert len(hits) == 1, "the agent config carries exactly one credential"
    return hits[0]


def _two_profiles() -> tuple[auth_file.Profile, auth_file.Profile]:
    a = store(ORG_A, org_name="Acme", current=True, token=TOKEN_A)
    b = store(ORG_B, org_name="Bravo", token=TOKEN_B)
    return a, b


async def test_a_switch_after_open_leaves_the_running_chat_on_its_profile(
    runtime: tuple[HarnessRuntime, FakeAdapterFactory], gateway: _Gateway
) -> None:
    rt, factory = runtime
    _a, b = _two_profiles()
    session = await _open(rt, CHAT)
    assert _agent_config_token(factory, CHAT) == TOKEN_A
    assert gateway.asked == [TOKEN_A], "the org flags were read as A at open"

    auth_file.set_current(b.key)
    await session.send_prompt("next turn")

    assert gateway.asked == [TOKEN_A, TOKEN_A], "the next turn's gateway read is still A"
    assert chat_token(session.credential) == TOKEN_A
    assert session.tool_binding.web_tools == WebToolFlags(search=True)


async def test_a_chat_opened_after_the_switch_acts_as_the_new_profile(
    runtime: tuple[HarnessRuntime, FakeAdapterFactory], gateway: _Gateway
) -> None:
    rt, factory = runtime
    _a, b = _two_profiles()
    first = await _open(rt, CHAT)
    auth_file.set_current(b.key)
    second = await _open(rt, OTHER)

    assert _agent_config_token(factory, CHAT) == TOKEN_A
    assert _agent_config_token(factory, OTHER) == TOKEN_B
    assert chat_token(first.credential) == TOKEN_A
    assert chat_token(second.credential) == TOKEN_B


async def test_a_child_opened_with_its_parents_credential_keeps_the_parents_profile(
    runtime: tuple[HarnessRuntime, FakeAdapterFactory], gateway: _Gateway
) -> None:
    """Subagents open with their parent's credential, so a spawn after a switch
    still runs (and bills) as the parent's org."""
    rt, factory = runtime
    _a, b = _two_profiles()
    parent = await _open(rt, CHAT)
    auth_file.set_current(b.key)
    child = await _open(rt, OTHER, credential=parent.credential)
    assert _agent_config_token(factory, OTHER) == TOKEN_A
    assert chat_token(child.credential) == TOKEN_A


async def test_a_relogin_of_the_same_profile_is_picked_up_by_the_running_chat(
    runtime: tuple[HarnessRuntime, FakeAdapterFactory], gateway: _Gateway
) -> None:
    rt, _factory = runtime
    a, _b = _two_profiles()
    session = await _open(rt, CHAT)
    fresher = make_jwt(org=ORG_A, exp=4102444801)
    auth_file.save_profile(a.model_copy(update={"token": fresher}), make_current=False)
    assert chat_token(session.credential) == fresher


async def test_logging_out_the_bound_profile_fails_the_chat_closed(
    runtime: tuple[HarnessRuntime, FakeAdapterFactory], gateway: _Gateway
) -> None:
    rt, _factory = runtime
    a, _b = _two_profiles()
    session = await _open(rt, CHAT)
    gateway.asked.clear()

    auth_file.remove_profile(a.key)  # B is current now

    with pytest.raises(GatewayAuthRequiredError, match="is gone"):
        chat_token(session.credential)
    with pytest.raises(GatewayAuthRequiredError):
        await session.send_prompt("after the logout")
    assert TOKEN_B not in gateway.asked, "the chat never fell through to the other profile"


async def test_a_project_pinned_to_another_org_refuses_to_open_a_chat(
    runtime: tuple[HarnessRuntime, FakeAdapterFactory], gateway: _Gateway
) -> None:
    rt, factory = runtime
    store(ORG_B, org_name="Bravo", current=True, token=TOKEN_B)
    rt.project.cloud_binding().write(CloudBinding(api_url=API, org_team_id=ORG_A, org_name="Acme"))

    with pytest.raises(ProjectOrgMismatchError, match="This project belongs to Acme"):
        await _open(rt, CHAT)
    assert factory.configs == [], "no agent was spawned"
    assert gateway.asked == []


async def test_a_pinned_project_opens_as_its_org_whatever_is_current(
    runtime: tuple[HarnessRuntime, FakeAdapterFactory], gateway: _Gateway
) -> None:
    rt, factory = runtime
    _a, b = _two_profiles()
    auth_file.set_current(b.key)
    rt.project.cloud_binding().write(CloudBinding(api_url=API, org_team_id=ORG_A, org_name="Acme"))
    await _open(rt, CHAT)
    assert _agent_config_token(factory, CHAT) == TOKEN_A


async def test_a_cloud_chats_own_token_is_never_replaced_by_a_profile(
    runtime: tuple[HarnessRuntime, FakeAdapterFactory], gateway: _Gateway
) -> None:
    rt, _factory = runtime
    _two_profiles()
    session = await _open(rt, CHAT, gateway_token=TOKEN_B)
    assert chat_token(session.credential) == TOKEN_B


# --- the audit trail of a bound chat ---------------------------------------------


def test_a_chats_audit_events_are_delivered_as_its_profile_after_a_switch(
    tmp_path: Path,
) -> None:
    a, b = _two_profiles()
    bind_session("sess-a", ProfileBinding(a, "current"))
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"accepted": 1})

    reporter = AuditReporter(
        api_url=API,
        token_provider=lambda: p.token if (p := auth_file.resolve_profile()) else None,
        credential_for=auth_file.load_profile,
        spool_path=tmp_path / "spool.jsonl",
        transport=httpx.MockTransport(handler),
        start_worker=False,
    )
    try:
        auth_file.set_current(b.key)
        reporter.session_started(session_id="sess-a", project="p", detail={})
        reporter.session_started(session_id="sess-unclaimed", project="p", detail={})
        reporter.flush_now()
    finally:
        reporter.close()

    by_token = {r.headers["authorization"]: r for r in seen}
    assert set(by_token) == {f"Bearer {TOKEN_A}", f"Bearer {TOKEN_B}"}
    claimed = by_token[f"Bearer {TOKEN_A}"]
    assert claimed.headers["x-alkera-org"] == ORG_A
    body = claimed.read().decode()
    assert "sess-a" in body and "sess-unclaimed" not in body
    assert "_alkera_profile" not in body, "the routing tag never reaches the wire"


def test_a_claimed_event_waits_in_the_spool_when_its_profile_is_gone(tmp_path: Path) -> None:
    a, _b = _two_profiles()
    bind_session("sess-gone", ProfileBinding(a, "current"))
    seen: list[httpx.Request] = []
    reporter = AuditReporter(
        api_url=API,
        token_provider=lambda: TOKEN_B,
        credential_for=auth_file.load_profile,
        spool_path=tmp_path / "spool.jsonl",
        transport=httpx.MockTransport(lambda r: seen.append(r) or httpx.Response(200, json={})),
        start_worker=False,
    )
    try:
        auth_file.remove_profile(a.key)
        reporter.session_started(session_id="sess-gone", project="p", detail={})
        reporter.flush_now()
    finally:
        reporter.close()
    assert seen == [], "nothing went out as another profile"
    assert "sess-gone" in (tmp_path / "spool.jsonl").read_text()
