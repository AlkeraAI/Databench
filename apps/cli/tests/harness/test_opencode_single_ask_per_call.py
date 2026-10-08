"""One gated call raises one ask.

The parent-hosted shell is an MCP tool as far as opencode is concerned, so
opencode raises its own permission ask before calling it — one that carries the
permission RULE glob and no arguments, because an MCP tool's arguments are the
tool's own business. The tool body then classifies the real command and raises
Alkera's ask for the same call. A reader therefore got TWO cards for one
command: first "Run a command? / Requested by the bash tool." naming nothing
(the fail-closed `opencode-empty` subject), then the real one naming
``$ uv run python -c 'print(1)'``. Answering the first decided nothing — the
tool's own gate still ran — and the person answered twice.

The vendor's ask for a tool this process hosts is now answered here instead of
forwarded: it is the gate's doorway, not the gate. What reaches the reader is
the one ask that names the command.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from _mocks.adapter_seam import drain, opencode_adapter
from _mocks.opencode_transport import NATIVE_SESSION, StubTransport
from alkera_cli.harness.adapters.opencode_http import OpencodeHttpAdapter
from alkera_cli.harness.adapters.opencode_translate import (
    OpencodeEventTranslator,
    _TranslatorContext,
    permission_is_parent_gated,
)
from alkera_core.schemas.chat import PermissionRequest, ToolCall

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from alkera_core.schemas.chat import Event

WORKSPACE = Path("/Users/someone/repo")
CALL = "call_x"
COMMAND = "uv run python -c 'print(1)'"


class _RecordingTransport(StubTransport):
    """`StubTransport` plus the body of every POST, keyed by path — the reply a
    permission answer carries is the whole point here."""

    def __init__(self) -> None:
        super().__init__()
        self.bodies: dict[str, dict[str, Any]] = {}

    async def post(self, path: str, json: dict[str, Any] | None = None, **kwargs: Any) -> Any:
        self.bodies[path] = dict(json or {})
        return await super().post(path, json=json, **kwargs)


def _vendor_ask(
    *,
    request_id: str = "perm_1",
    permission: str = "bash",
    patterns: list[str] | None = None,
    call_id: str | None = CALL,
) -> dict[str, Any]:
    """opencode's ``permission.asked`` for an MCP tool: the rule glob, no
    arguments, and the call it gates."""
    props: dict[str, Any] = {
        "id": request_id,
        "sessionID": NATIVE_SESSION,
        "permission": permission,
        "patterns": ["*"] if patterns is None else patterns,
        "metadata": {},
    }
    if call_id is not None:
        props["tool"] = {"messageID": "m1", "callID": call_id}
    return {"type": "permission.asked", "properties": props}


def _running_call(*, call_id: str = CALL, command: str = COMMAND) -> dict[str, Any]:
    """The frame that first carries the call's arguments."""
    return {
        "type": "message.part.updated",
        "properties": {
            "part": {
                "id": f"prt_{call_id}",
                "sessionID": NATIVE_SESSION,
                "messageID": "m1",
                "type": "tool",
                "tool": "bash",
                "callID": call_id,
                "state": {"status": "running", "input": {"command": command}},
            }
        },
    }


def _translator(*, parent_hosted_shell: bool) -> OpencodeEventTranslator:
    ctx = _TranslatorContext(
        session_id="sid",
        workspace_root=WORKSPACE,
        sandbox_dir=None,
        parent_hosted_shell=parent_hosted_shell,
    )
    return OpencodeEventTranslator(ctx)


