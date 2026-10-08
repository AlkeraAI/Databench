"""Editor-side slash dispatch (chat_slash.dispatch_ui) — the /usage payload.

The VS Code card is built from this payload: the account credit balance + split
PLUS this chat's spend (`chat_credits`), and deliberately NOT the account 30-day
usage / request count. Exercised with a stub context + mocked backend so no
network or real auth is touched.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from _profiles import ORG_A, ORG_B, store
from alkera_cli.account import auth_file
from alkera_cli.account.binding import ProfileBinding
from alkera_cli.chat import slash as chat_slash
from alkera_core.schemas.my_usage import MyCreditsResponse, MyUsageResponse


def _ctx(cost_total: float, credential: Any = None) -> Any:
    """A minimal SlashContext stand-in — `_ui_usage` reads the manifest cost and
    the sign-in the chat acts as (its credential; no project pin here)."""
    return SimpleNamespace(
        session=SimpleNamespace(
            manifest=SimpleNamespace(cost_total=cost_total), project=None, credential=credential
        )
    )


def test_onboarding_is_in_the_command_vocabulary() -> None:
    """The editor's `/onboarding` menu entry comes from `harness.list_commands`,
    which mirrors `chat_slash.COMMANDS` — so the command must live there or the
    composer can never offer it. The editor RUNS it client-side (a host command),
    so its editor dispatch is the cli_only hint, not a daemon action."""
    names = {spec.name for spec in chat_slash.COMMANDS}
    assert "onboarding" in names
    outcome = chat_slash.dispatch_ui("/onboarding", _ctx(0.0))
    assert outcome.kind == "cli_only"
    assert outcome.command == "onboarding"


def _bound_chat() -> tuple[Any, auth_file.Profile]:
    """A chat bound to profile A, with B made current after it opened."""
    a = store(ORG_A, org_name="Acme", current=True)
    b = store(ORG_B, org_name="Bravo")
    binding = ProfileBinding(a, "current")
    auth_file.set_current(b.key)
    return binding, a


def _credits() -> MyCreditsResponse:
    return MyCreditsResponse.model_validate(
        {
            "tier_key": "free",
            "tier_name": "Free",
            "pct_used": 16.0,
            "reset_at": None,
            "prepaid_credits": 4200,
        }
    )


def _usage() -> MyUsageResponse:
    return MyUsageResponse.model_validate(
        {
            "window": "30d",
            "total_requests": 12,
            "by_model": [],
            "daily": [],
        }
    )


def test_ui_usage_payload_carries_chat_used_not_account_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding, a = _bound_chat()
    asked: list[tuple[Any, ...]] = []

    def _fetch(api_url: str, token: str, **kw: Any) -> tuple[Any, Any]:
        asked.append((api_url, token, kw.get("org_id")))
        return _credits(), _usage()

    monkeypatch.setattr(chat_slash, "fetch_usage", _fetch)
    outcome = chat_slash.dispatch_ui("/usage", _ctx(1.2339, binding))
    # Read as the chat's own sign-in, not the one made current after it opened.
    assert asked == [(a.api_url, a.token, ORG_A)]
    assert outcome.kind == "ok"
    assert outcome.command == "usage"
    # This chat's spend ($1.2339 → 1,234 credits) + the obfuscated account summary.
    assert outcome.payload["chat_credits"] == 1234
    assert outcome.payload["credits"]["tier_name"] == "Free"
    assert outcome.payload["credits"]["pct_used"] == 16.0
    assert outcome.payload["credits"]["prepaid_credits"] == 4200
    # The account 30-day usage / request count is NOT part of the editor payload.
    assert "usage" not in outcome.payload
    assert "window" not in outcome.payload


def test_ui_usage_not_signed_in_is_an_error_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    outcome = chat_slash.dispatch_ui("/usage", _ctx(0.0))
    assert outcome.kind == "ok"
    assert "error" in outcome.payload
    assert "chat_credits" not in outcome.payload


def test_ui_usage_rejects_a_bad_window_before_any_backend_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding, _a = _bound_chat()
    monkeypatch.setattr(
        chat_slash, "fetch_usage", lambda *a, **k: pytest.fail("fetch_usage must not run")
    )
    outcome = chat_slash.dispatch_ui("/usage nonsense", _ctx(0.0, binding))
    assert outcome.kind == "bad_usage"
