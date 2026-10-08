"""The sensitive-path approval floor, and that both harness adapters apply it.

The floor raises a tool call that names a secret-bearing path to EGRESS so it
costs an approval, with the chat's own scratch dir exempt. It lives once in
``alkera_cli.harness.sensitive_paths``; these cases pin the floor itself (the
sandbox containment check refuses sibling-prefix and ``..`` tricks and follows
symlinks) and, through each adapter's real classification entry point, that a
read of ``~/.alkera/auth.yml`` is escalated on both the Claude and the opencode
lane.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from alkera_cli.contracts.tool_types import ActionDescriptor, Effect
from alkera_cli.harness.adapters.claude_agent import _descriptor_for_tool
from alkera_cli.harness.adapters.opencode_translate import (
    OpencodeEventTranslator,
    _TranslatorContext,
)
from alkera_cli.harness.sensitive_paths import (
    SENSITIVE_PATH_REASON,
    UNRESOLVED_PATH_REASON,
    escalate_sensitive_path,
    inside_sandbox,
)
from alkera_cli.plugins.plugin_base.permissions import CREDENTIAL_PATH_GATE_ENV
from alkera_core.schemas.chat import PermissionRequest

_WORKSPACE = Path("/work/project")
_SANDBOX = Path("/work/project/.alkera/chats/c1/sandbox")


@pytest.fixture(autouse=True)
def _credential_path_gate_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """The gate is off by default; these cases pin the mechanism with it on. The
    default-off contract lives in ``test_credential_path_gate_switch.py``."""
    monkeypatch.setenv(CREDENTIAL_PATH_GATE_ENV, "1")


def _read(target: str = "notes.md") -> ActionDescriptor:
    return ActionDescriptor(
        capability="fs", effect=Effect.READ, operation="read", raw=target, classifier="test"
    )


# --- the floor over a table of paths ----------------------------------------

_FLOOR_CASES = [
    pytest.param("~/.alkera/auth.yml", _SANDBOX, _WORKSPACE, True, id="gateway-token"),
    pytest.param("/home/dev/.ssh/id_ed25519", _SANDBOX, _WORKSPACE, True, id="ssh-key"),
    pytest.param("/home/dev/.aws/credentials", _SANDBOX, _WORKSPACE, True, id="aws-credentials"),
    pytest.param("/work/project/.env", _SANDBOX, _WORKSPACE, True, id="project-env"),
    pytest.param(f"{_SANDBOX}/plan.md", _SANDBOX, _WORKSPACE, False, id="inside-the-sandbox"),
    pytest.param(f"{_SANDBOX}", _SANDBOX, _WORKSPACE, False, id="the-sandbox-itself"),
    pytest.param(
        ".alkera/chats/c1/sandbox/plan.md",
        _SANDBOX,
        _WORKSPACE,
        False,
        id="relative-path-inside-the-sandbox",
    ),
    pytest.param(
        ".alkera/chats/c1/sandbox/plan.md",
        _SANDBOX,
        None,
        True,
        id="relative-path-with-no-root-to-resolve-against",
    ),
    pytest.param(
        f"{_SANDBOX}-evil/auth.yml", _SANDBOX, _WORKSPACE, True, id="sibling-sharing-a-prefix"
    ),
    pytest.param(
        f"{_SANDBOX}/../../../auth.yml", _SANDBOX, _WORKSPACE, True, id="dotdot-out-of-the-sandbox"
    ),
    pytest.param(
        f"{_SANDBOX}/../sandbox/plan.md", _SANDBOX, _WORKSPACE, False, id="dotdot-back-into-it"
    ),
    pytest.param(f"{_SANDBOX}/plan.md", None, _WORKSPACE, True, id="no-sandbox-dir"),
    pytest.param(f"{_SANDBOX}/plan.md", None, None, True, id="no-sandbox-and-no-root"),
    pytest.param("/work/project/src/main.py", _SANDBOX, _WORKSPACE, False, id="ordinary-file"),
]


@pytest.mark.parametrize(("target", "sandbox_dir", "workspace_root", "escalated"), _FLOOR_CASES)
def test_the_floor_escalates_a_secret_path_outside_the_sandbox(
    target: str, sandbox_dir: Path | None, workspace_root: Path | None, escalated: bool
) -> None:
    result = escalate_sensitive_path(
        _read(target), [target], sandbox_dir=sandbox_dir, workspace_root=workspace_root
    )
    if escalated:
        assert result.effect == Effect.EGRESS
        assert f"{SENSITIVE_PATH_REASON}: {target}" in result.reasons
    else:
        assert result.effect == Effect.READ
        assert result.reasons == []


@pytest.mark.parametrize(
    ("target", "inside"),
    [
        pytest.param(f"{_SANDBOX}/a/b.txt", True, id="nested"),
        pytest.param(f"{_SANDBOX}-evil", False, id="sibling-prefix"),
        pytest.param(f"{_SANDBOX}2/x", False, id="sibling-prefix-no-separator"),
        pytest.param(f"{_SANDBOX}/../x", False, id="dotdot-to-the-parent"),
        pytest.param(f"{_SANDBOX.parent}", False, id="the-parent"),
        pytest.param("bad\x00path", False, id="unresolvable-fails-closed"),
    ],
)
def test_sandbox_containment_compares_resolved_components(target: str, inside: bool) -> None:
    assert inside_sandbox(target, _SANDBOX, _WORKSPACE) is inside


def test_a_symlink_in_the_sandbox_to_a_secret_is_escalated(tmp_path: Path) -> None:
    home = tmp_path / ".alkera"
    sandbox = home / "chats" / "c1" / "sandbox"
    sandbox.mkdir(parents=True)
    (home / "auth.yml").write_text("token")
    link = sandbox / "innocent.txt"
    link.symlink_to(home / "auth.yml")

    result = escalate_sensitive_path(
        _read(str(link)), [str(link)], sandbox_dir=sandbox, workspace_root=tmp_path
    )

    assert result.effect == Effect.EGRESS


def test_a_symlink_that_lands_in_the_sandbox_stays_exempt(tmp_path: Path) -> None:
    home = tmp_path / ".alkera"
    sandbox = home / "chats" / "c1" / "sandbox"
    sandbox.mkdir(parents=True)
    (home / "scratch").symlink_to(sandbox)
    via_link = str(home / "scratch" / "plan.md")

    result = escalate_sensitive_path(
        _read(via_link), [via_link], sandbox_dir=sandbox, workspace_root=tmp_path
    )

    assert result.effect == Effect.READ


def test_any_one_sensitive_candidate_escalates_and_is_named() -> None:
    candidates = ["/work/project/src", f"{_SANDBOX}/plan.md", "/home/dev/.aws/credentials"]
    result = escalate_sensitive_path(
        _read(), candidates, sandbox_dir=_SANDBOX, workspace_root=_WORKSPACE
    )
    assert result.effect == Effect.EGRESS
    assert result.reasons == [f"{SENSITIVE_PATH_REASON}: /home/dev/.aws/credentials"]


def test_an_unresolved_directory_walk_fails_closed() -> None:
    result = escalate_sensitive_path(
        _read(), [], unresolved=True, sandbox_dir=_SANDBOX, workspace_root=None
    )
    assert result.effect == Effect.EGRESS
    assert result.reasons == [UNRESOLVED_PATH_REASON]


def test_a_tier_above_egress_is_kept() -> None:
    destroy = ActionDescriptor(
        capability="fs", effect=Effect.DESTROY, operation="delete", classifier="test"
    )
    result = escalate_sensitive_path(
        destroy, ["~/.alkera/auth.yml"], sandbox_dir=_SANDBOX, workspace_root=_WORKSPACE
    )
    assert result.effect == Effect.DESTROY


def test_the_floor_is_inert_with_the_gate_switched_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(CREDENTIAL_PATH_GATE_ENV, raising=False)
    result = escalate_sensitive_path(
        _read(), ["~/.alkera/auth.yml"], sandbox_dir=_SANDBOX, workspace_root=_WORKSPACE
    )
    assert result.effect == Effect.READ


# --- both adapters apply it --------------------------------------------------


def _claude_read_effect(path: str) -> str:
    descriptor = _descriptor_for_tool(
        "Read", {"file_path": path}, sandbox_dir=_SANDBOX, workspace_root=_WORKSPACE
    )
    assert descriptor is not None
    return str(descriptor.effect.value)


def _opencode_read_effect(path: str) -> str:
    ctx = _TranslatorContext(session_id="sid", workspace_root=_WORKSPACE, sandbox_dir=_SANDBOX)
    event = OpencodeEventTranslator(ctx).translate(
        {
            "type": "permission.asked",
            "properties": {
                "id": "perm_1",
                "permission": "read",
                "patterns": [path],
                "metadata": {},
            },
        }
    )
    assert isinstance(event, PermissionRequest)
    assert event.subject is not None
    return str(event.subject["effect"])


@pytest.mark.parametrize(
    "read_effect",
    [
        pytest.param(_claude_read_effect, id="claude"),
        pytest.param(_opencode_read_effect, id="opencode"),
    ],
)
@pytest.mark.parametrize(
    ("path", "expected"),
    [
        pytest.param("~/.alkera/auth.yml", "egress", id="gateway-token"),
        pytest.param("src/main.py", "read", id="ordinary-file"),
    ],
)
def test_each_adapter_escalates_a_read_of_the_gateway_token(
    read_effect: Callable[[str], str], path: str, expected: str
) -> None:
    assert read_effect(path) == expected
