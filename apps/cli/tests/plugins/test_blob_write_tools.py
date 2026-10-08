"""blob.materialize / blob.derive / blob.create — act on a spilled result.

materialize writes a real file the agent can run its own code over; derive
reshapes into a new result blob via typed params; create authors a blob from
supplied data. The tests pin the file round-trips, the typed reshape, the
injection refusal, and the write-effect floor (a WRITE tool needs a broker).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from alkera_cli.plugins.plugin_base.blob_inspect_tools import register_blob_inspect_tools
from alkera_cli.plugins.plugin_base.blob_tool import register_blob_tools
from alkera_cli.plugins.plugin_base.blob_write_tools import register_blob_write_tools
from alkera_cli.plugins.plugin_base.meta_tools import register_meta_tools
from alkera_cli.plugins.plugin_base.result_blob import write_rows_blob, write_text_blob
from alkera_cli.plugins.plugin_base.tool import ToolRegistry
from alkera_core.project.chats.blobs import BlobStore
from alkera_core.project.directory import ProjectDirectory

# A non-None sentinel passes the dispatch write-effect floor (`_gate_effect` only
# checks broker presence; materialize/create auto-allow their sandbox/scratch write).
_BROKER = object()


class _ApprovingBroker:
    """A broker that answers, for the one case that drives an egress tool (a
    `web.fetch` reaches the decision engine before the request is made)."""

    async def resolve(self, request: object) -> str:
        return "allow_once"


def _blobs(tmp_path: Path) -> BlobStore:
    return ProjectDirectory(tmp_path / ".alkera").blobs()


def _registry(tmp_path: Path) -> ToolRegistry:
    registry = ToolRegistry(_blobs(tmp_path))
    register_meta_tools(registry)
    register_blob_tools(registry)
    register_blob_inspect_tools(registry)
    register_blob_write_tools(registry)
    return registry


def _rows_handle(registry: ToolRegistry) -> str:
    rows = [[i, "east" if i % 2 == 0 else "west", float(i)] for i in range(20)]
    return write_rows_blob(registry._blobs, columns=["id", "region", "amount"], rows=rows).sha256


# --- registration ----------------------------------------------------------


def test_write_tools_effects() -> None:
    from alkera_cli.contracts.tool_types import Effect
    from alkera_cli.plugins.plugin_base.blob_write_tools import (
        BlobCreateTool,
        BlobDeriveTool,
        BlobMaterializeTool,
    )

    assert BlobMaterializeTool.spec.effect_hint == Effect.WRITE
    assert BlobCreateTool.spec.effect_hint == Effect.WRITE
    assert BlobDeriveTool.spec.effect_hint == Effect.READ  # read + reshape


def test_blob_create_is_hot_for_frictionless_presentation() -> None:
    from alkera_cli.plugins.plugin_base.blob_write_tools import (
        BlobCreateTool,
        BlobDeriveTool,
        BlobMaterializeTool,
    )

    # blob.create is the one write tool that's HOT — authoring a blob to present to
    # the user (or hand to a tool) is a first-class step, not a search-then-call detour.
    assert BlobCreateTool.spec.hot is True
    # The others stay lazy (found via tool search when acting on a result).
    assert BlobMaterializeTool.spec.hot is False
    assert BlobDeriveTool.spec.hot is False


# --- blob.materialize ------------------------------------------------------


@pytest.mark.parametrize("fmt", ["parquet", "csv", "json", "arrow"])
async def test_materialize_writes_a_readable_file(tmp_path: Path, fmt: str) -> None:
    registry = _registry(tmp_path)
    handle = _rows_handle(registry)
    sandbox = tmp_path / "sandbox"
    out = await registry.dispatch(
        "blob.materialize",
        {"handle": handle, "format": fmt},
        broker=_BROKER,
        sandbox_dir=sandbox,
    )
    assert out["format"] == fmt
    assert out["row_count"] == 20
    assert out["columns"] == ["id", "region", "amount"]
    path = Path(out["path"])
    assert path.parent == sandbox
    assert path.suffix == f".{fmt}"
    assert path.stat().st_size == out["bytes"] > 0  # noqa: ASYNC240 — tiny test file read

    # Round-trip the file back (no pandas dep — pyarrow / json).
    if fmt == "parquet":
        import pyarrow.parquet as pq

        assert pq.read_table(path).num_rows == 20
    elif fmt == "csv":
        import pyarrow.csv as pacsv

        assert pacsv.read_csv(path).num_rows == 20
    elif fmt == "arrow":
        import pyarrow.feather as feather

        assert feather.read_table(path).num_rows == 20
    else:
        doc = json.loads(path.read_text())  # noqa: ASYNC240 — tiny test file read
        assert doc["columns"] == ["id", "region", "amount"]
        assert len(doc["rows"]) == 20


async def test_materialize_custom_filename_gets_right_extension(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    handle = _rows_handle(registry)
    out = await registry.dispatch(
        "blob.materialize",
        {"handle": handle, "format": "parquet", "filename": "q3-revenue"},
        broker=_BROKER,
        sandbox_dir=tmp_path / "sandbox",
    )
    assert Path(out["path"]).name == "q3-revenue.parquet"


async def test_materialize_rejects_path_escape(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    handle = _rows_handle(registry)
    out = await registry.dispatch(
        "blob.materialize",
        {"handle": handle, "filename": "../escape"},
        broker=_BROKER,
        sandbox_dir=tmp_path / "sandbox",
    )
    assert "error" in out
    assert "filename" in out["error"]


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks")
@pytest.mark.parametrize("kind", ["rows", "text"])
async def test_materialize_never_writes_through_a_link_the_agent_planted(
    tmp_path: Path, kind: str
) -> None:
    """The daemon writes into the sandbox as itself, by a name the model chose.
    A link the agent planted at that name, aimed at a file on the host, would
    turn the write into one on that file; the write is refused at the open and
    the target keeps its bytes. A dangling link is refused the same way, so no
    file appears where the link points either."""
    registry = _registry(tmp_path)
    if kind == "rows":
        handle = _rows_handle(registry)
        args: dict[str, object] = {"handle": handle, "format": "csv", "filename": "out"}
        name = "out.csv"
    else:
        handle = write_text_blob(registry._blobs, text="OVERWRITTEN").sha256
        args = {"handle": handle, "filename": "out.txt"}
        name = "out.txt"
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    victim = tmp_path / "host-home" / "auth.yml"
    victim.parent.mkdir()
    victim.write_text("token: ORIGINAL\n")
    (sandbox / name).symlink_to(victim)
    out = await registry.dispatch("blob.materialize", args, broker=_BROKER, sandbox_dir=sandbox)
    assert "error" in out and "plain file" in out["error"]
    assert victim.read_text() == "token: ORIGINAL\n"
    assert (sandbox / name).is_symlink()  # left as the agent made it, still pointing out
    dangling = sandbox / "gone.txt"
    dangling.symlink_to(tmp_path / "host-home" / "never-made")
    out = await registry.dispatch(
        "blob.materialize",
        {"handle": handle, "filename": "gone.txt"}
        if kind == "text"
        else {**args, "filename": "gone"},
        broker=_BROKER,
        sandbox_dir=sandbox,
    )
    if kind == "text":
        assert "error" in out
        assert not (tmp_path / "host-home" / "never-made").exists()


@pytest.mark.skipif(sys.platform == "win32", reason="FIFOs")
async def test_materialize_is_not_held_by_a_fifo_the_agent_planted(tmp_path: Path) -> None:
    """A FIFO at the name holds its first writer until a reader comes; the
    agent never reads it, so a plain open would hold the daemon for good.
    The open does not wait, and what is not a plain file is refused."""
    import asyncio
    import os

    registry = _registry(tmp_path)
    handle = write_text_blob(registry._blobs, text="held").sha256
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    os.mkfifo(sandbox / "out.txt")
    out = await asyncio.wait_for(
        registry.dispatch(
            "blob.materialize",
            {"handle": handle, "filename": "out.txt"},
            broker=_BROKER,
            sandbox_dir=sandbox,
        ),
        timeout=5,
    )
    assert "error" in out and "plain file" in out["error"]


async def test_materialize_overwrites_a_plain_file_of_the_same_name(tmp_path: Path) -> None:
    """The control for the link refusal: a regular file at the name is simply
    replaced, which is what a second materialize of the same name did before."""
    registry = _registry(tmp_path)
    handle = write_text_blob(registry._blobs, text="second").sha256
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    (sandbox / "out.txt").write_text("first")
    out = await registry.dispatch(
        "blob.materialize",
        {"handle": handle, "filename": "out.txt"},
        broker=_BROKER,
        sandbox_dir=sandbox,
    )
    assert "error" not in out
    assert (sandbox / "out.txt").read_text() == "second"
    # The sandbox's user and group can read and write it: the chat tree's mode.
    assert (sandbox / "out.txt").stat().st_mode & 0o660 == 0o660


async def test_materialize_names_the_file_where_the_agent_sees_it(tmp_path: Path) -> None:
    """A bounded chat's agent sees its root at the sandbox's home, so the path
    the tool answers with is spelled there; without a fence the host path is the
    agent's path and is answered as before."""
    from alkera_cli.cloud.fence import SessionFence

    registry = _registry(tmp_path)
    handle = write_text_blob(registry._blobs, text="hello").sha256
    sandbox = tmp_path / "work" / "scratch"
    sandbox.mkdir(parents=True)
    fence = SessionFence(
        root=tmp_path,
        folder=sandbox,
        working_dir=sandbox,
        aliases=(("/home/alkera", sandbox),),
    )
    out = await registry.dispatch(
        "blob.materialize",
        {"handle": handle, "filename": "hello.txt"},
        broker=_BROKER,
        sandbox_dir=sandbox,
        fence=fence,
    )
    assert out["path"] == "/home/alkera/hello.txt"
    assert (sandbox / "hello.txt").read_text() == "hello"
    plain = await registry.dispatch(
        "blob.materialize",
        {"handle": handle, "filename": "again.txt"},
        broker=_BROKER,
        sandbox_dir=sandbox,
    )
    assert plain["path"] == str(sandbox / "again.txt")


