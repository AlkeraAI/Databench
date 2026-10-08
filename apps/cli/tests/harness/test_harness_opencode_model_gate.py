"""The opencode adapter runs a turn only on a model the injected config routes
through the Alkera gateway — never on a default of its own.

The injected config (``ALKERA_CONFIG_CONTENT``) used to pin opencode's free
hosted model so a credential-less sandbox "could still run a turn". That is the
hole this file closes: a chat that reaches the adapter with no gateway model
(a row created with none, a preference that never resolved, a resume of an
unpinned chat) silently ran on a model nobody metered, billed, audited or
chose. Now the config carries no model at all, and a turn whose model is
missing or names a provider the config does not declare is refused before any
request reaches the subprocess — with the refusal published as an error status
so every chat surface renders it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from alkera_cli.harness.adapter import HarnessModelError, PromptInput, SessionConfig
from alkera_cli.harness.adapters.opencode_http import (
    _OPENCODE_DEFAULT_CONFIG,
    OpencodeHttpAdapter,
)
from alkera_cli.harness.event_bus import EventBus
from alkera_cli.harness.opencode_binary import ResolvedOpencodeBinary
from alkera_core.schemas.chat import SessionStatusChanged

#: A provider the way the gateway builder (and the e2e runner) declares one.
MOCK_PROVIDER: dict[str, Any] = {
    "mock": {
        "npm": "@ai-sdk/openai-compatible",
        "options": {"baseURL": "http://127.0.0.1:1/v1", "apiKey": "t"},
        "models": {"mock-model": {"name": "mock"}},
    }
}

#: Built-in opencode provider ids that must never be admitted by name alone.
BUILT_INS = ("opencode", "anthropic", "openai", "zenmux", "google", "openrouter")


class _RecordingClient:
    """The only thing the adapter may do with the subprocess is POST to it;
    this records whether it did."""

    def __init__(self) -> None:
        self.posts: list[tuple[str, dict[str, Any]]] = []
        self.timeouts: list[Any] = []

    async def post(self, path: str, json: dict[str, Any] | None = None, **kwargs: Any) -> _Resp:
        self.posts.append((path, json or {}))
        self.timeouts.append(kwargs.get("timeout"))
        return _Resp()


class _Resp:
    def raise_for_status(self) -> None:
        return None


def _adapter(
    tmp_path: Path,
    *,
    model: dict[str, str] | None = None,
    agent_config: dict[str, Any] | None = None,
) -> tuple[OpencodeHttpAdapter, _RecordingClient]:
    native: dict[str, Any] = {}
    if agent_config is not None:
        native["agent_config"] = agent_config
    config = SessionConfig(
        session_id="our-sid",
        project_dir=tmp_path,
        chat_dir=tmp_path / "chat",
        model=model,
        harness_native=native,
    )
    binary = ResolvedOpencodeBinary(path=Path("/usr/bin/true"), prefix_args=(), source="staged")
    adapter = OpencodeHttpAdapter(config, binary=binary, event_bus=EventBus())
    adapter._harness_dir.mkdir(parents=True, exist_ok=True)
    client = _RecordingClient()
    adapter._state.started = True
    adapter._state.opencode_session_id = "oc-1"
    adapter._state.http_client = client  # type: ignore[assignment]
    return adapter, client


def _injected(adapter: OpencodeHttpAdapter) -> dict[str, Any]:
    return json.loads(adapter._build_env("pw").secrets["ALKERA_CONFIG_CONTENT"])


async def _refusal(adapter: OpencodeHttpAdapter, prompt: PromptInput) -> tuple[str, list[Any]]:
    """Send ``prompt`` expecting the refusal; returns its message and every event
    the bus carried, so a test can pin both the raise and the rendering."""
    sub = adapter.subscribe()
    with pytest.raises(HarnessModelError) as excinfo:
        await adapter.send_prompt(prompt)
    events: list[Any] = []
    async for ev in sub:
        events.append(ev)
        break
    return str(excinfo.value), events


# ---------------------------------------------------------------------------
# The injected config carries no model of its own
# ---------------------------------------------------------------------------


def test_the_default_config_names_no_model_and_no_provider(tmp_path: Path) -> None:
    """The whole hole: a config that pins a hosted model lets a chat with no
    gateway model run anyway. The default must carry neither a model nor a
    provider, so an unpinned chat has nothing to fall back on."""
    assert "model" not in _OPENCODE_DEFAULT_CONFIG
    assert "provider" not in _OPENCODE_DEFAULT_CONFIG
    adapter, _ = _adapter(tmp_path)
    config = _injected(adapter)
    assert "model" not in config
    assert "provider" not in config


@pytest.mark.parametrize("built_in", BUILT_INS)
def test_the_default_config_never_names_a_built_in_provider(tmp_path: Path, built_in: str) -> None:
    adapter, _ = _adapter(tmp_path)
    serialized = json.dumps(_injected(adapter))
    assert f'"{built_in}/' not in serialized
    assert f'"{built_in}"' not in serialized


# ---------------------------------------------------------------------------
# A turn with no admissible model is refused before the subprocess hears of it
# ---------------------------------------------------------------------------


async def test_no_model_anywhere_is_refused_before_any_prompt_is_posted(tmp_path: Path) -> None:
    adapter, client = _adapter(tmp_path)

    message, events = await _refusal(adapter, PromptInput(text="hi", turn_id="A"))

    assert client.posts == [], "the refusal must happen before the subprocess sees a prompt"
    assert "no model configured" in message
    # …and the reader sees it: the same words ride an error status on the bus,
    # stamped with the attempt so the surface settles that turn, not another.
    assert len(events) == 1
    status = events[0]
    assert isinstance(status, SessionStatusChanged)
    assert status.status == "error"
    assert status.turn_id == "A"
    assert status.detail == message


async def test_a_session_model_alone_does_not_admit_the_turn(tmp_path: Path) -> None:
    """``SessionConfig.model`` is what the manifest pinned, but opencode runs
    what its CONFIG names; a pinned model with no config behind it (the gateway
    builder was not consulted, or declined) is still a turn with no route."""
    adapter, client = _adapter(
        tmp_path, model={"provider_id": "alkera-anthropic", "model_id": "claude-x"}
    )

    message, _ = await _refusal(adapter, PromptInput(text="hi"))

    assert client.posts == []
    assert "no model configured" in message


@pytest.mark.parametrize(
    ("agent_config", "prompt", "named"),
    [
        pytest.param(
            {"provider": MOCK_PROVIDER, "model": "opencode/big-pickle"},
            PromptInput(text="hi"),
            "opencode/big-pickle",
            id="the config default names opencode's hosted model",
        ),
        pytest.param(
            {"provider": MOCK_PROVIDER, "model": "mock/mock-model"},
            PromptInput(text="hi", model={"provider_id": "anthropic", "model_id": "claude-x"}),
            "anthropic/claude-x",
            id="an explicit per-prompt model names a built-in provider",
        ),
        pytest.param(
            {"provider": MOCK_PROVIDER, "model": "mock/mock-model"},
            PromptInput(text="hi", model={"provider_id": "zenmux", "model_id": "m"}),
            "zenmux/m",
            id="an explicit per-prompt model names an undeclared custom provider",
        ),
        pytest.param(
            {"model": "mock/mock-model"},
            PromptInput(text="hi"),
            "mock/mock-model",
            id="a model whose provider the config never declares",
        ),
    ],
)
async def test_a_model_on_an_undeclared_provider_is_refused(
    tmp_path: Path, agent_config: dict[str, Any], prompt: PromptInput, named: str
) -> None:
    adapter, client = _adapter(tmp_path, agent_config=agent_config)

    message, events = await _refusal(adapter, prompt)

    assert client.posts == []
    assert named in message
    assert "gateway" in message
    assert [ev.status for ev in events if isinstance(ev, SessionStatusChanged)] == ["error"]


async def test_a_variant_over_a_session_model_on_an_undeclared_provider_is_refused(
    tmp_path: Path,
) -> None:
    """The effort path composes ``<model>::<variant>`` from the session's pinned
    model without consulting the config default — it is gated all the same."""
    adapter, client = _adapter(
        tmp_path,
        model={"provider_id": "opencode", "model_id": "big-pickle"},
        agent_config={"provider": MOCK_PROVIDER, "model": "mock/mock-model"},
    )

    message, _ = await _refusal(adapter, PromptInput(text="hi", variant="high"))

    assert client.posts == []
    assert "opencode/big-pickle::high" in message


async def test_a_variant_with_nothing_to_compose_from_is_refused_not_guessed(
    tmp_path: Path,
) -> None:
    adapter, client = _adapter(tmp_path)

    message, _ = await _refusal(adapter, PromptInput(text="hi", variant="high"))

    assert client.posts == []
    assert "no model configured" in message


# ---------------------------------------------------------------------------
# A declared provider's model is admitted — the gate refuses, it does not block
# ---------------------------------------------------------------------------


async def test_a_model_on_a_declared_provider_runs_the_turn(tmp_path: Path) -> None:
    adapter, client = _adapter(
        tmp_path, agent_config={"provider": MOCK_PROVIDER, "model": "mock/mock-model"}
    )

    await adapter.send_prompt(PromptInput(text="hi", turn_id="A"))

    assert [path for path, _ in client.posts] == ["/session/oc-1/prompt_async"]
    # The config default is what opencode runs — the body names nothing else.
    assert "providerID" not in client.posts[0][1]


async def test_an_explicit_model_on_a_declared_provider_is_forwarded(tmp_path: Path) -> None:
    adapter, client = _adapter(
        tmp_path, agent_config={"provider": MOCK_PROVIDER, "model": "mock/mock-model"}
    )

    await adapter.send_prompt(
        PromptInput(text="hi", model={"provider_id": "mock", "model_id": "mock-model"})
    )

    body = client.posts[0][1]
    # Nested, the shape opencode's prompt schema decodes; top-level ids are
    # dropped by it without an error.
    assert body["model"] == {"providerID": "mock", "modelID": "mock-model"}
    assert "providerID" not in body and "modelID" not in body


async def test_a_variant_over_a_declared_session_model_is_forwarded(tmp_path: Path) -> None:
    adapter, client = _adapter(
        tmp_path,
        model={"provider_id": "mock", "model_id": "mock-model"},
        agent_config={"provider": MOCK_PROVIDER, "model": "mock/mock-model"},
    )

    await adapter.send_prompt(PromptInput(text="hi", variant="high"))

    body = client.posts[0][1]
    assert body["model"] == {"providerID": "mock", "modelID": "mock-model::high"}


@pytest.mark.parametrize(
    ("sent", "expected"),
    [
        pytest.param(
            {"provider_id": "mock", "model_id": "other-model"},
            "other-model::low",
            id="the-turns-own-model",
        ),
        pytest.param(
            {"provider_id": "mock", "model_id": "other-model::high"},
            "other-model::low",
            id="the-turns-own-model-with-its-old-effort-replaced",
        ),
        pytest.param(None, "mock-model::low", id="no-model-means-the-session-model"),
    ],
)
async def test_a_variant_lands_on_the_turns_model_not_the_spawn_model(
    tmp_path: Path, sent: dict[str, str] | None, expected: str
) -> None:
    adapter, client = _adapter(
        tmp_path,
        model={"provider_id": "mock", "model_id": "mock-model"},
        agent_config={"provider": MOCK_PROVIDER, "model": "mock/mock-model"},
    )

    await adapter.send_prompt(PromptInput(text="hi", model=sent, variant="low"))

    assert client.posts[0][1]["model"] == {"providerID": "mock", "modelID": expected}


async def test_a_variant_only_turn_stays_on_the_model_the_last_turn_ran(tmp_path: Path) -> None:
    """After a turn moved the session onto another model, a turn naming only an
    effort stays there, as opencode itself would, rather than snapping back to
    the model the agent was spawned on."""
    adapter, client = _adapter(
        tmp_path,
        model={"provider_id": "mock", "model_id": "mock-model"},
        agent_config={"provider": MOCK_PROVIDER, "model": "mock/mock-model"},
    )

    await adapter.send_prompt(
        PromptInput(
            text="a", model={"provider_id": "mock", "model_id": "other-model"}, variant="high"
        )
    )
    await adapter.send_prompt(PromptInput(text="b", variant="low"))

    assert client.posts[1][1]["model"] == {"providerID": "mock", "modelID": "other-model::low"}


@pytest.mark.parametrize(
    ("model", "served"),
    [
        pytest.param({"provider_id": "mock", "model_id": "mock-model"}, True, id="declared"),
        pytest.param(
            {"provider_id": "mock", "model_id": "mock-model::high"}, True, id="declared-effort"
        ),
        pytest.param(
            {"provider_id": "mock", "model_id": "mock-model::max"}, False, id="undeclared-effort"
        ),
        pytest.param({"provider_id": "mock", "model_id": "other"}, False, id="undeclared-model"),
        pytest.param(
            {"provider_id": "alkera-openai", "model_id": "mock-model"},
            False,
            id="undeclared-provider",
        ),
    ],
)
def test_serves_model_reads_the_spawn_time_config(
    tmp_path: Path, model: dict[str, str], served: bool
) -> None:
    """A model the agent was not configured with is one it would refuse inside
    the agent ("Model not found"); the host respawns it instead."""
    provider = {
        "mock": {
            **MOCK_PROVIDER["mock"],
            "models": {"mock-model": {"name": "m"}, "mock-model::high": {"name": "m (high)"}},
        }
    }
    adapter, _client = _adapter(
        tmp_path, agent_config={"provider": provider, "model": "mock/mock-model"}
    )
    assert adapter.serves_model(model) is served


# ---------------------------------------------------------------------------
# The other request that names a model: a manual compaction
# ---------------------------------------------------------------------------


async def test_summarize_with_no_model_is_refused_not_defaulted(tmp_path: Path) -> None:
    adapter, client = _adapter(tmp_path)

    with pytest.raises(HarnessModelError):
        await adapter.compact()

    assert client.posts == []


async def test_summarize_reuses_the_configured_gateway_model(tmp_path: Path) -> None:
    adapter, client = _adapter(
        tmp_path, agent_config={"provider": MOCK_PROVIDER, "model": "mock/mock-model"}
    )

    await adapter.compact()

    path, body = client.posts[0]
    assert path == "/session/oc-1/summarize"
    assert (body["providerID"], body["modelID"]) == ("mock", "mock-model")
