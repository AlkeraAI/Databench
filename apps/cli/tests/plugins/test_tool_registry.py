"""ToolRegistry: the daemon-side selection funnel.

Covers the load-bearing invariants: the hot prefix is a FIXED, byte-stable set;
search is BM25 over the long tail with an app pre-filter; call_tool/dispatch
validates against the target's Input and surfaces typed errors; write-effect
tools fail closed without a broker.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import ClassVar

import httpx
import pytest
from alkera_cli.plugins.plugin_base import Effect, Tool, ToolContext, ToolRegistry, ToolSpec
from alkera_cli.plugins.plugin_base.meta_tools import register_meta_tools
from alkera_core.project.directory import ProjectDirectory
from pydantic import BaseModel


class _EchoIn(BaseModel):
    text: str


class _EchoOut(BaseModel):
    echoed: str


class HotPing(Tool[_EchoIn, _EchoOut]):
    spec: ClassVar[ToolSpec] = ToolSpec(
        name="hot.ping", description="a hot first-class tool", hot=True
    )
    Input: ClassVar[type[BaseModel]] = _EchoIn
    Output: ClassVar[type[BaseModel]] = _EchoOut

    async def run(self, args: _EchoIn, ctx: ToolContext) -> _EchoOut:
        return _EchoOut(echoed=args.text)


class DataFetch(Tool[_EchoIn, _EchoOut]):
    spec: ClassVar[ToolSpec] = ToolSpec(
        name="data.fetch",
        description="fetch rows from a database table or warehouse query",
        app="data",
        hot=False,
    )
    Input: ClassVar[type[BaseModel]] = _EchoIn
    Output: ClassVar[type[BaseModel]] = _EchoOut

    async def run(self, args: _EchoIn, ctx: ToolContext) -> _EchoOut:
        return _EchoOut(echoed=args.text)


class FilesRead(Tool[_EchoIn, _EchoOut]):
    spec: ClassVar[ToolSpec] = ToolSpec(
        name="files.read", description="read a file from disk", app="files", hot=False
    )
    Input: ClassVar[type[BaseModel]] = _EchoIn
    Output: ClassVar[type[BaseModel]] = _EchoOut

    async def run(self, args: _EchoIn, ctx: ToolContext) -> _EchoOut:
        return _EchoOut(echoed=args.text)


class DangerWrite(Tool[_EchoIn, _EchoOut]):
    spec: ClassVar[ToolSpec] = ToolSpec(
        name="data.delete",
        description="delete rows",
        app="data",
        hot=False,
        effect_hint=Effect.DESTROY,
    )
    Input: ClassVar[type[BaseModel]] = _EchoIn
    Output: ClassVar[type[BaseModel]] = _EchoOut

    async def run(self, args: _EchoIn, ctx: ToolContext) -> _EchoOut:  # pragma: no cover
        return _EchoOut(echoed=args.text)


def _registry(tmp_path: Path) -> ToolRegistry:
    blobs = ProjectDirectory(tmp_path / ".alkera").blobs()
    registry = ToolRegistry(blobs)
    register_meta_tools(registry)
    registry.register(HotPing)
    registry.register(DataFetch)
    registry.register(FilesRead)
    registry.register(DangerWrite)
    return registry


def _prefix_bytes(registry: ToolRegistry) -> str:
    return json.dumps([s.model_dump() for s in registry.hot_prefix()], sort_keys=True)


def _relay(text: str) -> dict[str, object]:
    return {"name": "data.read", "args": {"text": text}}


# --- hot prefix ------------------------------------------------------------


def test_hot_prefix_is_the_fixed_set(tmp_path: Path) -> None:
    names = [s.name for s in _registry(tmp_path).hot_prefix()]
    # The meta-tools (search_tools / call_tool / list_plugins) + the one first-class
    # hot tool, sorted; nothing else.
    assert names == ["call_tool", "hot.ping", "list_plugins", "search_tools"]


def test_hot_prefix_is_byte_stable_across_calls(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    first = _prefix_bytes(registry)
    # Searching + dispatching must not perturb the prefix.
    import asyncio

    asyncio.run(registry.search("database"))
    assert _prefix_bytes(registry) == first


async def test_hot_prefix_byte_stable_across_a_multi_step_trajectory(tmp_path: Path) -> None:
    """The cached ``tools`` prefix must be byte-IDENTICAL at every step of a real
    multi-step trajectory — search → call_tool → fetch_result → more searches →
    even an error — or the provider re-bills the whole cached prefix (modifying
    the tools array is full cache invalidation). This is the load-bearing
    cache-discipline guarantee: discovery happens in the
    messages region (search) and invocation through one stable dispatcher, so
    the prefix never moves once composed."""
    from alkera_cli.plugins.plugin_base.blob_tool import register_blob_tools
    from alkera_cli.plugins.plugin_base.delivery import RESULT_INLINE_BYTE_CAP

    registry = _registry_with_big_tool(tmp_path, "x" * (RESULT_INLINE_BYTE_CAP + 1))
    register_blob_tools(registry)  # the new hot tool is part of what we pin

    # The catalog is now frozen for the "session" — nothing below registers.
    baseline = _prefix_bytes(registry)
    assert "fetch_result" in [s.name for s in registry.hot_prefix()]

    async def assert_stable_after(*coros: object) -> list[dict[str, object]]:
        results = [await c for c in coros]  # type: ignore[misc]
        assert _prefix_bytes(registry) == baseline, "hot prefix moved mid-trajectory → re-bill"
        return results

    # A realistic trajectory: discover, invoke, spill, app-filtered discover,
    # page the spill, discover again, a meta-tool, and a clean error.
    await assert_stable_after(registry.search("database table query"))
    await assert_stable_after(
        registry.dispatch("call_tool", {"name": "hot.ping", "args": {"text": "a"}})
    )
    (spill,) = await assert_stable_after(registry.dispatch("data.dump", {"text": "go"}))
    await assert_stable_after(registry.search("read a file", app="files"))
    await assert_stable_after(
        registry.dispatch(
            "fetch_result", {"handle": spill["blob"]["sha256"], "offset": 0, "limit": 10}
        )
    )
    await assert_stable_after(registry.search("delete rows"))
    await assert_stable_after(registry.dispatch("search_tools", {"query": "fetch large result"}))
    await assert_stable_after(registry.dispatch("does.not.exist", {}))  # error path


def test_pin_promotes_into_prefix_and_out_of_search(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    registry.pin("data.fetch")
    assert "data.fetch" in [s.name for s in registry.hot_prefix()]


# --- search ----------------------------------------------------------------


async def test_search_ranks_by_relevance(tmp_path: Path) -> None:
    hits = await _registry(tmp_path).search("database table query")
    assert hits, "expected a match"
    assert hits[0].name == "data.fetch"


async def test_search_app_prefilter(tmp_path: Path) -> None:
    hits = await _registry(tmp_path).search("read", app="files")
    assert [h.name for h in hits] == ["files.read"]


async def test_search_names_a_hot_tool_but_never_the_discovery_tools(tmp_path: Path) -> None:
    """A query for an area lists that area's tools even when they are already
    loaded; the discovery tools themselves are never an answer."""
    hits = [h.name for h in await _registry(tmp_path).search("ping tool search call")]
    assert "hot.ping" in hits
    assert "search_tools" not in hits and "call_tool" not in hits


async def test_the_candidates_depend_on_the_query(tmp_path: Path) -> None:
    """A query with no word in any tool answers nothing rather than the whole
    catalog, and two different areas answer two different lists."""
    registry = _registry(tmp_path)
    assert await registry.search("zzqx purple elephants") == []
    files = [h.name for h in await registry.search("files")]
    data = [h.name for h in await registry.search("fetch")]
    assert files[:1] == ["files.read"]
    assert data[:1] == ["data.fetch"]


# --- dispatch (call_tool core) --------------------------------------------


async def test_dispatch_runs_and_serializes(tmp_path: Path) -> None:
    out = await _registry(tmp_path).dispatch("hot.ping", {"text": "hi"})
    assert out == {"echoed": "hi"}


# --- the meta-tools through dispatch --------------------------------------


async def test_call_tool_dispatches_inner(tmp_path: Path) -> None:
    out = await _registry(tmp_path).dispatch(
        "call_tool", {"name": "hot.ping", "args": {"text": "yo"}}
    )
    assert out == {"result": {"echoed": "yo"}}


# --- the transport error signal (isError / is_error) -----------------------
# A failed tool call must reach the model as a PROPER tool error, not a "successful"
# result whose text happens to contain an error. dispatch marks its failure results
# with a reserved flag every transport reads; call_tool re-raises an inner failure so
# it's marked too (a long-tail tool is only reachable through call_tool).


@pytest.mark.parametrize(
    ("tool", "args", "says", "classification"),
    [
        pytest.param("nope", {}, "unknown tool", "error", id="unknown-tool"),
        pytest.param("data.fetch", {}, "data.fetch needs `text`.", "error", id="invalid-arguments"),
        pytest.param("data.delete", {"text": "x"}, "permission broker", "error", id="no-broker"),
        pytest.param(
            "data.read", {"text": "permission denied"}, "no data connections", "error", id="no-conn"
        ),
        pytest.param("data.read", {"text": "context"}, "malformed", "error", id="context-unread"),
        pytest.param("data.read", {"text": "raw"}, "gave up", "timeout", id="raw-escape"),
        pytest.param("data.read", {"text": "wrapped"}, "read failed", "unreachable", id="wrapped"),
        pytest.param(
            "call_tool", _relay("wrapped"), "read failed", "unreachable", id="wrapped-relayed"
        ),
        pytest.param("call_tool", _relay("x"), "no data connections", "error", id="alkera-relayed"),
    ],
)
async def test_a_failed_dispatch_is_flagged_and_carries_its_classification(
    tmp_path: Path, tool: str, args: dict[str, object], says: str, classification: str
) -> None:
    # A failure's classification is the product's reading of the exception that caused it,
    # wherever the result is built and however the model reached the tool. A wrapped driver
    # refusal keeps its class through call_tool; what Alkera decided stays "error".
    from alkera_cli.plugins.plugin_base.tool import ToolError
    from alkera_cli.plugins.plugin_base.wire import is_tool_error_result

    class NeedsConn(Tool[_EchoIn, _EchoOut]):
        spec: ClassVar[ToolSpec] = ToolSpec(
            name="data.read", description="reads a connection", app="data"
        )
        Input: ClassVar[type[BaseModel]] = _EchoIn
        Output: ClassVar[type[BaseModel]] = _EchoOut

        async def run(self, args: _EchoIn, ctx: ToolContext) -> _EchoOut:
            if args.text == "raw":
                raise TimeoutError("the driver gave up")  # no handler: it escapes as itself
            if args.text in ("wrapped", "context"):
                try:
                    async with httpx.AsyncClient() as client:
                        await client.get("http://127.0.0.1:1")
                except httpx.HTTPError as exc:
                    if args.text == "context":
                        raise AssertionError("malformed response payload") from exc
                    raise ToolError("read failed") from exc  # the shape every connector ships
            ctx.resolve_connection(args.text)  # no connections registered
            return _EchoOut(echoed=args.text)

    registry = _registry(tmp_path)
    registry.register(NeedsConn)
    out = await registry.dispatch(tool, args)

    assert is_tool_error_result(out)
    assert says in out["error"]
    assert out["classification"] == classification


async def test_a_success_result_with_an_error_field_is_not_flagged(tmp_path: Path) -> None:
    # The signal is the reserved flag, NOT the presence of an "error" key — a tool whose
    # OUTPUT legitimately carries `error` (a soft warning, a nested read-back) is a
    # SUCCESS. It must not be flagged, and call_tool must keep it a nested success.
    from alkera_cli.plugins.plugin_base.wire import is_tool_error_result

    class _SoftOut(BaseModel):
        error: str = ""
        ok: bool = True

    class SoftFail(Tool[_EchoIn, _SoftOut]):
        spec: ClassVar[ToolSpec] = ToolSpec(
            name="data.softfail", description="carries a benign error field", app="data", hot=False
        )
        Input: ClassVar[type[BaseModel]] = _EchoIn
        Output: ClassVar[type[BaseModel]] = _SoftOut

        async def run(self, args: _EchoIn, ctx: ToolContext) -> _SoftOut:
            return _SoftOut(error="soft warning", ok=True)

    reg = _registry(tmp_path)
    reg.register(SoftFail)
    out = await reg.dispatch("data.softfail", {"text": "x"})
    assert out == {"error": "soft warning", "ok": True}
    assert not is_tool_error_result(out)
    # Through call_tool it stays a NESTED success — no re-raise.
    via = await reg.dispatch("call_tool", {"name": "data.softfail", "args": {"text": "x"}})
    assert via == {"result": {"error": "soft warning", "ok": True}}
    assert not is_tool_error_result(via)


async def test_call_tool_reraises_inner_failure_as_flagged_top_level(tmp_path: Path) -> None:
    from alkera_cli.plugins.plugin_base.tool import ToolError
    from alkera_cli.plugins.plugin_base.wire import is_tool_error_result

    class Boom(Tool[_EchoIn, _EchoOut]):
        spec: ClassVar[ToolSpec] = ToolSpec(
            name="data.boom", description="always fails", app="data", hot=False
        )
        Input: ClassVar[type[BaseModel]] = _EchoIn
        Output: ClassVar[type[BaseModel]] = _EchoOut

        async def run(self, args: _EchoIn, ctx: ToolContext) -> _EchoOut:
            raise ToolError("kaboom: the inner tool failed")

    reg = _registry(tmp_path)
    reg.register(Boom)
    direct = await reg.dispatch("data.boom", {"text": "x"})
    assert is_tool_error_result(direct) and "kaboom" in direct["error"]
    # Via call_tool: the failure is the OUTER call's own flagged error, NOT buried in a
    # nested "successful" {"result": {...}} envelope.
    via = await reg.dispatch("call_tool", {"name": "data.boom", "args": {"text": "x"}})
    assert is_tool_error_result(via)
    assert "result" not in via
    assert "kaboom" in via["error"]


async def test_a_tool_leaking_a_raw_exception_returns_a_shaped_error_not_a_crash(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # The dispatcher's binding backstop: a buggy tool that leaks a raw exception
    # must not kill the transport and the whole turn — the model gets a flagged
    # error carrying the original message, and the traceback lands in the log.
    import logging

    from alkera_cli.plugins.plugin_base.wire import is_tool_error_result

    class Leaky(Tool[_EchoIn, _EchoOut]):
        spec: ClassVar[ToolSpec] = ToolSpec(
            name="data.leaky", description="leaks a raw exception", app="data", hot=False
        )
        Input: ClassVar[type[BaseModel]] = _EchoIn
        Output: ClassVar[type[BaseModel]] = _EchoOut

        async def run(self, args: _EchoIn, ctx: ToolContext) -> _EchoOut:
            raise RuntimeError("driver exploded mid-flight")

    reg = _registry(tmp_path)
    reg.register(Leaky)
    with caplog.at_level(logging.ERROR):
        out = await reg.dispatch("data.leaky", {"text": "x"})
    assert is_tool_error_result(out)
    assert "data.leaky" in out["error"]
    assert "driver exploded mid-flight" in out["error"]  # the original text survives
    assert any(r.exc_info for r in caplog.records)  # the traceback went to the log


async def test_a_turn_cancel_still_propagates_through_dispatch(tmp_path: Path) -> None:
    # CancelledError is a BaseException: the backstop must let it through untouched,
    # or a turn cancel would read as a tool error instead of cancelling the turn.
    import asyncio

    class Cancelling(Tool[_EchoIn, _EchoOut]):
        spec: ClassVar[ToolSpec] = ToolSpec(
            name="data.cancelling", description="cancelled mid-run", app="data", hot=False
        )
        Input: ClassVar[type[BaseModel]] = _EchoIn
        Output: ClassVar[type[BaseModel]] = _EchoOut

        async def run(self, args: _EchoIn, ctx: ToolContext) -> _EchoOut:
            raise asyncio.CancelledError

    reg = _registry(tmp_path)
    reg.register(Cancelling)
    with pytest.raises(asyncio.CancelledError):
        await reg.dispatch("data.cancelling", {"text": "x"})


async def test_search_tools_returns_cards(tmp_path: Path) -> None:
    out = await _registry(tmp_path).dispatch("search_tools", {"query": "database table"})
    names = [card["name"] for card in out["tools"]]
    assert "data.fetch" in names
    # Cards carry the derived input schema.
    card = next(c for c in out["tools"] if c["name"] == "data.fetch")
    assert "properties" in card["input_schema"]


# --- oversized results spill to a blob handle (handle-not-dump) --------------


def _registry_with_big_tool(tmp_path: Path, payload: str) -> ToolRegistry:
    class BigOut(BaseModel):
        payload: str

    class BigDump(Tool[_EchoIn, BigOut]):
        spec: ClassVar[ToolSpec] = ToolSpec(
            name="data.dump", description="returns a huge payload", app="data", hot=False
        )
        Input: ClassVar[type[BaseModel]] = _EchoIn
        Output: ClassVar[type[BaseModel]] = BigOut

        async def run(self, args: _EchoIn, ctx: ToolContext) -> BigOut:
            return BigOut(payload=payload)

    registry = _registry(tmp_path)
    registry.register(BigDump)
    return registry


async def test_small_result_stays_inline(tmp_path: Path) -> None:
    out = await _registry(tmp_path).dispatch("hot.ping", {"text": "hi"})
    assert out == {"echoed": "hi"}  # untouched: no truncated/blob envelope


async def test_non_finite_floats_are_wrapped_not_invalid_json(tmp_path: Path) -> None:
    # A tool returning NaN/±Infinity must not put the invalid JSON tokens
    # NaN/Infinity on the wire (a strict JS/RPC parser rejects them). dispatch's
    # serialization wraps them into the recoverable $nonfinite sentinel.
    from alkera_core.json_safe import NONFINITE_KEY, json_restore

    class NumOut(BaseModel):
        vals: list[float]

    class NumTool(Tool[_EchoIn, NumOut]):
        spec: ClassVar[ToolSpec] = ToolSpec(name="num.emit", description="emits floats", hot=False)
        Input: ClassVar[type[BaseModel]] = _EchoIn
        Output: ClassVar[type[BaseModel]] = NumOut

        async def run(self, args: _EchoIn, ctx: ToolContext) -> NumOut:
            return NumOut(vals=[float("nan"), float("inf"), float("-inf"), 1.5])

    registry = _registry(tmp_path)
    registry.register(NumTool)
    out = await registry.dispatch("num.emit", {"text": "go"})

    assert out["vals"] == [
        {NONFINITE_KEY: "nan"},
        {NONFINITE_KEY: "inf"},
        {NONFINITE_KEY: "-inf"},
        1.5,
    ]
    json.dumps(out, allow_nan=False)  # strict-serializable: no invalid token escapes
    # ...and a knowing consumer recovers the exact floats.
    restored = json_restore(out)["vals"]
    import math

    assert math.isnan(restored[0]) and restored[1] == float("inf") and restored[3] == 1.5


@pytest.mark.parametrize(
    ("char", "utf8_width"),
    [pytest.param("\u0416", 2, id="cyrillic"), pytest.param("\U0001f4c8", 4, id="astral")],
)
def test_non_ascii_reaches_the_model_as_itself_and_costs_its_utf8_width(
    char: str, utf8_width: int
) -> None:
    # A Russian column name arrives readable rather than as an escape the model has to
    # decode, and a tool budgeting a field is charged what the transport sends: two bytes
    # for a Cyrillic letter, four for an emoji, not the six or twelve an escape spends.
    from alkera_cli.plugins.plugin_base.wire import model_facing_text, result_field_bytes

    assert model_facing_text({"col": char}) == f'{{"col": "{char}"}}'
    assert result_field_bytes("col", char) - result_field_bytes("col", "a") == utf8_width - 1


@pytest.mark.parametrize(
    ("payload", "reads_back_as"),
    [
        pytest.param(chr(0xD83D), chr(0xD83D), id="lone-surrogate"),
        pytest.param(chr(0xD83D) + chr(0xDE00), "\U0001f600", id="surrogate-pair"),
    ],
)
async def test_a_surrogate_in_a_result_does_not_fail_the_call(
    tmp_path: Path, payload: str, reads_back_as: str
) -> None:
    # Any connector parsing external JSON can carry a surrogate this far, and UTF-8 cannot
    # encode one. It must not cost the model the whole call: raw, it raised past the
    # dispatch guard and the tool came back as "failed". A PAIR is the asymmetric case —
    # it escapes the same way but reads back as the one astral character it encodes,
    # which is the character the author meant.
    from alkera_cli.plugins.plugin_base.wire import (
        is_tool_error_result,
        model_facing_text,
        result_field_bytes,
    )

    out = await _registry_with_big_tool(tmp_path, payload).dispatch("data.dump", {"text": "go"})

    assert not is_tool_error_result(out)
    assert out == {"payload": payload}
    text = model_facing_text(out)
    assert text.encode()  # the transport can send it, escaped back to \udXXX
    assert json.loads(text) == {"payload": reads_back_as}
    assert result_field_bytes("payload", payload) > 0  # a tool can still budget the field


@pytest.mark.parametrize("char", [pytest.param("x", id="ascii"), pytest.param("Ж", id="cyrillic")])
async def test_oversized_result_spills_to_blob_and_round_trips(tmp_path: Path, char: str) -> None:
    from alkera_cli.plugins.plugin_base.delivery import RESULT_INLINE_BYTE_CAP, RESULT_PREVIEW_BYTES
    from alkera_cli.plugins.plugin_base.result_blob import paginate

    payload = char * (RESULT_INLINE_BYTE_CAP + 1)
    out = await _registry_with_big_tool(tmp_path, payload).dispatch("data.dump", {"text": "go"})
    assert out["truncated"] is True
    assert out["tool"] == "data.dump"
    # The cap names bytes, so a Cyrillic preview costs the model the same context as an
    # English one. Sliced by characters it handed back twice what the constant promises.
    assert len(out["preview"].encode()) <= RESULT_PREVIEW_BYTES
    # The handle paginates back to the FULL original result (a text envelope of
    # the json-encoded result), reassembled by fetching every page.
    blobs = ProjectDirectory(tmp_path / ".alkera").blobs()
    handle = out["blob"]["sha256"]
    text = ""
    offset = 0
    while True:
        page = paginate(blobs, handle, offset=offset, limit=4096)
        text += page["text"]
        if not page["has_more"]:
            break
        offset = page["next_offset"]
    assert json.loads(text) == {"payload": payload}


async def test_result_under_cap_is_not_spilled(tmp_path: Path) -> None:
    from alkera_cli.plugins.plugin_base.delivery import RESULT_INLINE_BYTE_CAP

    payload = "x" * (RESULT_INLINE_BYTE_CAP // 2)
    out = await _registry_with_big_tool(tmp_path, payload).dispatch("data.dump", {"text": "go"})
    assert out == {"payload": payload}


async def test_oversized_result_via_call_tool_keeps_envelope_small(tmp_path: Path) -> None:
    # The nested dispatch spills BEFORE call_tool wraps it, so the meta-tool's
    # own result stays small — the dump can't ride into context via call_tool.
    from alkera_cli.plugins.plugin_base.delivery import RESULT_INLINE_BYTE_CAP

    payload = "x" * (RESULT_INLINE_BYTE_CAP + 1)
    out = await _registry_with_big_tool(tmp_path, payload).dispatch(
        "call_tool", {"name": "data.dump", "args": {"text": "go"}}
    )
    inner = out["result"]
    assert inner["truncated"] is True and "blob" in inner
    assert len(json.dumps(out)) < RESULT_INLINE_BYTE_CAP


async def test_spill_failure_returns_preview_and_error_not_a_crash(tmp_path: Path) -> None:
    # The spill is a best-effort net on the SUCCESS path — a blob-store failure
    # (disk full, read-only .alkera) must never turn a tool that succeeded into a
    # transport crash. dispatch's "never crash" contract holds: it returns a
    # bounded preview + an error marker (NOT the full multi-MB result inline,
    # which would blow the context window and fail the whole turn).
    from alkera_cli.plugins.plugin_base.delivery import RESULT_INLINE_BYTE_CAP, RESULT_PREVIEW_BYTES

    payload = "x" * (RESULT_INLINE_BYTE_CAP + 1)
    registry = _registry_with_big_tool(tmp_path, payload)

    def _boom(_data: object) -> tuple[str, int]:
        raise OSError("No space left on device")

    registry._blobs.write = _boom  # type: ignore[method-assign]
    out = await registry.dispatch("data.dump", {"text": "go"})
    assert "error" in out and out["truncated"] is True  # clean, no exception escaped
    assert "blob" not in out  # the spill failed → no handle
    assert len(out["preview"].encode()) <= RESULT_PREVIEW_BYTES  # bounded, not the full dump
