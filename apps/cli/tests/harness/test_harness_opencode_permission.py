"""Tests for the opencode adapter's permission + agent wiring."""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from alkera_cli.harness.adapter import PromptInput, SessionConfig
from alkera_cli.harness.adapters.opencode_http import (
    _OPENCODE_DEFAULT_CONFIG,
    OpencodeHttpAdapter,
)
from alkera_cli.harness.event_bus import EventBus
from alkera_cli.harness.opencode_binary import ResolvedOpencodeBinary

#: A gateway-routed chat's agent config: the turn gate admits a model only on a
#: provider the config declares, so the adapters here carry one.
_GATEWAY_CONFIG: dict[str, object] = {
    "provider": {"mock": {"npm": "@ai-sdk/openai-compatible", "options": {"baseURL": "http://x"}}},
    "model": "mock/mock-model",
}


def _make_adapter(
    tmp_path: Path, *, ripgrep_path: Path | None = None, fenced: bool = False
) -> OpencodeHttpAdapter:
    config = SessionConfig(
        session_id="our-sid",
        project_dir=tmp_path,
        chat_dir=tmp_path / "chat",
        fenced=fenced,
        harness_native={"agent_config": dict(_GATEWAY_CONFIG)},
    )
    binary = ResolvedOpencodeBinary(
        path=Path("/usr/bin/true"),
        prefix_args=(),
        source="staged",
        ripgrep_path=ripgrep_path,
    )
    return OpencodeHttpAdapter(config, binary=binary, event_bus=EventBus())


@pytest.fixture
def adapter(tmp_path: Path) -> OpencodeHttpAdapter:
    return _make_adapter(tmp_path)


def test_build_env_forces_ask_on_every_tool(
    adapter: OpencodeHttpAdapter,
) -> None:
    """Full passthrough: ALKERA_PERMISSION must make opencode emit
    permission.asked for EVERY permission-relevant tool — including the
    read-only ones — so OUR classifier+policy decide, not opencode's config.
    Only our own UX surfaces (question / plan_present / alkera_*) stay allowed."""
    env = adapter._build_env("pw").env
    assert "ALKERA_PERMISSION" in env
    perm = json.loads(env["ALKERA_PERMISSION"])
    # Wildcard ask catches every tool, read-only included.
    assert perm["*"] == "ask"
    # Read tools are NO LONGER vendor-allowed — they flow to our broker too.
    for not_allowed in ("read", "glob", "grep", "list", "lsp"):
        assert perm.get(not_allowed) != "allow", not_allowed
    # Our own UX surfaces stay allowed (they gate in-parent / own their prompt).
    assert perm["question"] == "allow"
    assert perm["plan_present"] == "allow"
    # Alkera owns these — denied via a `pattern:"*"` deny rule, which opencode
    # treats as the tool being DISABLED (removed from the toolset, not just
    # rejected on call).
    for denied in ("task", "todowrite", "skill"):
        assert perm[denied] == "deny", denied


@pytest.mark.skipif(os.name != "posix", reason="the bash auto-allow is POSIX-only")
def test_a_local_session_auto_allows_the_parent_hosted_bash(adapter: OpencodeHttpAdapter) -> None:
    """The control: a LOCAL (unfenced) session keeps the parent-hosted shell on
    ``allow`` — it gates its own write effects in-parent, exactly as before."""
    perm = json.loads(adapter._build_env("pw").env["ALKERA_PERMISSION"])
    assert perm.get("bash") == "allow"


