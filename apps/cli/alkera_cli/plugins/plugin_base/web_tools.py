"""The local web tools: ``web.search`` (key-less metasearch) and ``web.fetch``
(browse a result page).

Both run in the parent alkera process (the loopback-MCP host), from the user's
own machine and network, so the agent subprocess's gateway-only lockdown is
untouched and no search-provider API key is needed. ``ddgs`` aggregates several
engines with fallback when one rate-limits; its ``primp`` client (reused by
``web.fetch``) uses a real browser's TLS/HTTP2 fingerprint.

Whether a chat gets them is the org-level ``web_search_enabled`` toggle,
narrowed by the deployment's ``web.fetch`` switch. The registry carries both
tools and the toggle is applied per chat by withholding them from that chat's
view (``withheld_web_tools`` + ``ToolRegistry.restricted``), so on a box shared
by several orgs one chat's org never decides for the next.

Both tools are READ in every permission mode, ``read_only`` and ``plan``
included: searching is useless if the agent cannot open what it found.

What binds regardless of mode is where a fetch may go (:func:`_vet`, over
``alkera_core.egress``): http(s) only, and never a host that is or resolves to a
loopback, private, link-local, shared or otherwise non-global address (on a box,
``169.254.169.254`` is the cloud metadata service). The check covers the name
and every resolved address, refuses if any address is non-global, and runs again
at every redirect hop. The URL is parsed strictly and rebuilt, and only the
rebuilt string is fetched, so a spelling two parsers read differently is refused
or normalised. Each hop connects through a loopback tunnel to the one address
that was vetted, so a resolver that answers differently the second time changes
nothing.

This is a floor for ordinary page reads, not a fence against an agent granted
``bash``/``curl`` (the shell gate governs that). Each fetch also resolves
through the decision engine, so it is logged and a deny rule can refuse a host.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any, ClassVar

from alkera_core.egress import (
    PRIMP_PROXY_ENV,
    Dialer,
    EgressPolicy,
    EgressRefusedError,
    PinnedTunnel,
    Resolver,
    VettedTarget,
    configured_proxy_env,
    proxy_refusal,
    redirect_target,
    vet_url,
)
from pydantic import BaseModel, Field

from alkera_cli.contracts.tool_types import ActionDescriptor, Effect, ResourceRef
from alkera_cli.plugins.plugin_base.permissions import (
    binding_from_context,
    denied_error,
    gate_sql_action,
)
from alkera_cli.plugins.plugin_base.tool import Tool, ToolContext, ToolError, ToolRegistry, ToolSpec

#: One impersonation profile for every request: primp's version-less "chrome"
#: tracks its newest bundled Chrome fingerprint, so this never ages into an
#: implausible (blockable) old-browser signature.
_IMPERSONATE = "chrome"

_SEARCH_TIMEOUT_SECONDS = 10
_FETCH_TIMEOUT_SECONDS = 15
_STREAM_CHUNK = 65536
#: Hard ceiling on the DECOMPRESSED response bytes a single fetch reads into the
#: parent process. We stream the body and stop the instant we cross this, so a
#: huge page — or a compression bomb (``iter_bytes`` yields decompressed bytes,
#: so the cap bounds the real size, not the wire size) — can't exhaust memory.
#: Generous vs any real page; the readable text is then trimmed to ``max_chars``.
_MAX_FETCH_BYTES = 16 * 1024 * 1024
_REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})

_SEARCH_DESCRIPTION = (
    "Search the web. Returns result links (title, url, snippet) from a "
    "metasearch over general-purpose engines — no API key, run from this "
    "machine. Use `web.fetch` to read a promising result's page content.\n\n"
    "Notes:\n"
    "- `backend` defaults to `auto` (pick + fall back automatically); a "
    "comma-separated engine list (e.g. `duckduckgo,google,bing,brave`) pins it.\n"
    "- `timelimit` restricts result age: d (day), w (week), m (month), y (year).\n"
    "- Results are links + snippets only — always fetch the page before "
    "relying on details the snippet merely hints at."
)

_FETCH_DESCRIPTION = (
    "Fetch a web page (typically a `web.search` result) and return its readable "
    "content as Markdown (headings, lists, links, and code blocks preserved). "
    "Follows redirects; refuses private/loopback addresses.\n\n"
    "Big pages are FIRST-CLASS, exactly like `sql.query`: raise `max_chars` (up "
    "to 1,000,000) to capture the whole page — a result too large to inline "
    "spills to a content-addressed BLOB and you get a handle plus a preview. "
    "Page it with `fetch_result(handle, offset)`, or compute over it with the "
    "blob tools, instead of re-fetching with a smaller cap. `total_chars` / "
    "`truncated` report whether YOUR `max_chars` clipped the page — if it did "
    "and you need the rest, re-fetch with a larger `max_chars` and work from "
    "the blob.\n\n"
    "Reads in every permission mode, read-only included. Only PUBLIC web "
    "addresses are reachable: a host that is or resolves to a loopback, "
    "private or link-local address is refused, at every redirect too.\n\n"
    "Not for APIs that need auth — this is an anonymous browser-like GET."
)


class SearchResult(BaseModel):
    title: str = ""
    url: str = ""
    snippet: str = ""


class WebSearchInput(BaseModel):
    query: str = Field(min_length=1, max_length=400)
    max_results: int = Field(default=8, ge=1, le=20)
    region: str = Field(default="us-en", max_length=16)
    """Search locale, e.g. "us-en", "de-de"."""
    timelimit: str | None = Field(default=None, pattern="^[dwmy]$")
    """Restrict result age: d/w/m/y. None = any time."""
    backend: str = Field(default="auto", max_length=120)
    """"auto" or a comma-separated engine list (duckduckgo, google, bing, …)."""


class WebSearchResult(BaseModel):
    results: list[SearchResult] = Field(default_factory=list)
    query: str = ""


class WebFetchInput(BaseModel):
    url: str = Field(min_length=1, max_length=2000)
    max_chars: int = Field(default=20_000, ge=500, le=1_000_000)
    """Cap on returned content characters (`total_chars`/`truncated` report when
    it clipped the page). The ceiling is deliberately generous: an oversized
    result doesn't flood the context — the dispatch layer spills anything over
    the inline cap to a content-addressed blob (preview + handle), the same
    net `sql.query` rides."""


class WebFetchResult(BaseModel):
    url: str = ""
    """Final URL after redirects."""
    status: int = 0
    title: str = ""
    content: str = ""
    """Readable page text (markdown for HTML pages, verbatim otherwise)."""
    truncated: bool = False
    total_chars: int = 0


def _run_search(args: WebSearchInput) -> list[SearchResult]:
    """The blocking ddgs call — runs in a worker thread. Import deferred so
    merely importing this module (e.g. the tool-manifest exporter) never pays
    for ddgs' engine registry."""
    from ddgs import DDGS
    from ddgs.exceptions import DDGSException, RatelimitException, TimeoutException

    try:
        raw = DDGS(timeout=_SEARCH_TIMEOUT_SECONDS).text(
            args.query,
            region=args.region,
            # Unfiltered on purpose: agents research error messages, CVEs, and
            # forum threads that safesearch heuristics misclassify. Fixed (not a
            # model knob) so the surface stays one predictable behavior.
            safesearch="off",
            timelimit=args.timelimit,
            max_results=args.max_results,
            backend=args.backend,
        )
    except RatelimitException as exc:
        raise ToolError(
            "web search rate-limited by the search engines — wait a moment and retry, "
            f"or pass a different `backend`: {exc}"
        ) from exc
    except TimeoutException as exc:
        raise ToolError(f"web search timed out: {exc}") from exc
    except DDGSException as exc:
        raise ToolError(f"web search failed: {exc}") from exc
    out: list[SearchResult] = []
    for item in raw[: args.max_results]:
        url = str(item.get("href") or item.get("url") or "")
        if not url:
            continue
        out.append(
            SearchResult(
                title=str(item.get("title") or ""),
                url=url,
                snippet=str(item.get("body") or ""),
            )
        )
    return out


