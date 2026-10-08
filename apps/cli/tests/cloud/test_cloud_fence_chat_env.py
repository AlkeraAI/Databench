"""A cloud chat may install into its own default Python environment.

The sandbox makes every chat a uv environment under its runtime state
(``<chat>/.runtime/envs/alkera``), activates it, and the brief tells the model
to install there. The write fence used to bound writes to the working
directory alone and floor-refused every name under ``.alkera`` outside it, so
the environment the product made was one the chat could not install into, in
any stance: on staging a Bypass chat was refused ``uv pip install --python
<env>/bin/python …`` and fell back to a ``.venv`` inside its synced working
folder.

The fence is taken from :attr:`ChatMirror.session_fence` — the object the box
really builds — so each case is judged by production's bounds, not a hand-built
fence. The chat's own ``envs`` tree opens; the rest of its runtime state (the
agent's database is its record), every other chat's environment, and the rest
of the box stay refused in every stance.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from _adapter_factory import FakeAdapterFactory
from alkera_cli.cloud import CloudRestClient, CloudSocket, fence
from alkera_cli.cloud.mirror import ChatMirror
from alkera_cli.harness import HarnessRuntime
from alkera_cli.harness._fake import FakeAdapter
from alkera_cli.host import paths
from alkera_cli.plugins.plugin_base.permissions import gate_shell_action
from alkera_cli.plugins.plugin_base.permissions.gate import GateBinding
from alkera_core.project.directory import ProjectDirectory
from alkera_core.schemas.chat import PermissionOption, PermissionRequest

CHAT_ID = "chat-env"
OTHER_CHAT = "chat-other"
OWNER = "00000000-0000-4000-8000-000000000001"


class _Box:
    """A workspace with two chats, each with the default environment the
    sandbox makes, and the production fence of the first."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.workspace = tmp_path / "work"
        self.workspace.mkdir()
        self.alkera_home = tmp_path / "alkera-home"
        self.alkera_home.mkdir()
        (self.alkera_home / "auth.yml").write_text("token: secret\n")
        monkeypatch.setattr(paths, "ALKERA_HOME", self.alkera_home)
        runtime = HarnessRuntime(
            ProjectDirectory(self.workspace / ".alkera"),
            adapter_factory=FakeAdapterFactory(FakeAdapter, available=True),
        )
        for chat in (CHAT_ID, OTHER_CHAT):
            runtime.project.chats().create(session_id=chat, title="t", harness_type="agent").close()
        self.mirror = ChatMirror(
            chat_id=CHAT_ID,
            runtime=runtime,
            socket=cast(CloudSocket, object()),
            rest=CloudRestClient(
                api_url="http://objects.test",
                token="device-jwt",
                agent_id=CHAT_ID,
                transport=httpx.MockTransport(lambda _r: httpx.Response(404, json={})),
            ),
            user_id=OWNER,
            owner_user_id=OWNER,
        )
        self.chat = self.mirror.chat_folder
        self.working = self.mirror.working_dir
        self.working.mkdir(parents=True, exist_ok=True)
        (self.working / "notes.md").write_text("hello\n")
        self.runtime_state = self.chat / ".runtime"
        self.envs = self.runtime_state / "envs"
        self.env = self.envs / "alkera"
        (self.env / "bin").mkdir(parents=True)
        (self.env / "pyvenv.cfg").write_text("home = /opt/alkera/python/current/bin\n")
        (self.runtime_state / "agent").mkdir()
        (self.runtime_state / "agent" / "agent.db").write_text("")
        self.other_env = runtime.project.chats().path / OTHER_CHAT / ".runtime" / "envs" / "alkera"
        (self.other_env / "bin").mkdir(parents=True)
        self.fence = self.mirror.session_fence
        self.shell_env = {"HOME": str(self.working), "PWD": str(self.working), "PATH": "/usr/bin"}

    def spell(self, command: str) -> str:
        return command.format(
            env=self.env,
            envs=self.envs,
            chat=self.chat,
            runtime=self.runtime_state,
            other_env=self.other_env,
            home=self.alkera_home,
        )


