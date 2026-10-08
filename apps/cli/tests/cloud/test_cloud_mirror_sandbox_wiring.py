"""The chat row's sandbox figures reach the manifest the session is built from.

The row carried the org's tier figures and the box's own default was the free
floor — but the service built every mirror without the row's figures, so the
manifest named none and every pool chat ran at the floor whatever its plan.
The mirror the service builds for a row must seed the local chat with them.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from _adapter_factory import FakeAdapterFactory
from alkera_cli.cloud import CloudRestClient, CloudSocket
from alkera_cli.cloud.service import CloudMirrorService, MirrorSettings
from alkera_cli.harness import HarnessRuntime
from alkera_cli.harness._fake import FakeAdapter
from alkera_cli.harness.sandbox_scope import SCOPE_KEY
from alkera_core.project.directory import ProjectDirectory
from alkera_core.sandbox_tiers import TIER_LIMITS

CHAT_ID = "chat-sized-by-plan"
OWNER = "00000000-0000-4000-8000-000000000003"


def _row(**sandbox: Any) -> dict[str, Any]:
    return {
        "id": CHAT_ID,
        "title": "Sized",
        "owner_user_id": OWNER,
        "permission_mode": "read_only",
        **sandbox,
    }


def _service(tmp_path: Path) -> tuple[CloudMirrorService, HarnessRuntime]:
    runtime = HarnessRuntime(
        ProjectDirectory(tmp_path / ".alkera"),
        adapter_factory=FakeAdapterFactory(FakeAdapter, available=True),
    )
    settings = MirrorSettings(
        api_url="http://127.0.0.1:9",
        token="device-jwt",
        project_dir=tmp_path,
        machine_name="box",
        provider_pod_id="pod-box",
        machine_type_code="cpu3c",
    )
    rest = CloudRestClient(
        api_url=settings.api_url,
        token=settings.token,
        agent_id="machine:box",
        transport=httpx.MockTransport(lambda request: httpx.Response(404, json={})),
    )
    service = CloudMirrorService(settings, runtime, rest=rest, socket=cast(CloudSocket, object()))
    return service, runtime


@pytest.mark.parametrize("tier", ["free", "plus", "pro"])
def test_the_mirror_the_service_builds_seeds_the_rows_tier_figures_onto_the_manifest(
    tmp_path: Path, tier: str
) -> None:
    vcpu, memory_mb = TIER_LIMITS[tier]
    service, runtime = _service(tmp_path)
    mirror = service._default_mirror(CHAT_ID, _row(sandbox_vcpu=vcpu, sandbox_memory_mb=memory_mb))
    store = runtime.project.chats()
    mirror._seed_local_chat(store)
    chat = store.open(CHAT_ID)
    try:
        assert chat.manifest.harness["sandbox_vcpu"] == vcpu
        assert chat.manifest.harness["sandbox_memory_mb"] == memory_mb
    finally:
        chat.close()


def test_a_row_that_names_no_figure_leaves_the_manifest_to_the_box_default(
    tmp_path: Path,
) -> None:
    """An enterprise org with no override carries nulls: the manifest names
    nothing and the box applies its dedicated guard, never a stale figure."""
    service, runtime = _service(tmp_path)
    mirror = service._default_mirror(CHAT_ID, _row(sandbox_vcpu=None, sandbox_memory_mb=None))
    store = runtime.project.chats()
    mirror._seed_local_chat(store)
    chat = store.open(CHAT_ID)
    try:
        assert "sandbox_vcpu" not in chat.manifest.harness
        assert "sandbox_memory_mb" not in chat.manifest.harness
    finally:
        chat.close()


def test_a_row_re_read_with_new_figures_moves_the_manifest(tmp_path: Path) -> None:
    """The plan changed while the chat slept: the mirror built from the new row
    moves the record onto the new figures rather than keeping the old ones."""
    service, runtime = _service(tmp_path)
    store = runtime.project.chats()
    service._default_mirror(CHAT_ID, _row(sandbox_vcpu=1, sandbox_memory_mb=2048))._seed_local_chat(
        store
    )
    service._default_mirror(CHAT_ID, _row(sandbox_vcpu=4, sandbox_memory_mb=8192))._seed_local_chat(
        store
    )
    chat = store.open(CHAT_ID)
    try:
        harness = chat.manifest.harness
        assert (harness["sandbox_vcpu"], harness["sandbox_memory_mb"]) == (4, 8192)
    finally:
        chat.close()


def test_a_chat_that_left_its_workspace_stops_running_under_its_custody_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Opened as a member of a workspace, the chat's manifest carries the
    workspace's custody key; opened again after the chat left it, the mirror
    no longer names the key and the manifest must drop it, or the agent keeps
    running as the workspace's uid in a tree it is no longer part of. What the
    mirror does not own on the bag stays as it was."""
    service, runtime = _service(tmp_path)
    store = runtime.project.chats()
    monkeypatch.setattr(service._workspaces, "sandbox_bag", lambda _chat_id: {SCOPE_KEY: "ws-1"})
    service._default_mirror(CHAT_ID, _row(sandbox_vcpu=1))._seed_local_chat(store)
    chat = store.open(CHAT_ID)
    try:
        assert chat.manifest.harness[SCOPE_KEY] == "ws-1"
        chat.manifest.harness = {**chat.manifest.harness, "agent_session_id": "s-1"}
        chat.flush_manifest()
    finally:
        chat.close()

    monkeypatch.setattr(service._workspaces, "sandbox_bag", lambda _chat_id: {})
    service._default_mirror(CHAT_ID, _row(sandbox_vcpu=1))._seed_local_chat(store)

    chat = store.open(CHAT_ID)
    try:
        assert SCOPE_KEY not in chat.manifest.harness
        assert chat.manifest.harness["agent_session_id"] == "s-1"
        assert chat.manifest.harness["sandbox_vcpu"] == 1
    finally:
        chat.close()


def test_a_member_that_moved_workspace_runs_under_the_new_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, runtime = _service(tmp_path)
    store = runtime.project.chats()
    for key in ("ws-1", "ws-2"):
        monkeypatch.setattr(
            service._workspaces, "sandbox_bag", lambda _chat_id, k=key: {SCOPE_KEY: k}
        )
        service._default_mirror(CHAT_ID, _row())._seed_local_chat(store)
    chat = store.open(CHAT_ID)
    try:
        assert chat.manifest.harness[SCOPE_KEY] == "ws-2"
    finally:
        chat.close()
