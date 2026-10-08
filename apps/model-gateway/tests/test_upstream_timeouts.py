"""The gateway's per-step bounds come from settings, not from literals.

None of these bounds a chat turn — a turn is many model steps. They bound ONE
provider request, and they exist only because the credit-reservation sweeper
needs a bound. So the defaults are generous and every one of them is tunable
without a code change; these tests hold both halves of that.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, ClassVar

import pytest
from alkera_core.config import Settings, settings
from model_gateway.adapters import aioboto3_bedrock_client_factory
from model_gateway.app_factory import create_app


@pytest.mark.parametrize(
    ("field", "floor"),
    [
        pytest.param("gateway_upstream_read_timeout_seconds", 7200, id="upstream-silence"),
        pytest.param("gateway_bedrock_read_timeout_seconds", 86400, id="bedrock-silence"),
        pytest.param("gateway_max_stream_seconds", 86400, id="one-stream"),
    ],
)
def test_shipped_defaults_are_generous(field: str, floor: int) -> None:
    """A step that thinks for a long time must not be cut by the shipped default —
    one step is entitled to run for a day.

    Asserted on the model field rather than the live settings object so a local
    `.env` cannot make this pass while the shipped default is small."""
    assert float(Settings.model_fields[field].default) >= floor


@pytest.mark.asyncio
async def test_upstream_read_timeout_comes_from_the_setting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "gateway_upstream_read_timeout_seconds", 1234.0)
    app = create_app()
    try:
        assert app.state.http_client.timeout.read == 1234.0
        # The other three legs are unrelated to how long a model may think.
        assert app.state.http_client.timeout.connect == 10.0
    finally:
        await app.state.http_client.aclose()


class _RecordingSession:
    """Stands in for `aioboto3.Session`, capturing the client kwargs."""

    captured: ClassVar[dict[str, Any]] = {}

    def __init__(self, **_kwargs: Any) -> None:
        pass

    def client(self, service: str, **kwargs: Any) -> Any:
        _RecordingSession.captured = {"service": service, **kwargs}

        @asynccontextmanager
        async def _cm() -> Any:
            yield object()

        return _cm()


@pytest.fixture
def recording_boto(monkeypatch: pytest.MonkeyPatch) -> type[_RecordingSession]:
    import aioboto3

    _RecordingSession.captured = {}
    monkeypatch.setattr(aioboto3, "Session", _RecordingSession)
    return _RecordingSession


def test_bedrock_read_timeout_comes_from_the_setting(
    monkeypatch: pytest.MonkeyPatch, recording_boto: type[_RecordingSession]
) -> None:
    monkeypatch.setattr(settings, "gateway_bedrock_read_timeout_seconds", 4321)
    factory = aioboto3_bedrock_client_factory(region="us-east-1")

    factory()

    assert recording_boto.captured["service"] == "bedrock-runtime"
    assert recording_boto.captured["config"].read_timeout == 4321


def test_bedrock_reads_the_setting_at_call_time_not_at_import(
    monkeypatch: pytest.MonkeyPatch, recording_boto: type[_RecordingSession]
) -> None:
    """A factory built before the setting is raised still builds a client with the
    raised value — otherwise the default would be frozen at import."""
    factory = aioboto3_bedrock_client_factory(region="us-east-1")
    monkeypatch.setattr(settings, "gateway_bedrock_read_timeout_seconds", 999)

    factory()

    assert recording_boto.captured["config"].read_timeout == 999


def test_an_explicit_bedrock_read_timeout_wins_over_the_setting(
    monkeypatch: pytest.MonkeyPatch, recording_boto: type[_RecordingSession]
) -> None:
    monkeypatch.setattr(settings, "gateway_bedrock_read_timeout_seconds", 4321)
    factory = aioboto3_bedrock_client_factory(region="us-east-1", read_timeout=7)

    factory()

    assert recording_boto.captured["config"].read_timeout == 7
