"""A chat created in a stance is run in that stance by the box that opens it.

The reader's journey: the home composer's chip says ``Default``, the create
carries that stance, the row stores it, and the box — which may have started
long before the chat existed, and which reads the chat off the list or the
detail read rather than off the create — opens the harness session in it.
Between the row and the model there are four hand-offs, and any one of them
silently substituting the box's own floor is the bug a reader describes as
"I started one in default, but it thinks it's in read-only mode".

This file drives the box's half end to end with the record the backend really
emits (``ChatSessionRead``), through the production mirror factory and the
mirror's own ``start``: the stance the harness session runs in, the stance the
steering handed to the agent names, and — the observable difference — whether
an in-folder write is PARKED on a reader or refused by the mode before anyone
is asked.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytest
from _adapter_factory import FakeAdapterFactory
from alkera_cli.cloud import CloudRestClient, CloudSocket, DocHandle
from alkera_cli.cloud.mirror import ChatMirror
from alkera_cli.cloud.service import CloudMirrorService, MirrorSettings
from alkera_cli.contracts.tool_types import Effect
from alkera_cli.harness import HarnessRuntime
from alkera_cli.harness._fake import FakeAdapter
from alkera_cli.harness.permission_mode import MODE_LABELS
from alkera_cli.host import paths
from alkera_cli.plugins.plugin_base.permissions.config import (
    PermissionRule,
    PermissionsConfig,
    save_permissions,
)
from alkera_core.project.directory import ProjectDirectory
from alkera_core.schemas.chat import (
    PermissionOption,
    PermissionRequest,
    QuestionOption,
    QuestionPrompt,
    QuestionRequest,
)
from alkera_core.schemas.objects.api import ChatSessionRead

_T = datetime(2026, 9, 15, tzinfo=UTC)
CHAT_ID = "7b1c8c2e-4a4e-4f5e-9c0a-0f1a2b3c4d5e"
OWNER = "00000000-0000-4000-8000-000000000001"


class _LiveSocket(CloudSocket):
    """A socket whose document is live the moment it is opened: the subject is
    the session the mirror starts, not the transport."""

    def open_doc(self, doc_type: Any, doc_id: str, *, presence: bool = True) -> DocHandle:
        handle = super().open_doc(doc_type, doc_id, presence=presence)
        handle.can_write = True
        handle.live.set()
        return handle


def _record(permission_mode: str) -> dict[str, Any]:
    """The chat as ``GET /api/v1/chats`` (an item) and ``GET /api/v1/chats/{id}``
    both serialize it — the only two reads a box opens a chat from."""
    return ChatSessionRead(
        id=UUID(CHAT_ID),
        title="Ops",
        owner_user_id=UUID(OWNER),
        machine_id="machine:x",
        machine_status="ready",
        created_at=_T,
        updated_at=_T,
        last_seq=0,
        permission_mode=permission_mode,  # type: ignore[arg-type]
    ).model_dump(mode="json")


def _write_ask(*, raw: str, request_id: str) -> PermissionRequest:
    return PermissionRequest(
        event_id=f"ev-{request_id}",
        time=_T,
        session_id=CHAT_ID,
        request_id=request_id,
        tool_call_id=f"call-{request_id}",
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


class _Box:
    def __init__(
        self, service: CloudMirrorService, factory: FakeAdapterFactory, runtime: HarnessRuntime
    ) -> None:
        self.service = service
        self.factory = factory
        self.runtime = runtime
        self.mirror: ChatMirror | None = None

    async def open(self, record: dict[str, Any]) -> ChatMirror:
        """Open the chat the way the box does once it has read the record:
        the production factory, then the mirror's own start."""
        self.mirror = self.service._default_mirror(CHAT_ID, record)
        await self.mirror.start()
        return self.mirror

    @property
    def adapter(self) -> FakeAdapter:
        return self.factory.adapters[0]

    async def ask(self, request: PermissionRequest) -> str:
        assert self.mirror is not None
        await self.adapter.feed(request)
        deadline = asyncio.get_running_loop().time() + 5.0
        while asyncio.get_running_loop().time() < deadline:
            for replied, option in self.adapter.permission_replies:
                if replied == request.request_id:
                    return str(option)
            if request.request_id in self.mirror.pending_interrupts:
                return "parked"
            await asyncio.sleep(0.02)
        raise AssertionError(f"no answer for {request.request_id}")