class WebSearchTool(Tool[WebSearchInput, WebSearchResult]):
    spec: ClassVar[ToolSpec] = ToolSpec(
        name="web.search",
        title="Web search",
        description=_SEARCH_DESCRIPTION,
        app="web",
        hot=True,
        effect_hint=Effect.READ,
    )
    Input: ClassVar[type[BaseModel]] = WebSearchInput
    Output: ClassVar[type[BaseModel]] = WebSearchResult

    async def run(self, args: WebSearchInput, ctx: ToolContext) -> WebSearchResult:
        results = await asyncio.to_thread(_run_search, args)
        return WebSearchResult(results=results, query=args.query)


def _vet(url: str, *, resolver: Resolver | None = None) -> VettedTarget:
    """One hop through the shared guard, as a tool error. The policy is read per
    call, so the operator's ``EGRESS_PRIVATE_ALLOWLIST`` is the only thing that
    can admit a private address and no caller argument can."""
    try:
        return vet_url(url, policy=EgressPolicy.from_settings(), resolver=resolver)
    except EgressRefusedError as exc:
        if exc.code == "private":
            raise ToolError(f"web.fetch refuses non-public addresses: {exc}") from exc
        if exc.code == "local_name":
            raise ToolError(f"web.fetch refuses local/internal hosts: {exc}") from exc
        if exc.code == "scheme":
            raise ToolError(f"web.fetch only fetches http(s) URLs: {exc}") from exc
        raise ToolError(f"web.fetch: {exc}") from exc


