"""The shared Temporal client factory: option resolution, the lazy process-wide
client, and the never-raising nudge primitive.

No Temporal server is involved: ``Client.connect`` is replaced by a recorder, so
the tests pin what the factory DECIDES (TLS, key, identity, converter) and how the
nudge primitive behaves against a slow or failing orchestrator.
"""

from __future__ import annotations

import asyncio
import os
import socket
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from alkera_core.config import Settings
from alkera_core.temporal import (
    ClientOptions,
    client_options,
    close_shared_client,
    connect_client,
    default_identity,
    reset_shared_client,
    shared_client,
    start_workflow_best_effort,
)
from alkera_core.temporal import client as client_mod
from temporalio.client import Client, TLSConfig
from temporalio.common import WorkflowIDConflictPolicy
from temporalio.contrib.pydantic import pydantic_data_converter

#: This module IS the client factory, so it calls ``connect_client`` directly with
#: ``Client.connect`` faked — no socket is ever opened. The root conftest's guard
#: would otherwise refuse those calls on its way to catching implicit dials.
pytestmark = pytest.mark.real_temporal_client

_CERT_PEM = "-----BEGIN CERTIFICATE-----\nMIIBfake\n-----END CERTIFICATE-----\n"
_KEY_PEM = "-----BEGIN PRIVATE KEY-----\nMIIBfake\n-----END PRIVATE KEY-----\n"


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch) -> None:
    """A developer's `.env.local` or CI may export TEMPORAL_*; these tests build
    explicit settings and must not see them. The shared client is reset around
    every test so no case inherits another's connection."""
    for var in (
        "TEMPORAL_ADDRESS",
        "TEMPORAL_NAMESPACE",
        "TEMPORAL_API_KEY",
        "TEMPORAL_TLS",
        "TEMPORAL_TLS_CLIENT_CERT",
        "TEMPORAL_TLS_CLIENT_KEY",
    ):
        monkeypatch.delenv(var, raising=False)
    reset_shared_client()
    yield
    reset_shared_client()


def _settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, **overrides)  # type: ignore[arg-type, call-arg]


class _Recorder:
    """Stands in for the module logger: the app configures structlog with
    ``cache_logger_on_first_use``, which makes ``capture_logs`` order-dependent."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def warning(self, event: str, **kw: Any) -> None:
        self.calls.append((event, kw))


@pytest.fixture
def log_rec(monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    rec = _Recorder()
    monkeypatch.setattr(client_mod, "log", rec)
    return rec


# --- client_options ---------------------------------------------------------


def test_defaults_resolve_to_the_local_dev_server_without_tls() -> None:
    opts = client_options(_settings())
    assert opts == ClientOptions(
        target_host="localhost:7233",
        namespace="default",
        api_key=None,
        tls=False,
        identity=opts.identity,
    )


@pytest.mark.parametrize(
    ("api_key", "tls_flag", "expected_tls", "expected_key"),
    [
        pytest.param(None, None, False, None, id="no-key-auto-off"),
        pytest.param("tmprl_key", None, True, "tmprl_key", id="key-auto-on"),
        pytest.param("tmprl_key", True, True, "tmprl_key", id="key-explicit-on"),
        pytest.param(None, True, True, None, id="no-key-explicit-on"),
        pytest.param(None, False, False, None, id="no-key-explicit-off"),
        pytest.param("tmprl_key", False, False, "tmprl_key", id="key-explicit-off-honoured-here"),
        pytest.param("", None, False, None, id="blank-key-is-unset"),
    ],
)
def test_tls_and_key_resolution(
    api_key: str | None, tls_flag: bool | None, expected_tls: bool, expected_key: str | None
) -> None:
    """The local resolution honours whatever is configured; refusing a key over a
    cleartext link is the PRODUCTION validator's job (test_settings.py)."""
    opts = client_options(_settings(temporal_api_key=api_key, temporal_tls=tls_flag))
    assert opts.tls is expected_tls
    assert opts.api_key == expected_key


def test_address_and_namespace_come_from_settings() -> None:
    opts = client_options(
        _settings(temporal_address="temporal.internal:7233", temporal_namespace="prod.acct")
    )
    assert opts.target_host == "temporal.internal:7233"
    assert opts.namespace == "prod.acct"


def test_inline_pem_pair_becomes_a_client_tls_config() -> None:
    opts = client_options(
        _settings(temporal_tls_client_cert=_CERT_PEM, temporal_tls_client_key=_KEY_PEM)
    )
    assert isinstance(opts.tls, TLSConfig)
    assert opts.tls.client_cert == _CERT_PEM.encode()
    assert opts.tls.client_private_key == _KEY_PEM.encode()
    assert opts.tls.server_root_ca_cert is None


