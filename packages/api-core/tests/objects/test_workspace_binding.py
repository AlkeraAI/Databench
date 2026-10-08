"""A workspace's binding, as a reader is told it, and the shape it is stored in.

Until the box runs one sandbox per workspace each chat's spec is the box's
truth, so a workspace's binding is derived from its chats; once the workspace
is the authority its own fields are read instead. Pure: plain specs in, a
binding out.
"""

from __future__ import annotations

import pytest
from alkera_core.objects.workspaces import (
    WorkspaceBinding,
    effective_binding,
    record_box_report,
    workspace_spec_of,
)
from alkera_core.schemas.objects import ChatSpec, WorkspaceSpec
from pydantic import ValidationError

M1 = "6a2c1b4d-3e1f-4b3c-8e6d-82b4d0a1c222"
M2 = "7b3d2c5e-4f20-4c4d-9f7e-93c5e1b2d333"
EARLY = "2026-10-04T09:00:00+00:00"
LATE = "2026-10-04T10:00:00+00:00"


def _writes(mode: str | None) -> bool:
    return mode not in ("read_only", "plan")


def _binding(*chats: ChatSpec) -> WorkspaceBinding:
    return effective_binding(list(chats), is_writable=_writes)


def _reported(spec: WorkspaceSpec, *chats: ChatSpec) -> WorkspaceBinding:
    return effective_binding(list(chats), is_writable=_writes, spec=spec)


def test_a_workspace_of_one_reads_exactly_as_its_chat() -> None:
    chat = ChatSpec(
        machine_id=M1,
        machine_status="ready",
        mirror_state="asleep",
        wake_requested_at=EARLY,
        permission_mode="default",
    )
    assert _binding(chat) == WorkspaceBinding(
        machine_id=M1,
        machine_status="ready",
        mirror_state="asleep",
        wake_requested_at=EARLY,
        spare=False,
        writable=True,
    )


def test_a_workspace_with_no_chat_names_no_machine_and_is_not_a_spare() -> None:
    assert _binding() == WorkspaceBinding(
        machine_id=None,
        machine_status="none",
        mirror_state=None,
        wake_requested_at=None,
        spare=False,
        writable=False,
    )


@pytest.mark.parametrize(
    ("machines", "named"),
    [
        pytest.param((M1, M1), M1, id="every-chat-on-one-machine"),
        pytest.param((M1, M2), None, id="chats-on-two-machines-name-none"),
        pytest.param((M1, None), None, id="one-chat-unbound-names-none"),
    ],
)
def test_a_machine_is_named_only_when_every_chat_is_on_it(
    machines: tuple[str | None, str | None], named: str | None
) -> None:
    chats = [ChatSpec(machine_id=m, machine_status="ready") for m in machines]
    assert _binding(*chats).machine_id == named


@pytest.mark.parametrize(
    ("states", "expected"),
    [
        pytest.param(("awake", "asleep"), "awake", id="any-awake-is-awake"),
        pytest.param(("asleep", "asleep"), "asleep", id="all-asleep-is-asleep"),
        pytest.param(("asleep", None), None, id="one-unreported-is-unknown"),
    ],
)
def test_the_workspace_is_awake_when_any_chat_is(
    states: tuple[str | None, str | None], expected: str | None
) -> None:
    chats = [ChatSpec.model_validate({"mirror_state": state}) for state in states]
    assert _binding(*chats).mirror_state == expected


def test_the_latest_standing_wake_is_the_workspaces() -> None:
    chats = [ChatSpec(wake_requested_at=EARLY), ChatSpec(), ChatSpec(wake_requested_at=LATE)]
    assert _binding(*chats).wake_requested_at == LATE


@pytest.mark.parametrize(
    ("spares", "expected"),
    [
        pytest.param((True,), True, id="its-only-chat-is-a-spare"),
        pytest.param((True, False), False, id="one-claimed-chat-shows-it"),
    ],
)
def test_it_is_a_spare_only_when_every_chat_is(spares: tuple[bool, ...], expected: bool) -> None:
    chats = [ChatSpec(spare=spare) for spare in spares]
    assert _binding(*chats).spare is expected


@pytest.mark.parametrize(
    ("modes", "expected"),
    [
        pytest.param(("read_only", "plan"), False, id="no-chat-writes"),
        pytest.param(("read_only", "bypass"), True, id="one-writing-chat-makes-it-writable"),
    ],
)
def test_it_needs_a_writable_machine_when_any_chat_may_write(
    modes: tuple[str, ...], expected: bool
) -> None:
    chats = [ChatSpec.model_validate({"permission_mode": mode}) for mode in modes]
    assert _binding(*chats).writable is expected