def _max_redirects() -> int:
    from alkera_core.config import settings

    return settings.egress_max_redirects


def _refuse_non_public(url: str) -> str:
    """Validate ``url`` is a plain public-web target; returns the REBUILT URL,
    the only string that may be handed to the client. Refuses non-http(s)
    schemes, every spelling two parsers could read differently, and hosts that
    are — or resolve to — loopback / private / link-local / reserved addresses
    (incl. the cloud metadata range).

    This is the only thing standing between a page read and the machine's own
    network, so it is deliberately strict in three ways. It is checked at EVERY
    redirect hop, because a public URL may redirect inward. It checks the
    RESOLVED addresses, not the name, because a name an attacker controls can
    point anywhere — and it refuses if ANY resolved address is non-global, so a
    host answering with one public and one private address does not get in on
    the public one. And it runs whatever the permission mode is: no stance
    admits an internal read."""
    return _vet(url).url.url


#: Tags whose CONTENT is noise (not readable page text) — removed before the
#: Markdown conversion so scripts/styles/head metadata never leak into the output.
_STRIP_TAGS = ("script", "style", "noscript", "template", "head", "svg", "iframe")


def _html_to_markdown_and_title(html: str) -> tuple[str, str]:
    """Parse ``html`` ONCE, pull its ``<title>``, drop noise tags (script/style/
    head/…), and convert the remaining body to Markdown — preserving headings,
    lists, links, and code blocks while passing through the text of any tag it
    doesn't specially handle. Any parse/convert hiccup is non-fatal: fall back to
    the raw HTML string and an empty title, so a fetch never dies on a weird page.
    Uses the stdlib ``html.parser`` (no lxml coupling in the bundled binary)."""
    try:
        from bs4 import BeautifulSoup
        from markdownify import MarkdownConverter

        soup = BeautifulSoup(html, "html.parser")
        title_tag = soup.find("title")
        title = title_tag.get_text().strip() if title_tag else ""
        for tag in soup(_STRIP_TAGS):
            tag.decompose()
        body = soup.body or soup
        markdown = MarkdownConverter(heading_style="ATX").convert_soup(body).strip()
        return markdown, title
    except Exception:
        return html, ""


