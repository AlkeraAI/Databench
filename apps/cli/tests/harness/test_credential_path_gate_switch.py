"""The credential-path gate is OFF unless its switch is set.

The gate raised any action naming a secret-looking path (``~/.ssh``,
``~/.alkera/auth.yml``, a ``.env``, a ``*.pem``) to EGRESS on three lanes: the
opencode translator, the Claude adapter and the parent-hosted shell gate. It is a
broad substring scan that has not been tested against the paths a real session
names, and it refused the one file plan mode's own steering asks for: on a cloud
box opencode's worktree is ``/`` (a chat folder is not a git checkout), so the
chat's ``plan.md`` arrived spelled ``opt/alkera-work/.alkera/chats/…/plan.md``,
matched the ``.alkera`` marker, resolved against the wrong root, missed the
sandbox carve-out and came back "auto-rejects egress actions".

So the gate is off by default (``ALKERA_CREDENTIAL_PATH_GATE``), and with it off a
path decides nothing on its own: the effect a tool gave the action stands, and the
permission mode, the rule table and the sandbox carve-out decide — the sandbox
write is admitted in plan mode, a project write is still refused there, still asked
about in ``default`` and still refused in ``read_only``. The switch is the one thing
that brings the escalation back, on every lane.
"""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from _adapter_factory import FakeAdapterFactory
from alkera_cli.contracts.tool_types import Effect
from alkera_cli.harness._fake import FakeAdapter
from alkera_cli.harness.adapters.claude_agent import _descriptor_for_tool
from alkera_cli.harness.adapters.opencode_translate import (
    OpencodeEventTranslator,
    _TranslatorContext,
)
from alkera_cli.harness.permission_broker import PermissionBroker
from alkera_cli.harness.runtime import HarnessRuntime
from alkera_cli.plugins.plugin_base.permissions import (
    CREDENTIAL_PATH_GATE_ENV,
    credential_path_gate_enabled,
    gate_shell_action,
)
from alkera_cli.plugins.plugin_base.permissions.gate import READ_ONLY_SHELL_REASON, GateBinding
from alkera_core.project.directory import ProjectDirectory
from alkera_core.schemas.chat import PermissionRequest

_T = datetime(2026, 9, 16, tzinfo=UTC)
WORKSPACE = Path("/Users/someone/repo")


@pytest.fixture(autouse=True)
def _switch_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """The default: nothing in the environment names the switch."""
    monkeypatch.delenv(CREDENTIAL_PATH_GATE_ENV, raising=False)


class _Broker:
    def __init__(self, option: str) -> None:
        self._option = option
        self.prompts = 0

    async def resolve(self, request: Any) -> str:
        self.prompts += 1
        return self._option


class _Sink:
    def record(self, record: Any) -> None:
        pass


def _opencode_ask(
    permission: str,
    patterns: list[str],
    metadata: dict[str, Any] | None = None,
    *,
    workspace_root: Path | None = WORKSPACE,
    sandbox_dir: Path | None = None,
) -> PermissionRequest:
    """A real ``permission.asked`` through the real translator — the shape the
    runtime resolves, not a hand-built descriptor."""
    ctx = _TranslatorContext(
        session_id="sid", workspace_root=workspace_root, sandbox_dir=sandbox_dir
    )
    event = OpencodeEventTranslator(ctx).translate(
        {
            "type": "permission.asked",
            "properties": {
                "id": f"perm-{permission}",
                "permission": permission,
                "patterns": patterns,
                "metadata": metadata if metadata is not None else {},
            },
        }
    )
    assert isinstance(event, PermissionRequest)
    return event


def _subject(
    permission: str,
    patterns: list[str],
    metadata: dict[str, Any] | None = None,
    *,
    workspace_root: Path | None = WORKSPACE,
) -> dict[str, Any]:
    subject = _opencode_ask(permission, patterns, metadata, workspace_root=workspace_root).subject
    assert subject is not None
    return subject


