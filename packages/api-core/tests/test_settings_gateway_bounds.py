"""The gateway bounds a deployment may tune.

Each of these used to be a literal in the gateway's own modules. Three things
have to hold for the fold to be worth anything: the shipped default still equals
the figure that was hard-coded (so nothing moved when they became settings), the
documented env var actually reaches the field, and a nonsensical value is refused
at boot rather than discovered on the first request.
"""

from __future__ import annotations

import pytest
from _settings_env import seal_settings_env
from alkera_core.config import Settings
from pydantic import ValidationError


@pytest.fixture(autouse=True)
def _only_what_this_test_sets(monkeypatch: pytest.MonkeyPatch) -> None:
    """A shell that sourced the dev config, or a CI job that exported one of
    these, would otherwise reach a `Settings()` built here — and a hostile
    ambient value for a field a cross-check reads turns an "accepted" case into a
    refusal about something the test never set."""
    seal_settings_env(monkeypatch)


#: field name -> (env var, the figure that was hard-coded before the fold)
_SHIPPED: dict[str, tuple[str, float]] = {
    "gateway_upstream_max_attempts": ("GATEWAY_UPSTREAM_MAX_ATTEMPTS", 3),
    "gateway_retry_after_cap_seconds": ("GATEWAY_RETRY_AFTER_CAP_SECONDS", 30.0),
    "gateway_retry_backoff_base_seconds": ("GATEWAY_RETRY_BACKOFF_BASE_SECONDS", 0.5),
    "gateway_retry_backoff_max_seconds": ("GATEWAY_RETRY_BACKOFF_MAX_SECONDS", 8.0),
    "gateway_retry_backoff_jitter_seconds": ("GATEWAY_RETRY_BACKOFF_JITTER_SECONDS", 0.25),
    "gateway_upstream_connect_timeout_seconds": (
        "GATEWAY_UPSTREAM_CONNECT_TIMEOUT_SECONDS",
        10.0,
    ),
    "gateway_upstream_write_timeout_seconds": ("GATEWAY_UPSTREAM_WRITE_TIMEOUT_SECONDS", 30.0),
    "gateway_upstream_pool_timeout_seconds": ("GATEWAY_UPSTREAM_POOL_TIMEOUT_SECONDS", 10.0),
    "gateway_max_request_body_bytes": ("GATEWAY_MAX_REQUEST_BODY_BYTES", 32 * 1024 * 1024),
    "gateway_max_idempotency_key_length": ("GATEWAY_MAX_IDEMPOTENCY_KEY_LENGTH", 200),
    "gateway_per_principal_stream_share": ("GATEWAY_PER_PRINCIPAL_STREAM_SHARE", 4),
    "gateway_shed_retry_after_seconds": ("GATEWAY_SHED_RETRY_AFTER_SECONDS", 1),
    "gateway_stream_settle_margin_seconds": ("GATEWAY_STREAM_SETTLE_MARGIN_SECONDS", 60.0),
    "gateway_stream_expiry_close_seconds": ("GATEWAY_STREAM_EXPIRY_CLOSE_SECONDS", 10.0),
    "gateway_stream_expiry_retry_seconds": ("GATEWAY_STREAM_EXPIRY_RETRY_SECONDS", 0.25),
    "gateway_upstream_error_excerpt_chars": ("GATEWAY_UPSTREAM_ERROR_EXCERPT_CHARS", 300),
}

#: Zero is a real setting for the jitter alone — it makes the backoff curve
#: deterministic. Every other field here breaks the thing it sizes at zero.
_ZERO_IS_LEGITIMATE = {"gateway_retry_backoff_jitter_seconds"}

#: Fields whose accepted range is decided against ANOTHER setting, so the plain
#: "an ordinary value reaches the field" case gets a value of its own below.
_CROSS_CHECKED = {
    "gateway_retry_backoff_max_seconds": "20",
    "gateway_stream_expiry_retry_seconds": "3",
}


@pytest.mark.parametrize(
    ("field", "expected"),
    [pytest.param(f, v[1], id=f) for f, v in _SHIPPED.items()],
)
def test_shipped_default_matches_the_figure_it_replaced(field: str, expected: float) -> None:
    """Read off the model field, not the live settings object, so a developer's
    own `.env` cannot make this pass while the shipped default has drifted."""
    assert Settings.model_fields[field].default == expected


@pytest.mark.parametrize(
    ("field", "env", "raw", "expected"),
    [
        # 7 is inside every field's accepted range except the ones cross-checked
        # against another setting, which carry their own value.
        pytest.param(f, v[0], _CROSS_CHECKED.get(f, "7"), float(_CROSS_CHECKED.get(f, "7")), id=f)
        for f, v in _SHIPPED.items()
    ],
)
def test_env_var_reaches_the_field(
    monkeypatch: pytest.MonkeyPatch, field: str, env: str, raw: str, expected: float
) -> None:
    monkeypatch.setenv(env, raw)
    assert getattr(Settings(), field) == expected