def _run_fetch(
    args: WebFetchInput, *, resolver: Resolver | None = None, dial: Dialer | None = None
) -> WebFetchResult:
    """The blocking fetch — runs in a worker thread. Redirects are followed
    MANUALLY so each hop is vetted, rebuilt and pinned like the first (a public
    URL may redirect to an internal one), and the body is STREAMED with a hard
    byte cap so an oversized / compression-bomb response can't exhaust the
    parent. ``resolver`` and ``dial`` are the test seams for the name lookup and
    the outbound connection."""
    with contextlib.ExitStack() as tunnels:
        return _fetch_pinned(args, tunnels, resolver=resolver, dial=dial)


def _fetch_pinned(
    args: WebFetchInput,
    tunnels: contextlib.ExitStack,
    *,
    resolver: Resolver | None,
    dial: Dialer | None,
) -> WebFetchResult:
    import primp

    # A forward proxy makes the connection itself, so the address this process
    # vetted is not the one dialed and the tunnel has nothing to pin. That trade
    # is the deployment's to make, not a page's: unless it has been accepted, a
    # fetch that cannot be pinned is refused rather than quietly downgraded.
    #
    # Judged by the variables THIS client reads. primp ignores ALL_PROXY, so a
    # deployment that sets only that one still gets a pinned direct fetch here —
    # standing the tunnel down for it would send the request out direct AND
    # unpinned, through a proxy that was never going to be used.
    refusal = proxy_refusal(PRIMP_PROXY_ENV)
    if refusal is not None:
        raise ToolError(f"web.fetch: {refusal}")
    via_forward_proxy = bool(configured_proxy_env(PRIMP_PROXY_ENV))
    max_redirects = _max_redirects()
    target = _vet(args.url, resolver=resolver)
    resp = None
    for _hop in range(max_redirects + 1):
        url = target.url.url
        options: dict[str, Any] = {}
        if not via_forward_proxy:
            # One tunnel per hop: it connects to THIS hop's vetted address and
            # nothing else, whatever host the client asks it for.
            tunnel = tunnels.enter_context(
                PinnedTunnel(target, timeout=_FETCH_TIMEOUT_SECONDS, dial=dial)
            )
            options["proxy"] = tunnel.proxy_url
        client = primp.Client(
            impersonate=_IMPERSONATE,
            timeout=_FETCH_TIMEOUT_SECONDS,
            follow_redirects=False,
            **options,
        )
        try:
            resp = client.get(url, stream=True)
        except Exception as exc:  # primp raises rust-side error classes
            raise ToolError(f"web.fetch failed for {url}: {exc}") from exc
        if resp.status_code in _REDIRECT_CODES:
            location = dict(resp.headers).get("location", "")
            if location:
                _close_quietly(resp)  # free this hop before following
                try:
                    next_url = redirect_target(target.url, location)
                except EgressRefusedError as exc:
                    raise ToolError(f"web.fetch: {exc}") from exc
                target = _vet(next_url, resolver=resolver)
                continue
            # A redirect status with no Location: nothing to follow — treat the
            # response itself as terminal (leave it OPEN for the body read below).
        break  # terminal response — resp stays open
    else:
        raise ToolError(f"web.fetch: too many redirects (> {max_redirects}) from {args.url}")
    assert resp is not None

    headers = {k.lower(): v for k, v in dict(resp.headers).items()}
    status = int(resp.status_code)
    # The URL the floor built, not the client's echo of it: what is reported is
    # exactly what was vetted.
    final_url = target.url.url
    is_html = "html" in headers.get("content-type", "").lower()

    raw = bytearray()
    body_truncated = False
    try:
        for chunk in resp.iter_bytes(_STREAM_CHUNK):
            raw.extend(chunk)
            if len(raw) >= _MAX_FETCH_BYTES:
                body_truncated = True  # stop before a huge/bomb body exhausts memory
                break
    except Exception as exc:
        raise ToolError(f"web.fetch failed reading {final_url}: {exc}") from exc
    finally:
        _close_quietly(resp)

    body = bytes(raw).decode("utf-8", errors="replace")
    if is_html:
        content, title = _html_to_markdown_and_title(body)
    else:
        content = body  # JSON / plain text / anything non-HTML: verbatim
        title = ""

    total = len(content)
    return WebFetchResult(
        url=final_url,
        status=status,
        title=title,
        content=content[: args.max_chars],
        # Either the raw body hit the byte ceiling OR max_chars clipped the text.
        truncated=body_truncated or total > args.max_chars,
        total_chars=total,
    )


