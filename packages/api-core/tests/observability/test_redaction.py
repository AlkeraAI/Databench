"""Redaction: secret keys removed, secret-looking values scrubbed, home paths
reduced — and benign analytics fields (token COUNTS) left intact."""

from __future__ import annotations

from alkera_core.observability.redaction import (
    REDACTED,
    is_sensitive_key,
    scrub_mapping,
    scrub_path,
    scrub_text,
    scrub_value,
)


def test_sensitive_keys_match_case_and_dash_insensitively() -> None:
    for key in ("Authorization", "API-KEY", "api_key", "password", "Set-Cookie", "Sentry_DSN"):
        assert is_sensitive_key(key), key


def test_benign_keys_are_not_sensitive() -> None:
    # Critical: analytics/billing needs these — substring "token" must NOT match.
    for key in ("input_tokens", "output_tokens", "max_tokens", "user_id", "email", "org_id"):
        assert not is_sensitive_key(key), key


def test_scrub_text_redacts_jwt_and_bearer_and_provider_keys() -> None:
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.abcDEF123_-signature"
    assert jwt not in scrub_text(f"token={jwt}")
    assert scrub_text("Authorization: Bearer abcdef0123456789xyz") == (
        f"Authorization: Bearer {REDACTED}"
    )
    assert "sk-proj-" not in scrub_text("key sk-proj-ABCDEFGHIJKLMNOP1234")
    assert "ABSK" not in scrub_text("bedrock ABSKaWxlcm9uZGtleTEyMzQ1Ng==")


def test_scrub_text_leaves_plain_text_alone() -> None:
    assert scrub_text("hello world, nothing secret here") == "hello world, nothing secret here"


def test_scrub_text_does_not_redact_ordinary_basic_bearer_prose() -> None:
    # "Basic"/"Bearer" followed by a plain word is NOT a credential.
    for phrase in ("Basic understanding of the flow", "Bearer of bad news arrived"):
        assert scrub_text(phrase) == phrase
    # ...but a token-like value after the scheme still gets redacted.
    assert "[redacted]" in scrub_text("Bearer abcdef0123456789")


def test_scrub_path_reduces_home_dirs() -> None:
    assert scrub_path("/Users/robin/code/app.py") == "~/code/app.py"
    assert scrub_path("/home/ci/work/x") == "~/work/x"
    assert scrub_path(r"C:\Users\robin\AppData\thing") == r"~\AppData\thing"


def test_scrub_mapping_redacts_by_key_and_recurses() -> None:
    raw = {
        "authorization": "Bearer secret-token-value-123",
        "user_id": "u-1",
        "input_tokens": 42,
        "nested": {"password": "hunter2", "note": "/Users/bob/secret.txt"},
        "items": [{"api_key": "k"}, "Bearer abcdef0123456789"],
    }
    out = scrub_mapping(raw)
    assert out["authorization"] == REDACTED
    assert out["user_id"] == "u-1"
    assert out["input_tokens"] == 42
    assert out["nested"]["password"] == REDACTED
    assert out["nested"]["note"] == "~/secret.txt"
    assert out["items"][0]["api_key"] == REDACTED
    assert REDACTED in out["items"][1]


def test_scrub_mapping_does_not_mutate_input() -> None:
    raw = {"password": "x", "nested": {"token": "y"}}
    scrub_mapping(raw)
    assert raw["password"] == "x"
    assert raw["nested"]["token"] == "y"


def test_scrub_value_passes_through_non_containers() -> None:
    assert scrub_value(42) == 42
    assert scrub_value(None) is None
    assert scrub_value(3.14) == 3.14


def test_scrub_value_depth_cap_does_not_recurse_forever() -> None:
    cyclic: dict[str, object] = {}
    cyclic["self"] = cyclic
    # Must terminate (depth cap), not raise RecursionError.
    scrub_value(cyclic)
