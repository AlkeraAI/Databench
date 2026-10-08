"""The local web tools (`web.search` / `web.fetch`): result mapping, error
paths, the public-address guard (checked per redirect hop), org-flag-gated
registration, and the runtime's flag resolution. The ddgs/primp boundary is
mocked throughout — the real wire is pinned by the `live` tier
(``apps/cli/tests/e2e/test_web_tools_live.py``).

`web.fetch` is a READ in every mode, so a dispatch of it needs no approval — but
it still writes its decision, so the cases here that drive it supply a decisions
log. Which addresses it may reach, and that no stance lifts that floor, is
`test_web_fetch_gate.py`'s subject."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from _decision_sink import MemorySink
from alkera_cli.contracts.tool_types import Effect
from alkera_cli.harness.runtime import HarnessRuntime
from alkera_cli.harness.web_flags import WebToolFlags
from alkera_cli.plugins.plugin_base.mcp_entry import (
    alkera_tool_descriptors,
    web_registry_name,
    web_tool_descriptors,
)
from alkera_cli.plugins.plugin_base.plugin import WorkspaceEvent
from alkera_cli.plugins.plugin_base.registry import PluginRegistry
from alkera_cli.plugins.plugin_base.tool import ToolError, ToolRegistry
from alkera_cli.plugins.plugin_base.web_tools import (
    WebFetchInput,
    _refuse_non_public,
    _run_fetch,
    register_web_tools,
)
from alkera_core.project.directory import ProjectDirectory
from ddgs.exceptions import DDGSException, RatelimitException, TimeoutException


@pytest.fixture
def registry(tmp_path: Path) -> ToolRegistry:
    reg = ToolRegistry(ProjectDirectory(tmp_path / ".alkera").blobs())
    register_web_tools(reg)
    return reg


def _search_items(*rows: dict[str, Any]) -> list[dict[str, Any]]:
    return list(rows)


# ---------------------------------------------------------------------------
# web.search
# ---------------------------------------------------------------------------


async def test_search_maps_results_and_drops_urlless_rows(registry: ToolRegistry) -> None:
    with patch("ddgs.DDGS") as ddgs_cls:
        ddgs_cls.return_value.text.return_value = _search_items(
            {"title": "One", "href": "https://one.example", "body": "first"},
            # Alternate key spelling some backends emit.
            {"title": "Two", "url": "https://two.example", "body": "second"},
            {"title": "no url — dropped", "body": "x"},
        )
        out = await registry.dispatch("web.search", {"query": "hello"})
    assert out == {
        "results": [
            {"title": "One", "url": "https://one.example", "snippet": "first"},
            {"title": "Two", "url": "https://two.example", "snippet": "second"},
        ],
        "query": "hello",
    }


async def test_search_forwards_knobs_and_caps_results(registry: ToolRegistry) -> None:
    rows = [{"title": f"t{i}", "href": f"https://r{i}.example"} for i in range(30)]
    with patch("ddgs.DDGS") as ddgs_cls:
        ddgs_cls.return_value.text.return_value = rows
        out = await registry.dispatch(
            "web.search",
            {
                "query": "q",
                "max_results": 3,
                "region": "de-de",
                "timelimit": "w",
                "backend": "bing",
            },
        )
    # Forwarded verbatim to ddgs...
    kwargs = ddgs_cls.return_value.text.call_args.kwargs
    assert kwargs["region"] == "de-de"
    assert kwargs["timelimit"] == "w"
    assert kwargs["backend"] == "bing"
    assert kwargs["max_results"] == 3
    assert kwargs["safesearch"] == "off"  # fixed (unfiltered) policy, not a model knob
    # ...and the cap holds even when the engine over-returns.
    assert len(out["results"]) == 3


@pytest.mark.parametrize(
    ("exc", "hint"),
    [
        pytest.param(RatelimitException("429"), "rate-limited", id="ratelimit"),
        pytest.param(TimeoutException("slow"), "timed out", id="timeout"),
        pytest.param(DDGSException("boom"), "failed", id="generic"),
    ],
)
async def test_search_maps_ddgs_errors_to_tool_errors(
    registry: ToolRegistry, exc: Exception, hint: str
) -> None:
    with patch("ddgs.DDGS") as ddgs_cls:
        ddgs_cls.return_value.text.side_effect = exc
        out = await registry.dispatch("web.search", {"query": "q"})
    assert hint in out["error"]


@pytest.mark.parametrize(
    ("args", "error"),
    [
        pytest.param({"query": ""}, "`query` must have at least 1 character.", id="empty-query"),
        pytest.param(
            {"query": "q", "max_results": 0},
            "`max_results` must be greater than or equal to 1.",
            id="zero-results",
        ),
        pytest.param(
            {"query": "q", "max_results": 21},
            "`max_results` must be less than or equal to 20.",
            id="over-cap",
        ),
        pytest.param(
            {"query": "q", "timelimit": "z"},
            "`timelimit` must match pattern '^[dwmy]$'.",
            id="bad-timelimit",
        ),
        pytest.param(
            {"query": "x" * 401}, "`query` must have at most 400 characters.", id="query-too-long"
        ),
    ],
)
async def test_search_rejects_invalid_args(
    registry: ToolRegistry, args: dict[str, Any], error: str
) -> None:
    out = await registry.dispatch("web.search", args)
    assert out["error"] == error


# ---------------------------------------------------------------------------
# the public-address guard
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        pytest.param("file:///etc/passwd", id="file-scheme"),
        pytest.param("ftp://mirror.example/x", id="ftp-scheme"),
        pytest.param("http://", id="no-host"),
        pytest.param("http://localhost:8080/admin", id="localhost"),
        pytest.param("http://api.localhost/x", id="dot-localhost"),
        pytest.param("http://printer.local/x", id="mdns-local"),
        pytest.param("http://vault.internal/secrets", id="dot-internal"),
        pytest.param("http://127.0.0.1:9200/", id="loopback-v4"),
        pytest.param("http://[::1]/x", id="loopback-v6"),
        pytest.param("http://10.0.0.5/x", id="rfc1918-10"),
        pytest.param("http://172.16.0.1/x", id="rfc1918-172"),
        pytest.param("http://192.168.1.1/x", id="rfc1918-192"),
        pytest.param("http://169.254.169.254/latest/meta-data/", id="cloud-metadata"),
    ],
)
def test_refuse_non_public(url: str) -> None:
    with pytest.raises(ToolError):
        _refuse_non_public(url)


def test_refuse_name_resolving_to_private_address() -> None:
    # A perfectly public-looking NAME that resolves inward is refused too.
    with patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("192.168.7.7", 0))]) as resolver:
        with pytest.raises(ToolError, match="non-public"):
            _refuse_non_public("http://intranet.example.com/x")
    assert resolver.call_args.args[0] == "intranet.example.com"


def test_allows_public_ip_and_public_name() -> None:
    assert _refuse_non_public("https://93.184.216.34/x") == "https://93.184.216.34/x"
    with patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("93.184.216.34", 0))]):
        assert (
            _refuse_non_public(" https://example.com/page ") == "https://example.com/page"
        )  # stripped


def test_unresolvable_host_is_a_clean_tool_error() -> None:
    with patch("socket.getaddrinfo", side_effect=OSError("NXDOMAIN")):
        with pytest.raises(ToolError, match="could not resolve"):
            _refuse_non_public("https://no-such-host.example.com/")


# ---------------------------------------------------------------------------
# web.fetch (primp mocked)
# ---------------------------------------------------------------------------


class _FakeResponse(SimpleNamespace):
    """The primp streaming-Response surface `_run_fetch` reads: status_code, url,
    headers, iter_bytes(chunk), close()."""


def _resp(
    *,
    status: int = 200,
    url: str = "https://site.example/page",
    html: bool = True,
    body: str = "<html><head><title>Hi</title></head><body><p>Body</p></body></html>",
    location: str | None = None,
) -> _FakeResponse:
    """A fake streamed response. `body` is the RAW page bytes source; `_run_fetch`
    derives the returned content from it (markdownify for HTML, verbatim else)."""
    headers = {"content-type": "text/html; charset=utf-8" if html else "text/plain"}
    if location is not None:
        headers["location"] = location
    data = body.encode("utf-8")

    def _iter_bytes(chunk: int | None = None) -> Any:
        step = chunk or 65536
        for i in range(0, len(data), step):
            yield data[i : i + step]

    return _FakeResponse(
        status_code=status,
        url=url,
        headers=headers,
        iter_bytes=_iter_bytes,
        close=lambda: None,
    )


def _public_resolver() -> Any:
    return patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("93.184.216.34", 0))])


def test_fetch_html_converts_to_markdown_with_title() -> None:
    body = (
        "<html><head><title>Hi</title></head><body>"
        "<h1>Heading</h1><p>Body</p><pre><code>x = 1</code></pre>"
        "<script>tracker()</script></body></html>"
    )
    with _public_resolver(), patch("primp.Client") as client_cls:
        client_cls.return_value.get.return_value = _resp(body=body)
        out = _run_fetch(WebFetchInput(url="https://site.example/page"))
    assert out.status == 200
    assert out.title == "Hi"
    # Structure is preserved as Markdown; <script> noise is stripped.
    assert "# Heading" in out.content
    assert "Body" in out.content
    assert "x = 1" in out.content  # code block text NOT stripped
    assert "tracker()" not in out.content
    assert not out.truncated
    # The client impersonates a real browser, streams, and follows no redirects
    # automatically (each hop must re-pass the public-address check).
    assert client_cls.call_args.kwargs["impersonate"] == "chrome"
    assert client_cls.call_args.kwargs["follow_redirects"] is False
    assert client_cls.return_value.get.call_args.kwargs["stream"] is True


def test_fetch_non_html_returns_body_verbatim_no_title() -> None:
    with _public_resolver(), patch("primp.Client") as client_cls:
        client_cls.return_value.get.return_value = _resp(html=False, body='{"a": 1}')
        out = _run_fetch(WebFetchInput(url="https://site.example/data.json"))
    assert out.content == '{"a": 1}'  # not markdownified
    assert out.title == ""


def test_fetch_truncates_to_max_chars_and_reports_total() -> None:
    body = "x" * 5000
    with _public_resolver(), patch("primp.Client") as client_cls:
        client_cls.return_value.get.return_value = _resp(html=False, body=body)
        out = _run_fetch(WebFetchInput(url="https://site.example/big", max_chars=500))
    assert out.truncated
    assert out.total_chars == 5000
    assert out.content == body[:500]


def test_fetch_caps_oversized_body_at_the_byte_ceiling() -> None:
    # The P1 fix: the body is STREAMED and stops at a hard byte ceiling, so a huge
    # or compression-bomb response can't exhaust the parent process — even if
    # max_chars would allow more. (iter_bytes yields DECOMPRESSED bytes, so the
    # cap bounds the real size.) Patch the ceiling + chunk small so the cap bites
    # mid-stream without allocating megabytes in the test.
    body = "z" * 5000
    with (
        _public_resolver(),
        patch("primp.Client") as client_cls,
        patch("alkera_cli.plugins.plugin_base.web_tools._MAX_FETCH_BYTES", 1000),
        patch("alkera_cli.plugins.plugin_base.web_tools._STREAM_CHUNK", 256),
    ):
        client_cls.return_value.get.return_value = _resp(html=False, body=body)
        out = _run_fetch(WebFetchInput(url="https://site.example/huge", max_chars=1_000_000))
    assert out.truncated  # byte ceiling hit, though max_chars was generous
    assert len(out.content) < 5000  # stopped early — memory bounded
    assert body.startswith(out.content)  # a clean prefix of the page


def test_fetch_max_chars_ceiling_is_a_full_page() -> None:
    # The ceiling is deliberately generous (whole-page capture) — the blob-spill
    # net below, not a small cap, is what protects the context.
    assert WebFetchInput(url="https://x.example", max_chars=1_000_000).max_chars == 1_000_000
    with pytest.raises(ValueError):
        WebFetchInput(url="https://x.example", max_chars=1_000_001)


class _ApprovingBroker:
    """The session permission broker, approving whatever it is asked. ``web.fetch``
    is an egress, so a dispatch of it only reaches the request once a broker has
    said yes; these cases are about the fetch result, not the approval."""

    async def resolve(self, request: Any) -> str:
        return "allow_once"


async def test_fetch_oversized_result_spills_to_a_blob(registry: ToolRegistry) -> None:
    """A big `web_fetch` rides the SAME spill net as `sql.query`: over the inline
    cap the dispatch result is a preview + blob HANDLE, never a context flood."""
    body = "y" * 200_000  # ~3x the 64KiB inline cap, under the 16MiB byte ceiling
    with _public_resolver(), patch("primp.Client") as client_cls:
        client_cls.return_value.get.return_value = _resp(html=False, body=body)
        out = await registry.dispatch(
            "web.fetch",
            {"url": "https://site.example/huge", "max_chars": 500_000},
            broker=_ApprovingBroker(),
            decision_sink=MemorySink(),
        )
    assert out["truncated"] is True
    assert "blob" in out and out["blob"]["sha256"]
    assert len(out["preview"]) <= 2048
    # And the full content survived intact in the blob envelope (kind="text",
    # whose text is the JSON-encoded tool result — the fetch_result page source).
    import json as _json

    envelope = _json.loads(registry._blobs.read(out["blob"]["sha256"]))
    assert _json.loads(envelope["text"])["content"] == body


def test_fetch_follows_redirects_rechecking_each_hop() -> None:
    hops = [
        _resp(status=302, location="https://next.example/dest"),
        _resp(url="https://next.example/dest", html=False, body="landed"),
    ]
    with _public_resolver(), patch("primp.Client") as client_cls:
        client_cls.return_value.get.side_effect = hops
        out = _run_fetch(WebFetchInput(url="https://site.example/start"))
    assert out.content == "landed"
    assert out.url == "https://next.example/dest"
    calls = [c.args[0] for c in client_cls.return_value.get.call_args_list]
    assert calls == ["https://site.example/start", "https://next.example/dest"]


def test_fetch_redirect_to_private_address_is_refused() -> None:
    # The whole point of the manual redirect loop: a PUBLIC url 302-ing into a
    # private address must be refused at the hop, not fetched.
    with _public_resolver(), patch("primp.Client") as client_cls:
        client_cls.return_value.get.return_value = _resp(
            status=302, location="http://169.254.169.254/latest/meta-data/"
        )
        with pytest.raises(ToolError, match="non-public"):
            _run_fetch(WebFetchInput(url="https://site.example/redirect"))


def test_fetch_relative_redirect_resolves_against_current_url() -> None:
    hops = [
        _resp(status=301, location="/moved"),
        _resp(url="https://site.example/moved", html=False, body="here"),
    ]
    with _public_resolver(), patch("primp.Client") as client_cls:
        client_cls.return_value.get.side_effect = hops
        out = _run_fetch(WebFetchInput(url="https://site.example/old"))
    calls = [c.args[0] for c in client_cls.return_value.get.call_args_list]
    assert calls[1] == "https://site.example/moved"
    assert out.content == "here"


def test_fetch_too_many_redirects_is_a_tool_error() -> None:
    with _public_resolver(), patch("primp.Client") as client_cls:
        client_cls.return_value.get.return_value = _resp(status=302, location="/again")
        with pytest.raises(ToolError, match="too many redirects"):
            _run_fetch(WebFetchInput(url="https://site.example/loop"))


def test_fetch_redirect_without_location_returns_that_response() -> None:
    with _public_resolver(), patch("primp.Client") as client_cls:
        client_cls.return_value.get.return_value = _resp(status=302, html=False, body="dead end")
        out = _run_fetch(WebFetchInput(url="https://site.example/x"))
    assert out.status == 302
    assert out.content == "dead end"


def test_fetch_transport_error_is_a_tool_error() -> None:
    with _public_resolver(), patch("primp.Client") as client_cls:
        client_cls.return_value.get.side_effect = RuntimeError("connection reset")
        with pytest.raises(ToolError, match=r"web\.fetch failed"):
            _run_fetch(WebFetchInput(url="https://site.example/x"))


def test_fetch_no_title_tag_is_not_fatal() -> None:
    with _public_resolver(), patch("primp.Client") as client_cls:
        client_cls.return_value.get.return_value = _resp(
            body="<html><body><p>text</p></body></html>"
        )
        out = _run_fetch(WebFetchInput(url="https://site.example/x"))
    assert out.title == ""
    assert "text" in out.content


# ---------------------------------------------------------------------------
# org-flag-gated registration + the runtime's flag resolution
# ---------------------------------------------------------------------------


async def _plugin_registry(tmp_path: Path) -> PluginRegistry:
    plugins = PluginRegistry(ProjectDirectory(tmp_path / ".alkera"), tmp_path)
    await plugins.discover()
    await plugins.evaluate_activation(WorkspaceEvent(kind="open", workspace_root=tmp_path))
    return plugins


async def test_registration_defaults_off(tmp_path: Path) -> None:
    plugins = await _plugin_registry(tmp_path)
    registry = plugins.tool_registry()
    names = {s.name for s in registry.all_specs()}
    assert "web.search" not in names
    assert "web.fetch" not in names
    # And the binding backstop refuses too — not merely hidden.
    out = await registry.dispatch("web.search", {"query": "q"})
    assert "unknown tool" in out["error"]


async def test_registration_on_when_org_flag_set(tmp_path: Path) -> None:
    plugins = await _plugin_registry(tmp_path)
    registry = plugins.tool_registry(web_search_enabled=True)
    specs = {s.name: s for s in registry.all_specs()}
    assert specs["web.search"].hot and specs["web.fetch"].hot
    assert specs["web.search"].effect_hint == Effect.READ
    # Reading the page a search found is the other half of one read, so it
    # carries the same hint — and the same availability in every mode.
    assert specs["web.fetch"].effect_hint == Effect.READ
    # Advertised on the DEDICATED `web` mount under bare names — so the backend's
    # `<server>_<tool>` composition reads `web_search`/`web_fetch` — and NOT on the
    # main `alkera` mount (which would compose an `alkera_`-prefixed name).
    assert {d.name for d in web_tool_descriptors(registry)} == {"search", "fetch"}
    alkera_advertised = {d.name for d in alkera_tool_descriptors(registry)}
    assert not {n for n in alkera_advertised if n.startswith("web.")}


async def test_web_mount_names_map_back_to_registry_names(tmp_path: Path) -> None:
    """The `web` mount's call path: an advertised bare name dispatches the real
    registry tool (the loopback server routes `search` → `web.search`)."""
    plugins = await _plugin_registry(tmp_path)
    registry = plugins.tool_registry(web_search_enabled=True)
    assert web_registry_name("search") == "web.search"
    with patch("ddgs.DDGS") as ddgs_cls:
        ddgs_cls.return_value.text.return_value = [
            {"title": "T", "href": "https://t.example", "body": "s"}
        ]
        out = await registry.dispatch(web_registry_name("search"), {"query": "q"})
    assert out["results"][0]["url"] == "https://t.example"


async def test_web_mount_respects_tool_scope(tmp_path: Path) -> None:
    # A subagent restricted to an explicit allowlist that omits the web tools
    # must not see them on the web mount either (list AND dispatch are paired).
    plugins = await _plugin_registry(tmp_path)
    registry = plugins.tool_registry(web_search_enabled=True)
    assert web_tool_descriptors(registry, ["sql.query"]) == []
    # A read_only subagent is the one sent to find things out, and both halves of
    # that loop are reads: it keeps the search AND the page it found.
    assert {d.name for d in web_tool_descriptors(registry, "read_only")} == {"search", "fetch"}


async def test_runtime_bool_flag_threads_to_tool_registry(tmp_path: Path) -> None:
    on = HarnessRuntime(ProjectDirectory(tmp_path / "on" / ".alkera"), web_search_enabled=True)
    off = HarnessRuntime(ProjectDirectory(tmp_path / "off" / ".alkera"))
    names_on = {s.name for s in (await on.tool_registry()).all_specs()}
    names_off = {s.name for s in (await off.tool_registry()).all_specs()}
    assert "web.search" in names_on
    assert "web.search" not in names_off


async def test_runtime_async_provider_is_resolved_every_build_not_cached(tmp_path: Path) -> None:
    # The org flag is re-read on EACH registry build, never memoized — so an admin
    # who later disables web access sees the tools drop on the next rebuild, rather
    # than them staying live for the daemon's lifetime.
    state = {"enabled": True, "calls": 0}

    async def provider() -> bool:
        state["calls"] += 1
        return bool(state["enabled"])

    rt = HarnessRuntime(ProjectDirectory(tmp_path / ".alkera"), web_search_enabled=provider)
    names = {s.name for s in (await rt.tool_registry()).all_specs()}
    assert "web.search" in names
    assert state["calls"] == 1

    # A later disable IS observed on the next resolution (no sticky True).
    state["enabled"] = False
    assert await rt._resolve_web_search_flag() == WebToolFlags()
    assert state["calls"] == 2


@pytest.mark.parametrize(
    ("flag", "expect_web_mount"),
    [pytest.param(True, True, id="flag-on"), pytest.param(False, False, id="flag-off")],
)
async def test_open_chat_injects_the_web_mount_only_when_enabled(
    tmp_path: Path, flag: bool, expect_web_mount: bool
) -> None:
    """The spawned backend's MCP config: with the org flag on, a SECOND `web`
    server entry appears (its URL the dedicated /mcp-web mount) so the model sees
    `web_search`/`web_fetch`; with it off, only the `alkera` entry — no empty
    extra server to connect to."""
    from alkera_cli.harness._fake import FakeAdapter
    from alkera_cli.harness.adapter import SessionConfig
    from alkera_cli.harness.event_bus import EventBus
    from alkera_cli.harness.runtime import AdapterFactory

    class _RecordingFactory(AdapterFactory):
        def __init__(self) -> None:
            super().__init__(binary=None)
            self.configs: list[SessionConfig] = []

        def __call__(  # type: ignore[override]
            self, config: SessionConfig, *, bus: EventBus, harness_type: str = "agent"
        ) -> FakeAdapter:
            self.configs.append(config)
            adapter = FakeAdapter()
            adapter._bus = bus
            return adapter

    factory = _RecordingFactory()
    rt = HarnessRuntime(
        ProjectDirectory(tmp_path / ".alkera"),
        adapter_factory=factory,
        web_search_enabled=flag,
    )
    session = await rt.open_chat(create=True)
    try:
        cfg = factory.configs[-1]
        mcp = cfg.harness_native["alkera_mcp"]
        assert "alkera" in mcp
        if expect_web_mount:
            assert mcp["web"]["url"].endswith("/mcp-web")
            assert mcp["web"]["type"] == "remote"
            assert mcp["web"]["headers"] == mcp["alkera"]["headers"]  # same bearer
        else:
            assert "web" not in mcp
    finally:
        await rt.close_chat(session.session_id)


@pytest.mark.parametrize(
    ("flags", "reachable"),
    [
        pytest.param(WebToolFlags(search=True, fetch=True), True, id="switch-on"),
        pytest.param(WebToolFlags(search=True, fetch=False), False, id="switch-off"),
        pytest.param(WebToolFlags(), False, id="org-web-off"),
    ],
)
async def test_open_chat_hands_the_claude_backend_the_web_fetch_decision(
    tmp_path: Path, flags: WebToolFlags, reachable: bool
) -> None:
    """The claude backend has no `web` MCP mount — its web reach is the vendor's
    own WebFetch, which the registry cannot take away on its own. The runtime has
    to hand the adapter the SAME decision the opencode mount is derived from, or
    `AGENT_WEB_FETCH_ENABLED=false` holds on one backend and not the other."""
    from alkera_cli.harness._fake import FakeAdapter
    from alkera_cli.harness.adapter import SessionConfig
    from alkera_cli.harness.event_bus import EventBus
    from alkera_cli.harness.registry import CLAUDE_HARNESS
    from alkera_cli.harness.runtime import AdapterFactory

    class _RecordingFactory(AdapterFactory):
        def __init__(self) -> None:
            super().__init__(binary=None)
            self.configs: list[SessionConfig] = []

        def __call__(  # type: ignore[override]
            self, config: SessionConfig, *, bus: EventBus, harness_type: str = "agent"
        ) -> FakeAdapter:
            self.configs.append(config)
            adapter = FakeAdapter()
            adapter._bus = bus
            return adapter

    factory = _RecordingFactory()
    rt = HarnessRuntime(
        ProjectDirectory(tmp_path / ".alkera"),
        adapter_factory=factory,
        web_search_enabled=flags,
    )
    session = await rt.open_chat(create=True, harness_type=CLAUDE_HARNESS)
    try:
        native = factory.configs[-1].harness_native
        assert native["web_fetch_enabled"] is reachable
        # Claude never gets the mount either way — the flag IS the whole seam.
        assert "web" not in native["alkera_mcp"]
    finally:
        await rt.close_chat(session.session_id)


async def test_runtime_provider_failure_reads_disabled_and_can_retry(tmp_path: Path) -> None:
    attempts = 0

    async def flaky() -> bool:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("gateway down")
        return True

    rt = HarnessRuntime(ProjectDirectory(tmp_path / ".alkera"), web_search_enabled=flaky)
    names = {s.name for s in (await rt.tool_registry()).all_specs()}
    assert "web.search" not in names  # fail-closed, no crash
    # A failed/False read is NOT memoized — a later rebuild can still enable.
    assert await rt._resolve_web_search_flag() == WebToolFlags(search=True, fetch=True)
    assert attempts == 2


async def test_a_bool_provider_still_means_both_web_tools(tmp_path: Path) -> None:
    """A provider written before `web.fetch` got its own deployment switch
    answers with a plain bool. It must keep meaning what it always meant —
    both tools — rather than silently dropping fetch."""

    async def legacy() -> bool:
        return True

    rt = HarnessRuntime(ProjectDirectory(tmp_path / ".alkera"), web_search_enabled=legacy)
    names = {s.name for s in (await rt.tool_registry()).all_specs()}
    assert {"web.search", "web.fetch"} <= names
