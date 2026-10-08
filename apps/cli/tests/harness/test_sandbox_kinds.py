"""A workspace sandbox of a registered kind (a notebook kernel, say) runs as
the workspace's tree identity, is named after itself alone, and is refused
where the box cannot run its mode."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from alkera_cli.harness.sandbox import (
    SandboxRefusedError,
    SandboxSettings,
    SandboxSpec,
)
from alkera_cli.harness.sandbox_kinds import (
    SandboxIdentity,
    WorkspaceSandboxRequest,
    box_tools,
    plan_workspace_sandbox,
    register_workspace_sandbox,
    sandbox_identity,
    unregister_workspace_sandbox,
    workspace_container,
    workspace_sandbox_kinds,
)
from alkera_cli.harness.sandbox_layout import chat_container, chat_netns, chat_slice
from alkera_cli.harness.sandbox_probe import SandboxCapability
from alkera_cli.harness.sandbox_scope import scope_for

WORKSPACE = "ws-6f1c1d2e-0000-4000-8000-00000000abcd"
MEMBER = "c1a2b3c4-0000-4000-8000-000000000001"

NONE_BOX = SandboxCapability(
    platform="linux",
    root=True,
    setpriv="/usr/bin/setpriv",
    setfacl="/usr/bin/setfacl",
    runsc=None,
    cgroup="cgroupfs",
    reason="injected: none",
    ip="/usr/sbin/ip",
    nft="/usr/sbin/nft",
    unshare="/usr/bin/unshare",
)


@dataclass(frozen=True)
class ProbeKind:
    """A stand-in kind: its container is the workspace root and nothing else."""

    name: str = "probe"

    def spec(
        self,
        request: WorkspaceSandboxRequest,
        *,
        identity: SandboxIdentity,
        settings: SandboxSettings,
        cap: SandboxCapability,
    ) -> SandboxSpec:
        return SandboxSpec(
            chat_id=identity.container,
            folder=request.folder,
            uid=identity.uid,
            net_uid=identity.net_uid,
            vcpu=request.vcpu or 1,
            memory_mb=request.memory_mb or 512,
            home=settings.home,
            mode=settings.mode,
            cgroup=cap.cgroup,
            **box_tools(cap),
        )

    def argv(self, request: WorkspaceSandboxRequest, spec: SandboxSpec) -> tuple[str, ...]:
        return ("/usr/bin/kernel", "--root", str(request.folder))


@dataclass(frozen=True)
class ImpostorKind(ProbeKind):
    """A kind whose spec names another sandbox's identity."""

    name: str = "impostor"

    def spec(
        self,
        request: WorkspaceSandboxRequest,
        *,
        identity: SandboxIdentity,
        settings: SandboxSettings,
        cap: SandboxCapability,
    ) -> SandboxSpec:
        own = ProbeKind.spec(self, request, identity=identity, settings=settings, cap=cap)
        from dataclasses import replace

        return replace(own, chat_id=MEMBER)


class Ledger:
    """A uid per name, handed out in order, as an org worker's ledger does."""

    def __init__(self) -> None:
        self.uids: dict[str, int] = {}

    def __call__(self, name: str) -> int:
        return self.uids.setdefault(name, 20000 + len(self.uids))


@pytest.fixture
def kinds() -> Iterator[None]:
    register_workspace_sandbox(ProbeKind())
    register_workspace_sandbox(ProbeKind(name="other"))
    register_workspace_sandbox(ImpostorKind())
    yield
    for name in ("probe", "other", "impostor"):
        unregister_workspace_sandbox(name)


def _request(tmp_path: Path) -> WorkspaceSandboxRequest:
    return WorkspaceSandboxRequest(
        workspace=WORKSPACE, folder=tmp_path / "ws", state_dir=tmp_path / "state"
    )


def _settings(mode: str = "none") -> SandboxSettings:
    return SandboxSettings.from_env({"ALKERA_SANDBOX_MODE": mode})