async def test_materialize_without_sandbox_errors(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    handle = _rows_handle(registry)
    out = await registry.dispatch(
        "blob.materialize", {"handle": handle}, broker=_BROKER
    )  # no sandbox_dir
    assert "error" in out
    assert "sandbox" in out["error"]


async def test_materialize_is_refused_without_a_broker(tmp_path: Path) -> None:
    # A WRITE tool requires the permission-broker floor (no broker → refused).
    registry = _registry(tmp_path)
    handle = _rows_handle(registry)
    out = await registry.dispatch(
        "blob.materialize", {"handle": handle}, sandbox_dir=tmp_path / "sandbox"
    )
    assert "error" in out
    assert "permission broker" in out["error"]


# --- blob.materialize on TEXT blobs (a spilled tool result, e.g. web.fetch) ---


@pytest.mark.parametrize(
    "text",
    [
        pytest.param('{"url": "https://ex.com", "content": "# Page\\n\\nBody."}', id="json-body"),
        pytest.param("<html><body>a plain HTML page</body></html>", id="html-body"),
        pytest.param("2026-07-20 ERROR something\n2026-07-20 INFO recovered", id="log-body"),
        pytest.param("", id="empty-body"),
    ],
)
async def test_materialize_text_blob_writes_verbatim_txt(tmp_path: Path, text: str) -> None:
    # A spilled tool result is a text blob — and it may be ANY text (JSON, HTML,
    # logs, …), NOT only JSON. Materializing it is a valid action that writes the
    # bytes verbatim to a .txt file, with no content sniffing (no JSON overfit).
    from alkera_cli.plugins.plugin_base.result_blob import write_text_blob

    registry = _registry(tmp_path)
    handle = write_text_blob(registry._blobs, text=text).sha256
    sandbox = tmp_path / "sandbox"
    out = await registry.dispatch(
        "blob.materialize",
        {"handle": handle, "format": "parquet"},  # tabular format is ignored for text
        broker=_BROKER,
        sandbox_dir=sandbox,
    )
    assert "error" not in out, out
    assert out["format"] == "text"
    assert out["row_count"] == 0
    assert out["char_count"] == len(text)
    path = Path(out["path"])
    assert path.parent == sandbox
    assert path.suffix == ".txt"
    assert path.read_text() == text  # verbatim  # noqa: ASYNC240


async def test_materialize_text_blob_keeps_model_supplied_extension(tmp_path: Path) -> None:
    # The model picks the extension via `filename` — text has no single right one.
    from alkera_cli.plugins.plugin_base.result_blob import write_text_blob

    registry = _registry(tmp_path)
    handle = write_text_blob(registry._blobs, text="# A Markdown Page").sha256
    sandbox = tmp_path / "sandbox"
    md = await registry.dispatch(
        "blob.materialize",
        {"handle": handle, "filename": "fetched.md"},
        broker=_BROKER,
        sandbox_dir=sandbox,
    )
    assert Path(md["path"]).name == "fetched.md"
    # A bare stem (no extension) still defaults to .txt.
    bare = await registry.dispatch(
        "blob.materialize",
        {"handle": handle, "filename": "fetched"},
        broker=_BROKER,
        sandbox_dir=sandbox,
    )
    assert Path(bare["path"]).name == "fetched.txt"


async def test_materialize_oversized_web_fetch_result_end_to_end(tmp_path: Path) -> None:
    """The real path the user hit: a big web.fetch result SPILLS to a text blob
    (the dispatch spill net), and that handle then materializes to a file."""
    from unittest.mock import patch

    from _decision_sink import MemorySink
    from alkera_cli.plugins.plugin_base.web_tools import register_web_tools

    registry = _registry(tmp_path)
    register_web_tools(registry)
    body = "z" * 200_000  # over the 64KiB inline cap → spills to a blob
    data = body.encode("utf-8")
    with (
        patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("93.184.216.34", 0))]),
        patch("primp.Client") as client_cls,
    ):
        resp = client_cls.return_value.get.return_value
        resp.status_code = 200
        resp.url = "https://site.example/huge"
        resp.headers = {"content-type": "text/plain"}  # verbatim body, no markdown conversion
        resp.iter_bytes = lambda chunk=None: iter([data])
        resp.close = lambda: None
        # A fetch is an egress, so it needs a broker that answers and a decisions
        # log to record the outcome — the sentinel broker is not enough here.
        fetched = await registry.dispatch(
            "web.fetch",
            {"url": "https://site.example/huge", "max_chars": 500_000},
            broker=_ApprovingBroker(),
            decision_sink=MemorySink(),
            sandbox_dir=tmp_path / "sandbox",
        )
    assert fetched["truncated"] is True
    handle = fetched["blob"]["sha256"]

    out = await registry.dispatch(
        "blob.materialize", {"handle": handle}, broker=_BROKER, sandbox_dir=tmp_path / "sandbox"
    )
    assert "error" not in out, out
    assert out["format"] == "text"
    # The materialized file holds the full fetched page, not the truncated preview.
    # (The spilled result is a JSON tool-output; a .txt file parses as JSON fine.)
    assert json.loads(Path(out["path"]).read_text())["content"] == body  # noqa: ASYNC240


