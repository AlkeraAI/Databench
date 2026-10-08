"""E2E: agent bash commands run with the TRUE original environment.

The agent runs commands through our parent-hosted shell, advertised under the
native name ``bash`` (opencode's
native bash is disabled), so the command is spawned by the ALKERA parent process —
NOT inside opencode's sandboxed subprocess. Its env is ``build_child_env(os.environ)``:
the parent's true environment minus a small ``_ENV_SCRUB`` denylist of secrets and
sandbox/provider leaks (and the ``ALKERA_DISABLE_*`` lockdown prefix), with ``PWD``
set to the working dir. This drives a REAL opencode (bun-dev) whose scripted model
runs ``env -0`` and asserts the captured environment equals this test process's
environment byte-for-byte, minus exactly the scrub set — the regression that
motivated it: bash children inherited the sandbox, so `uv` cached wheels + whole
interpreters inside the chat's `.runtime/` and git lost `~/.config/git`.

Tolerated differences are ONLY:
- the SCRUBBED secrets/leaks (``_ENV_SCRUB`` + any ``ALKERA_DISABLE_*``) — present
  in the parent, deliberately withheld from a user command (e.g. the server
  password, gateway provider creds, ``OPENCODE_CONFIG``);
- what any locally-run `bash -c` changes itself (`_`, `SHLVL`, `PWD`, `OLDPWD`) —
  the env behaves exactly as if the user ran the command in their own
  non-interactive shell (no rc sourcing, matching upstream);
- ON WINDOWS ONLY: the handful of vars Git-bash/MSYS POSIX-izes at startup
  (`HOME`, `PATH`, `TEMP`, `TMP`, `PATHEXT`, `PSMODULEPATH`) — the shell
  reformatting the same inherited values, not our spawn changing them.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import pytest
from _helpers.opencode_runner import opencode_e2e_runtime
from _mocks.mock_openai_server import FOLLOWUP_KEY, text_chunks, tool_call_chunks
from alkera_cli.harness.permission_broker import PermissionBroker
from alkera_cli.plugins.plugin_base.bash_exec import _ENV_SCRUB
from alkera_core.schemas.chat import Event, PermissionRequest, SessionStatusChanged

pytestmark = [
    pytest.mark.opencode_e2e,
    # The parent-hosted shell + build_child_env env contract is POSIX-only; on
    # Windows the agent uses opencode's native bash (a different, vendored env path).
    pytest.mark.skipif(os.name != "posix", reason="the parent-hosted shell is POSIX-only"),
]

_MODEL = {"provider_id": "mock", "model_id": "mock-model"}

#: Vars a plain `bash -c` (and the tool's own spawn plumbing) legitimately
#: sets or rewrites in any child — identical to running the command yourself.
_SHELL_OWNED = {"_", "SHLVL", "PWD", "OLDPWD"}

#: On Windows the agent's shell is Git-bash/MSYS, which POSIX-izes a handful of
#: inherited Windows vars at startup — HOME/TEMP/TMP become /c/… or /tmp, PATH
#: flips `;`+`\` to `:`+`/c/…`, PATHEXT gains `.CPL`, PSMODULEPATH gets PowerShell
#: 7's modules prepended. That is the shell reformatting the SAME inherited
#: values, not our spawn dropping or sandboxing them — so their byte form can't
#: match the Windows-native parent. Tolerate them in the exact-equality check on
#: Windows only; the not-LOST / not-LEAKED checks below still cover them (each is
#: present in both), and the canary + config values still prove exact passthrough.
_WINDOWS_SHELL_NORMALIZED = (
    {"HOME", "PATH", "PATHEXT", "PSMODULEPATH", "TEMP", "TMP"} if os.name == "nt" else set()
)


async def _drain(sub: Any, *, budget_seconds: float = 90.0) -> list[Event]:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + budget_seconds
    out: list[Event] = []
    seen_running = False
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            return out
        try:
            async with asyncio.timeout(remaining):
                ev: Event = await anext(sub)
        except (TimeoutError, StopAsyncIteration):
            return out
        out.append(ev)
        if isinstance(ev, SessionStatusChanged):
            if ev.status == "running":
                seen_running = True
            elif ev.status in ("idle", "error") and seen_running:
                return out


def _parse_env0(blob: bytes) -> dict[str, str]:
    """Parse `env -0` output (NUL-separated `NAME=value`, values may contain
    newlines)."""
    out: dict[str, str] = {}
    for entry in blob.split(b"\0"):
        if not entry:
            continue
        name, sep, value = entry.partition(b"=")
        if sep:
            out[name.decode()] = value.decode()
    return out


async def test_agent_bash_sees_the_exact_original_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Plant tripwires BEFORE the harness spawns: a custom cache root (the var the
    # uv bug hijacked) + a canary that must ride through untouched, and two scrubbed
    # leaks (a provider/opencode config + the server password) that must NOT reach an
    # agent command — so the scrub is genuinely exercised, not trivially absent.
    monkeypatch.setenv("XDG_CACHE_HOME", "/custom/original/cache")
    monkeypatch.setenv("ALKERA_E2E_ENV_CANARY", "keep me exactly\nacross lines")
    monkeypatch.setenv("OPENCODE_CONFIG", "/users/original/opencode.json")
    monkeypatch.setenv("ALKERA_SERVER_PASSWORD", "supersecret-should-be-scrubbed")

    capture = tmp_path / "captured.env0"
    # opencode's NATIVE bash is disabled — the agent runs through our parent-hosted
    # the bare ``bash`` name (our loopback-MCP tool, re-advertised unprefixed),
    # spawned from THIS process.
    script = {
        "*": tool_call_chunks(
            "bash",
            {"command": f"env -0 > '{capture}'", "description": "capture the environment"},
        ),
        FOLLOWUP_KEY: text_chunks("captured"),
    }

    async def _allow(request: PermissionRequest) -> str:
        return "allow_once"

    async with opencode_e2e_runtime(tmp_path, mock_script=script) as (runtime, sid, _server):
        session = await runtime.open_chat(
            sid, permission_broker=PermissionBroker(_allow, default_timeout_seconds=30.0)
        )
        # our bash spawns the command from THIS (parent) process with
        # build_child_env(os.environ), so our os.environ IS the source the agent sees.
        original = dict(os.environ)
        sub = session.subscribe()
        await session.send_prompt("capture the environment", model=_MODEL)
        events = await _drain(sub)
        await runtime.close_chat(sid)

    statuses = [e.status for e in events if isinstance(e, SessionStatusChanged)]
    assert "idle" in statuses and "error" not in statuses, f"turn failed: {statuses}"
    assert capture.exists(), "the bash command never ran"

    captured = _parse_env0(capture.read_bytes())

    # The user's real env rides through untouched (NOT the sandbox / .runtime).
    assert captured["XDG_CACHE_HOME"] == "/custom/original/cache"
    assert captured["ALKERA_E2E_ENV_CANARY"] == "keep me exactly\nacross lines"

    # No secret / sandbox / provider leak reaches an agent command (the scrub set) —
    # including the two we planted, so the denylist is actually doing work.
    for forbidden in (
        "OPENCODE_CONFIG",
        "ALKERA_SERVER_PASSWORD",
        "ALKERA_SHELL_ENV_RESTORE",
        "ALKERA_CONFIG_CONTENT",
        "ALKERA_PERMISSION",
        "ALKERA_TEST_HOME",
    ):
        assert forbidden not in captured, forbidden

    # EXACT equality, both directions: the agent's env == our env MINUS the scrub set
    # (and the ALKERA_DISABLE_* lockdown prefix), modulo the shell-owned vars and the
    # Windows shell's POSIX-ization.
    tolerated = _SHELL_OWNED | _ENV_SCRUB | _WINDOWS_SHELL_NORMALIZED

    def _comparable(env: dict[str, str]) -> dict[str, str]:
        return {
            k: v
            for k, v in env.items()
            if k not in tolerated and not k.startswith("ALKERA_DISABLE_")
        }

    comparable_captured = _comparable(captured)
    comparable_original = _comparable(original)
    missing = set(comparable_original) - set(comparable_captured)
    extra = set(comparable_captured) - set(comparable_original)
    assert not missing, f"user env vars LOST in agent bash: {sorted(missing)}"
    assert not extra, f"non-original vars LEAKED into agent bash: {sorted(extra)}"
    changed = {
        k: (comparable_original[k], comparable_captured[k])
        for k in comparable_original
        if comparable_original[k] != comparable_captured[k]
    }
    assert not changed, f"user env vars MODIFIED in agent bash: {changed}"
