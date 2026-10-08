"""Sentry: gated no-op without a DSN, redacting before_send, trace-tagged
capture once initialized. We never start a real client — `sentry_sdk.init` and
friends are monkeypatched."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
import sentry_sdk
from alkera_core.observability import sentry
from alkera_core.observability.context import bind_trace_id, reset_trace_id


@pytest.fixture(autouse=True)
def _reset_sentry_state() -> Iterator[None]:
    sentry._initialized = False
    yield
    sentry._initialized = False


def test_init_is_noop_without_dsn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sentry.settings, "sentry_dsn", None)
    called = {"init": False}
    monkeypatch.setattr(sentry.sentry_sdk, "init", lambda **_k: called.__setitem__("init", True))
    assert sentry.init_sentry("backend") is False
    assert called["init"] is False


def test_capture_is_noop_without_init() -> None:
    assert sentry.capture_exception(RuntimeError("x")) is None
    assert sentry.capture_message("hi") is None


def test_init_refuses_self_hosted_server_component(monkeypatch: pytest.MonkeyPatch) -> None:
    """A self-hosted server component (no explicit dsn) NEVER starts Sentry, even with a
    DSN configured — telemetry must not leave the customer's VPC."""
    monkeypatch.setattr(sentry.settings, "self_hosted", True)
    monkeypatch.setattr(sentry.settings, "sentry_dsn", "https://k@example.test/1")
    called = {"init": False}
    monkeypatch.setattr(sentry.sentry_sdk, "init", lambda **_k: called.__setitem__("init", True))
    assert sentry.init_sentry("gateway") is False
    assert called["init"] is False


def test_init_allows_explicit_dsn_channel_even_when_self_hosted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The CLI/daemon pass their OWN dsn (a separate telemetry channel) and are exempt
    from the server-side self-hosted gate."""
    monkeypatch.setattr(sentry.settings, "self_hosted", True)
    monkeypatch.setattr(sentry.sentry_sdk, "init", lambda **_k: None)
    monkeypatch.setattr(sentry.sentry_sdk, "set_tag", lambda *_a, **_k: None)
    assert sentry.init_sentry("daemon", dsn="https://k@example.test/1") is True


def test_init_with_dsn_configures_redacting_before_send(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}
    monkeypatch.setattr(sentry.sentry_sdk, "init", lambda **kw: captured.update(kw))
    monkeypatch.setattr(sentry.sentry_sdk, "set_tag", lambda *_a, **_k: None)
    monkeypatch.setattr(sentry.settings, "app_env", "staging")
    monkeypatch.setattr(sentry.settings, "sentry_environment", None)

    assert sentry.init_sentry("gateway", dsn="https://k@example.test/1") is True
    assert captured["dsn"] == "https://k@example.test/1"
    assert captured["environment"] == "staging"
    assert captured["send_default_pii"] is False
    assert captured["before_send"] is sentry._before_send


def test_init_disables_auto_enabling_integrations(monkeypatch: pytest.MonkeyPatch) -> None:
    """sentry_sdk imports its auto-enabling integrations by dynamic string name —
    a class Nuitka can't follow, so a frozen binary crashes loading them at init
    (the field crash: ModuleNotFoundError sentry_sdk.integrations.aiohttp). We
    disable the auto-enabling set so those framework integrations are never
    loaded."""
    captured: dict[str, Any] = {}
    monkeypatch.setattr(sentry.sentry_sdk, "init", lambda **kw: captured.update(kw))
    monkeypatch.setattr(sentry.sentry_sdk, "set_tag", lambda *_a, **_k: None)
    assert sentry.init_sentry("daemon", dsn="https://k@example.test/1") is True
    assert captured["auto_enabling_integrations"] is False


def test_init_caller_can_override_auto_enabling(monkeypatch: pytest.MonkeyPatch) -> None:
    """The auto-enabling default is a setdefault, not a hard override — a caller
    that genuinely wants framework auto-detection can still ask for it."""
    captured: dict[str, Any] = {}
    monkeypatch.setattr(sentry.sentry_sdk, "init", lambda **kw: captured.update(kw))
    monkeypatch.setattr(sentry.sentry_sdk, "set_tag", lambda *_a, **_k: None)
    sentry.init_sentry("daemon", dsn="https://k@example.test/1", auto_enabling_integrations=True)
    assert captured["auto_enabling_integrations"] is True


def test_init_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    counter = {"n": 0}
    monkeypatch.setattr(
        sentry.sentry_sdk, "init", lambda **_k: counter.__setitem__("n", counter["n"] + 1)
    )
    monkeypatch.setattr(sentry.sentry_sdk, "set_tag", lambda *_a, **_k: None)
    sentry.init_sentry("backend", dsn="https://k@example.test/1")
    sentry.init_sentry("backend", dsn="https://k@example.test/1")
    assert counter["n"] == 1


def test_shutdown_closes_client_and_resets(monkeypatch: pytest.MonkeyPatch) -> None:
    """`shutdown_sentry` flushes/closes the client and flips back to uninitialized,
    so a long-running process stops reporting and `capture_*` no-op again."""
    closed = {"n": 0}

    class _FakeClient:
        def close(self) -> None:
            closed["n"] += 1

    monkeypatch.setattr(sentry.sentry_sdk, "get_client", lambda: _FakeClient())
    sentry._initialized = True
    sentry.shutdown_sentry()
    assert closed["n"] == 1
    assert sentry.capture_exception(RuntimeError("x")) is None


def test_shutdown_is_noop_when_not_initialized(monkeypatch: pytest.MonkeyPatch) -> None:
    """No client to close when Sentry never started — don't even touch the SDK."""
    touched = {"get_client": False}
    monkeypatch.setattr(
        sentry.sentry_sdk, "get_client", lambda: touched.__setitem__("get_client", True)
    )
    sentry.shutdown_sentry()  # _initialized is False (autouse reset)
    assert touched["get_client"] is False