# ---------------------------------------------------------------------------
# The predicate: which asks this process answers for itself
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("native_kind", "parent_hosted_shell", "gated"),
    [
        pytest.param("bash", True, True, id="bash-where-the-parent-shell-is-registered"),
        pytest.param("bash", False, False, id="bash-where-it-is-still-the-vendor-shell"),
        pytest.param("alkera_sql_query", True, True, id="an-alkera-mcp-tool"),
        pytest.param("alkera_sql_query", False, True, id="an-alkera-mcp-tool-on-windows-too"),
        pytest.param("web_fetch", True, True, id="the-parent-web-mount"),
        pytest.param("edit", True, False, id="a-vendor-tool-that-nothing-else-gates"),
        pytest.param("webfetch", True, False, id="the-vendor-fetch-is-not-the-web-mount"),
        pytest.param("external_directory", True, False, id="the-boundary-gate-is-not-a-tool"),
    ],
)
def test_only_a_tool_this_process_hosts_answers_its_own_vendor_ask(
    native_kind: str, parent_hosted_shell: bool, gated: bool
) -> None:
    """The vendor ask is swallowed for exactly the tools whose body raises
    Alkera's own ask. A vendor tool nobody else gates keeps its ask — swallowing
    that one would run it unasked."""
    assert permission_is_parent_gated(native_kind, parent_hosted_shell=parent_hosted_shell) is gated


# ---------------------------------------------------------------------------
# The translator: the recorded sequence
# ---------------------------------------------------------------------------


def test_the_vendor_ask_for_the_parent_hosted_shell_is_not_an_ask() -> None:
    """The recorded sequence's first frame. It used to translate into the
    `opencode-empty` card that named no command."""
    translator = _translator(parent_hosted_shell=True)
    assert translator.translate(_vendor_ask()) is None
    assert translator._ctx.parent_gated_requests == ["perm_1"]
    # And nothing is parked waiting for the part that would have named it.
    assert translator._ctx.asks_awaiting_part == {}


def test_the_whole_recorded_sequence_publishes_no_card() -> None:
    """vendor ask → the part naming the command → the part closing. The command
    is still remembered (the tool's own ask is the one that names it), and not
    one `PermissionRequest` comes out of the translator."""
    translator = _translator(parent_hosted_shell=True)
    out = [
        translator.translate(_vendor_ask()),
        translator.translate(_running_call()),
    ]
    published = [
        ev
        for result in out
        if result is not None
        for ev in (result if isinstance(result, list) else [result])
    ]
    assert not [ev for ev in published if isinstance(ev, PermissionRequest)]
    assert translator._ctx.command_by_call_id[CALL] == COMMAND


def test_the_part_landing_first_does_not_resurrect_the_vendor_ask() -> None:
    """The other arrival order: the part is seen before the ask. The ask is
    still not a card — the re-raise path only ever feeds asks that were parked,
    and a parent-gated one never parks."""
    translator = _translator(parent_hosted_shell=True)
    translator.translate(_running_call())
    assert translator.translate(_vendor_ask()) is None
    assert translator._ctx.parent_gated_requests == ["perm_1"]


def test_a_vendor_shell_ask_still_raises_its_card() -> None:
    """The control, and the reason the predicate is not a blanket rule: where the
    parent-hosted shell is NOT registered (Windows today), `bash` is opencode's
    own shell and its ask is the only gate there is."""
    translator = _translator(parent_hosted_shell=False)
    event = translator.translate(_vendor_ask(patterns=["ls -la"]))
    assert isinstance(event, PermissionRequest)
    assert event.request_id == "perm_1"
    assert translator._ctx.parent_gated_requests == []


def test_a_reset_forgets_unanswered_vendor_asks() -> None:
    """A `/clear` drops the session the ids belong to; replying to them after
    would answer an ask opencode has already forgotten."""
    translator = _translator(parent_hosted_shell=True)
    translator.translate(_vendor_ask())
    translator.reset()
    assert translator._ctx.parent_gated_requests == []


# ---------------------------------------------------------------------------
# The adapter: exactly one card reaches the bus, and the vendor gets its answer
# ---------------------------------------------------------------------------


def _wired(tmp_path: Path) -> tuple[OpencodeHttpAdapter, _RecordingTransport]:
    adapter = opencode_adapter(tmp_path)
    transport = _RecordingTransport()
    adapter._state.http_client = transport  # type: ignore[assignment]
    adapter._state.started = True
    adapter._state.opencode_session_id = NATIVE_SESSION
    adapter._translator_ctx.parent_hosted_shell = True
    return adapter, transport


