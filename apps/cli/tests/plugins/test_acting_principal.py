"""Who the gate thinks is acting, read without touching the network.

The principal decides whether an affected asset's owner is foreign, and it is read
across a trust boundary before the tool server binds. A stubbed transport keeps
escalation from depending on whoever is signed in on the machine running the suite.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from alkera_cli.account.auth_file import StoredAuth
from alkera_cli.plugins.plugin_base.permissions.actor import (
    resolve_acting_principal,
    set_auth_source,
)

_Answers = Callable[[httpx.Request], httpx.Response]
_REAL_CLIENT = httpx.AsyncClient
_SIGNED_IN = StoredAuth(
    api_url="https://api.invalid", token="t", expires_at=datetime.now(UTC) + timedelta(days=1)
)


def _scopes(*tokens: str, sync_enabled: bool = True) -> _Answers:
    """A backend whose caller belongs to ``tokens``. The share offer mirrors the
    membership while sync is on and empties when it is off, as the real backend's
    does; ``member_scopes`` serves either way, and the org-settings read answers
    with escalation armed."""
    scopes = {
        "scopes": [{"scope": t, "label": t} for t in tokens] if sync_enabled else [],
        "member_scopes": list(tokens),
    }
    settings = {"ownership_escalation_enabled": True}

    def _answer(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/kb/scopes"):
            return httpx.Response(200, json=scopes)
        if request.url.path.endswith("/org/settings"):
            return httpx.Response(200, json=settings)
        return httpx.Response(404)

    return _answer


def _down(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("this stub refuses every request")


def _network(monkeypatch: pytest.MonkeyPatch, *, answers: _Answers) -> list[str]:
    """A transport standing in for the network. Returns the paths the resolver
    asked for."""
    asked: list[str] = []

    def _serve(request: httpx.Request) -> httpx.Response:
        asked.append(request.url.path)
        return answers(request)

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *a, **kw: _REAL_CLIENT(*a, transport=httpx.MockTransport(_serve), **kw),
    )
    return asked


def _bind(
    monkeypatch: pytest.MonkeyPatch, *, answers: _Answers, signed_in: bool = True
) -> list[str]:
    """A stubbed sign-in and a transport standing in for the network. Installing
    the sign-in drops the resolver's cache, so a test whose sign-in stands and
    only the network changes rebinds through ``_network`` alone. Returns the
    paths the resolver asked for."""
    set_auth_source(lambda: _SIGNED_IN if signed_in else None)
    return _network(monkeypatch, answers=answers)


async def test_an_unreadable_principal_is_unknown_and_never_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Signed out, and signed in against a backend that cannot answer, both leave
    the principal unknown. An empty one would read as "every owner is foreign"."""
    asked = _bind(monkeypatch, answers=_scopes("team:finance"), signed_in=False)
    assert await resolve_acting_principal() is None
    assert asked == [], "a signed-out machine called the backend anyway"

    tried = _bind(monkeypatch, answers=_down)
    assert await resolve_acting_principal() is None
    assert tried, "the signed-in read never reached the backend"


async def test_a_principal_keeps_only_shared_scopes_and_survives_a_blip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Membership is what makes an owner familiar, so only team and org tokens
    count. A later refresh that fails keeps serving that read rather than emptying
    it, since membership rarely changes and a blip would widen what escalates."""
    _bind(monkeypatch, answers=_scopes("team:finance", "org:acme", "private", "team:"))

    known = await resolve_acting_principal()

    assert known is not None
    assert "team:finance" in known.teams, "a shared scope was dropped"
    assert not known.teams - {"team:finance", "org:acme"}, "an unshared scope became membership"

    tried = _network(monkeypatch, answers=_down)
    assert await resolve_acting_principal(ttl_seconds=0.0) == known
    assert tried, "the stale entry was served without attempting a refresh"


async def test_a_sync_disabled_org_still_resolves_the_actors_teams(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sync off empties the share offer and must not empty membership with it.
    A principal read from the offer would call every owner foreign in the one
    org that opted out of sharing, and each write there would escalate."""
    _bind(monkeypatch, answers=_scopes("team:finance", "org:acme", sync_enabled=False))

    known = await resolve_acting_principal()

    assert known is not None
    assert "team:finance" in known.teams, "sync off emptied the caller's teams"
    assert not known.teams - {"team:finance", "org:acme"}
    assert known.escalation_enabled, "the org-settings read never reached the principal"