@pytest.mark.parametrize(
    ("env", "raw"),
    [
        pytest.param(v[0], bad, id=f"{f}-{name}")
        for f, v in _SHIPPED.items()
        if f not in _ZERO_IS_LEGITIMATE
        for name, bad in (("zero", "0"), ("negative", "-1"))
    ],
)
def test_a_bound_at_or_below_zero_is_refused(
    monkeypatch: pytest.MonkeyPatch, env: str, raw: str
) -> None:
    """Zero does not widen any of these — it breaks the thing it sizes: no
    attempt ever made, a body limit nothing can satisfy, a stream cut before it
    starts, a shed that tells the caller to come straight back. The boot has to
    say so."""
    monkeypatch.setenv(env, raw)
    with pytest.raises(ValidationError):
        Settings()


def test_zero_jitter_is_accepted_and_a_negative_spread_is_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The asymmetric case: zero is the one legitimate way to switch the spread
    off, while a negative one draws waits BELOW the computed backoff — and
    sleeping a negative duration returns instantly rather than raising, so
    nothing downstream would report it."""
    monkeypatch.setenv("GATEWAY_RETRY_BACKOFF_JITTER_SECONDS", "0")
    assert Settings().gateway_retry_backoff_jitter_seconds == 0.0
    monkeypatch.setenv("GATEWAY_RETRY_BACKOFF_JITTER_SECONDS", "-0.5")
    with pytest.raises(ValidationError, match="GATEWAY_RETRY_BACKOFF_JITTER_SECONDS"):
        Settings()


@pytest.mark.parametrize(
    ("retry", "close", "accepted"),
    [
        pytest.param("0.25", "10", True, id="shipped-pair"),
        pytest.param("9.99", "10", True, id="a-gap-just-inside-the-budget"),
        pytest.param("10", "10", False, id="gap-equals-the-budget"),
        pytest.param("30", "10", False, id="gap-past-the-budget"),
    ],
)
def test_the_expiry_retry_gap_must_fit_inside_its_close_budget(
    monkeypatch: pytest.MonkeyPatch, retry: str, close: str, accepted: bool
) -> None:
    """The watchdog sleeps the gap between close attempts and stops at the
    budget, so a gap at or past the budget spends the whole of it on the first
    wait — the budget then buys no retries whatever it is set to."""
    monkeypatch.setenv("GATEWAY_STREAM_EXPIRY_RETRY_SECONDS", retry)
    monkeypatch.setenv("GATEWAY_STREAM_EXPIRY_CLOSE_SECONDS", close)
    if accepted:
        assert Settings().gateway_stream_expiry_retry_seconds == float(retry)
    else:
        with pytest.raises(ValidationError, match="GATEWAY_STREAM_EXPIRY_RETRY_SECONDS"):
            Settings()


def test_an_idempotency_key_longer_than_its_column_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The key is persisted as the request-id column, a VARCHAR(255). A ceiling
    above it accepts a key the database then rejects mid-admission."""
    monkeypatch.setenv("GATEWAY_MAX_IDEMPOTENCY_KEY_LENGTH", "256")
    with pytest.raises(ValidationError, match="between 1 and 255"):
        Settings()
    monkeypatch.setenv("GATEWAY_MAX_IDEMPOTENCY_KEY_LENGTH", "255")
    assert Settings().gateway_max_idempotency_key_length == 255


def test_a_backoff_ceiling_below_its_base_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """A ceiling under the first wait makes the curve shrink with each attempt —
    "retry harder the longer the provider is down"."""
    monkeypatch.setenv("GATEWAY_RETRY_BACKOFF_BASE_SECONDS", "5")
    monkeypatch.setenv("GATEWAY_RETRY_BACKOFF_MAX_SECONDS", "1")
    with pytest.raises(ValidationError, match="GATEWAY_RETRY_BACKOFF_MAX_SECONDS"):
        Settings()
    monkeypatch.setenv("GATEWAY_RETRY_BACKOFF_MAX_SECONDS", "5")
    assert Settings().gateway_retry_backoff_max_seconds == 5.0


@pytest.mark.parametrize(
    ("margin", "cap", "accepted"),
    [
        pytest.param("60", "86400", True, id="shipped-pair"),
        pytest.param("120", "120", False, id="margin-equals-the-cap"),
        pytest.param("300", "120", False, id="margin-past-the-cap"),
        pytest.param("119", "120", True, id="margin-just-inside-the-cap"),
    ],
)
def test_the_settle_margin_must_leave_a_stream_behind(
    monkeypatch: pytest.MonkeyPatch, margin: str, cap: str, accepted: bool
) -> None:
    """The stream is cut this far before the sweeper reclaims its hold, so a
    margin at or past the cap leaves the pipeline's one-second floor as the whole
    budget for every step."""
    monkeypatch.setenv("GATEWAY_STREAM_SETTLE_MARGIN_SECONDS", margin)
    monkeypatch.setenv("GATEWAY_MAX_STREAM_SECONDS", cap)
    if accepted:
        assert Settings().gateway_stream_settle_margin_seconds == float(margin)
    else:
        with pytest.raises(ValidationError, match="GATEWAY_STREAM_SETTLE_MARGIN_SECONDS"):
            Settings()