@pytest.mark.asyncio(loop_scope="function")
async def test_the_adapter_answers_the_vendor_ask_and_publishes_nothing(
    tmp_path: Path,
) -> None:
    """What a subscriber — the runtime, and through it the mirror — sees for the
    recorded sequence: no permission card at all from opencode's side, while
    opencode gets the `once` that lets the tool body run on to the real gate."""
    adapter, transport = _wired(tmp_path)
    sub: AsyncIterator[Event] = adapter.subscribe()
    await adapter._handle_native(_vendor_ask())
    await adapter._handle_native(_running_call())
    events = await drain(sub)
    assert not [ev for ev in events if isinstance(ev, PermissionRequest)]
    assert transport.bodies["/permission/perm_1/reply"] == {"reply": "once"}


@pytest.mark.asyncio(loop_scope="function")
async def test_two_gated_calls_are_each_answered_once(tmp_path: Path) -> None:
    """Every call gets its own answer, in arrival order — a drain that dropped
    the tail would park the second call forever."""
    adapter, transport = _wired(tmp_path)
    sub: AsyncIterator[Event] = adapter.subscribe()
    await adapter._handle_native(_vendor_ask(request_id="perm_1", call_id="call_1"))
    await adapter._handle_native(_vendor_ask(request_id="perm_2", call_id="call_2"))
    events = await drain(sub)
    assert not [ev for ev in events if isinstance(ev, PermissionRequest)]
    replies = [p for p in transport.paths if p.startswith("/permission/")]
    assert replies == ["/permission/perm_1/reply", "/permission/perm_2/reply"]
    assert adapter._translator_ctx.parent_gated_requests == []


@pytest.mark.asyncio(loop_scope="function")
async def test_a_failed_reply_does_not_take_the_stream_down(tmp_path: Path) -> None:
    """The reply is best-effort: opencode times its own ask out, and raising
    inside the SSE reader would end the turn over a control call."""
    adapter, transport = _wired(tmp_path)
    transport.raises["/permission/perm_1/reply"] = RuntimeError("no route")
    sub: AsyncIterator[Event] = adapter.subscribe()
    await adapter._handle_native(_vendor_ask())
    await adapter._handle_native(_running_call())
    events = await drain(sub)
    assert not [ev for ev in events if isinstance(ev, PermissionRequest)]
    # The call's own frames still flowed.
    assert [ev for ev in events if isinstance(ev, ToolCall)]
    assert adapter._translator_ctx.parent_gated_requests == []


def test_the_permission_ruleset_keeps_bash_in_lockstep_with_the_parent_shell(
    tmp_path: Path,
) -> None:
    """A fenced session used to drop the `bash` auto-allow, which is what made
    opencode ask for the parent-hosted shell in the first place. Where the shell
    is ours, the ruleset says so — fenced or not — and the fence lives in the
    tool's own gate, over the real command."""
    import os

    if os.name != "posix":  # the parent-hosted shell is POSIX-only today
        pytest.skip("the parent-hosted shell is registered on POSIX only")
    from _mocks.adapter_seam import GATEWAY_AGENT_CONFIG
    from alkera_cli.harness.adapter import SessionConfig
    from alkera_cli.harness.event_bus import EventBus
    from alkera_cli.harness.opencode_binary import ResolvedOpencodeBinary

    fenced = OpencodeHttpAdapter(
        SessionConfig(
            session_id="our-sid",
            project_dir=tmp_path,
            chat_dir=tmp_path / "chat",
            fenced=True,
            harness_native={"agent_config": dict(GATEWAY_AGENT_CONFIG)},
        ),
        binary=ResolvedOpencodeBinary(path=Path("/usr/bin/true"), prefix_args=(), source="staged"),
        event_bus=EventBus(),
    )
    perm = json.loads(fenced._build_env("pw").env["ALKERA_PERMISSION"])
    assert perm["bash"] == "allow"