@pytest.mark.skipif(os.name != "posix", reason="the bash auto-allow is POSIX-only")
def test_a_fenced_session_keeps_bash_with_the_tool_that_gates_it(tmp_path: Path) -> None:
    """A fenced (cloud box) session keeps the same auto-allow.

    Dropping it was meant as a second layer behind the in-tool
    ``gate_shell_action``. It never was one: ``bash`` under the parent-hosted
    shell IS that tool, and opencode's ask for an MCP tool carries the
    permission rule glob and no arguments — so the policy had nothing to decide
    over, failed closed to a prompt naming no command, and the reader answered
    that card AND the real one the tool's own gate raised. The fence and the
    read-only refusal both live in that gate, over the real command."""
    adapter = _make_adapter(tmp_path, fenced=True)
    perm = json.loads(adapter._build_env("pw").env["ALKERA_PERMISSION"])
    assert perm["bash"] == "allow"
    assert perm["*"] == "ask"


def test_build_env_isolates_all_xdg_roots_from_the_user_machine(
    adapter: OpencodeHttpAdapter,
) -> None:
    """Every opencode root is Alkera-owned, so the spawned opencode reads
    NOTHING (config / data / state / cache / home) from the user's machine."""
    env = adapter._build_env("pw").env
    # Data stays per-chat: the read-only `agent/agent.db` enrichment path.
    assert Path(env["XDG_DATA_HOME"]).name == ".runtime"  # OS-native separators
    # Config / state / cache / home are the DISCOVERY roots (agent md files,
    # plugin tool modules, `<cache>/agent/bin` on PATH) — they must NOT sit in
    # the project tree, where the agent's own write tool can reach them.
    for var in ("XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME", "ALKERA_TEST_HOME"):
        assert Path(env[var]).name != ".runtime", var
    # Project-config walk from the user's workspace is disabled.
    assert env["ALKERA_DISABLE_PROJECT_CONFIG"] == "true"


def test_build_env_keeps_config_discovery_out_of_the_project_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """opencode discovers agent definitions (`agent/**/*.md`, whose `permission:`
    frontmatter merges AFTER our ALKERA_PERMISSION ruleset) and plugin tool
    modules (`tool/*.ts`, dynamically imported) under its CONFIG dir, and puts
    `<cache>/agent/bin` on PATH. If any of those roots lives inside the project,
    one agent-approved file write disables the whole permission gate for every
    later turn. They must all resolve outside the workspace."""
    from alkera_cli.host import paths

    home = tmp_path / "alkera-home"
    monkeypatch.setattr(paths, "ALKERA_HOME", home)
    project = tmp_path / "workspace"
    adapter = _make_adapter(project)

    env = adapter._build_env("pw").env
    for var in ("XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME", "ALKERA_TEST_HOME"):
        root = Path(env[var]).resolve()
        assert not root.is_relative_to(project.resolve()), var
        assert root.is_relative_to(home.resolve()), var


def test_global_instructions_are_written_outside_the_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The instruction file handed to the agent must not live where the agent
    can rewrite it — otherwise a prompt injection persists itself into the
    system prompt of every later turn."""
    from alkera_cli.host import paths

    home = tmp_path / "alkera-home"
    monkeypatch.setattr(paths, "ALKERA_HOME", home)
    project = tmp_path / "workspace"
    config = SessionConfig(
        session_id="our-sid",
        project_dir=project,
        chat_dir=project / ".alkera" / "chats" / "our-sid",
        harness_native={"global_instructions": "be careful"},
    )
    adapter = OpencodeHttpAdapter(
        config,
        binary=ResolvedOpencodeBinary(path=Path("/usr/bin/true"), prefix_args=(), source="staged"),
        event_bus=EventBus(),
    )

    oc_config = json.loads(adapter._build_env("pw").secrets["ALKERA_CONFIG_CONTENT"])
    [instructions] = oc_config["instructions"]
    written = Path(instructions)
    assert written.is_file()
    assert written.read_text(encoding="utf-8") == "be careful"
    assert not written.resolve().is_relative_to(project.resolve())
    assert written.resolve().is_relative_to(home.resolve())


def test_build_env_hands_the_password_and_config_over_by_file_only(
    adapter: OpencodeHttpAdapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The agent's loopback API can spawn a PTY and run shell commands; its
    authorization middleware is a NO-OP when no password is configured. Every
    spawn must therefore carry one, and with its config (the gateway token) it
    rides in the secrets file the env names, never the env itself, which stays
    readable in /proc/<pid>/environ and passes to every child. An inherited
    password is scrubbed, so it can never be the one a launch falls back to."""
    monkeypatch.setenv("ALKERA_SERVER_PASSWORD", "inherited")
    launch = adapter._build_env("s3cret-token")
    assert launch.secrets["ALKERA_SERVER_PASSWORD"] == "s3cret-token"
    assert "ALKERA_CONFIG_CONTENT" in launch.secrets
    assert "ALKERA_SERVER_PASSWORD" not in launch.env
    assert "ALKERA_CONFIG_CONTENT" not in launch.env
    assert launch.env["ALKERA_SECRETS_FILE"] == str(adapter._agent_state_dir / "launch.json")


