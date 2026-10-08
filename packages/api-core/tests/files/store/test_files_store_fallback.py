"""A run whose object store is not there runs on a directory instead.

Almost nothing in the default suite is about S3 — the Files suites install a
filesystem factory of their own — but plenty of it reaches the store because
PRODUCTION code does: making an org makes a dedup domain, and making a domain
stamps its prefix. Pointed at an endpoint nothing is listening on, every one of
those pays a connect ladder, and on a CI runner with no object store at all
that arithmetic is what turns a job into one that does not finish.

So the root conftest asks the endpoint whether it is there, once, before any
app import, and stands the filesystem driver in when it is not.
"""

from __future__ import annotations

import socket
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from alkera_core.config import settings


@pytest.fixture
def listening() -> Iterator[str]:
    """An endpoint something is actually listening on."""
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    try:
        yield f"http://127.0.0.1:{listener.getsockname()[1]}"
    finally:
        listener.close()


@pytest.fixture
def closed() -> Iterator[str]:
    """An endpoint nothing is listening on."""
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    yield f"http://127.0.0.1:{port}"


def test_an_endpoint_that_is_listening_is_left_alone(
    files_endpoint_probe: Callable[[str | None], bool], listening: str
) -> None:
    """The decision has to be a real connection, not a guess from the URL: a
    session that HAS a store must keep running against it."""
    assert files_endpoint_probe(listening) is True


def test_an_endpoint_nothing_answers_is_reported_absent(
    files_endpoint_probe: Callable[[str | None], bool], closed: str
) -> None:
    """The case the fallback exists for. A dead port is the shape a pr-gate
    runner presents, where `.env.workspace` names a SeaweedFS nobody started."""
    assert files_endpoint_probe(closed) is False


@pytest.mark.parametrize(
    "endpoint",
    [
        pytest.param("", id="nothing configured"),
        pytest.param(None, id="unset"),
        pytest.param("not a url at all", id="unreadable"),
        pytest.param("http://", id="no host"),
    ],
)
def test_an_endpoint_that_cannot_be_read_counts_as_present(
    files_endpoint_probe: Callable[[str | None], bool], endpoint: str | None
) -> None:
    """This one fails OPEN, the opposite direction to the database guard. The
    cost of being wrong here is silently running a suite that meant to exercise
    a real endpoint against a local directory, which is a false green — so
    anything the check cannot resolve is treated as a store that is there."""
    assert files_endpoint_probe(endpoint) is True


def test_what_the_session_fell_back_to_is_what_the_code_reads(
    files_store_fallback: tuple[str, str] | None,
) -> None:
    """The invariant either way round. A session that fell back runs the
    filesystem driver under the directory it made for itself; one that did not
    is still pointed at the endpoint it was configured with. Nothing in between
    — a half-applied fallback would have the drivers and the settings
    disagreeing about where this run's bytes live.
    """
    if files_store_fallback is None:
        assert settings.files_store_provider in ("filesystem", "s3_compatible", "aws")
        return
    _endpoint, root = files_store_fallback
    assert settings.files_store_provider == "filesystem"
    assert settings.files_store_root_path == Path(root)


def test_the_fallback_never_touches_the_bucket_this_run_owns(
    files_store_fallback: tuple[str, str] | None,
    files_test_bucket: str | None,
) -> None:
    """Standing in a directory is a change of DRIVER, not of tenancy: the
    bucket name the run was given stays exactly as the isolation set it, so the
    invariant that the suite never writes into the deployment's bucket is
    decided in one place and this cannot quietly reopen it."""
    assert settings.files_store_bucket == files_test_bucket


def test_the_run_says_in_its_header_which_driver_it_is_on(
    files_store_header: Callable[[], list[str]],
    files_store_fallback: tuple[str, str] | None,
) -> None:
    """A suite that quietly ran on a directory is a CI log with nothing in it
    to say so — and the decision is taken before a terminal reporter exists, so
    anything printed at conftest import is swallowed by pytest's own capture.
    The header is where every run prints, so that is where it goes; a session
    that kept its endpoint says nothing, because there is nothing to say."""
    header = files_store_header()

    if files_store_fallback is None:
        assert header == []
        return
    endpoint, root = files_store_fallback
    assert len(header) == 1
    assert root in header[0]
    assert (endpoint or "<unset>") in header[0]
    assert "filesystem" in header[0]
