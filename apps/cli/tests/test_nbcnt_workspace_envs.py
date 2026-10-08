"""Workspace-level environments: a member's agent and its workspace's kernels
share one environments directory under the org's root; a chat on its own keeps
the layout it always had; and the shared trees are put right cheaply."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from alkera_cli.cloud.workspace_seat import WORKSPACE_KEY_PREFIX
from alkera_cli.files.chat_fs import TreeIdentity
from alkera_cli.harness import sandbox as sb
from alkera_cli.harness import sandbox_env
from alkera_cli.harness.adapter import SessionConfig
from alkera_cli.harness.adapters import opencode_http, opencode_sandbox
from alkera_cli.harness.adapters.opencode_http import OpencodeHttpAdapter
from alkera_cli.harness.event_bus import EventBus
from alkera_cli.harness.opencode_binary import ResolvedOpencodeBinary
from alkera_cli.harness.sandbox_layout import (
    ENVS_MOUNT,
    chat_binds,
    envs_dir_for,
    workspace_dir_name,
    workspace_envs_dir,
)
from alkera_cli.harness.sandbox_layout import (
    WORKSPACE_KEY_PREFIX as LAYOUT_PREFIX,
)
from alkera_cli.harness.sandbox_ownership import ENV_TREES_FLOOR
from alkera_cli.harness.sandbox_probe import SandboxCapability
from alkera_cli.harness.sandbox_scope import (
    SCOPE_KEY,
    TOPOLOGY_ENV,
    forget_scope,
    register_scope,
    scope_for,
)

WS = "ws:8a7c1f2e-0000-4000-8000-000000000001"
CHAT_A = "chat_aaaaaaaaaaaaaaaa"
CHAT_B = "chat_bbbbbbbbbbbbbbbb"
SOLO = "chat_cccccccccccccccc"


@pytest.fixture(autouse=True)
def _scopes() -> Iterator[None]:
    yield
    for session in (CHAT_A, CHAT_B, SOLO):
        forget_scope(session)


def test_the_layout_spells_the_workspace_key_prefix_the_cloud_mints() -> None:
    assert LAYOUT_PREFIX == WORKSPACE_KEY_PREFIX


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        pytest.param(WS, WS.removeprefix("ws:"), id="a-workspace-is-its-id"),
        pytest.param("ws:../../etc", None, id="a-path-is-hashed"),
        pytest.param("ws:", None, id="no-id-is-hashed"),
        pytest.param("chat_cccccccccccccccc", None, id="a-chat-key-is-hashed"),
    ],
)
def test_a_workspace_directory_is_its_id_or_a_hash_never_a_path(
    key: str, expected: str | None
) -> None:
    name = workspace_dir_name(key)
    if expected is not None:
        assert name == expected
    else:
        assert "/" not in name and ".." not in name and len(name) <= 20
    assert workspace_envs_dir(Path("/orgs/3/envs"), key).parent == Path("/orgs/3/envs")


def test_a_member_runs_with_its_workspaces_environments_a_chat_on_its_own_with_its_own() -> None:
    root = Path("/orgs/3/envs")
    state = Path("/orgs/3/work/.alkera/chats/c/.runtime")
    assert envs_dir_for(state_dir=state, workspace=WS, envs_root=root) == root / WS.removeprefix(
        "ws:"
    )
    assert envs_dir_for(state_dir=state, workspace=None, envs_root=root) == state / "envs"
    binds = chat_binds(runtime_dir=state, agent_config_root=Path("/c"), envs_dir=root / "w")
    assert next(b for b in binds if b.destination == ENVS_MOUNT).source == root / "w"
    own = chat_binds(runtime_dir=state, agent_config_root=Path("/c"))
    assert next(b for b in own if b.destination == ENVS_MOUNT).source == state / "envs"


@pytest.mark.parametrize(
    ("override", "floor", "expected"),
    [
        pytest.param("/srv/envs", "/orgs/3", "/srv/envs", id="the-override-wins"),
        pytest.param(
            "relative/envs", "/orgs/3", "/orgs/3/envs", id="a-relative-override-is-not-one"
        ),
        pytest.param("", "/orgs/3", "/orgs/3/envs", id="under-the-org-root"),
        pytest.param("", "", "HOME/envs", id="under-alkera-home-without-a-worker"),
    ],
)
def test_where_a_process_keeps_its_workspaces_environments(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, override: str, floor: str, expected: str
) -> None:
    from alkera_cli.host import paths

    monkeypatch.setattr(paths, "ALKERA_HOME", tmp_path)
    monkeypatch.setenv(sandbox_env.ENV_WORKSPACE_ENVS_ROOT, override)
    monkeypatch.setenv(ENV_TREES_FLOOR, floor)
    want = Path(expected.replace("HOME", str(tmp_path)))
    assert sandbox_env.workspace_envs_root() == want


@pytest.mark.parametrize(
    ("mode", "shared"),
    [
        pytest.param("gvisor", True, id="gvisor-members-share-the-workspace-s"),
        pytest.param("none", False, id="a-none-box-member-keeps-its-own"),
        pytest.param("", False, id="the-default-mode-is-none"),
    ],
)
def test_a_session_finds_its_environments_through_its_scope(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mode: str, shared: bool
) -> None:
    """A member shares its workspace's environments only where every member
    runs as the workspace's uid in a container of its own; on a ``none`` box
    a member runs as its own uid and its environment (what its interpreter
    loads) stays its own, as a chat on its own always does."""
    monkeypatch.setenv(sandbox_env.ENV_WORKSPACE_ENVS_ROOT, str(tmp_path / "envs"))
    monkeypatch.setenv(sb.ENV_MODE, mode)
    state = tmp_path / "chats" / CHAT_A / ".runtime"
    assert sandbox_env.session_envs_dir(CHAT_A, state) == state / "envs"
    register_scope(scope_for(CHAT_A, {SCOPE_KEY: WS}))
    workspace = tmp_path / "envs" / WS.removeprefix("ws:")
    want = workspace if shared else state / "envs"
    assert sandbox_env.session_envs_dir(CHAT_A, state) == want
    assert sandbox_env.session_default_env(CHAT_A, state) == want / "alkera"


# --- through the real agent launch ---------------------------------------------------------

GVISOR_READY = SandboxCapability(
    platform="linux",
    root=True,
    setpriv="/usr/bin/setpriv",
    setfacl="/usr/bin/setfacl",
    runsc="/usr/bin/runsc",
    cgroup="systemd",
    reason="injected: gvisor ready",
    rootfs="/opt/alkera/rootfs/current",
    ip="/usr/sbin/ip",
    nft="/usr/sbin/nft",
    unshare="/usr/bin/unshare",
    mount_ns=True,
    uv="/usr/local/bin/uv",
    python_home="/opt/alkera/python/current",
    resolvers=("172.31.0.2",),
)
UIDS = {WS: 20100, CHAT_A: 20101, CHAT_B: 20102, SOLO: 20103}


def _launch(tmp_path: Path, session: str, workspace: str | None) -> sb.SandboxLaunch:
    chat_dir = tmp_path / ".alkera" / "chats" / session
    shared = tmp_path / ".alkera" / "workspaces" / "ws" / "files"
    config = SessionConfig(
        session_id=session,
        project_dir=tmp_path,
        chat_dir=chat_dir,
        fenced=True,
        working_dir=shared if workspace else chat_dir / "scratch",
        harness_native={SCOPE_KEY: workspace} if workspace else {},
    )
    binary = ResolvedOpencodeBinary(
        path=Path("/opt/alkera/agent/opencode"),
        prefix_args=(),
        source="bundled",
        ripgrep_path=Path("/opt/alkera/rg/rg"),
    )
    adapter = OpencodeHttpAdapter(config, binary=binary, event_bus=EventBus())
    return adapter._sandbox_launch(["/opt/alkera/agent/opencode", "serve"], {"PATH": "/usr/bin"})


def _envs_bind_and_env(launch: sb.SandboxLaunch) -> tuple[str, dict[str, str]]:
    config = next(
        s for s in launch.before if isinstance(s, sb.WriteStep) and s.path.name == "config.json"
    )
    oci = json.loads(config.content)
    source = next(m["source"] for m in oci["mounts"] if m["destination"] == ENVS_MOUNT)
    env = dict(item.split("=", 1) for item in oci["process"]["env"])
    return source, env


def test_members_share_the_workspaces_environments_and_a_solo_chat_keeps_its_own(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in (sb.ENV_MODE, sb.ENV_DEDICATED, sb.ENV_ROOTFS, sb.ENV_NET, sb.ENV_PYTHON):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(sb.ENV_MODE, "gvisor")
    monkeypatch.setenv(TOPOLOGY_ENV, "per_chat")
    monkeypatch.setenv(sandbox_env.ENV_WORKSPACE_ENVS_ROOT, str(tmp_path / "orgenvs"))
    monkeypatch.setattr(opencode_http, "current_capability", lambda: GVISOR_READY)
    monkeypatch.setattr(opencode_sandbox, "ensure_chat_uid", lambda name, **_: UIDS[name])
    a_source, a_env = _envs_bind_and_env(_launch(tmp_path, CHAT_A, WS))
    b_source, b_env = _envs_bind_and_env(_launch(tmp_path, CHAT_B, WS))
    solo_source, solo_env = _envs_bind_and_env(_launch(tmp_path, SOLO, None))
    workspace = (tmp_path / "orgenvs" / WS.removeprefix("ws:")).as_posix()
    assert a_source == b_source == workspace
    assert solo_source == (tmp_path / ".alkera" / "chats" / SOLO / ".runtime" / "envs").as_posix()
    # Wire-identical inside: every container sees its environments at one path.
    for env in (a_env, b_env, solo_env):
        assert env["VIRTUAL_ENV"] == f"{ENVS_MOUNT}/alkera"
        assert env["UV_CACHE_DIR"] == f"{ENVS_MOUNT}/.uv-cache"


# --- the shared trees' repair, run for real -------------------------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX owners and modes")
def test_the_repair_walks_a_wrong_tree_once_and_never_again(tmp_path: Path) -> None:
    tree = tmp_path / "files"
    (tree / "a" / "b").mkdir(parents=True)
    (tree / "a" / "b" / "f").write_text("x")
    os.chmod(tree / "a", 0o755)
    os.chmod(tree / "a" / "b" / "f", 0o604)
    step = sb.RepairStep(tree, TreeIdentity(os.getuid(), os.getgid()), when_wrong=True)

    sb.run_steps((step,))
    assert oct(tree.stat().st_mode & 0o7777) == "0o2770"
    assert oct((tree / "a").stat().st_mode & 0o777) == "0o770"
    assert oct((tree / "a" / "b" / "f").stat().st_mode & 0o777) == "0o660"
    # A right root is not walked again: what changed below it stays.
    os.chmod(tree / "a" / "b" / "f", 0o604)
    sb.run_steps((step,))
    assert oct((tree / "a" / "b" / "f").stat().st_mode & 0o777) == "0o604"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX links")
def test_the_repair_refuses_a_link_in_a_trees_place(tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    os.chmod(elsewhere, 0o700)
    (tmp_path / "files").symlink_to(elsewhere)
    step = sb.RepairStep(tmp_path / "files", TreeIdentity(os.getuid(), os.getgid()))
    with pytest.raises(sb.SandboxRefusedError, match="files"):
        sb.run_steps((step,))
    assert oct(elsewhere.stat().st_mode & 0o7777) == "0o700"