def test_build_env_sets_no_phone_home_lockdown_flags(
    adapter: OpencodeHttpAdapter,
) -> None:
    """The spawn env must disable every opencode outbound call that isn't our
    model gateway, plus pin a deterministic DB name. Regression guard for the
    'no phone-home' guarantee — if a flag is dropped, opencode silently starts
    reaching out (models.dev, opncd.ai, GitHub, version checks) again."""
    env = adapter._build_env("pw").env
    for flag in (
        "ALKERA_DISABLE_MODELS_FETCH",  # models.dev catalog fetch + refresh
        "ALKERA_DISABLE_SHARE",  # opncd.ai session sharing
        "ALKERA_DISABLE_AUTOUPDATE",  # version-check pings
        "ALKERA_DISABLE_LSP_DOWNLOAD",  # LSP-server downloads
        "ALKERA_DISABLE_CHANNEL_DB",  # deterministic agent.db filename
        "ALKERA_DISABLE_NPM_INSTALL",  # runtime npm installer (plugins/formatters/…)
        "ALKERA_PURE",  # skip external plugins entirely
    ):
        assert env.get(flag) == "true", flag
    # The ripgrep download gate is CONDITIONAL: this fixture's binary carries no
    # bundled rg (ripgrep_path=None), so the flag must stay UNSET — otherwise
    # opencode's glob/grep (which hard-die without rg) would break wherever no
    # system rg exists. The gate is only set when we ship a bundled rg (see
    # test_build_env_prepends_bundled_rg_to_path_and_disables_download).
    assert "ALKERA_DISABLE_RIPGREP_DOWNLOAD" not in env