def test_init_after_shutdown_reinitializes(monkeypatch: pytest.MonkeyPatch) -> None:
    """enable → disable → enable: a second `init_sentry` after `shutdown_sentry`
    actually starts a fresh client (the idempotent guard was cleared)."""
    counter = {"n": 0}
    monkeypatch.setattr(
        sentry.sentry_sdk, "init", lambda **_k: counter.__setitem__("n", counter["n"] + 1)
    )
    monkeypatch.setattr(sentry.sentry_sdk, "set_tag", lambda *_a, **_k: None)
    monkeypatch.setattr(sentry.sentry_sdk, "get_client", lambda: _ClosableClient())

    assert sentry.init_sentry("daemon", dsn="https://k@example.test/1") is True
    sentry.shutdown_sentry()
    assert sentry.init_sentry("daemon", dsn="https://k@example.test/1") is True
    assert counter["n"] == 2  # the re-init ran, not short-circuited


class _ClosableClient:
    def close(self) -> None:
        return None


def test_init_disables_frame_local_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    """Frame locals carry plaintext passwords / decrypted credentials / API keys —
    sentry_sdk ships a repr() of every one of them by default."""
    captured: dict[str, Any] = {}
    monkeypatch.setattr(sentry.sentry_sdk, "init", lambda **kw: captured.update(kw))
    monkeypatch.setattr(sentry.sentry_sdk, "set_tag", lambda *_a, **_k: None)
    assert sentry.init_sentry("backend", dsn="https://k@example.test/1") is True
    assert captured["include_local_variables"] is False