@pytest.fixture
def box(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Box:
    return _Box(tmp_path, monkeypatch)


# --------------------------------------------------------------------------- #
# The shell, judged by the production fence
# --------------------------------------------------------------------------- #

OWN_ENV_INSIDE = [
    pytest.param("echo x > {env}/marker", id="write-into-the-env"),
    pytest.param("cp notes.md {env}/lib-notes.md", id="copy-into-the-env"),
    pytest.param("echo x > {envs}/.uv-cache/entry", id="write-the-cache-beside-it"),
    pytest.param("cat {env}/pyvenv.cfg", id="read-the-env"),
    pytest.param("cp {env}/pyvenv.cfg here.cfg", id="copy-out-of-the-env"),
]

STILL_ESCAPES = [
    pytest.param("echo x > {runtime}/agent/agent.db", "write", id="the-agents-database"),
    pytest.param("echo x > {runtime}/marker", "write", id="the-rest-of-the-runtime-state"),
    pytest.param("echo x > {chat}/manifest.json", "write", id="the-chats-manifest"),
    pytest.param("echo x > {other_env}/marker", "write", id="another-chats-env"),
    pytest.param("cat {other_env}/pyvenv.cfg", "read", id="read-another-chats-env"),
    pytest.param("cat {runtime}/agent/agent.db", "read", id="read-the-agents-database"),
    pytest.param("echo x > {env}/../../../../{other}/x", "write", id="dotdot-out-of-the-env"),
    pytest.param("cat {home}/auth.yml", "read", id="the-operators-home"),
    pytest.param("echo x > /tmp/elsewhere", "write", id="elsewhere-on-the-box"),
]


@pytest.mark.parametrize("command", OWN_ENV_INSIDE)
def test_the_chats_own_environment_is_inside_its_bounds(box: _Box, command: str) -> None:
    verdict = box.fence.judge_shell(box.spell(command), cwd=box.working, env=box.shell_env)
    assert verdict == fence.INSIDE, verdict


@pytest.mark.parametrize(("command", "bound"), STILL_ESCAPES)
def test_everything_beside_the_environment_stays_outside(
    box: _Box, command: str, bound: str
) -> None:
    spelled = box.spell(command.replace("{other}", OTHER_CHAT))
    verdict = box.fence.judge_shell(spelled, cwd=box.working, env=box.shell_env)
    assert verdict.escaped, verdict
    assert verdict.bound == bound


def test_a_link_planted_in_the_environment_is_judged_where_it_points(box: _Box) -> None:
    """The chat can write its environment, so it can plant a link there; the
    link is the location it points at, never a door through the env."""
    (box.env / "door").symlink_to(box.other_env)
    for command in (f"echo x > {box.env}/door/marker", f"cat {box.env}/door/pyvenv.cfg"):
        verdict = box.fence.judge_shell(command, cwd=box.working, env=box.shell_env)
        assert verdict.escaped, (command, verdict)


# --------------------------------------------------------------------------- #
# The in-tool gate, in the stance the staging chat ran
# --------------------------------------------------------------------------- #


class _Sink:
    def __init__(self) -> None:
        self.records: list[Any] = []

    def record(self, rec: Any) -> None:
        self.records.append(rec)


INSTALLS = [
    pytest.param(
        "uv pip install --python {env}/bin/python matplotlib numpy -q", id="uv-pip-python-flag"
    ),
    pytest.param(
        "VENV={env} && uv pip install --python $VENV/bin/python matplotlib",
        id="through-an-assignment",
    ),
    pytest.param("{env}/bin/pip install matplotlib", id="the-envs-own-pip"),
]


@pytest.mark.parametrize("command", INSTALLS)
async def test_bypass_installs_into_the_chats_environment(box: _Box, command: str) -> None:
    """The staging refusal, replayed: every one of these was refused by the
    fence in Bypass ("… is outside this chat's sandbox") before the fix."""
    sink = _Sink()
    res = await gate_shell_action(
        box.spell(command),
        mode="bypass",
        binding=GateBinding(decision_sink=sink, broker=None, fence=box.fence),
        cwd=box.working,
        env=box.shell_env,
    )
    assert res.allowed is True, res.reason
    assert [rec.decided_by for rec in sink.records] != ["fence"]


@pytest.mark.parametrize("mode", ["default", "auto", "bypass"])
async def test_installing_into_another_chats_environment_is_refused_in_every_stance(
    box: _Box, mode: str
) -> None:
    sink = _Sink()
    res = await gate_shell_action(
        box.spell("uv pip install --python {other_env}/bin/python matplotlib"),
        mode=mode,
        binding=GateBinding(decision_sink=sink, broker=None, fence=box.fence),
        cwd=box.working,
        env=box.shell_env,
    )
    assert res.allowed is False
    assert res.reason and "workspace policy" in res.reason
    assert [(rec.decision, rec.decided_by) for rec in sink.records] == [("reject", "fence")]


# --------------------------------------------------------------------------- #
# A file tool's ask (the harness chokepoint)
# --------------------------------------------------------------------------- #


def _write_ask(raw: str) -> PermissionRequest:
    return PermissionRequest(
        event_id="ev-w",
        time="2026-09-29T00:00:00Z",
        session_id=CHAT_ID,
        request_id="req-w",
        tool_call_id="call-w",
        permission_kind="edit",
        canonical_kind="edit",
        patterns=[],
        subject={
            "capability": "fs",
            "effect": "write",
            "operation": "edit",
            "raw": raw,
            "targets": [{"kind": "file", "name": raw}],
            "classifier": "opencode-tool",
        },
        options=[
            PermissionOption(option_id="allow_once", name="Allow"),
            PermissionOption(option_id="reject_once", name="Reject"),
        ],
    )


@pytest.mark.parametrize(
    ("target", "inside"),
    [
        pytest.param("{env}/lib/sitecustomize.py", True, id="a-file-in-the-env"),
        pytest.param("{runtime}/agent/agent.db", False, id="the-agents-database"),
        pytest.param("{other_env}/lib/sitecustomize.py", False, id="another-chats-env"),
    ],
)
def test_a_file_tools_write_ask_follows_the_same_bound(
    box: _Box, target: str, inside: bool
) -> None:
    verdict = box.fence.judge_ask(_write_ask(box.spell(target)), writing=True)
    assert (verdict == fence.INSIDE) is inside, verdict