def _close_quietly(resp: Any) -> None:
    with contextlib.suppress(Exception):
        resp.close()


class WebFetchTool(Tool[WebFetchInput, WebFetchResult]):
    spec: ClassVar[ToolSpec] = ToolSpec(
        name="web.fetch",
        title="Web fetch",
        description=_FETCH_DESCRIPTION,
        app="web",
        hot=True,
        effect_hint=Effect.READ,
    )
    Input: ClassVar[type[BaseModel]] = WebFetchInput
    Output: ClassVar[type[BaseModel]] = WebFetchResult

    async def run(self, args: WebFetchInput, ctx: ToolContext) -> WebFetchResult:
        await gate_web_fetch(args.url, ctx)
        return await asyncio.to_thread(_run_fetch, args)


async def gate_web_fetch(url: str, ctx: ToolContext) -> None:
    """Put the read on the record before it is made.

    Reading a public page is a READ, so every mode allows it and nobody is
    prompted — the same answer ``web.search`` gets, because the two halves of
    one loop cannot disagree about whether looking something up is permitted.
    The request still resolves through the decision engine rather than around
    it: the URL lands in the decisions log, and a deny rule that names a host
    still refuses it.

    Where the request may GO is not this function's business and is not the
    mode's either — ``_refuse_non_public`` holds that line on the initial URL
    and on every redirect hop, whatever this decides."""
    descriptor = ActionDescriptor(
        capability="network",
        effect=Effect.READ,
        operation="web_fetch",
        targets=[ResourceRef(kind="url", name=url)],
        raw=url,
        classifier="web_tools",
        confidence="exact",
    )
    gate = await gate_sql_action(
        descriptor, mode=ctx.permission_mode, binding=binding_from_context(ctx)
    )
    if gate.cap_token is None:
        detail = gate.reason or f"fetching {url} was not approved"
        raise ToolError(denied_error(detail))


def register_web_tools(registry: ToolRegistry, *, fetch_enabled: bool = True) -> None:
    """Register the local web tools.

    A registry built for one known org (the CLI's, a test's) registers exactly
    what that org may use; a runtime that serves chats of several orgs
    registers both and hands each chat a view with :func:`withheld_web_tools`
    taken out. ``fetch_enabled=False`` leaves ``web.fetch`` OUT rather than
    refusing it at call time: a tool that is not registered is never advertised
    to the model, so an install that keeps its agents off the public internet
    does not spend a turn on a refusal it was always going to get."""
    registry.register(WebSearchTool)
    if fetch_enabled:
        registry.register(WebFetchTool)


#: Every local web tool, by registry name.
WEB_TOOL_NAMES: frozenset[str] = frozenset({WebSearchTool.spec.name, WebFetchTool.spec.name})


def withheld_web_tools(*, search: bool, fetch: bool) -> frozenset[str]:
    """The web tools a chat whose org flags read ``search`` / ``fetch`` does not
    get. The org toggle governs both; the deployment's fetch switch only ever
    subtracts, so ``fetch`` can never grant past ``search``."""
    if not search:
        return WEB_TOOL_NAMES
    if not fetch:
        return frozenset({WebFetchTool.spec.name})
    return frozenset()


__all__ = [
    "WEB_TOOL_NAMES",
    "SearchResult",
    "WebFetchInput",
    "WebFetchResult",
    "WebFetchTool",
    "WebSearchInput",
    "WebSearchResult",
    "WebSearchTool",
    "gate_web_fetch",
    "register_web_tools",
    "withheld_web_tools",
]