def test_caller_cannot_re_enable_frame_local_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unlike the auto-enabling-integrations knob, this one is NOT a setdefault: a
    caller asking for frame locals is refused (and must not crash on the duplicate
    keyword either)."""
    captured: dict[str, Any] = {}
    monkeypatch.setattr(sentry.sentry_sdk, "init", lambda **kw: captured.update(kw))
    monkeypatch.setattr(sentry.sentry_sdk, "set_tag", lambda *_a, **_k: None)
    assert (
        sentry.init_sentry("daemon", dsn="https://k@example.test/1", include_local_variables=True)
        is True
    )
    assert captured["include_local_variables"] is False


@pytest.mark.parametrize(
    "event_factory",
    [
        pytest.param(
            lambda frame: {"exception": {"values": [{"stacktrace": {"frames": [frame]}}]}},
            id="exception-stacktrace",
        ),
        pytest.param(
            lambda frame: {"threads": {"values": [{"stacktrace": {"frames": [frame]}}]}},
            id="thread-stacktrace",
        ),
        pytest.param(
            lambda frame: {"stacktrace": {"frames": [frame]}},
            id="top-level-stacktrace",
        ),
        pytest.param(
            lambda frame: {
                "exception": {
                    "values": [
                        {"stacktrace": {"frames": [dict(frame)]}},
                        {"stacktrace": {"frames": [frame]}},
                    ]
                }
            },
            id="chained-exception-every-value",
        ),
    ],
)
def test_before_send_strips_frame_locals(event_factory: Any) -> None:
    """Whatever attached them, frame locals never leave the process — the SDK's own
    scrubber matches variable NAMES non-recursively, so a secret inside a
    container-typed local (a bound request model) sails past it."""
    frame = {
        "filename": "auth.py",
        "function": "login",
        "lineno": 170,
        "vars": {
            "payload": "LoginRequest(email='victim@corp.example', password='Hunter2-Real')",
            "provider_api_key": "'sk-ant-REALPROVIDERKEY0123456789'",
        },
    }
    out = sentry._before_send(event_factory(frame), {})
    serialized = repr(out)
    assert "Hunter2-Real" not in serialized
    assert "sk-ant-REALPROVIDERKEY0123456789" not in serialized
    assert '"vars"' not in serialized and "'vars'" not in serialized
    # Everything else about the frame survives — the diagnostic value is the stack.
    assert "login" in serialized


def test_before_send_keeps_non_frame_payload_intact() -> None:
    """The frame walk must not eat unrelated keys that merely nest deeply."""
    event = {
        "exception": {"values": [{"type": "RuntimeError", "value": "boom"}]},
        "extra": {"note": "keep me"},
    }
    out = sentry._before_send(event, {})
    assert out["exception"]["values"][0]["value"] == "boom"
    assert out["extra"]["note"] == "keep me"


#: Referenced by NAME inside the frame below, so the literal never appears in the
#: source context Sentry attaches — only in the frame's locals, which is exactly
#: the channel under test.
_PLAINTEXT_PASSWORD = "Hunter2-Real-Password"
_PROVIDER_API_KEY = "sk-ant-REALPROVIDERKEY0123456789"


class _CapturingTransport(sentry_sdk.Transport):
    """Keeps events in memory instead of shipping them to Sentry."""

    def __init__(self) -> None:
        super().__init__()
        self.events: list[dict[str, Any]] = []

    def capture_envelope(self, envelope: Any) -> None:
        event = envelope.get_event()
        if event is not None:
            self.events.append(event)


def test_frame_locals_never_reach_the_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    """End-to-end through the REAL sentry_sdk: an unknown/renamed init option is
    silently ignored by the SDK, so prove the SHIPPED event carries no frame vars
    and no secret value — not merely that we passed the right kwarg."""
    monkeypatch.setattr(sentry.settings, "self_hosted", False)
    transport = _CapturingTransport()

    assert (
        sentry.init_sentry("backend", dsn="https://k@example.test/1", transport=transport) is True
    )
    try:

        def _login() -> None:
            password = _PLAINTEXT_PASSWORD
            provider_api_key = _PROVIDER_API_KEY
            assert password and provider_api_key
            raise RuntimeError("pool timeout")

        try:
            _login()
        except RuntimeError as exc:
            sentry.capture_exception(exc)
        sentry.sentry_sdk.get_client().flush()
    finally:
        sentry.shutdown_sentry()

    assert len(transport.events) == 1
    payload = json.dumps(transport.events[0], default=str)
    assert _PLAINTEXT_PASSWORD not in payload
    assert _PROVIDER_API_KEY not in payload
    assert '"vars"' not in payload
    # The stack itself is still reported — we removed the locals, not the frames.
    assert "_login" in payload


def test_before_send_redacts_secrets() -> None:
    event = {
        "request": {"headers": {"Authorization": "Bearer secret-abc123def456"}},
        "extra": {"password": "hunter2", "note": "/Users/robin/x"},
        "message": "boom",
    }
    out = sentry._before_send(event, {})
    assert out is not None
    assert out["request"]["headers"]["Authorization"] == "[redacted]"
    assert out["extra"]["password"] == "[redacted]"
    assert out["extra"]["note"] == "~/x"


def test_capture_exception_tags_trace_id(monkeypatch: pytest.MonkeyPatch) -> None:
    sentry._initialized = True
    scope = _FakeScope()
    monkeypatch.setattr(sentry.sentry_sdk, "new_scope", lambda: _fake_scope_cm(scope))
    seen: dict[str, Any] = {}
    monkeypatch.setattr(
        sentry.sentry_sdk, "capture_exception", lambda err: seen.update(err=err) or "evt-1"
    )

    token = bind_trace_id("trace-xyz")
    try:
        err = RuntimeError("boom")
        assert sentry.capture_exception(err, component="daemon") == "evt-1"
    finally:
        reset_trace_id(token)

    assert seen["err"] is err
    assert scope.tags["trace_id"] == "trace-xyz"
    assert scope.tags["component"] == "daemon"


class _FakeScope:
    def __init__(self) -> None:
        self.tags: dict[str, str] = {}

    def set_tag(self, key: str, value: str) -> None:
        self.tags[key] = value


@contextmanager
def _fake_scope_cm(scope: _FakeScope) -> Iterator[_FakeScope]:
    yield scope