def test_a_workspace_sandbox_runs_as_the_identity_its_members_run_as(
    tmp_path: Path, kinds: None
) -> None:
    """Every member's processes run as the workspace's tree uid so each reads
    and writes every shared file; the kernel must too, and no other uid."""
    ledger = Ledger()
    member = scope_for(MEMBER, {"sandbox_scope": WORKSPACE}, topology="per_chat")
    member_identity = sandbox_identity(member.tree, member.container, ensure=ledger)

    plan = plan_workspace_sandbox(
        "probe", _request(tmp_path), settings=_settings(), cap=NONE_BOX, ensure=ledger
    )

    assert plan.spec.uid == member_identity.uid
    assert plan.spec.net_uid is not None
    assert plan.spec.net_uid not in (member_identity.uid, member_identity.net_uid)


def test_its_scope_and_network_are_its_own_never_a_members_or_another_kinds(
    tmp_path: Path, kinds: None
) -> None:
    ledger = Ledger()
    probe = plan_workspace_sandbox(
        "probe", _request(tmp_path), settings=_settings(), cap=NONE_BOX, ensure=ledger
    )
    other = plan_workspace_sandbox(
        "other", _request(tmp_path), settings=_settings(), cap=NONE_BOX, ensure=ledger
    )
    owners = (probe.spec.chat_id, other.spec.chat_id, MEMBER, WORKSPACE)
    for name_of in (chat_container, chat_slice, chat_netns):
        assert len({name_of(owner) for owner in owners}) == len(owners)
    assert probe.spec.network.host_if != other.spec.network.host_if


def test_the_launch_is_the_boxs_runtime_over_the_kinds_argv(tmp_path: Path, kinds: None) -> None:
    plan = plan_workspace_sandbox(
        "probe", _request(tmp_path), settings=_settings(), cap=NONE_BOX, ensure=Ledger()
    )
    launch = plan.launch({"KERNEL": "1"})
    assert plan.argv == ("/usr/bin/kernel", "--root", str(tmp_path / "ws"))
    assert f"--reuid={plan.spec.uid}" in launch.prefix
    assert launch.cwd == tmp_path / "ws"


def test_a_gvisor_box_that_cannot_run_runsc_refuses_rather_than_downgrades(
    tmp_path: Path, kinds: None
) -> None:
    with pytest.raises(SandboxRefusedError, match="gVisor"):
        plan_workspace_sandbox(
            "probe",
            _request(tmp_path),
            settings=_settings("gvisor"),
            cap=NONE_BOX,
            ensure=Ledger(),
        )


def test_a_kind_whose_spec_is_not_its_own_identity_is_refused(tmp_path: Path, kinds: None) -> None:
    with pytest.raises(SandboxRefusedError, match="not its own identity"):
        plan_workspace_sandbox(
            "impostor", _request(tmp_path), settings=_settings(), cap=NONE_BOX, ensure=Ledger()
        )


def test_an_unknown_kind_is_refused(tmp_path: Path) -> None:
    # A name no kind is registered under: ``kernel`` is the notebook lane's,
    # registered in any process that has made a kernel sandbox.
    with pytest.raises(SandboxRefusedError, match="no workspace sandbox of kind"):
        plan_workspace_sandbox(
            "unregistered", _request(tmp_path), settings=_settings(), cap=NONE_BOX, ensure=Ledger()
        )


@pytest.mark.parametrize(
    "name",
    [
        pytest.param("agent", id="the-agent-is-planned-from-its-chat"),
        pytest.param("Kernel", id="uppercase"),
        pytest.param("ker nel", id="space"),
        pytest.param("", id="empty"),
    ],
)
def test_a_kind_name_that_could_be_confused_is_refused(name: str) -> None:
    with pytest.raises(ValueError, match="cannot name"):
        register_workspace_sandbox(ProbeKind(name=name))


def test_a_kind_is_registered_once(kinds: None) -> None:
    assert {"probe", "other"} <= set(workspace_sandbox_kinds())
    with pytest.raises(ValueError, match="already registered"):
        register_workspace_sandbox(ProbeKind())


def test_a_workspace_sandbox_needs_its_workspace() -> None:
    with pytest.raises(SandboxRefusedError):
        workspace_container("  ", "probe")
    assert workspace_container(WORKSPACE, "probe") != workspace_container(WORKSPACE, "other")