def test_once_the_box_reported_its_word_on_the_box_and_the_sandbox_is_read() -> None:
    """The box alone knows which box holds the workspace and whether its
    sandbox is up: a chat whose agent server stopped reads asleep while the
    sandbox stays up for a sibling, and the workspace must still read awake.
    What placement and the chats own stays derived: a stale copy of the
    machine's status or of writability on the workspace would drift."""
    spec = record_box_report(
        # A row an earlier build wrote, with copies of the chats' fields.
        WorkspaceSpec.model_validate({"machine_status": "draining", "writable": False}),
        machine_id=M2,
        sandbox_state="awake",
        memory_used_mb=640,
        at=LATE,
    )
    chats = (
        ChatSpec(machine_id=M1, machine_status="ready", mirror_state="asleep"),
        ChatSpec(
            machine_id=M1, machine_status="ready", mirror_state="asleep", permission_mode="bypass"
        ),
    )
    binding = _reported(spec, *chats)
    assert (binding.machine_id, binding.mirror_state, binding.sandbox_state) == (
        M2,
        "awake",
        "awake",
    )
    assert binding.machine_status == "ready"
    assert binding.writable is True
    assert (spec.binding_authority, spec.sandbox_memory_used_mb, spec.sandbox_reported_at) == (
        "workspace",
        640,
        LATE,
    )


@pytest.mark.parametrize(
    ("state", "mirror"),
    [
        pytest.param("waking", "awake", id="waking-reads-awake"),
        pytest.param("awake", "awake", id="awake"),
        pytest.param("asleep", "asleep", id="asleep-even-with-an-awake-chat"),
    ],
)
def test_the_sandbox_state_decides_whether_the_workspace_reads_awake(
    state: str, mirror: str
) -> None:
    spec = record_box_report(
        WorkspaceSpec(),
        machine_id=M1,
        sandbox_state=state,
        memory_used_mb=None,
        at=LATE,  # type: ignore[arg-type]
    )
    binding = _reported(spec, ChatSpec(machine_id=M1, mirror_state="awake"))
    assert binding.mirror_state == mirror
    assert binding.sandbox_state == state


def test_without_a_report_a_workspace_bound_by_its_chats_names_no_sandbox_state() -> None:
    binding = _reported(WorkspaceSpec(), ChatSpec(machine_id=M1, mirror_state="awake"))
    assert binding.sandbox_state is None
    assert binding.mirror_state == "awake"


def test_the_latest_wake_is_compared_as_an_instant_not_as_text() -> None:
    """``+02:00`` sorts after ``Z`` as text; as instants the UTC one is later."""
    earlier_but_larger_text = "2026-10-04T12:00:00+02:00"
    later = "2026-10-04T11:00:00+00:00"
    chats = [
        ChatSpec(wake_requested_at=earlier_but_larger_text),
        ChatSpec(wake_requested_at=later),
        ChatSpec(wake_requested_at="not a time"),
    ]
    assert _binding(*chats).wake_requested_at == later


def test_a_spec_from_nothing_is_a_native_project_bound_by_its_chats() -> None:
    spec = workspace_spec_of(None)
    assert (spec.kind, spec.layout, spec.adopted_chat_id) == ("project", "native", None)


def test_an_unknown_field_from_a_newer_writer_survives_a_round_trip() -> None:
    raw = {**WorkspaceSpec(kind="main").model_dump(mode="json"), "keeper_pid": 4242}
    assert WorkspaceSpec.model_validate(raw).model_dump(mode="json")["keeper_pid"] == 4242


@pytest.mark.parametrize(
    "bad",
    [
        pytest.param({"kind": "scratch"}, id="an-unknown-kind"),
        pytest.param({"layout": "moved"}, id="an-unknown-layout"),
    ],
)
def test_a_spec_a_writer_could_not_have_meant_is_refused(bad: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        WorkspaceSpec.model_validate(bad)


def test_a_chat_spec_from_before_workspaces_reads_with_no_workspace() -> None:
    old = {"schema_version": "1.9.0", "machine_id": M1, "machine_status": "ready"}
    spec = ChatSpec.model_validate(old)
    assert spec.workspace_id is None
    assert spec.schema_version == ChatSpec.SCHEMA_VERSION
    assert spec.model_dump(mode="json")["workspace_id"] is None