@pytest.fixture
async def box(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[_Box]:
    workspace = tmp_path / "work"
    (workspace / "src").mkdir(parents=True)
    home = tmp_path / "alkera-home"
    home.mkdir()
    monkeypatch.setattr(paths, "ALKERA_HOME", home)
    factory = FakeAdapterFactory(FakeAdapter, available=True)
    runtime = HarnessRuntime(ProjectDirectory(workspace / ".alkera"), adapter_factory=factory)
    # Both effects ask, so a filesystem write reaches the mirror's resolver
    # whenever the MODE lets it — what is under test is the mode's decision,
    # never the rule table quietly auto-allowing the write.
    save_permissions(
        workspace / ".alkera",
        PermissionsConfig(
            rules=[
                PermissionRule(capability="fs", effect=Effect.READ, decision="ask"),
                PermissionRule(capability="fs", effect=Effect.WRITE, decision="ask"),
            ]
        ),
    )
    rest = CloudRestClient(
        api_url="http://objects.test",
        token="device-jwt",
        agent_id="machine:x",
        transport=httpx.MockTransport(lambda _r: httpx.Response(404, json={})),
    )
    settings = MirrorSettings(
        api_url="http://objects.test",
        token="device-jwt",
        project_dir=workspace,
        machine_name="demo box",
        user_id=OWNER,
        provider_pod_id="pod-demo",
        machine_type_code="cpu3c",
    )
    service = CloudMirrorService(settings, runtime, rest=rest, socket=_LiveSocket(rest))
    handle = _Box(service, factory, runtime)
    try:
        yield handle
    finally:
        if handle.mirror is not None:
            await handle.mirror.stop()


#: The sentence each stance's steering opens with — the words the agent reads.
#: ``plan`` carries its own richer prompt rather than the "You are in … mode"
#: template the other two restate.
STEERING = {
    "default": f"You are in {MODE_LABELS['default']} mode",
    "read_only": f"You are in {MODE_LABELS['read_only']} mode",
    "plan": "Plan mode is READ-ONLY this turn",
    "bypass": f"You are in {MODE_LABELS['bypass']} mode",
}


def _narrow_the_sandbox(mirror: ChatMirror) -> None:
    """The working directory is both the write fence and the sandbox, so a file
    write there is admitted in every mode. The stance decides a write that is
    inside the fence and outside the sandbox, so the sandbox is narrowed to a
    subdirectory to leave the rest of the working directory to the mode."""
    assert mirror.session is not None and mirror.session.tool_binding is not None
    mirror.session.tool_binding.sandbox_dir = mirror.working_dir / "plans"


@pytest.mark.parametrize(
    ("stored", "outcome"),
    [
        pytest.param("default", "parked", id="default: the write is the reader's to answer"),
        pytest.param("read_only", "reject_once", id="read_only: the mode refuses it unasked"),
        pytest.param("plan", "reject_once", id="plan: the mode refuses it unasked"),
        pytest.param("bypass", "allow_once", id="bypass: the mode allows it unasked"),
    ],
)
async def test_the_box_runs_a_chat_in_the_stance_its_record_was_created_with(
    box: _Box, stored: str, outcome: str
) -> None:
    """The record says one word; the harness session, the steering the agent is
    handed and the fate of an in-folder write all have to say the same one.

    ``default`` is the case the reader hit: the ask must be PARKED on a reader
    (nobody has answered it, and the harness has not been told no), and the
    agent must have been told it is in default mode — not read-only. The two
    analyst stances are the control: the same write is refused by the mode
    before any reader is asked, with the mode named as the decider. ``bypass``
    is the other end of the same axis: the write runs and nobody is asked, which
    is the whole reason a reader picks it — and if it silently parked instead,
    the unattended job it was picked for would stall on an ask nobody is there
    to answer.
    """
    mirror = await box.open(_record(stored))

    assert mirror.permission_mode == stored
    assert mirror.session is not None
    assert mirror.session.permission_mode == stored, "the session runs in the record's stance"

    # The stance reaches the model as steering on the prompt, not only as
    # policy: a session in `default` that told the agent it was read-only would
    # make the agent refuse work the policy would have offered a reader.
    await mirror.session.send_prompt("create notes.txt containing hello in this folder")
    steering = "\n".join(p.system or "" for p in box.adapter.sent_prompts)
    assert STEERING[stored] in steering
    for other, sentence in STEERING.items():
        if other != stored:
            assert sentence not in steering, other

    _narrow_the_sandbox(mirror)
    target = str(mirror.working_dir / "notes.txt")
    assert await box.ask(_write_ask(raw=target, request_id="req-w")) == outcome
    if outcome == "parked":
        assert box.adapter.permission_replies == [], "nobody has answered a parked ask"
        assert box.adapter.permission_reply_reasons == []
    elif outcome == "allow_once":
        assert mirror.pending_interrupts == [], "a mode allow is never parked on a reader"
    else:
        assert mirror.pending_interrupts == [], "a mode refusal is never parked on a reader"
        (reason,) = [r for rid, r in box.adapter.permission_reply_reasons if rid == "req-w"]
        assert reason is not None and "mode" in reason, reason


async def test_a_record_with_no_stance_opens_at_the_floor_not_the_local_manifests_default(
    box: _Box,
) -> None:
    """A chat from before the stance existed reads as read-only on the box even
    though the LOCAL chat manifest the mirror creates defaults to ``default``:
    the record decides, and the manifest is written from it, never the other
    way round."""
    record = _record("default")
    del record["permission_mode"]

    mirror = await box.open(record)

    assert mirror.permission_mode == "read_only"
    assert mirror.session is not None
    assert mirror.session.permission_mode == "read_only"
    _narrow_the_sandbox(mirror)
    target = str(mirror.working_dir / "notes.txt")
    assert await box.ask(_write_ask(raw=target, request_id="req-floor")) == "reject_once"


async def test_in_plan_mode_the_box_admits_the_plan_file_and_parks_the_plan_on_the_reader(
    box: _Box,
) -> None:
    """Plan mode's own steering tells the agent to write ``plan.md`` into its
    sandbox and hand that path to the plan tool. A reader saw the box refuse
    exactly that write ("auto-rejects egress actions … path may hold
    credentials"), so the plan never reached them. The sandbox the steering
    names is the sandbox the binding fences, and a write into it is the
    sandbox's to admit — in plan mode, with nobody asked; the plan the tool
    then presents is parked on the reader, not refused by the mode, and is on
    the transcript with its Markdown, which is what the plan page renders."""
    mirror = await box.open(_record("plan"))
    assert mirror.session is not None
    binding = mirror.session.tool_binding
    assert binding is not None and binding.sandbox_dir is not None
    sandbox = Path(binding.sandbox_dir)

    await mirror.session.send_prompt("plan the audit")
    steering = "\n".join(p.system or "" for p in box.adapter.sent_prompts)
    assert f"`{sandbox}/plan.md`" in steering, "the steering names the binding's sandbox"

    plan_file = str(sandbox / "plan.md")
    assert await box.ask(_write_ask(raw=plan_file, request_id="req-plan")) == "allow_once"
    assert mirror.pending_interrupts == [], "the sandbox admits it; no reader is asked"
    (reason,) = [r for rid, r in box.adapter.permission_reply_reasons if rid == "req-plan"]
    assert reason is None

    presented = QuestionRequest(
        event_id="ev-present",
        time=_T,
        session_id=CHAT_ID,
        request_id="req-present",
        tool_call_id="call-present",
        questions=[
            QuestionPrompt(
                question="# Audit plan\n\n1. Read the marts.",
                header="alkera:plan-approval",
                options=[QuestionOption(label="Accept — run normally (ask before each change)")],
            )
        ],
        kind="plan_approval",
        plan_markdown="# Audit plan\n\n1. Read the marts.",
    )
    await box.adapter.feed(presented)
    deadline = asyncio.get_running_loop().time() + 5.0
    while "req-present" not in mirror.pending_interrupts:
        assert asyncio.get_running_loop().time() < deadline, "the plan never reached the reader"
        await asyncio.sleep(0.02)
    assert box.adapter.question_rejects == [], "a plan is parked on the reader, never refused"
    on_record = [
        ev
        for ev in box.runtime.project.chats().read_events(CHAT_ID)
        if isinstance(ev, QuestionRequest) and ev.request_id == "req-present"
    ]
    assert [ev.plan_markdown for ev in on_record] == ["# Audit plan\n\n1. Read the marts."]
    assert on_record[0].kind == "plan_approval"
