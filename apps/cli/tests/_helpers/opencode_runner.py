"""bun-driven opencode test runner.

E2 of the harness stabilization sweep. Lets a test spawn a REAL
opencode subprocess from source (no Nuitka build needed) and point it
at the mock OpenAI server so end-to-end runs are deterministic.

Usage::

    @pytest.mark.opencode_e2e
    async def test_something(tmp_path, monkeypatch):
        async with opencode_e2e_adapter(
            tmp_path,
            monkeypatch,
            mock_script={"say hi": text_chunks("Hi!")},
        ) as (adapter, mock_server):
            await adapter.send_prompt(PromptInput(text="say hi"))
            ...

The helper auto-skips the test if ``bun`` is not on ``PATH`` (local
dev without bun installed) or if ``vendor/opencode/packages/opencode/
src/index.ts`` is missing.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from _mocks.mock_openai_server import MockOpenAIServer, MockScript
from alkera_cli.harness.adapter import SessionConfig
from alkera_cli.harness.adapters.opencode_http import OpencodeHttpAdapter
from alkera_cli.harness.event_bus import EventBus
from alkera_cli.harness.opencode_binary import ResolvedOpencodeBinary, resolve_ripgrep_bin

if TYPE_CHECKING:
    pass


def _repo_root() -> Path:
    """``apps/cli/tests/_helpers/`` → repo root."""
    return Path(__file__).resolve().parents[4]


def _check_bun_and_vendor() -> tuple[Path, Path]:
    """Skip the test if bun or vendor/opencode is missing. Otherwise
    return ``(bun_path, opencode_src_index_ts_path)``."""
    bun_path = shutil.which("bun")
    if bun_path is None:
        pytest.skip("bun not installed — required for opencode e2e tests")
    oc_src = _repo_root() / "vendor" / "opencode" / "packages" / "opencode" / "src" / "index.ts"
    if not oc_src.is_file():
        pytest.skip(
            "vendor/opencode source missing — it's a git subtree committed "
            "in-tree; re-checkout the repo (or run `make opencode-bump`)"
        )
    return Path(bun_path), oc_src


def _bun_binary(ripgrep_path: Path | None = None) -> ResolvedOpencodeBinary:
    """The agent binary the e2e suite spawns, carrying the `rg` it must use.

    With no `rg` named, the one the product resolver would ship is used (the
    staged rg beside the binary, an `ALKERA_RIPGREP_BIN`, or one on PATH). A
    binary carrying none makes the agent download rg from github.com inside the
    first glob or grep, into a cache the per-test agent root deletes, so every
    such test fetched it again and a slow or refused download failed the tool."""
    if ripgrep_path is None:
        ripgrep_path = resolve_ripgrep_bin()
    # == ALKERA EDIT — when ALKERA_OPENCODE_BIN points at an executable, run THAT
    # (compiled-mode, no prefix_args) instead of bun-dev source. Lets the
    # opencode_e2e suite exercise the actual staged/obfuscated `alkera-agent`
    # binary — obfuscation lives only in the compiled output and is invisible to
    # bun-dev source runs. Falls back to bun-dev when the env var is unset.
    env_bin = os.environ.get("ALKERA_OPENCODE_BIN", "").strip()
    if env_bin:
        p = Path(env_bin).expanduser()
        if p.is_file() and os.access(p, os.X_OK):
            return ResolvedOpencodeBinary(
                path=p.absolute(),
                prefix_args=(),
                source="env",
                ripgrep_path=ripgrep_path,
            )
    bun_path, oc_src = _check_bun_and_vendor()
    return ResolvedOpencodeBinary(
        path=bun_path,
        prefix_args=("run", str(oc_src.absolute())),
        source="bun-dev",
        ripgrep_path=ripgrep_path,
    )


#: The reasoning efforts the mock model declares, as the gateway catalog would.
MOCK_EFFORTS = ("low", "medium", "high", "none")


def _build_oc_config(
    base_url: str,
    *,
    extra_config: dict[str, Any] | None = None,
    model_limit: dict[str, int] | None = None,
) -> dict[str, Any]:
    """The opencode config pointing at the mock provider.

    This is the DIRECT (no-gateway) harness path: it forces
    `@ai-sdk/openai-compatible` (chat-completions) so opencode hits
    `POST /v1/chat/completions`. It exercises the harness's IR translation +
    opencode-internal events (compaction, clear, permissions) against the
    cheapest possible wire — independent of which upstream API the product uses.
    (The gateway-proxied e2e — `gateway_stack` — is the one that drives the real
    product's `@ai-sdk/openai` Responses wire; the mock serves BOTH endpoints.)

    Crucially, the provider id is ``mock``, NOT ``openai``. opencode keeps a
    hardcoded ``custom`` loader keyed by built-in provider id (see
    `vendor/opencode/.../provider/provider.ts`): the ``openai`` entry forces
    ``sdk.responses(modelID)`` — the OpenAI *Responses* API
    (`POST /v1/responses`) — REGARDLESS of the ``npm`` override. With provider
    id ``openai``, opencode would therefore hit `/v1/responses` even though we
    asked for the compatible SDK. A non-built-in id like ``mock`` has no custom
    loader, so opencode falls through to ``sdk.languageModel(id)`` →
    chat-completions for the ``-compatible`` package. (This is the SAME
    fall-through mechanism the real `alkera-openai` provider relies on — except
    there the package is `@ai-sdk/openai`, whose `languageModel` IS the Responses
    API.) (`models` is required or opencode reports "no provider has this model"
    and never calls the LLM; `model_limit` sets the context window so a small
    limit + mock-reported usage can trigger auto-compaction.)"""
    model_entry: dict[str, Any] = {"name": "Mock Model"}
    if model_limit is not None:
        model_entry["limit"] = model_limit
    oc_config: dict[str, Any] = {
        "provider": {
            "mock": {
                "npm": "@ai-sdk/openai-compatible",
                "options": {
                    "baseURL": base_url,
                    "apiKey": "test-key-not-used",
                    "name": "mock",
                },
                # One entry per effort as well, the shape the gateway config
                # gives every model (`<id>::<effort>`): a turn that carries an
                # effort names one of these, and opencode refuses a model key
                # its config does not declare.
                "models": {
                    "mock-model": model_entry,
                    **{f"mock-model::{e}": dict(model_entry) for e in MOCK_EFFORTS},
                },
            }
        },
        "model": "mock/mock-model",
    }
    if extra_config:
        oc_config.update(extra_config)
    return oc_config


@asynccontextmanager
async def opencode_e2e_adapter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    mock_script: MockScript,
    session_id: str = "e2e-sid",
    chat_dir: Path | None = None,
    extra_config: dict[str, Any] | None = None,
    harness_native: dict[str, Any] | None = None,
    compaction_summary: str | None = None,
    error_on_marker: str | None = None,
    model_limit: dict[str, int] | None = None,
    ripgrep_path: Path | None = None,
) -> AsyncIterator[tuple[OpencodeHttpAdapter, MockOpenAIServer]]:
    """Spin up the mock OpenAI server + a bun-launched opencode
    subprocess wired to it, then yield a started OpencodeHttpAdapter.

    The opencode config (mock provider + default model + any
    `extra_config`, e.g. compaction tuning) is injected via
    `SessionConfig.harness_native["agent_config"]` — the adapter
    merges it into `OPENCODE_CONFIG_CONTENT` (the env var itself is
    scrubbed by the adapter's isolation, so we MUST use this seam).

    `chat_dir` + `harness_native` (e.g. `{"agent_session_id": ...}`)
    let a test re-open the SAME chat to exercise resume."""
    _check_bun_and_vendor()  # early skip if bun / vendor source is missing

    # 1. Start the mock OpenAI server first so we can wire its base_url
    # into the opencode provider config.
    server = MockOpenAIServer(
        mock_script,
        compaction_summary=compaction_summary,
        error_on_marker=error_on_marker,
    )
    await server.start()
    try:
        oc_config = _build_oc_config(
            server.base_url, extra_config=extra_config, model_limit=model_limit
        )
        native: dict[str, Any] = {"agent_config": oc_config}
        if harness_native:
            native.update(harness_native)

        # Build the adapter with a bun-dev binary so we run from source
        # (no Nuitka build required for e2e). `ripgrep_path`, when set, makes
        # the adapter put that rg first on PATH + disable the rg download —
        # the bundled-ripgrep path under test.
        binary = _bun_binary(ripgrep_path=ripgrep_path)
        resolved_chat_dir = chat_dir or (tmp_path / "chat")
        resolved_chat_dir.mkdir(parents=True, exist_ok=True)
        config = SessionConfig(
            session_id=session_id,
            project_dir=tmp_path,
            chat_dir=resolved_chat_dir,
            harness_native=native,
        )
        adapter = OpencodeHttpAdapter(config, binary=binary, event_bus=EventBus())
        try:
            await adapter.start()
            yield adapter, server
        finally:
            await adapter.stop()
    finally:
        await server.stop()


@asynccontextmanager
async def opencode_e2e_runtime(
    tmp_path: Path,
    *,
    mock_script: MockScript,
    extra_config: dict[str, Any] | None = None,
    compaction_summary: str | None = None,
    error_on_marker: str | None = None,
    error_status: int = 400,
    model_limit: dict[str, int] | None = None,
    safety_judge: Any = None,
    web_search_enabled: bool = False,
    subagents_enabled: bool = True,
    notebook_host_factory: Any = None,
    permission_mode: str | None = None,
) -> AsyncIterator[tuple[Any, str, MockOpenAIServer]]:
    """Full-runtime e2e harness: a real `HarnessRuntime` (bun-dev binary)
    backing a chat whose manifest carries the mock `agent_config`.

    Unlike `opencode_e2e_adapter` (which drives the adapter directly), this
    goes through the WHOLE alkera path — `runtime.open_chat` →
    `ChatSession` → the persist pump (→ chat.jsonl) → manifest native-state
    pinning. Yields `(runtime, session_id, server)`; the caller drives
    `open_chat`/`close_chat` (so it can exercise resume). The mock server
    stays up across open/close so its base_url is stable for resume.

    `web_search_enabled` mirrors the org toggle: on, the runtime registers the
    local web tools and mounts them for the subprocess, so the model is offered
    `web_search`/`web_fetch` (and the vendor's own fetch tool is dropped).

    `subagents_enabled` mirrors `ALKERA_SUBAGENTS_ENABLED` without touching the
    process env: off, the runtime withholds `spawn_agent`/`list_agent_types` and
    the subprocess drops opencode's own `task` tool.

    `notebook_host_factory` serves the notebook tools from the given engine
    (the runtime's own seam); `permission_mode` starts the chat in that mode."""
    _check_bun_and_vendor()  # early skip if bun / vendor source is missing
    from alkera_cli.harness.runtime import AdapterFactory, HarnessRuntime
    from alkera_core.project.directory import ProjectDirectory

    server = MockOpenAIServer(
        mock_script,
        compaction_summary=compaction_summary,
        error_on_marker=error_on_marker,
        error_status=error_status,
    )
    await server.start()
    try:
        oc_config = _build_oc_config(
            server.base_url, extra_config=extra_config, model_limit=model_limit
        )
        project = ProjectDirectory(tmp_path / ".alkera")
        runtime = HarnessRuntime(
            project,
            adapter_factory=AdapterFactory(binary=_bun_binary()),
            safety_judge=safety_judge,
            web_search_enabled=web_search_enabled,
            subagents_enabled=subagents_enabled,
            notebook_host_factory=notebook_host_factory,
        )
        # Create the chat + inject the agent_config into its manifest BEFORE
        # opening, so the runtime threads it into harness_native and the
        # adapter merges it into OPENCODE_CONFIG_CONTENT.
        store = project.chats()
        chat = store.create(title="e2e")
        sid = chat.session_id
        chat.manifest.harness = {**chat.manifest.harness, "agent_config": oc_config}
        if permission_mode is not None:
            chat.manifest.permission_mode = permission_mode
        chat.flush_manifest()
        chat.close()
        try:
            yield runtime, sid, server
        finally:
            await runtime.close_all()
    finally:
        await server.stop()


__all__ = ["opencode_e2e_adapter", "opencode_e2e_runtime"]