# --------------------------------------------------------------------------- #
# The switch itself.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("value", "enabled"),
    [
        pytest.param(None, False, id="unset"),
        pytest.param("", False, id="empty"),
        pytest.param("0", False, id="zero"),
        pytest.param("false", False, id="false"),
        pytest.param("off", False, id="off"),
        pytest.param("no", False, id="no"),
        pytest.param("enabled", False, id="an-unknown-word-is-off"),
        pytest.param("1", True, id="one"),
        pytest.param("true", True, id="true"),
        pytest.param(" TRUE ", True, id="true-spaced-and-upper"),
        pytest.param("yes", True, id="yes"),
        pytest.param("on", True, id="on"),
    ],
)
def test_the_switch_is_off_unless_its_variable_names_a_true_word(
    monkeypatch: pytest.MonkeyPatch, value: str | None, enabled: bool
) -> None:
    if value is None:
        monkeypatch.delenv(CREDENTIAL_PATH_GATE_ENV, raising=False)
    else:
        monkeypatch.setenv(CREDENTIAL_PATH_GATE_ENV, value)
    assert credential_path_gate_enabled() is enabled


# --------------------------------------------------------------------------- #
# Off: every lane keeps the effect the tool gave the action.
# --------------------------------------------------------------------------- #

# (permission, patterns, metadata, workspace_root, the effect the tool itself has)
_OPENCODE_ASKS = [
    pytest.param(
        "read",
        ["/Users/someone/.alkera/auth.yml"],
        {},
        WORKSPACE,
        "read",
        id="read-the-gateway-token",
    ),
    pytest.param(
        "read", ["../../.ssh/id_ed25519"], {}, WORKSPACE, "read", id="read-an-ssh-key-relative"
    ),
    pytest.param(
        "edit",
        ["../.alkera/permissions.yml"],
        {"filepath": "/srv/app/.alkera/permissions.yml", "diff": "@@"},
        WORKSPACE,
        "write",
        id="edit-under-the-alkera-dir",
    ),
    pytest.param(
        "grep",
        ["aws_secret_access_key"],
        {"pattern": "aws_secret_access_key", "path": "/Users/someone/.aws"},
        WORKSPACE,
        "read",
        id="grep-the-aws-dir",
    ),
    pytest.param(
        "lsp",
        ["*"],
        {"operation": "definition", "filePath": "/etc/ssl/private/server.pem", "line": 1},
        WORKSPACE,
        "read",
        id="lsp-on-a-key-file",
    ),
    pytest.param("bash", ["cat ~/.ssh/id_rsa"], {}, WORKSPACE, "read", id="shell-read-of-a-key"),
    # The shell classifier itself calls this one a write; the point is that the
    # `gcloud auth` marker no longer raises it past that.
    pytest.param(
        "bash", ["gcloud auth print-access-token"], {}, WORKSPACE, "write", id="shell-gcloud-auth"
    ),
    pytest.param(
        "grep", ["TODO"], {"pattern": "TODO"}, None, "read", id="directory-walk-with-no-root"
    ),
    pytest.param(
        "grep",
        ["BEGIN"],
        {"pattern": "BEGIN"},
        Path("/Users/someone/.ssh"),
        "read",
        id="workspace-is-a-secret-dir",
    ),
]


@pytest.mark.parametrize(("permission", "patterns", "metadata", "root", "effect"), _OPENCODE_ASKS)
def test_opencode_keeps_the_effect_its_tool_gave_the_action(
    permission: str, patterns: list[str], metadata: dict[str, Any], root: Path | None, effect: str
) -> None:
    subject = _subject(permission, patterns, metadata, workspace_root=root)
    assert subject["effect"] == effect
    assert subject["reasons"] == [], "no path is named as a reason when the gate is off"