def test_pem_file_pair_is_read_from_disk(tmp_path: Path) -> None:
    cert = tmp_path / "client.crt"
    key = tmp_path / "client.key"
    # write_bytes, not write_text: text mode rewrites "\n" to the platform
    # newline, and a PEM is bytes the loader hands to the TLS stack verbatim.
    cert.write_bytes(_CERT_PEM.encode())
    key.write_bytes(_KEY_PEM.encode())
    opts = client_options(
        _settings(temporal_tls_client_cert=str(cert), temporal_tls_client_key=str(key))
    )
    assert isinstance(opts.tls, TLSConfig)
    assert opts.tls.client_cert == _CERT_PEM.encode()
    assert opts.tls.client_private_key == _KEY_PEM.encode()


def test_a_client_certificate_implies_tls_even_when_the_flag_says_off() -> None:
    """Presenting a certificate over a cleartext link is meaningless; the pair wins."""
    opts = client_options(
        _settings(
            temporal_tls=False,
            temporal_tls_client_cert=_CERT_PEM,
            temporal_tls_client_key=_KEY_PEM,
        )
    )
    assert isinstance(opts.tls, TLSConfig)


@pytest.mark.parametrize(
    "half",
    [
        pytest.param({"temporal_tls_client_cert": _CERT_PEM}, id="cert-only"),
        pytest.param({"temporal_tls_client_key": _KEY_PEM}, id="key-only"),
    ],
)
def test_half_an_mtls_pair_is_refused(half: dict[str, str]) -> None:
    with pytest.raises(ValueError, match="must be set together"):
        client_options(_settings(**half))


def test_missing_pem_file_fails_at_option_resolution(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        client_options(
            _settings(
                temporal_tls_client_cert=str(tmp_path / "nope.crt"),
                temporal_tls_client_key=str(tmp_path / "nope.key"),
            )
        )


def test_default_identity_names_component_host_and_pid() -> None:
    identity = default_identity("worker-money")
    assert identity == f"worker-money@{socket.gethostname()}:{os.getpid()}"
    assert client_options(_settings(), component="backend").identity.startswith("backend@")
    assert client_options(_settings()).identity.startswith("alkera@")


def test_explicit_identity_wins_over_the_component_default() -> None:
    assert client_options(_settings(), identity="custom", component="x").identity == "custom"


# --- connect_client --------------------------------------------------------


class _ConnectSpy:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def __call__(self, target_host: str, **kwargs: Any) -> object:
        self.calls.append((target_host, kwargs))
        return object()


@pytest.fixture
def connect_spy(monkeypatch: pytest.MonkeyPatch) -> _ConnectSpy:
    spy = _ConnectSpy()
    monkeypatch.setattr(Client, "connect", spy)
    return spy


async def test_connect_passes_every_resolved_option_and_the_pydantic_converter(
    connect_spy: _ConnectSpy,
) -> None:
    await connect_client(
        _settings(
            temporal_address="cloud.example:7233",
            temporal_namespace="ns.acct",
            temporal_api_key="k",
        ),
        identity="me",
    )
    (host, kwargs) = connect_spy.calls[0]
    assert host == "cloud.example:7233"
    assert kwargs == {
        "namespace": "ns.acct",
        "api_key": "k",
        "tls": True,
        "identity": "me",
        "data_converter": pydantic_data_converter,
    }


# --- shared_client ---------------------------------------------------------


async def test_shared_client_connects_once_and_is_reused(connect_spy: _ConnectSpy) -> None:
    first = await shared_client()
    second = await shared_client()
    assert first is second
    assert len(connect_spy.calls) == 1


async def test_concurrent_first_callers_open_one_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    async def slow_connect(*_: Any, **__: Any) -> object:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.02)
        return object()

    monkeypatch.setattr(client_mod, "connect_client", slow_connect)
    results = await asyncio.gather(*(shared_client() for _ in range(5)))
    assert calls == 1
    assert all(r is results[0] for r in results)


async def test_reset_and_close_drop_the_shared_client(connect_spy: _ConnectSpy) -> None:
    a = await shared_client()
    reset_shared_client()
    b = await shared_client()
    await close_shared_client()
    c = await shared_client()
    assert a is not b and b is not c
    assert len(connect_spy.calls) == 3


async def test_shared_client_is_not_built_at_import() -> None:
    assert client_mod._shared is None


# --- start_workflow_best_effort ---------------------------------------------


class _StartSpy:
    def __init__(self, *, fail: Exception | None = None, hang: bool = False) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._fail = fail
        self._hang = hang

    async def start_workflow(self, workflow: str, **kwargs: Any) -> object:
        self.calls.append((workflow, kwargs))
        if self._fail is not None:
            raise self._fail
        if self._hang:
            await asyncio.sleep(60)
        return object()


