"""The syslog/SIEM log forwarder ships redacted events and never raises."""

from __future__ import annotations

import json
import socket

from alkera_core.logging import _SyslogForwarder


def test_forwarder_ships_json_over_udp() -> None:
    srv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    srv.bind(("127.0.0.1", 0))
    srv.settimeout(2.0)
    port = srv.getsockname()[1]
    try:
        fwd = _SyslogForwarder(f"127.0.0.1:{port}")
        event = {"event": "test.thing", "level": "error", "answer": 42}
        out = fwd(None, "error", event)
        assert out is event  # the processor returns the event unchanged for the renderer

        data, _ = srv.recvfrom(65536)
        text = data.decode()
        # RFC priority prefix: facility local0 (16) * 8 + severity error (3) = 131.
        assert text.startswith("<131>")
        parsed = json.loads(text[text.index(">") + 1 :])
        assert parsed["event"] == "test.thing"
        assert parsed["answer"] == 42
    finally:
        srv.close()


def test_forwarder_never_raises_on_unreachable_endpoint() -> None:
    # Nothing is listening on TCP :1 — the connect fails, but logging must not.
    fwd = _SyslogForwarder("tcp://127.0.0.1:1")
    event = {"event": "x", "level": "info"}
    assert fwd(None, "info", event) is event


def test_default_port_and_udp_scheme_parsing() -> None:
    assert _SyslogForwarder("siem.internal")._addr == ("siem.internal", 514)
    assert _SyslogForwarder("siem.internal:6514")._addr == ("siem.internal", 6514)
    tcp = _SyslogForwarder("tcp://siem.internal:601")
    assert tcp._tcp is True and tcp._addr == ("siem.internal", 601)