@pytest.mark.parametrize(
    ("tool", "tool_input", "root", "effect"),
    [
        pytest.param(
            "Read",
            {"file_path": "~/.alkera/auth.yml"},
            WORKSPACE,
            Effect.READ,
            id="read-the-gateway-token",
        ),
        pytest.param(
            "Read", {"file_path": ".env"}, WORKSPACE, Effect.READ, id="read-a-project-env"
        ),
        pytest.param(
            "Grep",
            {"pattern": "AKIA", "path": "/home/dev/.aws"},
            WORKSPACE,
            Effect.READ,
            id="grep-the-aws-dir",
        ),
        pytest.param(
            "Write",
            {"file_path": "/home/dev/.ssh/authorized_keys"},
            WORKSPACE,
            Effect.WRITE,
            id="write-under-ssh",
        ),
        pytest.param(
            "NotebookEdit",
            {"notebook_path": "/home/dev/.alkera/n.ipynb"},
            WORKSPACE,
            Effect.WRITE,
            id="edit-under-the-alkera-home",
        ),
        pytest.param("Grep", {"pattern": "x"}, None, Effect.READ, id="directory-walk-with-no-root"),
    ],
)
def test_the_claude_lane_keeps_the_effect_its_tool_gave_the_action(
    tool: str, tool_input: dict[str, Any], root: Path | None, effect: Effect
) -> None:
    descriptor = _descriptor_for_tool(
        tool, tool_input, sandbox_dir=WORKSPACE / ".alkera/chats/c1/sandbox", workspace_root=root
    )
    assert descriptor is not None
    assert descriptor.effect == effect
    assert descriptor.reasons == []


async def _shell(command: str, *, mode: str, broker: Any) -> Any:
    return await gate_shell_action(
        command, mode=mode, binding=GateBinding(decision_sink=_Sink(), broker=broker)
    )


async def test_a_shell_read_of_a_secret_path_is_a_plain_read_in_default() -> None:
    """The shell lane: the read corpus decides, so ``cat`` of the token file runs
    without a prompt — the mode alone governs it."""
    broker = _Broker("reject_once")  # would reject IF it were ever asked
    res = await _shell("cat ~/.alkera/auth.yml", mode="default", broker=broker)
    assert res.allowed is True
    assert broker.prompts == 0


@pytest.mark.parametrize("mode", ["read_only", "plan"])
async def test_the_no_shell_modes_still_refuse_it_by_mode(mode: str) -> None:
    broker = _Broker("allow_once")  # would allow IF it were ever asked
    res = await _shell("cat ~/.ssh/id_rsa", mode=mode, broker=broker)
    assert res.allowed is False
    assert broker.prompts == 0
    assert res.reason == READ_ONLY_SHELL_REASON


async def test_the_destroy_floor_is_not_the_credential_gate() -> None:
    """The asymmetric half: only the path escalation is off. A destructive command
    naming a secret dir is still a DESTROY, and still asked about."""
    broker = _Broker("reject_once")
    res = await _shell("rm -rf ~/.ssh", mode="default", broker=broker)
    assert res.allowed is False
    assert broker.prompts == 1


# --------------------------------------------------------------------------- #
# The ask the reader saw refused, and what decides it now.
# --------------------------------------------------------------------------- #


def _cloud_box(tmp_path: Path) -> tuple[Path, Path, Path]:
    """The layout of a cloud box: the workspace the mirror serves, and a chat
    sandbox under its ``.alkera``. opencode's worktree there is ``/``, so it
    reports every path relative to the filesystem root — no leading slash."""
    workspace = tmp_path / "opt" / "alkera-work"
    sandbox = workspace / ".alkera" / "chats" / "faf72c93" / "sandbox"
    sandbox.mkdir(parents=True)
    plan = sandbox / "plan.md"
    return workspace, sandbox, plan


def _as_opencode_spells_it(path: Path) -> str:
    """The spelling the reader saw: forward slashes, no leading slash, and on
    Windows no drive letter either — opencode's worktree on the box is the
    filesystem root, so every path it reports is relative to that."""
    return re.sub(r"^(?:[A-Za-z]:)?/*", "", path.as_posix())


def test_the_plan_file_write_the_reader_saw_refused_is_a_write(tmp_path: Path) -> None:
    workspace, sandbox, plan = _cloud_box(tmp_path)
    subject = _opencode_ask(
        "edit",
        [_as_opencode_spells_it(plan)],
        {"filepath": str(plan), "diff": "@@"},
        workspace_root=workspace,
        sandbox_dir=sandbox,
    ).subject
    assert subject is not None
    assert subject["effect"] == "write"
    assert subject["reasons"] == []


