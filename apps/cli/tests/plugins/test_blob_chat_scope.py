"""Every chat-facing blob tool names blobs as its own chat.

A box serves many people's chats over one content-addressed blob store, and a
blob handle is just a hash: one that leaks (pasted into a chat, guessed from
shared content) must not open another chat's result, reveal that another chat
holds it, or let one chat delete it. These dispatch the real tools through the
real registry door over a real store and pin that chat B asking for chat A's
handle gets, from every tool, exactly the answer a hash nobody wrote gets, and
that A's result survives B's delete. The chat's own handles, including the
spill a large result becomes, stay readable to it and to its subagents.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, ClassVar

import pytest
from alkera_cli.contracts.tool_types import Effect
from alkera_cli.plugins.plugin_base.blob_inspect_tools import register_blob_inspect_tools
from alkera_cli.plugins.plugin_base.blob_tool import register_blob_tools
from alkera_cli.plugins.plugin_base.blob_write_tools import register_blob_write_tools
from alkera_cli.plugins.plugin_base.meta_tools import register_meta_tools
from alkera_cli.plugins.plugin_base.tool import (
    Tool,
    ToolContext,
    ToolRegistry,
    ToolSpec,
)
from alkera_core.project.directory import ProjectDirectory
from pydantic import BaseModel

CHAT_A, CHAT_B = "chat-of-org-a", "chat-of-org-b"
RANDOM = hashlib.sha256(b"no chat ever wrote this").hexdigest()
_BROKER = object()  # passes the write-effect floor; these tools auto-allow


def _registry(tmp_path: Path, *, scoped: bool) -> ToolRegistry:
    registry = ToolRegistry(ProjectDirectory(tmp_path / ".alkera").blobs())
    register_meta_tools(registry)
    register_blob_tools(registry)
    register_blob_inspect_tools(registry)
    register_blob_write_tools(registry)
    # A box's chats each dispatch through a scoped view; a person's own daemon
    # through the registry itself. The blob scope is the chat's either way.
    return registry.restricted(frozenset(), connection_ids=frozenset()) if scoped else registry


async def _call(
    registry: ToolRegistry,
    tmp_path: Path,
    name: str,
    args: dict[str, Any],
    *,
    chat: str,
    owner: str = "",
) -> dict[str, Any]:
    sandbox = tmp_path / "sandboxes" / chat
    sandbox.mkdir(parents=True, exist_ok=True)
    return await registry.dispatch(
        name,
        args,
        broker=_BROKER,
        session_id=chat,
        owner_session_id=owner,
        alkera_dir=tmp_path / ".alkera",
        sandbox_dir=sandbox,
    )


async def _rows_in(registry: ToolRegistry, tmp_path: Path, chat: str) -> str:
    created = await _call(
        registry,
        tmp_path,
        "blob.create",
        {"columns": ["region", "revenue"], "rows": [["east", 10], ["west", 20]]},
        chat=chat,
    )
    return str(created["blob"]["sha256"])


#: Every read a chat can make of a handle, with the arguments it makes it with.
READS: list[tuple[str, dict[str, Any]]] = [
    ("fetch_result", {}),
    ("blob.info", {}),
    ("blob.profile", {}),
    ("blob.query", {"sql": "SELECT * FROM result"}),
    ("blob.derive", {"limit": 1}),
    ("blob.materialize", {"format": "csv"}),
]


def _masked(answer: dict[str, Any], handle: str) -> str:
    return repr(answer).replace(handle, "<handle>")


@pytest.mark.parametrize("scoped", [pytest.param(True, id="box"), pytest.param(False, id="own")])
@pytest.mark.parametrize(("tool", "extra"), [pytest.param(t, a, id=t) for t, a in READS])
async def test_another_chats_handle_answers_like_a_hash_nobody_wrote(
    tmp_path: Path, scoped: bool, tool: str, extra: dict[str, Any]
) -> None:
    registry = _registry(tmp_path, scoped=scoped)
    handle = await _rows_in(registry, tmp_path, CHAT_A)

    own = await _call(registry, tmp_path, tool, {"handle": handle, **extra}, chat=CHAT_A)
    assert "error" not in own, own

    leaked = await _call(registry, tmp_path, tool, {"handle": handle, **extra}, chat=CHAT_B)
    unknown = await _call(registry, tmp_path, tool, {"handle": RANDOM, **extra}, chat=CHAT_B)
    assert "error" in leaked, leaked
    assert _masked(leaked, handle) == _masked(unknown, RANDOM)
    assert "east" not in repr(leaked)


async def test_deleting_another_chats_handle_answers_like_nothing_and_keeps_it(
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path, scoped=True)
    handle = await _rows_in(registry, tmp_path, CHAT_A)

    leaked = await _call(registry, tmp_path, "blob.delete", {"handle": handle}, chat=CHAT_B)
    unknown = await _call(registry, tmp_path, "blob.delete", {"handle": RANDOM}, chat=CHAT_B)
    assert _masked(leaked, handle) == _masked(unknown, RANDOM)
    assert leaked["deleted"] is False
    assert "chat" not in leaked["message"].lower()

    still = await _call(registry, tmp_path, "fetch_result", {"handle": handle}, chat=CHAT_A)
    assert still["rows"] == [["east", 10], ["west", 20]]


async def test_a_delete_never_says_another_chat_holds_the_same_content(tmp_path: Path) -> None:
    """Both chats produced the same rows (one file on disk). B's delete reads
    exactly like the delete of content only B had, and A still reads its own."""
    registry = _registry(tmp_path, scoped=True)
    shared = await _rows_in(registry, tmp_path, CHAT_A)
    assert await _rows_in(registry, tmp_path, CHAT_B) == shared
    alone = await _call(
        registry, tmp_path, "blob.create", {"text": "only b wrote this"}, chat=CHAT_B
    )
    alone_handle = alone["blob"]["sha256"]

    with_holder = await _call(registry, tmp_path, "blob.delete", {"handle": shared}, chat=CHAT_B)
    without = await _call(registry, tmp_path, "blob.delete", {"handle": alone_handle}, chat=CHAT_B)
    assert with_holder["deleted"] is True and without["deleted"] is True
    assert set(with_holder) == set(without)
    assert with_holder["message"] == f"Deleted ({with_holder['freed_bytes']} bytes)."
    assert "chat" not in with_holder["message"].lower()

    a_reads = await _call(registry, tmp_path, "fetch_result", {"handle": shared}, chat=CHAT_A)
    assert a_reads["rows"] == [["east", 10], ["west", 20]]
    b_reads = await _call(registry, tmp_path, "fetch_result", {"handle": shared}, chat=CHAT_B)
    assert "error" in b_reads


async def test_a_chats_own_delete_removes_what_only_it_held(tmp_path: Path) -> None:
    registry = _registry(tmp_path, scoped=True)
    handle = await _rows_in(registry, tmp_path, CHAT_A)
    out = await _call(registry, tmp_path, "blob.delete", {"handle": handle}, chat=CHAT_A)
    assert out["deleted"] is True and out["freed_bytes"] > 0
    assert not registry._blobs.exists(handle)


class _LoudInput(BaseModel):
    pass


class _LoudOutput(BaseModel):
    text: str


class _LoudTool(Tool[_LoudInput, _LoudOutput]):
    """A tool whose result is far past the inline cap, so the door spills it."""

    spec: ClassVar[ToolSpec] = ToolSpec(name="probe.loud", effect_hint=Effect.READ)
    Input: ClassVar[type[BaseModel]] = _LoudInput
    Output: ClassVar[type[BaseModel]] = _LoudOutput

    async def run(self, args: _LoudInput, ctx: ToolContext) -> _LoudOutput:
        return _LoudOutput(text="org a's rows " * 40_000)


async def test_a_large_results_spill_is_readable_by_its_chat_only(tmp_path: Path) -> None:
    """The delivery door spills an oversized result to a blob as the chat that
    ran the tool, so the handle in the result pages for that chat and no other."""
    registry = ToolRegistry(ProjectDirectory(tmp_path / ".alkera").blobs())
    register_blob_tools(registry)
    registry.register(_LoudTool)
    view = registry.restricted(frozenset(), connection_ids=frozenset())
    spilled = await _call(view, tmp_path, "probe.loud", {}, chat=CHAT_A)
    handle = spilled["blob"]["sha256"]
    own = await _call(view, tmp_path, "fetch_result", {"handle": handle}, chat=CHAT_A)
    assert "org a's rows" in own["text"]
    other = await _call(view, tmp_path, "fetch_result", {"handle": handle}, chat=CHAT_B)
    assert "error" in other and "org a" not in repr(other)


async def test_a_subagent_and_its_root_chat_follow_each_others_handles(tmp_path: Path) -> None:
    registry = _registry(tmp_path, scoped=True)
    made_by_child = await _call(
        registry,
        tmp_path,
        "blob.create",
        {"text": "found by the subagent"},
        chat="child-of-a",
        owner=CHAT_A,
    )
    handle = made_by_child["blob"]["sha256"]
    assert (await _call(registry, tmp_path, "fetch_result", {"handle": handle}, chat=CHAT_A))[
        "text"
    ] == "found by the subagent"
    assert "error" in await _call(
        registry, tmp_path, "fetch_result", {"handle": handle}, chat=CHAT_B
    )


async def test_a_session_less_call_reads_the_whole_store(tmp_path: Path) -> None:
    """The editor's own ``tool.call`` with no chat (a person's own daemon) has
    no chat to scope to and reads the project's store as before."""
    registry = _registry(tmp_path, scoped=False)
    handle = await _rows_in(registry, tmp_path, CHAT_A)
    out = await registry.dispatch("fetch_result", {"handle": handle})
    assert out["rows"] == [["east", 10], ["west", 20]]
