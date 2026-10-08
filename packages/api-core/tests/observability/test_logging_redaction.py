"""The logging redaction processor scrubs secrets from emitted events."""

from __future__ import annotations

from alkera_core.logging import _redact_processor


def test_redact_processor_scrubs_sensitive_keys_and_values() -> None:
    event = {
        "event": "auth.login",
        "authorization": "Bearer abcdef0123456789",
        "user_id": "u-1",
        "input_tokens": 100,
        "exception": 'Traceback: open("/Users/robin/.alkera/auth.yml")',
    }
    out = _redact_processor(None, "info", event)
    assert out["authorization"] == "[redacted]"
    assert out["user_id"] == "u-1"
    assert out["input_tokens"] == 100
    assert "/Users/robin" not in out["exception"]
    assert "~/.alkera/auth.yml" in out["exception"]


def test_redact_processor_never_raises() -> None:
    # A non-mapping-ish event must not blow up logging.
    weird = {"event": "x", "obj": object()}
    out = _redact_processor(None, "info", weird)
    assert out["event"] == "x"