def test_build_env_no_ripgrep_changes_without_bundled_rg(
    adapter: OpencodeHttpAdapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no bundled rg, _build_env touches neither PATH nor the gate — so
    opencode keeps its system-rg/download fallback (glob/grep still work)."""
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    env = adapter._build_env("pw").env
    assert env["PATH"] == "/usr/bin:/bin"
    assert "ALKERA_DISABLE_RIPGREP_DOWNLOAD" not in env


def test_build_env_prepends_bundled_rg_to_path_and_disables_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the resolved binary ships a bundled rg, _build_env puts its dir
    FIRST on PATH (so opencode's `which("rg")` resolves OUR rg) and sets the
    download gate (zero outbound calls except our gateway)."""
    rg_dir = tmp_path / "bundled-bin"
    rg_dir.mkdir()
    rg = rg_dir / "rg"
    rg.write_text("#!/bin/sh\nexit 0\n")
    rg.chmod(rg.stat().st_mode | stat.S_IXUSR)
    adapter = _make_adapter(tmp_path, ripgrep_path=rg)

    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    env = adapter._build_env("pw").env

    # Our rg's directory is the FIRST PATH entry; the inherited PATH follows.
    assert env["PATH"].split(os.pathsep)[0] == str(rg_dir)
    assert env["PATH"].endswith("/usr/bin:/bin")
    assert env["ALKERA_DISABLE_RIPGREP_DOWNLOAD"] == "true"


def test_build_env_sets_rg_path_even_with_empty_inherited_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Edge case: an empty inherited PATH must not yield a leading
    `os.pathsep` (which would inject the CWD as a search dir)."""
    rg_dir = tmp_path / "bundled-bin"
    rg_dir.mkdir()
    rg = rg_dir / "rg"
    rg.write_text("#!/bin/sh\nexit 0\n")
    rg.chmod(rg.stat().st_mode | stat.S_IXUSR)
    adapter = _make_adapter(tmp_path, ripgrep_path=rg)

    monkeypatch.delenv("PATH", raising=False)
    env = adapter._build_env("pw").env
    assert env["PATH"] == str(rg_dir)


def test_harness_dir_is_the_runtime_dir(
    adapter: OpencodeHttpAdapter,
    tmp_path: Path,
) -> None:
    """The per-chat sandbox is `<chat>/.runtime`, where every existing chat
    keeps its harness state."""
    assert adapter._harness_dir == tmp_path / "chat" / ".runtime"


def test_capabilities_omit_share_and_name_is_the_stored_slug() -> None:
    """`share` is omitted (it would POST a session to opncd.ai), and `name` is the
    `agent` slug every existing chat manifest stores for this adapter."""
    assert OpencodeHttpAdapter.name == "agent"
    assert "share" not in OpencodeHttpAdapter.capabilities


def test_build_env_scrubs_inherited_config_leaks(
    adapter: OpencodeHttpAdapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A user shell that exports opencode config/auth/db vars — under either the
    opencode-native names or our renamed ALKERA_CONFIG_CONTENT — must NOT leak
    into our isolated harness."""
    monkeypatch.setenv("OPENCODE_CONFIG", "/home/u/.config/opencode/opencode.json")
    monkeypatch.setenv("OPENCODE_CONFIG_DIR", "/home/u/.config/opencode")
    monkeypatch.setenv("OPENCODE_AUTH_CONTENT", '{"anthropic": "leak"}')
    monkeypatch.setenv("OPENCODE_DB", "/home/u/opencode.db")
    monkeypatch.setenv("OPENCODE_CONFIG_CONTENT", '{"model": "user/leak"}')
    # The renamed injection surface must be scrubbed too (then overwritten below).
    monkeypatch.setenv("ALKERA_CONFIG_CONTENT", '{"model": "user/leak2"}')
    launch = adapter._build_env("pw")
    env = launch.env
    assert "OPENCODE_CONFIG" not in env
    assert "OPENCODE_CONFIG_DIR" not in env
    assert "OPENCODE_AUTH_CONTENT" not in env
    assert "OPENCODE_DB" not in env
    # We never set the legacy OPENCODE_CONFIG_CONTENT name — it's fully gone.
    assert "OPENCODE_CONFIG_CONTENT" not in env
    # CONFIG_CONTENT is OURS — neither the legacy nor the renamed user value wins;
    # the injected payload is exactly our default config plus the session's own
    # gateway config, no leaked user keys.
    assert "ALKERA_CONFIG_CONTENT" not in env
    assert json.loads(launch.secrets["ALKERA_CONFIG_CONTENT"]) == {
        **_OPENCODE_DEFAULT_CONFIG,
        **_GATEWAY_CONFIG,
    }
    # Make the anti-leak guarantee explicit: neither leaked model value survives.
    assert "user/leak" not in launch.secrets["ALKERA_CONFIG_CONTENT"]
    assert "user/leak2" not in launch.secrets["ALKERA_CONFIG_CONTENT"]


def test_build_env_sets_its_settings_only_under_the_product_prefix(
    adapter: OpencodeHttpAdapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every setting the harness passes uses the `ALKERA_` prefix the vendored
    opencode reads, so none can collide with an `OPENCODE_*` value the user's own
    shell exports for their own opencode (those are scrubbed, see above). Start
    from a base with no inherited OPENCODE* so only what `_build_env` adds is
    under test."""
    for key in [k for k in os.environ if k.startswith("OPENCODE")]:
        monkeypatch.delenv(key, raising=False)
    env = adapter._build_env("pw").env
    leaked = sorted(k for k in env if k.startswith("OPENCODE"))
    assert leaked == [], f"harness contributed OPENCODE-prefixed env vars: {leaked}"
    # Sanity: the renamed names ARE what we set.
    assert "ALKERA_SECRETS_FILE" in env
    assert "ALKERA_PERMISSION" in env


def test_build_env_pins_no_model_of_its_own(tmp_path: Path) -> None:
    """The sandbox has NO default model: a credential-less chat must not run a
    turn at all, let alone on opencode's free hosted model. The only model the
    injected config ever names is the one the session's gateway config brought."""
    bare = SessionConfig(session_id="bare", project_dir=tmp_path, chat_dir=tmp_path / "chat")
    binary = ResolvedOpencodeBinary(path=Path("/usr/bin/true"), prefix_args=(), source="staged")
    launch = OpencodeHttpAdapter(bare, binary=binary, event_bus=EventBus())._build_env("pw")
    content = json.loads(launch.secrets["ALKERA_CONFIG_CONTENT"])
    assert "model" not in content
    assert "provider" not in content


async def test_send_prompt_drops_plan_agent_but_forwards_others(
    adapter: OpencodeHttpAdapter,
) -> None:
    """Plan mode is driven by OUR per-turn steering + the broker policy, NOT opencode's
    built-in "plan" agent — whose plan.txt reminder orders the model that ANY edit is
    "STRICTLY FORBIDDEN ... ZERO exceptions" and would block it from writing its sandbox
    plan.md. So `agent="plan"` is dropped from the body (the turn runs on the default
    agent, like read_only). Any OTHER agent value (a subagent) is still forwarded."""
    captured: dict[str, object] = {}

    class _FakeClient:
        async def post(self, path: str, json: dict[str, object]):
            captured["path"] = path
            captured["body"] = json
            return _FakeResp()

    class _FakeResp:
        def raise_for_status(self) -> None:
            return None

    # Hand-wire the minimal state send_prompt needs.
    adapter._state.started = True
    adapter._state.opencode_session_id = "oc-1"
    adapter._state.http_client = _FakeClient()  # type: ignore[assignment]

    # plan → dropped: the default agent + our steering/gate drive plan mode.
    await adapter.send_prompt(PromptInput(text="hi", agent="plan"))
    body = captured["body"]
    assert isinstance(body, dict)
    assert "agent" not in body
    # Must use the ASYNC endpoint — the sync `/message` endpoint blocks
    # until the whole turn finishes and overruns the HTTP read timeout
    # on long (subagent / exploration) turns.
    assert captured["path"] == "/session/oc-1/prompt_async"

    # A non-plan agent (a subagent) is still forwarded so opencode routes through it.
    await adapter.send_prompt(PromptInput(text="explore", agent="explore"))
    body2 = captured["body"]
    assert isinstance(body2, dict)
    assert body2["agent"] == "explore"


async def test_send_prompt_omits_agent_when_unset(
    adapter: OpencodeHttpAdapter,
) -> None:
    captured: dict[str, object] = {}

    class _FakeClient:
        async def post(self, path: str, json: dict[str, object]):
            captured["body"] = json
            return _FakeResp()

    class _FakeResp:
        def raise_for_status(self) -> None:
            return None

    adapter._state.started = True
    adapter._state.opencode_session_id = "oc-1"
    adapter._state.http_client = _FakeClient()  # type: ignore[assignment]

    await adapter.send_prompt(PromptInput(text="hi"))
    body = captured["body"]
    assert isinstance(body, dict)
    assert "agent" not in body


async def test_send_prompt_injects_synthetic_reminder_when_system_set(
    adapter: OpencodeHttpAdapter,
) -> None:
    """A per-turn `system` directive (plan mode) is injected as a LEADING
    synthetic text part — the high-reliability steering channel — plus
    the `system` field as backup. The synthetic flag keeps it out of our
    UI + out of opencode's own reminder wrapping."""
    captured: dict[str, object] = {}

    class _FakeClient:
        async def post(self, path: str, json: dict[str, object]):
            captured["body"] = json
            return _FakeResp()

    class _FakeResp:
        def raise_for_status(self) -> None:
            return None

    adapter._state.started = True
    adapter._state.opencode_session_id = "oc-1"
    adapter._state.http_client = _FakeClient()  # type: ignore[assignment]

    await adapter.send_prompt(PromptInput(text="design it", system="PLAN RULES"))
    body = captured["body"]
    assert isinstance(body, dict)
    parts = body["parts"]
    assert isinstance(parts, list)
    # First part is the synthetic reminder; the user's text follows.
    assert parts[0]["synthetic"] is True
    assert "PLAN RULES" in parts[0]["text"]
    assert parts[0]["text"].startswith("<system-reminder>")
    assert parts[1] == {"type": "text", "text": "design it"}
    # The `system` field is intentionally NOT set — the synthetic part is
    # the only steering channel. opencode appends `system` last in the
    # system block where the model under-weights it; duplicating the
    # directive there just bloats prompt cost.
    assert "system" not in body


async def test_send_prompt_no_synthetic_part_without_system(
    adapter: OpencodeHttpAdapter,
) -> None:
    captured: dict[str, object] = {}

    class _FakeClient:
        async def post(self, path: str, json: dict[str, object]):
            captured["body"] = json
            return _FakeResp()

    class _FakeResp:
        def raise_for_status(self) -> None:
            return None

    adapter._state.started = True
    adapter._state.opencode_session_id = "oc-1"
    adapter._state.http_client = _FakeClient()  # type: ignore[assignment]

    await adapter.send_prompt(PromptInput(text="hi"))
    body = captured["body"]
    assert isinstance(body, dict)
    parts = body["parts"]
    assert parts == [{"type": "text", "text": "hi"}]
    assert "system" not in body


class _RecordingHttp:
    """Stub http client that records POSTs (no real socket)."""

    def __init__(self) -> None:
        self.posts: list[tuple[str, dict]] = []

    async def post(self, url: str, json: dict) -> httpx.Response:
        self.posts.append((url, json))
        return httpx.Response(200, request=httpx.Request("POST", f"http://opencode{url}"))


@pytest.mark.asyncio
async def test_reject_reason_rides_the_reply_message(adapter: OpencodeHttpAdapter) -> None:
    # A reject's reason posts as opencode's optional `message` — the model then
    # sees <reason> verbatim as the call's error.
    http = _RecordingHttp()
    adapter._state.http_client = http  # type: ignore[assignment]
    adapter._state.started = True
    await adapter.resolve_permission(
        "per_1", "reject_once", reason="judge: looks like exfiltration"
    )
    [(url, body)] = http.posts
    assert url == "/permission/per_1/reply"
    assert body == {"reply": "reject", "message": "judge: looks like exfiltration"}


@pytest.mark.asyncio
async def test_reject_without_reason_omits_message(adapter: OpencodeHttpAdapter) -> None:
    http = _RecordingHttp()
    adapter._state.http_client = http  # type: ignore[assignment]
    adapter._state.started = True
    await adapter.resolve_permission("per_2", "reject_once")
    [(_url, body)] = http.posts
    assert body == {"reply": "reject"}


@pytest.mark.asyncio
async def test_allow_never_carries_a_message(adapter: OpencodeHttpAdapter) -> None:
    # A reason on an ALLOW (e.g. the judge's "ok" note) must not leak into the
    # reply — opencode only renders feedback on rejects.
    http = _RecordingHttp()
    adapter._state.http_client = http  # type: ignore[assignment]
    adapter._state.started = True
    await adapter.resolve_permission("per_3", "allow_once", reason="fine")
    [(_url, body)] = http.posts
    assert body == {"reply": "once"}


def _ask(adapter: OpencodeHttpAdapter, request_id: str, always: list[str]) -> None:
    """Put one ask through the translator so the adapter knows what an ``always``
    reply to it would record.

    A vendor tool raises it. ``bash`` would not: the parent-hosted shell gates
    its own command, so the vendor ask for it is answered by the adapter and
    never reaches a reader to be answered ``always`` at all."""
    adapter._translate(
        {
            "type": "permission.asked",
            "properties": {
                "id": request_id,
                "permission": "edit",
                "patterns": ["*"],
                "metadata": {},
                "always": always,
            },
        }
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "always",
    [
        pytest.param(["*"], id="the-match-everything-glob"),
        pytest.param(["**"], id="a-double-star"),
        pytest.param(["?*"], id="a-question-star"),
        pytest.param(["*", "ls *"], id="one-open-glob-beside-a-narrow-one"),
        pytest.param([], id="no-globs-at-all"),
    ],
)
async def test_always_is_never_replied_for_an_ask_that_scopes_nothing(
    adapter: OpencodeHttpAdapter, always: list[str]
) -> None:
    """opencode copies EVERY glob in the ask's ``always`` into its approved
    ruleset and then stops raising the ask, so one that takes anything is a
    standing grant over every later call of the tool — past our broker, our
    fence and our audit — whether it stands alone or sits beside a narrow one,
    and whichever of the spellings it is written in. Whatever a caller sends,
    the wire carries ``once``."""
    http = _RecordingHttp()
    adapter._state.http_client = http  # type: ignore[assignment]
    adapter._state.started = True
    _ask(adapter, "per_4", always)
    await adapter.resolve_permission("per_4", "allow_always")
    [(_url, body)] = http.posts
    assert body == {"reply": "once"}


@pytest.mark.asyncio
async def test_always_is_replied_when_the_ask_carries_a_real_scope(
    adapter: OpencodeHttpAdapter,
) -> None:
    """A glob that names a directory IS the scope the reader chose, so the
    standing grant travels — the downgrade is aimed at the unscoped ask, not at
    always-allow."""
    http = _RecordingHttp()
    adapter._state.http_client = http  # type: ignore[assignment]
    adapter._state.started = True
    _ask(adapter, "per_5", ["/tmp/data/*"])
    await adapter.resolve_permission("per_5", "allow_always")
    [(_url, body)] = http.posts
    assert body == {"reply": "always"}


def _asked_then_reset(adapter: OpencodeHttpAdapter) -> None:
    """A scoped ask whose record a reconnect dropped — `/clear` and every stream
    reset run the translator's `reset`."""
    _ask(adapter, "per_6", ["/tmp/data/*"])
    adapter._translator.reset()


def _never_asked(adapter: OpencodeHttpAdapter) -> None:
    """A reply for an ask this adapter never translated at all."""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "prepare",
    [
        pytest.param(_asked_then_reset, id="a-stream-reset-between-ask-and-reply"),
        pytest.param(_never_asked, id="an-ask-this-adapter-never-saw"),
    ],
)
async def test_always_needs_a_scope_this_adapter_proved(
    adapter: OpencodeHttpAdapter, prepare: Callable[[OpencodeHttpAdapter], None]
) -> None:
    """The standing grant rides on proof, not on the absence of suspicion. An ask
    that cannot vouch for its own globs — its record dropped, or never made here
    at all — is answered once. One extra prompt is the whole cost; the shape this
    replaces would have granted every later call of the tool."""
    http = _RecordingHttp()
    adapter._state.http_client = http  # type: ignore[assignment]
    adapter._state.started = True
    prepare(adapter)
    await adapter.resolve_permission("per_6", "allow_always")
    [(_url, body)] = http.posts
    assert body == {"reply": "once"}


# ---------------------------------------------------------------------------
# Shell env restore — agent commands get the TRUE original environment
# ---------------------------------------------------------------------------


def test_build_env_ships_the_exact_reverse_diff_for_shell_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sandbox (XDG redirects, scrubs, ALKERA_* blobs, PATH prepend) must
    apply to opencode ITSELF only: applying ALKERA_SHELL_ENV_RESTORE to the
    spawn env reconstructs the original process environment BYTE-FOR-BYTE —
    this is the contract the vendored shell tool enforces for every agent
    bash command (the uv-cache-inside-the-chat-dir bug)."""
    from alkera_cli.harness.adapters.opencode_http import SHELL_ENV_RESTORE_VAR

    original = {
        "HOME": "/Users/someone",
        "PATH": "/opt/homebrew/bin:/usr/bin",
        # The user's own cache root — the var the uv bug hijacked.
        "XDG_CACHE_HOME": "/Users/someone/.cache",
        # A scrubbed opencode leak: invisible to opencode, restored for bash.
        "OPENCODE_CONFIG": "/Users/someone/.config/opencode/config.json",
        # An arbitrary user var that must ride through untouched.
        "MY_PROJECT_TOKEN": "keep-me-exactly",
    }
    monkeypatch.setattr(os, "environ", dict(original))
    rg_dir = tmp_path / "rg-bundle"
    rg_dir.mkdir()
    adapter = _make_adapter(tmp_path, ripgrep_path=rg_dir / "rg")

    env = adapter._build_env("secret-pw").env

    # Sanity: the sandbox really diverged (otherwise this test proves nothing).
    # Compare the final path component, not a "/.runtime" suffix — the code uses
    # native separators (str(chat_dir / ".runtime")), so Windows yields "\.runtime".
    assert env["XDG_CACHE_HOME"] != original["XDG_CACHE_HOME"]
    assert Path(env["XDG_DATA_HOME"]).name == ".runtime"
    assert "OPENCODE_CONFIG" not in env
    assert env["PATH"].startswith(str(rg_dir))

    restore = json.loads(env[SHELL_ENV_RESTORE_VAR])
    restored = {k: v for k, v in env.items() if k != SHELL_ENV_RESTORE_VAR}
    for key, value in restore.items():
        if value is None:
            restored.pop(key, None)
        else:
            restored[key] = value

    # EXACT reconstruction — nothing missing, nothing extra, nothing changed.
    assert restored == original

    # The blob itself spells out that every alkera-internal injection
    # (the secrets file's name, permission ruleset, lockdown flags) is unset
    # for agent commands.
    for secret in ("ALKERA_SECRETS_FILE", "ALKERA_PERMISSION"):
        assert secret in restore and restore[secret] is None, secret


async def test_a_refused_permission_reply_raises_instead_of_vanishing(
    adapter: OpencodeHttpAdapter,
) -> None:
    """opencode answers an unknown or already-settled ask with a 4xx. httpx does
    not raise on it, so the reply used to count as delivered with no exception
    and no log line — the ask stayed open and nobody knew."""
    import httpx

    class _RefusingClient:
        async def post(self, path: str, json: dict[str, object]) -> httpx.Response:
            return httpx.Response(
                404,
                json={"name": "PermissionNotFoundError"},
                request=httpx.Request("POST", f"http://opencode{path}"),
            )

    adapter._state.started = True
    adapter._state.opencode_session_id = "oc-1"
    adapter._state.http_client = _RefusingClient()  # type: ignore[assignment]
    with pytest.raises(httpx.HTTPStatusError):
        await adapter.resolve_permission("per_gone", "allow_once")