def test_the_switch_brings_that_refusal_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same ask with the gate on is the bug: the relative spelling, joined to
    a workspace root that is not opencode's ``/`` worktree, lands outside the
    sandbox carve-out and is raised to EGRESS. Pinned so the cause stays on
    record for whoever turns the gate back on."""
    monkeypatch.setenv(CREDENTIAL_PATH_GATE_ENV, "1")
    workspace, sandbox, plan = _cloud_box(tmp_path)
    subject = _opencode_ask(
        "edit",
        [_as_opencode_spells_it(plan)],
        {"filepath": str(plan), "diff": "@@"},
        workspace_root=workspace,
        sandbox_dir=sandbox,
    ).subject
    assert subject is not None
    assert subject["effect"] == "egress"
    assert any("credentials or private keys" in r for r in subject["reasons"])


class _Rig:
    def __init__(self, runtime: HarnessRuntime, session: Any, adapter: FakeAdapter) -> None:
        self.runtime = runtime
        self.session = session
        self.adapter = adapter
        self.prompted: list[PermissionRequest] = []

    @property
    def sandbox(self) -> Path:
        binding = self.session.tool_binding
        assert binding is not None and binding.sandbox_dir is not None
        return Path(binding.sandbox_dir)

    async def ask(self, request: PermissionRequest) -> str:
        await self.adapter.feed(request)
        for _ in range(250):
            for replied, option in self.adapter.permission_replies:
                if replied == request.request_id:
                    return str(option)
            await asyncio.sleep(0.02)
        raise AssertionError(f"no answer for {request.request_id}")

    def reason(self, request_id: str) -> str | None:
        return next(r for rid, r in self.adapter.permission_reply_reasons if rid == request_id)


@pytest.fixture
async def rig(tmp_path: Path) -> Any:
    """A real session over a fake adapter: the ask goes through the translator
    and the runtime's own decision chokepoint, and a prompt the policy raises
    reaches this broker."""
    factory = FakeAdapterFactory(lambda: FakeAdapter(reply_text="done"), available=True)
    workspace = tmp_path / "work"
    workspace.mkdir()
    runtime = HarnessRuntime(ProjectDirectory(workspace / ".alkera"), adapter_factory=factory)
    holder: list[_Rig] = []

    async def _resolver(req: Any) -> str:
        holder[0].prompted.append(req)
        return "allow_once"

    broker = PermissionBroker(_resolver, default_timeout_seconds=2.0)
    session = await runtime.open_chat(create=True, harness_type="agent", permission_broker=broker)
    session._last_user_text = "plan the work"
    holder.append(_Rig(runtime, session, factory.adapters[0]))
    try:
        yield holder[0]
    finally:
        await runtime.close_chat(session.session_id)


def _edit_ask(rig: _Rig, target: Path, request_id: str) -> PermissionRequest:
    """The edit ask as the adapter builds it for this session, with the path in
    the spelling opencode uses when its worktree is ``/``."""
    ask = _opencode_ask(
        "edit",
        [_as_opencode_spells_it(target)],
        {"filepath": str(target), "diff": "@@"},
        workspace_root=rig.runtime.project.path.parent,
        sandbox_dir=rig.sandbox,
    )
    return ask.model_copy(update={"request_id": request_id, "session_id": rig.session.session_id})


async def test_in_plan_mode_the_sandbox_plan_file_is_admitted_and_nobody_is_asked(
    rig: _Rig,
) -> None:
    rig.session.set_permission_mode("plan")
    assert await rig.ask(_edit_ask(rig, rig.sandbox / "plan.md", "req-plan")) == "allow_once"
    assert rig.prompted == []


@pytest.mark.parametrize(
    ("mode", "outcome", "asked"),
    [
        pytest.param("plan", "reject_once", 0, id="plan-refuses-by-mode"),
        pytest.param("read_only", "reject_once", 0, id="read_only-refuses-by-mode"),
        pytest.param("default", "allow_once", 1, id="default-asks-the-human"),
    ],
)
async def test_a_project_write_is_decided_by_the_mode_alone(
    rig: _Rig, mode: str, outcome: str, asked: int
) -> None:
    rig.session.set_permission_mode(mode)
    project_file = rig.runtime.project.path.parent / "src" / "main.py"
    assert await rig.ask(_edit_ask(rig, project_file, f"req-{mode}")) == outcome
    assert len(rig.prompted) == asked
    if outcome == "reject_once":
        reason = rig.reason(f"req-{mode}")
        assert reason is not None and "mode" in reason
        assert "credentials" not in reason


# --------------------------------------------------------------------------- #
# On: the escalation is back on every lane.
# --------------------------------------------------------------------------- #


def test_the_switch_restores_the_opencode_escalation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(CREDENTIAL_PATH_GATE_ENV, "1")
    assert _subject("read", ["/Users/someone/.alkera/auth.yml"])["effect"] == "egress"
    walk = _subject("grep", ["TODO"], {"pattern": "TODO"}, workspace_root=None)
    assert walk["effect"] == "egress"
    assert any("cannot rule out" in r for r in walk["reasons"])


def test_the_switch_restores_the_claude_escalation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(CREDENTIAL_PATH_GATE_ENV, "1")
    descriptor = _descriptor_for_tool(
        "Read", {"file_path": "~/.alkera/auth.yml"}, sandbox_dir=None, workspace_root=WORKSPACE
    )
    assert descriptor is not None
    assert descriptor.effect == Effect.EGRESS


async def test_the_switch_restores_the_shell_escalation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(CREDENTIAL_PATH_GATE_ENV, "1")
    broker = _Broker("reject_once")
    res = await _shell("cat ~/.alkera/auth.yml", mode="default", broker=broker)
    assert res.allowed is False
    assert broker.prompts == 1


# --------------------------------------------------------------------------- #
# On: the escalation only ever raises a tier. An EXEC that names a secret path
# stays EXEC on every lane, because EGRESS is below it and bypass waives EGRESS.
# --------------------------------------------------------------------------- #

_EXEC_NAMING_A_KEY = "psql -c \"COPY t FROM PROGRAM 'cat ~/.ssh/id_rsa'\""


def test_opencode_keeps_an_exec_that_names_a_secret_path_exec(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(CREDENTIAL_PATH_GATE_ENV, "1")
    assert _subject("bash", [_EXEC_NAMING_A_KEY])["effect"] == "exec"
    # Inverted: a read naming the same key is raised, so the switch is on.
    assert _subject("bash", ["cat ~/.ssh/id_rsa"])["effect"] == "egress"


def test_the_claude_lane_keeps_an_exec_that_names_a_secret_path_exec(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Claude lane escalates what its file tools name; no file tool is EXEC
    today, so the invariant is pinned on the escalation itself: a tier above EGRESS
    comes back untouched, a read naming the same key is raised."""
    from alkera_cli.harness.sensitive_paths import escalate_sensitive_path
    from alkera_cli.plugins.plugin_base.permissions import classify_command

    monkeypatch.setenv(CREDENTIAL_PATH_GATE_ENV, "1")
    kwargs: dict[str, Any] = {"sandbox_dir": None, "workspace_root": WORKSPACE}
    key = ["/home/dev/.ssh/id_rsa"]
    exec_d = classify_command(_EXEC_NAMING_A_KEY)
    assert exec_d.effect == Effect.EXEC
    assert escalate_sensitive_path(exec_d, key, **kwargs).effect == Effect.EXEC
    read_d = classify_command("cat notes.md")
    assert escalate_sensitive_path(read_d, key, **kwargs).effect == Effect.EGRESS


async def test_the_shell_gate_keeps_an_exec_that_names_a_secret_path_exec(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lowered to EGRESS it would run in bypass; kept at EXEC, bypass refuses it."""
    monkeypatch.setenv(CREDENTIAL_PATH_GATE_ENV, "1")
    broker = _Broker("allow_once")
    res = await _shell(_EXEC_NAMING_A_KEY, mode="bypass", broker=broker)
    assert res.allowed is False
    assert broker.prompts == 0
    # Inverted: the read naming the same key is EGRESS, which bypass runs.
    assert (await _shell("cat ~/.ssh/id_rsa", mode="bypass", broker=broker)).allowed is True
