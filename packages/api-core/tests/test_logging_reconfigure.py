"""A second ``configure_logging()`` must reach the loggers already in use.

``configure_logging`` sets ``cache_logger_on_first_use``, so a module-level
``log = get_logger(__name__)`` freezes the processor chain it was first used
with. More than one entrypoint configures logging in a single process -- the
FastAPI app factory, the Slack socket-mode runner, a test that builds a second
app -- and every one of them must land on the loggers that already spoke, or a
newly configured SIEM sink silently never hears from them.
"""

from __future__ import annotations

import json
import socket
from collections.abc import Iterator

import pytest
from alkera_core.config import settings
from alkera_core.logging import configure_logging, get_logger
from structlog.testing import capture_logs


@pytest.fixture(autouse=True)
def _restore_global_logging() -> Iterator[None]:
    # structlog's configuration is process-global; hand the next test the chain
    # this process's settings actually ask for.
    yield
    configure_logging()


def _udp_listener() -> tuple[socket.socket, int]:
    srv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    srv.bind(("127.0.0.1", 0))
    # A pass-fast deadline: loopback UDP either arrives at once or the chain the
    # logger is running never had the forwarder in it.
    srv.settimeout(5.0)
    return srv, srv.getsockname()[1]


def test_a_reconfigure_reaches_a_logger_that_already_logged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The production consequence: a syslog endpoint configured on the second
    call must receive events from a logger that was already warm."""
    configure_logging()
    log = get_logger("alkera.tests.reconfigure.forwarding")
    log.warning("warms.the.cached.bound.logger")

    srv, port = _udp_listener()
    try:
        monkeypatch.setattr(settings, "log_syslog_endpoint", f"127.0.0.1:{port}")
        configure_logging()

        log.warning("after.reconfigure", answer=42)

        data, _ = srv.recvfrom(65536)
        payload = data.decode()
        parsed = json.loads(payload[payload.index(">") + 1 :])
        assert parsed["event"] == "after.reconfigure"
        assert parsed["answer"] == 42
    finally:
        srv.close()


def _forwarded(srv: socket.socket) -> dict[str, object]:
    """The next event off the syslog socket, as the sink parses it."""
    data, _ = srv.recvfrom(65536)
    payload = data.decode()
    parsed: dict[str, object] = json.loads(payload[payload.index(">") + 1 :])
    return parsed


def test_a_reconfigure_re_levels_a_logger_that_already_spoke(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Turning the level down must reach a warm logger, and turning it up must
    silence one.

    The same coupling as the processor chain, on the other half of the config:
    ``wrapper_class`` is baked into a bound logger at first use, so an operator
    who raises the verbosity to chase an incident would get nothing from any
    logger that had already spoken -- which, on a running process, is all of
    them.
    """
    monkeypatch.setattr(settings, "log_level", "WARNING")
    configure_logging()
    log = get_logger("alkera.tests.reconfigure.level")
    log.warning("warms.the.cached.bound.logger")

    srv, port = _udp_listener()
    try:
        monkeypatch.setattr(settings, "log_syslog_endpoint", f"127.0.0.1:{port}")
        monkeypatch.setattr(settings, "log_level", "DEBUG")
        configure_logging()

        log.debug("the.operator.turned.it.up", answer=42)

        arrived = _forwarded(srv)
        assert arrived["event"] == "the.operator.turned.it.up"
        assert arrived["answer"] == 42

        # And back up: the debug line the same warm logger writes next is gone,
        # proved by the error behind it arriving first rather than by waiting
        # out a deadline on a datagram that was never sent.
        monkeypatch.setattr(settings, "log_level", "ERROR")
        configure_logging()

        log.debug("must.not.arrive")
        log.info("must.not.arrive.either")
        log.error("the.operator.turned.it.back.down")

        assert _forwarded(srv)["event"] == "the.operator.turned.it.back.down"
    finally:
        srv.close()


def test_a_reconfigure_leaves_a_warm_logger_capturable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same coupling as seen from a test: ``capture_logs`` swaps the chain in
    place, so a logger stranded on a previous chain would go silent -- a test
    asserting on a log line would read an empty list and blame the product."""
    configure_logging()
    log = get_logger("alkera.tests.reconfigure.capture")
    log.warning("warms.the.cached.bound.logger")

    monkeypatch.setattr(settings, "log_syslog_endpoint", None)
    configure_logging()

    with capture_logs() as events:
        log.warning("captured.after.reconfigure", answer=42)

    assert [event["event"] for event in events] == ["captured.after.reconfigure"]
    assert events[0]["answer"] == 42
