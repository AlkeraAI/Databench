"""The agent assertion headers: built by the CLI, parsed by the backend, and
never spelled anywhere else. Every half-formed combination is an error, not a
silent downgrade to a plain user session."""

from __future__ import annotations

import pytest
from alkera_core.authz import AgentAssertion, AgentHeaderError, agent_headers, parse_agent_assertion
from alkera_core.authz.headers import (
    ACTOR_AGENT,
    ACTOR_HEADER,
    AGENT_ID_HEADER,
    AGENT_ID_PATTERN,
)
from starlette.datastructures import Headers

VALID_IDS = [
    pytest.param("s", id="one-char"),
    pytest.param("s-1", id="dash"),
    pytest.param("sess_42", id="underscore"),
    pytest.param("01HZXK4Q.chat:main-2", id="every-allowed-punctuation"),
    pytest.param("A" * 128, id="128-chars-upper-bound"),
    pytest.param("9abc", id="leading-digit"),
]

INVALID_IDS = [
    pytest.param("", id="empty"),
    pytest.param("has space", id="space"),
    pytest.param("A" * 129, id="129-chars"),
    pytest.param("-leading-dash", id="leading-dash"),
    pytest.param(".leading-dot", id="leading-dot"),
    pytest.param(":leading-colon", id="leading-colon"),
    pytest.param("_leading-underscore", id="leading-underscore"),
    pytest.param("sess/1", id="slash"),
    pytest.param("sess\n1", id="newline"),
    pytest.param("sess\r\nX-Injected: 1", id="crlf-injection"),
    pytest.param("séss", id="non-ascii"),
    pytest.param("sess 1\x00", id="nul"),
]


# ---------------------------------------------------------------------------
# Constants — the single place the header names live
# ---------------------------------------------------------------------------


def test_header_names_are_pinned() -> None:
    assert ACTOR_HEADER == "X-Alkera-Actor"
    assert AGENT_ID_HEADER == "X-Alkera-Agent-Id"
    assert ACTOR_AGENT == "agent"
    assert AGENT_ID_PATTERN.pattern == r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"


def test_agent_header_error_is_a_value_error() -> None:
    assert issubclass(AgentHeaderError, ValueError)


# ---------------------------------------------------------------------------
# agent_headers — the builder
# ---------------------------------------------------------------------------


def test_agent_headers_builds_both_headers() -> None:
    assert agent_headers("s-1") == {"X-Alkera-Actor": "agent", "X-Alkera-Agent-Id": "s-1"}


@pytest.mark.parametrize("session_id", VALID_IDS)
def test_agent_headers_round_trip_through_the_parser(session_id: str) -> None:
    assert parse_agent_assertion(agent_headers(session_id)) == AgentAssertion(session_id=session_id)


@pytest.mark.parametrize("session_id", INVALID_IDS)
def test_agent_headers_refuses_an_id_the_server_would_reject(session_id: str) -> None:
    with pytest.raises(AgentHeaderError):
        agent_headers(session_id)


# ---------------------------------------------------------------------------
# parse_agent_assertion — the parser
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "headers",
    [
        pytest.param({}, id="no-headers"),
        pytest.param(
            {"Authorization": "Bearer x", "Content-Type": "application/json"}, id="unrelated"
        ),
        pytest.param(
            {"X-Alkera-Actor-Extra": "agent", "X-Alkera-Agent": "s-1"}, id="near-miss-names"
        ),
    ],
)
def test_no_assertion_when_neither_header_is_present(headers: dict[str, str]) -> None:
    assert parse_agent_assertion(headers) is None


@pytest.mark.parametrize("session_id", VALID_IDS)
def test_valid_pair_parses(session_id: str) -> None:
    headers = {"X-Alkera-Actor": "agent", "X-Alkera-Agent-Id": session_id}
    assert parse_agent_assertion(headers) == AgentAssertion(session_id=session_id)