async def test_a_drain_nudge_is_a_signal_with_start(log_rec: _Recorder) -> None:
    spy = _StartSpy()
    ok = await start_workflow_best_effort(
        spy,  # type: ignore[arg-type]
        "billing.process_stripe_events",
        id="billing.process_stripe_events",
        task_queue="money",
        signal="more_work",
        fail_log_event="billing.webhook.nudge_failed",
    )
    assert ok is True
    (workflow, kwargs) = spy.calls[0]
    assert workflow == "billing.process_stripe_events"
    assert kwargs == {
        "id": "billing.process_stripe_events",
        "task_queue": "money",
        "start_signal": "more_work",
        "rpc_timeout": timedelta(seconds=2.0),
    }
    assert "id_conflict_policy" not in kwargs
    assert log_rec.calls == []


async def test_a_keyed_nudge_attaches_to_the_existing_run(log_rec: _Recorder) -> None:
    spy = _StartSpy()
    ok = await start_workflow_best_effort(
        spy,  # type: ignore[arg-type]
        "github.render_gate_check",
        id="github.render_gate_check:abc",
        task_queue="sync",
        timeout_s=0.5,
        fail_log_event="gate.check_render.nudge_failed",
    )
    assert ok is True
    (_, kwargs) = spy.calls[0]
    assert kwargs["id_conflict_policy"] is WorkflowIDConflictPolicy.USE_EXISTING
    assert kwargs["rpc_timeout"] == timedelta(seconds=0.5)
    assert "start_signal" not in kwargs
    assert "args" not in kwargs, "no arguments given: none are sent"


@pytest.mark.parametrize("shape", ["keyed", "drain"])
async def test_workflow_arguments_travel_with_either_shape(shape: str, log_rec: _Recorder) -> None:
    """Per-entity work takes its entity id as the workflow's argument; the
    arguments ride along with whichever start shape the nudge uses."""
    spy = _StartSpy()
    ok = await start_workflow_best_effort(
        spy,  # type: ignore[arg-type]
        "connections.probe_team_connection",
        id="connections.probe_team_connection:abc",
        task_queue="sync",
        args=("abc",),
        signal="more_work" if shape == "drain" else None,
        fail_log_event="team_connection.nudge_failed",
    )
    assert ok is True
    (_, kwargs) = spy.calls[0]
    assert kwargs["args"] == ["abc"]
    assert ("start_signal" in kwargs) is (shape == "drain")
    assert log_rec.calls == []


async def test_a_failing_orchestrator_is_logged_and_swallowed(log_rec: _Recorder) -> None:
    spy = _StartSpy(fail=RuntimeError("frontend down"))
    ok = await start_workflow_best_effort(
        spy,  # type: ignore[arg-type]
        "billing.process_stripe_events",
        id="billing.process_stripe_events",
        task_queue="money",
        signal="more_work",
        fail_log_event="billing.webhook.nudge_failed",
    )
    assert ok is False
    assert len(log_rec.calls) == 1
    event, fields = log_rec.calls[0]
    assert event == "billing.webhook.nudge_failed"
    assert fields["error"] == "frontend down"
    assert fields["workflow_id"] == "billing.process_stripe_events"
    assert fields["task_queue"] == "money"


async def test_an_exception_without_a_message_is_still_named(log_rec: _Recorder) -> None:
    spy = _StartSpy(fail=ConnectionError())
    await start_workflow_best_effort(
        spy,  # type: ignore[arg-type]
        "x",
        id="x",
        task_queue="default",
        fail_log_event="x.nudge_failed",
    )
    assert log_rec.calls[0][1]["error"] == "ConnectionError"


async def test_a_hanging_orchestrator_returns_within_the_budget(log_rec: _Recorder) -> None:
    spy = _StartSpy(hang=True)
    started = time.monotonic()
    ok = await start_workflow_best_effort(
        spy,  # type: ignore[arg-type]
        "x",
        id="x",
        task_queue="default",
        timeout_s=0.05,
        fail_log_event="x.nudge_failed",
    )
    elapsed = time.monotonic() - started
    assert ok is False
    assert elapsed < 1.0
    assert log_rec.calls[0][0] == "x.nudge_failed"


async def test_cancellation_is_not_swallowed() -> None:
    """A cancelled request must still cancel; only failures are best-effort."""
    spy = _StartSpy(hang=True)
    task = asyncio.ensure_future(
        start_workflow_best_effort(
            spy,  # type: ignore[arg-type]
            "x",
            id="x",
            task_queue="default",
            timeout_s=5,
            fail_log_event="x.nudge_failed",
        )
    )
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
