"""The shared no-oracle probe: three classes of "not yours", one answer.

The no-oracle contract asks two things of every route that takes an id. The response must
be byte-identical for a nonexistent id, an id belonging to another org, and an
id in the caller's own org that the caller may not read — otherwise the status,
a header, or a body field tells a stranger that something exists. And the route
must do the *same work* on all three, or the query count (and, on a busy
server, the latency and the bill) becomes the oracle the body refused to be.

Both checks live here rather than in one test file so the other route lanes
reuse the same probe instead of each writing a weaker one.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from _statement_log import counting
from alkera_core.auth import revocation
from httpx import AsyncClient, Response
from sqlalchemy.engine import Engine

#: Headers that legitimately differ between two responses to two requests and
#: so are excluded from the byte-identity comparison. Nothing else is: a
#: ``Content-Length`` that differed would mean a body that differed, and a
#: ``Retry-After`` or a cache header that differed would be an oracle.
VOLATILE_HEADERS: frozenset[str] = frozenset(
    {"date", "x-request-id", "request-id", "x-trace-id", "server"}
)


@dataclass(frozen=True, slots=True)
class Probe:
    """One observation of a route: what came back and how much work it cost."""

    status: int
    headers: tuple[tuple[str, str], ...]
    body: bytes
    statements: int
    #: The statements themselves, so a count that differs can say *which* work
    #: the two requests did not share. A bare pair of numbers sends the next
    #: reader back to the database to find out.
    sql: tuple[str, ...] = ()


#: What the per-request trace id is replaced with in a compared body. The error
#: envelope carries the trace id the ``X-Trace-Id`` header does, so it is the one
#: member of a refusal's body that differs between two requests by design.
TRACE_MASK = b"<trace-id>"

#: The one body every "not yours" class answers, as a probe records it: the
#: error envelope with its trace id masked, and the flat ``code`` older boxes
#: still read beside it.
OPAQUE_NOT_FOUND_BODY: dict[str, Any] = {
    "error": {
        "type": "urn:alkera:error:not_found",
        "code": "not_found",
        "status": 404,
        "message": "Not found.",
        "trace_id": TRACE_MASK.decode(),
    },
    "code": "not_found",
}


def _comparable_body(response: Response) -> bytes:
    trace_id = response.headers.get("x-trace-id")
    if not trace_id:
        return response.content
    return response.content.replace(trace_id.encode(), TRACE_MASK)


def _comparable(response: Response) -> tuple[tuple[str, str], ...]:
    return tuple(
        sorted(
            (name.lower(), value)
            for name, value in response.headers.items()
            if name.lower() not in VOLATILE_HEADERS
        )
    )


async def probe(
    client: AsyncClient,
    engine: Engine,
    method: str,
    url: str,
    *,
    json: Any | None = None,
    headers: Mapping[str, str] | None = None,
) -> Probe:
    """Send one request and capture its answer and its statement count."""
    with counting(engine) as statements:
        response = await client.request(method, url, json=json, headers=dict(headers or {}))
    return Probe(
        status=response.status_code,
        headers=_comparable(response),
        body=_comparable_body(response),
        statements=len(statements),
        sql=tuple(statements),
    )


def work_report(probes: Sequence[Probe], labels: Sequence[str]) -> str:
    """The statements one probe ran that another did not, named per label.

    The comparison is a multiset difference against the first probe, so a
    repeated statement that one side ran one more time shows up as the one
    extra copy rather than vanishing into a set.
    """
    baseline = Counter(_shape(text) for text in probes[0].sql)
    lines: list[str] = []
    for observed, label in zip(probes[1:], labels[1:], strict=False):
        here = Counter(_shape(text) for text in observed.sql)
        extra = here - baseline
        missing = baseline - here
        for statement, count in extra.items():
            lines.append(f"  {label} ran {count}x more: {statement}")
        for statement, count in missing.items():
            lines.append(f"  {label} ran {count}x fewer: {statement}")
    return "\n".join(lines) if lines else "  (the same statements, in a different order)"


def _shape(statement: str) -> str:
    """One statement, collapsed to a line short enough to read in a failure."""
    flat = " ".join(statement.split())
    return flat if len(flat) <= 160 else f"{flat[:157]}..."


def quiesce_auth() -> None:
    """Put the per-request auth freshness in the same state before a measurement.

    ``assert_token_active`` trusts a cached "this session is not revoked"
    answer for a few seconds and re-checks it against Postgres once it is
    stale, so whether a request pays that one statement is decided by how long
    ago the caller's *previous* request was — not by anything the route did.
    Two requests that are compared to each other must therefore start from the
    same answer, or a loaded runner that puts more than the window between them
    reports a difference in the clock as a difference in the route.

    Dropping the cache makes every measured request re-check, which is the same
    footing ``test_files_statement_counts`` puts both sides of its delta on.
    """
    revocation._cache.reset()


def echoes_any(body: bytes, ids: Sequence[str]) -> str | None:
    """The first id ``body`` echoes back, if any.

    An echoed id is how a body that is otherwise identical still differs in
    ``Content-Length`` — and it is also, on its own, a confirmation that the
    server looked the id up. A "not yours" body must name nothing.
    """
    text = body.decode("utf-8", "replace")
    for candidate in ids:
        if candidate and candidate in text:
            return candidate
    return None


def assert_byte_identical(probes: Sequence[Probe], ids: Sequence[str]) -> None:
    """The three answers agree on status, headers and body, and name no id."""
    first = probes[0]
    for other in probes[1:]:
        assert other.status == first.status, f"status differs: {first.status} vs {other.status}"
        assert other.headers == first.headers, f"headers differ: {first.headers} vs {other.headers}"
        assert other.body == first.body, f"body differs: {first.body!r} vs {other.body!r}"
    for observed, identifier in zip(probes, ids, strict=False):
        leaked = echoes_any(observed.body, [identifier])
        assert leaked is None, f"the response echoed the id it was asked about: {leaked}"


def assert_same_work(probes: Sequence[Probe], labels: Sequence[str] = ()) -> None:
    """The three answers cost the same number of statements."""
    counts = [observed.statements for observed in probes]
    named = list(labels) or [str(index) for index in range(len(probes))]
    assert len(set(counts)) == 1, (
        f"statement counts differ across the three classes: {counts}\n{work_report(probes, named)}"
    )


async def assert_no_oracle(
    client: AsyncClient,
    engine: Engine,
    method: str,
    urls: Sequence[str],
    ids: Sequence[str],
    *,
    json: Any | None = None,
    headers: Mapping[str, str] | None = None,
    same_work: bool = True,
    labels: Sequence[str] = (),
) -> list[Probe]:
    """Drive ``method`` at each of ``urls`` and assert the no-oracle contract.

    ``urls`` are the "not yours" classes already formatted for the route under
    test; ``ids`` are the ids they carry, so the probe can assert none is echoed
    back; ``labels`` names them in the failure report, in the same order. The
    names belong to the caller because the classes do: a helper that assumed
    "nonexistent, other-org, unreadable" would confidently tell the next caller
    which class did the extra work and be wrong about it.
    """
    probes = []
    for url in urls:
        quiesce_auth()
        probes.append(await probe(client, engine, method, url, json=json, headers=headers))
    assert_byte_identical(probes, ids)
    if same_work:
        assert_same_work(probes, labels)
    return probes


__all__ = [
    "VOLATILE_HEADERS",
    "Probe",
    "assert_byte_identical",
    "assert_no_oracle",
    "assert_same_work",
    "counting",
    "echoes_any",
    "probe",
    "quiesce_auth",
    "work_report",
]