@pytest.mark.parametrize(
    "headers",
    [
        pytest.param({"x-alkera-actor": "agent", "x-alkera-agent-id": "s-1"}, id="lowercase"),
        pytest.param({"X-ALKERA-ACTOR": "agent", "X-ALKERA-AGENT-ID": "s-1"}, id="uppercase"),
        pytest.param({"x-Alkera-actor": "agent", "X-alkera-Agent-id": "s-1"}, id="mixed"),
    ],
)
def test_header_names_are_case_insensitive(headers: dict[str, str]) -> None:
    assert parse_agent_assertion(headers) == AgentAssertion(session_id="s-1")


@pytest.mark.parametrize(
    "headers",
    [
        pytest.param({"X-Alkera-Actor": "user", "X-Alkera-Agent-Id": "s-1"}, id="actor-not-agent"),
        pytest.param(
            {"X-Alkera-Actor": "Agent", "X-Alkera-Agent-Id": "s-1"},
            id="actor-value-is-case-sensitive",
        ),
        pytest.param(
            {"X-Alkera-Actor": "agent ", "X-Alkera-Agent-Id": "s-1"}, id="actor-trailing-space"
        ),
        pytest.param({"X-Alkera-Actor": "", "X-Alkera-Agent-Id": "s-1"}, id="actor-empty"),
        pytest.param({"X-Alkera-Actor": "agent"}, id="actor-without-id"),
        pytest.param({"X-Alkera-Agent-Id": "s-1"}, id="id-without-actor"),
        pytest.param({"X-Alkera-Actor": "user"}, id="non-agent-actor-alone"),
        pytest.param({"X-Alkera-Actor": "agent", "X-Alkera-Agent-Id": ""}, id="id-empty"),
        pytest.param(
            {"X-Alkera-Actor": "agent", "x-alkera-actor": "user", "X-Alkera-Agent-Id": "s-1"},
            id="actor-given-twice-with-different-values",
        ),
        pytest.param(
            {"X-Alkera-Actor": "agent", "X-Alkera-Agent-Id": "s-1", "x-alkera-agent-id": "s-2"},
            id="id-given-twice-with-different-values",
        ),
    ],
)
def test_half_formed_or_contradictory_assertions_are_errors(headers: dict[str, str]) -> None:
    with pytest.raises(AgentHeaderError):
        parse_agent_assertion(headers)


@pytest.mark.parametrize("session_id", INVALID_IDS)
def test_malformed_agent_id_is_an_error(session_id: str) -> None:
    with pytest.raises(AgentHeaderError):
        parse_agent_assertion({"X-Alkera-Actor": "agent", "X-Alkera-Agent-Id": session_id})


def test_same_header_repeated_with_the_same_value_is_one_header() -> None:
    headers = {"X-Alkera-Actor": "agent", "x-alkera-actor": "agent", "X-Alkera-Agent-Id": "s-1"}
    assert parse_agent_assertion(headers) == AgentAssertion(session_id="s-1")


def test_parser_reads_a_real_asgi_headers_object() -> None:
    """The backend passes `request.headers` straight in: a case-insensitive
    multi-mapping whose raw pairs may repeat."""
    ok = Headers(
        raw=[(b"host", b"api"), (b"x-alkera-actor", b"agent"), (b"x-alkera-agent-id", b"s-1")]
    )
    assert parse_agent_assertion(ok) == AgentAssertion(session_id="s-1")
    assert parse_agent_assertion(Headers(raw=[(b"host", b"api")])) is None
    contradictory = Headers(
        raw=[
            (b"x-alkera-actor", b"agent"),
            (b"x-alkera-agent-id", b"s-1"),
            (b"x-alkera-agent-id", b"s-2"),
        ]
    )
    with pytest.raises(AgentHeaderError):
        parse_agent_assertion(contradictory)


def test_parser_accepts_any_mapping_of_header_pairs() -> None:
    """Server frameworks hand over a case-insensitive multi-mapping whose
    `.items()` yields raw pairs; the parser only needs that protocol."""

    class Pairs:
        def __init__(self, raw: list[tuple[str, str]]) -> None:
            self._raw = raw

        def items(self) -> list[tuple[str, str]]:
            return list(self._raw)

    pairs = Pairs([("x-alkera-actor", "agent"), ("x-alkera-agent-id", "s-1"), ("host", "api")])
    assert parse_agent_assertion(pairs) == AgentAssertion(session_id="s-1")  # type: ignore[arg-type]