async def test_materialize_missing_blob_is_a_clean_error(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    out = await registry.dispatch(
        "blob.materialize",
        {"handle": "0" * 64},
        broker=_BROKER,
        sandbox_dir=tmp_path / "sandbox",
    )
    assert "error" in out
    assert "garbage-collected" in out["error"]


# --- blob.derive -----------------------------------------------------------


async def test_derive_select_filter_sort_limit(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    handle = _rows_handle(registry)
    out = await registry.dispatch(
        "blob.derive",
        {
            "handle": handle,
            "select_columns": ["id", "amount"],
            "where": "region = 'east'",
            "order_by": ["id"],
            "descending": True,
            "limit": 3,
        },
    )
    assert out["columns"] == ["id", "amount"]
    # east rows are even ids 0..18; top-3 descending → 18, 16, 14
    assert [r[0] for r in out["preview_rows"]] == [18, 16, 14]


async def test_derive_distinct(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    handle = _rows_handle(registry)
    out = await registry.dispatch(
        "blob.derive", {"handle": handle, "select_columns": ["region"], "distinct": True}
    )
    assert sorted(r[0] for r in out["preview_rows"]) == ["east", "west"]


async def test_derive_refuses_injection_in_where(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    handle = _rows_handle(registry)
    out = await registry.dispatch(
        "blob.derive",
        {"handle": handle, "where": "1=1; DROP TABLE result"},
    )
    assert "error" in out
    assert "read-only" in out["error"]


# --- blob.create -----------------------------------------------------------


async def test_create_from_rows(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    out = await registry.dispatch(
        "blob.create",
        {"columns": ["a", "b"], "rows": [[1, 2], [3, 4]]},
        broker=_BROKER,
    )
    assert out["kind"] == "rows"
    assert out["total"] == 2
    page = await registry.dispatch("fetch_result", {"handle": out["blob"]["sha256"]})
    assert page["columns"] == ["a", "b"]
    assert page["rows"] == [[1, 2], [3, 4]]


async def test_create_from_text(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    out = await registry.dispatch("blob.create", {"text": "a note to stash"}, broker=_BROKER)
    assert out["kind"] == "text"
    page = await registry.dispatch("fetch_result", {"handle": out["blob"]["sha256"]})
    assert page["text"] == "a note to stash"


async def test_create_requires_rows_or_text(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    out = await registry.dispatch("blob.create", {}, broker=_BROKER)
    assert "error" in out

    out2 = await registry.dispatch("blob.create", {"text": "x", "rows": [[1]]}, broker=_BROKER)
    assert "error" in out2
    assert "not both" in out2["error"]


# --- blob.delete (scoped to the chat, reference-aware) ---------------------


def _write_chat_referencing(tmp_path: Path, sid: str, sha: str) -> None:
    chat_dir = tmp_path / ".alkera" / "chats" / sid
    chat_dir.mkdir(parents=True, exist_ok=True)
    event = {
        "event_type": "part.created",
        "part": {"type": "tool", "output": {"blob": {"sha256": sha, "size": 1, "media_type": "x"}}},
    }
    (chat_dir / "chat.jsonl").write_text(json.dumps(event) + "\n", encoding="utf-8")


def _rows_handle_of(registry: ToolRegistry, chat: str) -> str:
    """A rows blob the chat ``chat`` wrote, so it is one that chat may name."""
    rows = [[i, "east", float(i)] for i in range(20)]
    blobs = registry._blobs.for_chat(chat)
    return write_rows_blob(blobs, columns=["id", "region", "amount"], rows=rows).sha256


async def test_delete_removes_a_sole_reference(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    sha = _rows_handle_of(registry, "c1")
    _write_chat_referencing(tmp_path, "c1", sha)  # only this chat references it
    out = await registry.dispatch(
        "blob.delete",
        {"handle": sha},
        broker=_BROKER,
        session_id="c1",
        alkera_dir=tmp_path / ".alkera",
    )
    assert out["deleted"] is True
    assert out["freed_bytes"] > 0
    assert not registry._blobs.exists(sha)


async def test_delete_keeps_bytes_another_chats_transcript_names(tmp_path: Path) -> None:
    """Another chat's transcript still names the content, so the bytes stay; the
    answer is the one a sole holder gets, and says nothing about the other chat."""
    registry = _registry(tmp_path)
    sha = _rows_handle_of(registry, "c1")
    _write_chat_referencing(tmp_path, "c1", sha)
    _write_chat_referencing(tmp_path, "c2", sha)  # a second chat references the same content
    out = await registry.dispatch(
        "blob.delete",
        {"handle": sha},
        broker=_BROKER,
        session_id="c1",
        alkera_dir=tmp_path / ".alkera",
    )
    assert out["deleted"] is True
    assert out["message"] == f"Deleted ({out['freed_bytes']} bytes)."
    assert registry._blobs.exists(sha)  # bytes kept for c2
    assert not registry._blobs.for_chat("c1").exists(sha)  # but c1 let go of them


async def test_delete_requires_a_chat_session(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    sha = _rows_handle(registry)
    out = await registry.dispatch("blob.delete", {"handle": sha}, broker=_BROKER)
    assert "error" in out
    assert "chat session" in out["error"]


# --- the in-memory engine's configuration is locked --------------------------
#
# ``run_read_only_sql_over`` turns external access off; the lock is what keeps a
# statement from turning anything else — the memory ceiling, the thread count —
# once the engine is running. Only the lock refuses these: with external access
# already off, DuckDB refuses a re-enable on its own, so a case built on that
# would pass with the lock deleted.

LOCKED_SETTINGS = [
    pytest.param("SET memory_limit = '100TB'", id="memory-limit"),
    pytest.param("SET threads = 64", id="threads"),
    pytest.param("SET home_directory = '/'", id="home-directory"),
    pytest.param("SET autoinstall_known_extensions = true", id="autoinstall"),
]


@pytest.mark.parametrize("statement", LOCKED_SETTINGS)
def test_the_in_memory_engines_configuration_cannot_be_changed_by_sql(statement: str) -> None:
    from alkera_cli.plugins.plugin_base.blob_compute import run_read_only_sql_over
    from alkera_cli.plugins.plugin_base.tool import ToolError

    tables = {"result": (["a"], [[1]])}
    with pytest.raises(ToolError, match="query failed"):
        run_read_only_sql_over(tables, statement)
    # The same engine still answers an ordinary read.
    assert run_read_only_sql_over(tables, "SELECT a FROM result") == (["a"], [[1]])
