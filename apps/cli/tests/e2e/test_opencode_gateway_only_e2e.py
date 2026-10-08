"""E2E proof, against a REAL opencode subprocess, that a model only ever comes from
the gateway config the harness injects — never from opencode's own registry.

opencode ships an ``opencode`` provider that is "connected" with no credential at
all (its free hosted models), plus every other built-in that an auth file or an
env key on the machine would light up. A turn that named one of them ran outside
the Alkera gateway: no credential, no metering, no billing, no audit, on a model
the reader never chose. Two gates close it, and this file drives both through the
real process:

- the **vendored registry guard** (``provider.ts``): under the harness only the
  providers the injected config declares exist, and resolving a model on any
  other provider is refused with a clear error rather than met with a default;
- the **adapter's turn gate** (``opencode_http.py``): a turn whose model the
  injected config does not route is refused before a request is posted at all.

The first is exercised by posting straight at opencode's prompt endpoint —
around the adapter's gate — so the guard is proven on its own.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import pytest
from _helpers.opencode_runner import opencode_e2e_adapter
from _mocks.mock_openai_server import text_chunks
from alkera_cli.harness.adapter import HarnessModelError, PromptInput
from alkera_cli.harness.adapters.opencode_http import OpencodeHttpAdapter
from alkera_core.schemas.chat import SessionStatusChanged

pytestmark = [
    pytest.mark.opencode_e2e,
    pytest.mark.skipif(os.name != "posix", reason="parent-hosted alkera loopback is POSIX-only"),
]

#: The words the vendored guard refuses with (provider.ts `notAdmittedMessage`).
GUARD_REFUSAL = "is not served through the Alkera gateway"


def _seed_builtin_catalog(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Give the subprocess the catalog a shipped binary carries: opencode's own
    hosted provider with a free model in it.

    A bun-dev checkout has no compiled-in models.dev snapshot, so without this
    the built-in would be absent for the wrong reason (no catalog) rather than
    the right one (the guard). With it, opencode's provider loader would connect
    ``opencode`` with no credential at all — exactly what a chat with no gateway
    model used to run on — unless the registry guard disables it first. The
    catalog file is read through opencode's own override, which the spawned
    process inherits from this one.
    """
    model = {
        "id": "big-pickle",
        "name": "Big Pickle",
        "release_date": "2025-01-01",
        "attachment": False,
        "reasoning": False,
        "temperature": True,
        "tool_call": True,
        "cost": {"input": 0, "output": 0},
        "limit": {"context": 128000, "output": 8000},
        "modalities": {"input": ["text"], "output": ["text"]},
    }
    catalog = {
        "opencode": {
            "id": "opencode",
            "name": "OpenCode Zen",
            "env": [],
            "npm": "@ai-sdk/openai-compatible",
            "api": "http://127.0.0.1:9/v1",
            "models": {"big-pickle": model},
        }
    }
    path = tmp_path / "models.json"
    path.write_text(json.dumps(catalog))
    monkeypatch.setenv("OPENCODE_MODELS_PATH", str(path))


async def _first_terminal_status(
    adapter: OpencodeHttpAdapter, sub: Any, *, within: float = 60.0
) -> SessionStatusChanged:
    async def _wait() -> SessionStatusChanged:
        async for ev in sub:
            if isinstance(ev, SessionStatusChanged) and ev.status in ("error", "idle", "completed"):
                return ev
        raise AssertionError("the bus closed before the turn settled")

    return await asyncio.wait_for(_wait(), timeout=within)


async def test_the_registry_admits_only_the_injected_configs_providers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The guard's first half: with the harness config injected, opencode's own
    provider list holds the declared mock provider and nothing built in — not
    even the hosted ``opencode`` provider the seeded catalog would otherwise
    connect with no credential, and nothing an auth file or env key on the
    machine running this suite could light up."""
    _seed_builtin_catalog(tmp_path, monkeypatch)
    script = {"*": text_chunks("unused")}
    async with opencode_e2e_adapter(tmp_path, monkeypatch, mock_script=script) as (
        adapter,
        _server,
    ):
        client = adapter._state.http_client
        assert client is not None
        listed = (await client.get("/provider")).json()

    connected = set(listed["connected"])
    catalogued = {p["id"] for p in listed["all"]}
    # The seed took: the hosted provider IS in the catalog the process reads
    # (`all` is the catalog plus whatever is connected), so its absence from
    # the connected set below is the guard's doing and nothing else's.
    assert "opencode" in catalogued, catalogued
    assert connected == {"mock"}, connected


async def test_a_prompt_naming_a_built_in_provider_is_refused_inside_opencode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The guard's second half, driven around the adapter's own gate: a prompt
    posted straight at opencode naming ``opencode/big-pickle`` — the exact model
    an unpinned chat used to run on, present in the seeded catalog — ends the
    turn with the guard's refusal, and the mock provider (the only one declared)
    hears nothing about it."""
    _seed_builtin_catalog(tmp_path, monkeypatch)
    script = {"*": text_chunks("must never be asked")}
    async with opencode_e2e_adapter(
        tmp_path,
        monkeypatch,
        mock_script=script,
        extra_config={"model": "opencode/big-pickle"},
    ) as (adapter, server):
        client = adapter._state.http_client
        sid = adapter._state.opencode_session_id
        assert client is not None and sid is not None
        sub = adapter.subscribe()
        resp = await client.post(
            f"/session/{sid}/prompt_async",
            json={
                "parts": [{"type": "text", "text": "hi what model are you on?"}],
                "providerID": "opencode",
                "modelID": "big-pickle",
            },
        )
        resp.raise_for_status()
        terminal = await _first_terminal_status(adapter, sub)

    assert terminal.status == "error", f"the turn ran instead of being refused: {terminal!r}"
    assert GUARD_REFUSAL in str(terminal.detail), terminal.detail
    assert "opencode/big-pickle" in str(terminal.detail), terminal.detail
    assert server.requests == [], "the declared provider was asked to run a refused turn"


async def test_the_adapter_refuses_a_config_default_off_the_gateway_before_posting(
    tmp_path: Any,
) -> None:
    """The adapter's gate through the real process: a config whose default model
    names opencode's hosted provider is refused by the adapter itself — the
    reader sees the refusal as an error status, no prompt is posted, and the
    mock provider is never reached."""
    script = {"*": text_chunks("must never be asked")}
    async with opencode_e2e_adapter(
        tmp_path,
        pytest.MonkeyPatch(),
        mock_script=script,
        extra_config={"model": "opencode/big-pickle"},
    ) as (adapter, server):
        sub = adapter.subscribe()
        with pytest.raises(HarnessModelError) as excinfo:
            await adapter.send_prompt(PromptInput(text="hi", turn_id="A"))
        rendered = await _first_terminal_status(adapter, sub, within=5.0)

    assert "opencode/big-pickle" in str(excinfo.value)
    assert rendered.status == "error" and rendered.turn_id == "A"
    assert rendered.detail == str(excinfo.value)
    assert server.requests == []
