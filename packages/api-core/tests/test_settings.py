"""Settings smoke + production-mode validator tests.

Verifies:
- `alkera_core.config.Settings` instantiates with all defaults (env loads,
  validators pass, AppEnv Literal accepted).
- The production-mode validator rejects every insecure default, individually.
- The new SMTP auth/TLS + version fields exist with sane defaults.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path

import pytest
from _settings_env import seal_settings_env
from alkera_core.config import (
    FILES_STORE_DRIVERS,
    FILES_UPLOAD_PART_RESIDENT_BYTES,
    ORG_USAGE_METRIC_KEYS,
    REALTIME_WS_MAX_FRAME_BYTES,
    UPSTREAM_POOL_MAX_CONNECTIONS,
    MissingServerSecretError,
    Settings,
    get_settings,
)
from alkera_core.models.files.stores import STORE_DRIVERS
from pydantic import ValidationError


@pytest.fixture(autouse=True)
def _isolate_settings_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Seal this module off from ambient process env.

    `Settings(_env_file=None)` disables the dotenv FILES but NOT `os.environ` —
    pydantic-settings always layers real env vars on top of the defaults. CI's
    `test` job exports `OAUTH_MOCK_ENABLED=true`, the `e2e` job exports the
    Google/GitHub creds, and a dev shell may export any of these. Left in place
    they leak into the "hermetic" default/validator assertions below (e.g.
    `test_mock_oauth_disabled_by_default` would read a real `true`, and
    `_prod_settings()`'s clean baseline would trip the API_PUBLIC_BASE_URL guard
    on ambient OAuth creds). These tests verify DEFAULTS and EXPLICIT configs
    only, so the ambient environment must not bleed in.

    The seal walks the model (`seal_settings_env`), so it cannot fall behind the
    settings it has to cover: a hand-kept list silently stopped covering
    `FILES_INLINE_OPERATIONS`, and an environment exporting it turned all
    fourteen "production baseline is accepted" cases red.
    """
    seal_settings_env(monkeypatch)


def _prod_settings(**overrides: object) -> Settings:
    """A baseline 'production'-shaped Settings dict — all required fields set.

    Tests override one field at a time to verify the validator catches each
    insecure configuration independently.
    """
    base: dict[str, object] = {
        "app_env": "production",
        "auth_jwt_secret": "real-prod-secret-not-the-fallback",
        "token_hash_pepper": "real-prod-token-pepper-at-least-32-bytes",
        "auth_cookie_secure": True,
        "smtp_host": "smtp.example.com",
        "smtp_username": "smtp-user",
        "smtp_password": "smtp-pass",
        "database_url": "postgresql+asyncpg://prod:prod@db.internal:5432/alkera",
        "api_cors_origins": "https://app.alkera.example",
        "frontend_base_url": "https://app.alkera.example",
        "oauth_mock_enabled": False,
        "turnstile_secret_key": "real-prod-turnstile-secret",
        "temporal_address": "temporal.example.internal:7233",
    }
    base.update(overrides)
    # `_env_file=None` keeps the test hermetic — it must validate ONLY the
    # explicit `base` config, not whatever a dev has in `.env`/`.env.local`
    # (e.g. real OAuth creds, which would otherwise leak into these cases).
    return Settings(_env_file=None, **base)  # type: ignore[arg-type, call-arg]


def _staging_settings(**overrides: object) -> Settings:
    """A baseline 'staging'-shaped Settings dict.

    Staging's lighter validator requires a real cookie/cors/frontend but allows
    Mailpit + the in-box Postgres (unlike production). Tests override one field
    at a time.
    """
    base: dict[str, object] = {
        "app_env": "staging",
        "auth_jwt_secret": "real-staging-secret-not-the-fallback",
        "token_hash_pepper": "real-staging-token-pepper",
        "auth_cookie_secure": True,
        "api_cors_origins": "https://pr-1.staging.example.com",
        "frontend_base_url": "https://pr-1.staging.example.com",
        "oauth_mock_enabled": False,
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)  # type: ignore[arg-type, call-arg]


def test_settings_instantiates_with_defaults():
    settings = Settings()
    assert settings.app_env in {"local", "staging", "production"}
    assert settings.database_url.startswith("postgresql+asyncpg://")
    assert settings.database_url_sync.startswith("postgresql+psycopg://")


def test_device_authorization_defaults():
    # RFC 8628 device-grant tuning knobs ship with safe, non-secret defaults.
    s = Settings()
    assert s.auth_device_code_ttl_seconds == 600
    assert s.auth_device_poll_interval_seconds == 1
    assert s.auth_device_user_code_length == 8
    assert s.auth_device_user_code_length % 2 == 0  # rendered as two equal groups
    assert s.auth_device_max_poll_attempts > 0
    assert s.auth_device_max_user_code_attempts > 0


def test_files_version_retention_defaults_are_the_alkera_hosted_rule():
    # What Alkera hosts: the newest five versions, or ninety days of them.
    s = Settings(_env_file=None)
    assert s.files_version_keep_newest == 5
    assert s.files_version_keep_window_days == 90


def test_a_self_hosted_deployment_sets_its_own_version_retention(monkeypatch):
    # The whole point of the settings: a self-hosted install keeps deeper
    # history from the environment, with no code change.
    monkeypatch.setenv("FILES_VERSION_KEEP_NEWEST", "250")
    monkeypatch.setenv("FILES_VERSION_KEEP_WINDOW_DAYS", "3650")
    s = Settings(_env_file=None)
    assert s.files_version_keep_newest == 250
    assert s.files_version_keep_window_days == 3650


@pytest.mark.parametrize(
    ("field", "env", "default", "raw", "parsed"),
    [
        pytest.param(
            "brand_sales_email",
            "BRAND_SALES_EMAIL",
            None,
            "deals@example.test",
            "deals@example.test",
            id="sales-contact",
        ),
    ],
)
def test_a_deployment_sets_its_own_sales_contact(monkeypatch, field, env, default, raw, parsed):
    monkeypatch.delenv(env, raising=False)
    assert getattr(Settings(_env_file=None), field) == default
    monkeypatch.setenv(env, raw)
    assert getattr(Settings(_env_file=None), field) == parsed


def test_the_org_assertion_mode_defaults_to_log_and_a_deployment_sets_enforce(monkeypatch):
    monkeypatch.delenv("ORG_ASSERTION_MODE", raising=False)
    assert Settings(_env_file=None).org_assertion_mode == "log"
    monkeypatch.setenv("ORG_ASSERTION_MODE", "enforce")
    assert Settings(_env_file=None).org_assertion_mode == "enforce"


def test_an_unknown_org_assertion_mode_is_refused(monkeypatch):
    monkeypatch.setenv("ORG_ASSERTION_MODE", "warn")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_get_settings_is_cached():
    a = get_settings()
    b = get_settings()
    assert a is b, "get_settings() should return the same instance via lru_cache"


def test_cors_origins_list_parses():
    settings = Settings()
    origins = settings.cors_origins_list
    assert isinstance(origins, list)
    assert all(isinstance(o, str) and o for o in origins)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param("", [], id="empty-is-the-default"),
        pytest.param("https://portal.example", ["https://portal.example"], id="one"),
        pytest.param(
            " http://a.example:8080 , https://b.example ",
            ["http://a.example:8080", "https://b.example"],
            id="whitespace-and-ports",
        ),
    ],
)
def test_csrf_trusted_origins_parses(raw: str, expected: list[str]):
    assert Settings(_env_file=None, auth_csrf_trusted_origins=raw).csrf_trusted_origins_list == (
        expected
    )


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param("portal.example", id="no-scheme"),
        pytest.param("ftp://portal.example", id="not-http"),
        pytest.param("https://portal.example/app", id="carries-a-path"),
        pytest.param("https://portal.example?x=1", id="carries-a-query"),
        pytest.param("https://*.example", id="wildcard"),
        pytest.param("https://", id="no-host"),
        pytest.param("https://user@portal.example", id="carries-credentials"),
    ],
)
def test_csrf_trusted_origins_rejects_anything_that_is_not_a_bare_origin(raw: str):
    """A value that can never match the `Origin` header a browser sends would
    read as a broken guard rather than a mistyped setting. Refuse at boot."""
    with pytest.raises(ValidationError, match="not a bare origin"):
        Settings(_env_file=None, auth_csrf_trusted_origins=raw)


def test_org_usage_analytics_defaults_to_full_disclosure():
    s = Settings(_env_file=None)
    assert s.org_usage_analytics_enabled is True
    assert s.org_usage_metrics_set == ORG_USAGE_METRIC_KEYS


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param("totals,by_member", frozenset({"totals", "by_member"}), id="subset"),
        pytest.param(" totals , daily ", frozenset({"totals", "daily"}), id="whitespace"),
        pytest.param("all", ORG_USAGE_METRIC_KEYS, id="all"),
        pytest.param("all,totals", ORG_USAGE_METRIC_KEYS, id="all-wins-over-subset"),
        pytest.param("", frozenset(), id="empty-set-shows-nothing"),
    ],
)
def test_org_usage_metrics_parses(raw: str, expected: frozenset[str]):
    assert Settings(_env_file=None, org_usage_metrics=raw).org_usage_metrics_set == expected


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param("totals,by_membre", id="typo"),
        pytest.param("provider_cost", id="never-a-metric"),
    ],
)
def test_org_usage_metrics_rejects_unknown_keys_at_boot(raw: str):
    """A typo must refuse to boot, not silently hide a section."""
    with pytest.raises(ValidationError, match="Unknown org usage metric"):
        Settings(_env_file=None, org_usage_metrics=raw)


def test_stripe_configured_requires_both_secret_and_webhook():
    assert Settings(_env_file=None).stripe_configured is False
    # A secret key alone can charge but never PROVISION (no webhook) → not configured.
    assert Settings(_env_file=None, stripe_secret_key="sk_test_x").stripe_configured is False
    assert (
        Settings(
            _env_file=None, stripe_secret_key="sk_test_x", stripe_webhook_secret="whsec_x"
        ).stripe_configured
        is True
    )


def test_local_stays_lenient_on_partial_or_test_stripe_config():
    # Local must NOT raise on a partial/test config — the incremental .env.local
    # setup (key first, then the `stripe listen` whsec_) can't be allowed to block boot.
    Settings(_env_file=None, stripe_secret_key="sk_test_x")
    Settings(_env_file=None, stripe_secret_key="sk_test_x", stripe_webhook_secret="whsec_x")
    Settings(_env_file=None, stripe_price_plus_monthly="price_x")


def test_access_token_ttl_defaults_to_thirty_minutes():
    """The browser access token is short-lived by default; the refresh cookie,
    not a long `exp`, is what keeps a user signed in."""
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.auth_token_ttl_seconds == 30 * 60
    assert s.auth_token_ttl_production_max_seconds == 60 * 60


@pytest.mark.parametrize(
    "ttl",
    [
        pytest.param(3601, id="one-second-over"),
        pytest.param(86400, id="the-old-one-day-default"),
        pytest.param(604800, id="the-seven-days-an-assessment-still-called-excessive"),
    ],
)
def test_prod_rejects_an_access_token_ttl_above_one_hour(ttl: int):
    with pytest.raises(ValueError, match="AUTH_TOKEN_TTL_SECONDS must be at most 3600"):
        _prod_settings(auth_token_ttl_seconds=ttl)


@pytest.mark.parametrize(
    "ttl",
    [pytest.param(3600, id="exactly-one-hour"), pytest.param(900, id="fifteen-minutes")],
)
def test_prod_accepts_an_access_token_ttl_of_at_most_one_hour(ttl: int):
    assert _prod_settings(auth_token_ttl_seconds=ttl).auth_token_ttl_seconds == ttl


def test_local_does_not_cap_the_access_token_ttl():
    """The ceiling is a production guard; a developer's long-lived local session
    is not a misconfiguration."""
    s = Settings(_env_file=None, app_env="local", auth_token_ttl_seconds=604800)  # type: ignore[call-arg]
    assert s.auth_token_ttl_seconds == 604800


DAY = 60 * 60 * 24


def test_session_windows_default_to_the_standard_idle_and_absolute_pair():
    """Seven days of neglect ends a browser session; thirty days ends it
    however active it was. The second is the bound activity cannot move."""
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.auth_refresh_idle_seconds == 7 * DAY
    assert s.auth_refresh_absolute_seconds == 30 * DAY
    assert s.auth_refresh_idle_seconds < s.auth_refresh_absolute_seconds


@pytest.mark.parametrize(
    "idle",
    [
        pytest.param(30 * DAY, id="equal-to-the-absolute-lifetime"),
        pytest.param(31 * DAY, id="past-the-absolute-lifetime"),
    ],
)
def test_prod_rejects_a_refresh_idle_window_the_absolute_lifetime_ends_first(idle: int):
    """An idle window at or past the absolute lifetime is not a policy — the
    absolute bound always fires first, so the idle timeout is configured and
    silently unreachable."""
    with pytest.raises(ValueError, match="AUTH_REFRESH_IDLE_SECONDS must be below"):
        _prod_settings(auth_refresh_idle_seconds=idle)


@pytest.mark.parametrize(
    "idle",
    [
        pytest.param(30 * DAY - 1, id="a-second-inside"),
        pytest.param(DAY, id="one-day"),
    ],
)
def test_prod_accepts_a_refresh_idle_window_inside_the_absolute_lifetime(idle: int):
    assert _prod_settings(auth_refresh_idle_seconds=idle).auth_refresh_idle_seconds == idle


@pytest.mark.parametrize(
    "idle",
    [
        pytest.param(30 * DAY, id="equal-to-the-absolute-lifetime"),
        pytest.param(90 * DAY, id="the-cli-token-lifetime"),
    ],
)
def test_prod_rejects_an_access_idle_window_the_absolute_lifetime_ends_first(idle: int):
    """The same rule for the access token's own idle window. A live connection
    slides it, so a value the absolute lifetime always beats would read as a
    bound that is never reached."""
    with pytest.raises(ValueError, match="AUTH_IDLE_TIMEOUT_SECONDS must be below"):
        _prod_settings(auth_idle_timeout_seconds=idle)


def test_prod_accepts_the_hosted_access_idle_window_and_the_off_switch():
    """The hosted deployment pins one day, and 0 means the access token has no
    idle window of its own — neither is a misconfiguration."""
    assert _prod_settings(auth_idle_timeout_seconds=DAY).auth_idle_timeout_seconds == DAY
    assert _prod_settings(auth_idle_timeout_seconds=0).auth_idle_timeout_seconds == 0


@pytest.mark.parametrize(
    "grace",
    [
        pytest.param(61, id="one-second-over"),
        pytest.param(3600, id="an-hour"),
    ],
)
def test_prod_rejects_a_refresh_reuse_grace_over_a_minute(grace: int):
    """Inside the grace a copied refresh token is handed the legitimate
    successor instead of ending the family, so a wide grace switches reuse
    detection off in all but name."""
    with pytest.raises(ValueError, match="AUTH_REFRESH_REUSE_GRACE_SECONDS must be at most"):
        _prod_settings(auth_refresh_reuse_grace_seconds=grace)


@pytest.mark.parametrize("grace", [pytest.param(0, id="strict"), pytest.param(60, id="a-minute")])
def test_prod_accepts_a_refresh_reuse_grace_of_at_most_a_minute(grace: int):
    assert (
        _prod_settings(auth_refresh_reuse_grace_seconds=grace).auth_refresh_reuse_grace_seconds
        == grace
    )


@pytest.mark.parametrize(
    "absolute",
    [
        pytest.param(30 * DAY + 1, id="one-second-over"),
        pytest.param(90 * DAY, id="a-quarter"),
        pytest.param(365 * DAY, id="a-year"),
    ],
)
def test_prod_rejects_an_absolute_session_lifetime_over_thirty_days(absolute: int):
    """Nothing slides the absolute lifetime, so it is exactly how long a
    compromised session stays usable. Thirty days is the ceiling."""
    with pytest.raises(ValueError, match="AUTH_REFRESH_ABSOLUTE_SECONDS must be at most"):
        _prod_settings(auth_refresh_absolute_seconds=absolute)


@pytest.mark.parametrize(
    "absolute",
    [
        pytest.param(30 * DAY, id="exactly-thirty-days"),
        pytest.param(14 * DAY, id="a-fortnight"),
    ],
)
def test_prod_accepts_an_absolute_session_lifetime_of_at_most_thirty_days(absolute: int):
    s = _prod_settings(auth_refresh_absolute_seconds=absolute, auth_refresh_idle_seconds=DAY)
    assert s.auth_refresh_absolute_seconds == absolute


def test_local_does_not_cap_the_session_windows():
    """The ceilings are production guards; a developer pinning a long local
    session is not a misconfiguration."""
    s = Settings(  # type: ignore[call-arg]
        _env_file=None,
        app_env="local",
        auth_refresh_absolute_seconds=365 * DAY,
        auth_refresh_idle_seconds=366 * DAY,
    )
    assert s.auth_refresh_absolute_seconds == 365 * DAY


def test_prod_refuses_to_run_with_rate_limiting_off():
    """The edge WAF bounds sustained volume only; the app-level classes are the
    burst control, so a production API with them off has no burst control."""
    with pytest.raises(ValueError, match="RATE_LIMIT_ENABLED must be 'true' in production"):
        _prod_settings(rate_limit_enabled=False)


def test_prod_accepts_rate_limiting_on_and_local_may_turn_it_off():
    assert _prod_settings(rate_limit_enabled=True).rate_limit_enabled is True
    local = Settings(_env_file=None, app_env="local", rate_limit_enabled=False)  # type: ignore[call-arg]
    assert local.rate_limit_enabled is False


@pytest.mark.parametrize(
    ("overrides", "refused"),
    [
        pytest.param(
            {"stripe_secret_key": "rk_test_x", "stripe_webhook_secret": "whsec_x"},
            "STRIPE_SECRET_KEY must be a live key",
            id="a-restricted-test-key-is-refused-too",
        ),
        pytest.param(
            {
                "stripe_secret_key": "sk_live_x",
                "stripe_webhook_secret": "whsec_x",
                "stripe_publishable_key": "pk_test_x",
            },
            "STRIPE_PUBLISHABLE_KEY must be a live key",
            id="a-test-publishable-key-is-refused",
        ),
        pytest.param(
            {"stripe_publishable_key": "pk_test_x"},
            "STRIPE_PUBLISHABLE_KEY must be a live key",
            id="a-test-publishable-key-is-refused-even-with-no-secret-key",
        ),
    ],
)
def test_prod_rejects_every_test_mode_stripe_key(overrides: dict[str, object], refused: str):
    """Production admits only live keys of every kind, the mirror image of
    staging admitting only test-mode keys."""
    with pytest.raises(ValueError, match=refused):
        _prod_settings(**overrides)


def test_prod_accepts_a_live_publishable_key():
    settings = _prod_settings(
        stripe_secret_key="sk_live_x",
        stripe_webhook_secret="whsec_x",
        stripe_publishable_key="pk_live_x",
    )
    assert settings.stripe_publishable_key == "pk_live_x"


def test_prod_rejects_test_mode_stripe_secret_key():
    with pytest.raises(ValueError, match="STRIPE_SECRET_KEY must be a live key"):
        _prod_settings(stripe_secret_key="sk_test_abc", stripe_webhook_secret="whsec_abc")


def test_prod_rejects_stripe_secret_without_webhook():
    with pytest.raises(ValueError, match="STRIPE_WEBHOOK_SECRET must be set"):
        _prod_settings(stripe_secret_key="sk_live_abc")


def test_prod_rejects_stripe_price_without_secret():
    with pytest.raises(ValueError, match="STRIPE_PRICE"):
        _prod_settings(stripe_price_plus_monthly="price_abc")


def test_prod_accepts_coherent_live_stripe_config():
    s = _prod_settings(
        stripe_secret_key="sk_live_abc",
        stripe_webhook_secret="whsec_abc",
        stripe_price_plus_monthly="price_plus",
        stripe_price_pro_monthly="price_pro",
    )
    assert s.stripe_configured is True


# A syntactically PEM-shaped key; the validators check shape, not cryptographic validity.
_PRIVATE_KEY_PEM = "-----BEGIN PRIVATE KEY-----\nMIIBfake\n-----END PRIVATE KEY-----\n"
_TEMPORAL_CERT_PEM = "-----BEGIN CERTIFICATE-----\nMIIBfake\n-----END CERTIFICATE-----\n"


def test_prod_rejects_proxy_mode_without_token():
    with pytest.raises(ValueError, match="ALKERA_PROXY_TOKEN must be set"):
        _prod_settings(gateway_upstream="proxy")


def test_prod_accepts_proxy_mode_with_token_and_url():
    s = _prod_settings(
        gateway_upstream="proxy",
        alkera_proxy_token="ptok_live_abc",
        alkera_proxy_url="https://upstream.example",
    )
    assert s.gateway_proxy_mode is True
    assert s.gateway_proxy_configured is True


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param(
            {"gateway_upstream": "proxy", "alkera_proxy_token": "ptok_live_abc"},
            id="proxy-mode",
        ),
        pytest.param({"alkera_proxy_token": "ptok_live_abc"}, id="heartbeat-token"),
    ],
)
def test_prod_refuses_an_upstream_token_with_no_upstream_url(overrides: dict[str, str]) -> None:
    with pytest.raises(ValueError, match="ALKERA_PROXY_URL must be set"):
        _prod_settings(**overrides)


def test_the_default_names_no_upstream_gateway():
    s = _prod_settings()
    assert s.alkera_proxy_url == ""
    assert s.gateway_proxy_configured is False


def test_prod_rejects_unknown_gateway_upstream():
    with pytest.raises(ValueError, match="GATEWAY_UPSTREAM must be"):
        _prod_settings(gateway_upstream="sideways")


def test_direct_mode_is_the_default_and_needs_no_token():
    s = _prod_settings()
    assert s.gateway_upstream == "direct"
    assert s.gateway_proxy_mode is False
    assert s.gateway_proxy_configured is False


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param(
            {"billing_reservation_stale_seconds": 120, "billing_reservation_touch_seconds": 60.0},
            id="stale-equals-two-touches",
        ),
        pytest.param(
            {"billing_reservation_stale_seconds": 90, "billing_reservation_touch_seconds": 60.0},
            id="stale-below-two-touches",
        ),
    ],
)
def test_prod_rejects_a_stale_window_that_cannot_survive_a_missed_touch(overrides):
    """A window no wider than two stamps reclaims the hold of a stream that is
    still running — its settle then no-ops and the usage is served free."""
    with pytest.raises(ValueError, match="BILLING_RESERVATION_STALE_SECONDS must exceed twice"):
        _prod_settings(**overrides)


def test_prod_rejects_a_touch_cadence_of_zero():
    with pytest.raises(ValueError, match="BILLING_RESERVATION_TOUCH_SECONDS must be greater"):
        _prod_settings(billing_reservation_touch_seconds=0)


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({}, id="defaults"),
        pytest.param(
            {"billing_reservation_stale_seconds": 121, "billing_reservation_touch_seconds": 60.0},
            id="stale-just-above-two-touches",
        ),
        pytest.param(
            {"billing_reservation_stale_seconds": 60, "billing_reservation_touch_seconds": 5.0},
            id="fast-touch-with-a-tight-window",
        ),
    ],
)
def test_prod_accepts_a_stale_window_wider_than_two_touches(overrides):
    s = _prod_settings(**overrides)
    assert s.billing_reservation_stale_seconds > 2 * s.billing_reservation_touch_seconds


def test_the_reservation_liveness_defaults_reclaim_a_dead_hold_in_minutes():
    """The default window is minutes, not the six hours a single step may stream
    for — that gap is what left a killed gateway's hold refusing the user."""
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.billing_reservation_touch_seconds == 60.0
    assert s.billing_reservation_stale_seconds == 900
    assert s.billing_reservation_stale_seconds < s.gateway_max_stream_seconds


def test_prod_rejects_unresolvable_outbound_ca_bundle():
    with pytest.raises(ValueError, match="OUTBOUND_CA_BUNDLE must be"):
        _prod_settings(outbound_ca_bundle="/no/such/corp-ca.pem")


def test_prod_accepts_inline_pem_outbound_ca_bundle():
    # Inline PEM (the marker is present) is accepted at validation time; it's parsed
    # for real only when an outbound client is built.
    s = _prod_settings(
        outbound_ca_bundle="-----BEGIN CERTIFICATE-----\nMIIB...\n-----END CERTIFICATE-----\n"
    )
    assert s.outbound_ca_bundle is not None


def test_is_self_hosted_is_the_inverse_of_stripe_configured():
    assert (
        Settings(_env_file=None, stripe_secret_key=None, stripe_webhook_secret=None).is_self_hosted
        is True
    )  # type: ignore[call-arg]
    s = Settings(_env_file=None, stripe_secret_key="sk_live_x", stripe_webhook_secret="whsec_x")  # type: ignore[call-arg]
    assert s.is_self_hosted is False
    assert s.stripe_configured is True


def test_self_hosted_explicit_override_wins_over_stripe_inference():
    # Explicit SELF_HOSTED=true wins even with Stripe keys present (inference → False).
    s = Settings(  # type: ignore[call-arg]
        _env_file=None,
        self_hosted=True,
        stripe_secret_key="sk_live_x",
        stripe_webhook_secret="whsec_x",
    )
    assert s.is_self_hosted is True
    # Explicit SELF_HOSTED=false lets a component with NO Stripe keys (e.g. a SaaS
    # gateway) opt back into SaaS behaviour / telemetry.
    s2 = Settings(  # type: ignore[call-arg]
        _env_file=None, self_hosted=False, stripe_secret_key=None, stripe_webhook_secret=None
    )
    assert s2.is_self_hosted is False


def test_prod_rejects_malformed_secret_box_key():
    with pytest.raises(ValueError, match="SECRET_BOX_KEY must be"):
        _prod_settings(secret_box_key="not-a-valid-fernet-key")


def test_prod_accepts_valid_secret_box_key_and_previous():
    from cryptography.fernet import Fernet

    active, prev = Fernet.generate_key().decode(), Fernet.generate_key().decode()
    s = _prod_settings(secret_box_key=active, secret_box_keys_previous=prev)
    assert s.secret_box_previous_keys_list == [prev]


def test_file_mounted_secrets_dir_is_read(tmp_path):
    # K8s/Docker file secrets: /run/secrets/<KEY> → the matching setting.
    (tmp_path / "auth_jwt_secret").write_text("from-a-mounted-file")
    s = Settings(_env_file=None, _secrets_dir=str(tmp_path))  # type: ignore[call-arg]
    assert s.auth_jwt_secret == "from-a-mounted-file"


def test_env_var_beats_a_file_mounted_secret(tmp_path, monkeypatch):
    (tmp_path / "auth_jwt_secret").write_text("from-a-mounted-file")
    monkeypatch.setenv("AUTH_JWT_SECRET", "from-the-env")
    s = Settings(_env_file=None, _secrets_dir=str(tmp_path))  # type: ignore[call-arg]
    assert s.auth_jwt_secret == "from-the-env"


def test_personal_email_domains_is_a_raw_configurable_setting():
    # api-core only holds the RAW extras knob; the vendored default list + the
    # union now live in backend.auth.email_policy (so the CLI binary never
    # bundles the snapshot). See test_email_policy.py for the union behavior.
    s = Settings(_env_file=None, personal_email_domains="Acme.IO, partner.example")  # type: ignore[call-arg]
    assert s.personal_email_domains == "Acme.IO, partner.example"
    assert not hasattr(s, "personal_email_domains_set")


def test_new_smtp_and_version_fields_default_safely(monkeypatch: pytest.MonkeyPatch):
    # Assert the true defaults — isolate from any ambient env (some runtimes
    # export APP_VERSION/BUILD_ID) that pydantic-settings would otherwise read.
    for var in (
        "APP_VERSION",
        "BUILD_ID",
        "SMTP_USERNAME",
        "SMTP_PASSWORD",
        "SMTP_USE_TLS",
        "SMTP_TIMEOUT_SECONDS",
    ):
        monkeypatch.delenv(var, raising=False)
    s = Settings()
    assert s.smtp_username is None
    assert s.smtp_password is None
    assert s.smtp_use_tls is False
    assert s.smtp_timeout_seconds == 10
    assert s.app_version == "0.0.0"
    assert s.build_id is None


@pytest.mark.parametrize(
    ("field", "env_var", "value"),
    [
        pytest.param("redis_url", "REDIS_URL", "redis://cache:6379/0", id="redis-url"),
        pytest.param(
            "celery_broker_url", "CELERY_BROKER_URL", "redis://broker:6379/0", id="broker-url"
        ),
        pytest.param(
            "celery_result_backend",
            "CELERY_RESULT_BACKEND",
            "redis://broker:6379/1",
            id="result-backend",
        ),
        pytest.param(
            "sqs_queue_url",
            "SQS_QUEUE_URL",
            "https://sqs.us-east-1.amazonaws.com/1/alkera",
            id="sqs-queue-url",
        ),
    ],
)
def test_the_retired_broker_settings_are_gone_and_stay_ignored(
    field: str, env_var: str, value: str, monkeypatch: pytest.MonkeyPatch
):
    """Background work runs on Temporal; the cache/broker/queue settings that fed the
    retired task runner are not fields any more, and an operator's leftover env var
    for one of them is ignored rather than resurrected as configuration."""
    assert field not in Settings.model_fields
    assert not hasattr(Settings(_env_file=None), field)  # type: ignore[call-arg]
    monkeypatch.setenv(env_var, value)
    assert not hasattr(Settings(_env_file=None), field)  # type: ignore[call-arg]
    assert Settings(_env_file=None).temporal_address == "localhost:7233"  # type: ignore[call-arg]


def test_production_baseline_is_accepted():
    """The reference 'good' production config must not trip the validator."""
    s = _prod_settings()
    assert s.is_production
    assert s.auth_cookie_secure is True


@pytest.mark.parametrize("on", [True, False], ids=["on", "off"])
def test_production_boots_with_several_chats_per_workspace_on_or_off(on: bool):
    """Placement refuses a shared-workspace chat on a box that cannot serve one,
    so the switch needs no boot-time guard: production starts either way."""
    assert _prod_settings(workspaces_multi_chat=on).workspaces_multi_chat is on


@pytest.mark.parametrize(
    ("minutes", "refused"),
    [
        pytest.param(1, True, id="a-test-window"),
        pytest.param(59, True, id="just-under-an-hour"),
        pytest.param(60, False, id="an-hour"),
        pytest.param(1440, False, id="the-default-day"),
    ],
)
def test_production_refuses_a_chat_idle_window_of_minutes(minutes: int, refused: bool):
    """A window of minutes is a test setting: served to production boxes it
    would put a reader's chat to sleep between two questions. Local keeps it."""
    if refused:
        with pytest.raises(ValueError, match="COMPUTE_CHAT_IDLE_MINUTES"):
            _prod_settings(compute_chat_idle_minutes=minutes)
    else:
        assert (
            _prod_settings(compute_chat_idle_minutes=minutes).compute_chat_idle_minutes == minutes
        )
    local = Settings(_env_file=None, compute_chat_idle_minutes=minutes)  # type: ignore[call-arg]
    assert local.compute_chat_idle_minutes == minutes


def test_the_chat_sleep_policy_ships_a_day_and_an_85_percent_line():
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert (s.compute_chat_idle_minutes, s.compute_chat_memory_pressure_percent) == (1440, 85)


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"compute_chat_idle_minutes": 0}, id="idle-zero"),
        pytest.param({"compute_chat_memory_pressure_percent": 0}, id="pressure-zero"),
        pytest.param({"compute_chat_memory_pressure_percent": 101}, id="pressure-past-100"),
    ],
)
def test_a_chat_sleep_policy_no_box_could_follow_is_refused_everywhere(overrides: dict[str, int]):
    with pytest.raises(ValueError):
        Settings(_env_file=None, **overrides)  # type: ignore[call-arg]


def test_production_refuses_an_unbounded_idle_transaction():
    """A request that dies inside a transaction would otherwise hold its row
    locks for as long as its connection lives."""
    with pytest.raises(ValueError, match="DATABASE_IDLE_IN_TRANSACTION_TIMEOUT_MS"):
        _prod_settings(database_idle_in_transaction_timeout_ms=0)
    # The background profile may switch it off: nothing waits on a job.
    assert (
        _prod_settings(
            database_background_idle_in_transaction_timeout_ms=0
        ).database_background_idle_in_transaction_timeout_ms
        == 0
    )


def test_production_refuses_to_run_without_the_unsaved_session_sweep():
    """Off, edits a stopped process took and never wrote back are written by
    nobody until someone types in that document again."""
    with pytest.raises(ValueError, match="REALTIME_CRDT_UNSAVED_SWEEP_ENABLED"):
        _prod_settings(realtime_crdt_unsaved_sweep_enabled=False)


def test_production_refuses_insecure_cookie():
    with pytest.raises(ValueError, match="AUTH_COOKIE_SECURE"):
        _prod_settings(auth_cookie_secure=False)


@pytest.mark.parametrize("bad_host", ["localhost", ""], ids=["localhost", "empty"])
def test_production_refuses_local_or_blank_smtp_when_email_enabled(bad_host: str):
    # With email ON, the relay host must be real — both the local-dev default and a
    # blank value (e.g. compose passing `SMTP_HOST=${SMTP_HOST:-}` unset) are rejected.
    with pytest.raises(ValueError, match="SMTP_HOST"):
        _prod_settings(smtp_host=bad_host)


def test_production_allows_no_email_mode():
    # EMAIL_ENABLED=false: a no-email / air-gapped deployment boots with NO relay
    # configured — the SMTP_HOST=localhost guard is skipped entirely.
    s = _prod_settings(
        email_enabled=False, smtp_host="localhost", smtp_username=None, smtp_password=None
    )
    assert s.email_enabled is False


@pytest.mark.parametrize("empty", [None, ""], ids=["none", "empty"])
def test_production_allows_anonymous_relay(empty: str | None):
    # Enterprise / on-prem relays are frequently internal, IP-allowlisted smart
    # hosts that take mail with NO credentials — production must boot with both
    # SMTP_USERNAME and SMTP_PASSWORD unset.
    s = _prod_settings(smtp_username=empty, smtp_password=empty)
    assert s.smtp_username in (None, "")
    assert s.smtp_password in (None, "")


@pytest.mark.parametrize("missing", [None, ""], ids=["none", "empty"])
def test_production_refuses_half_set_smtp_credentials(missing: str | None):
    # A half-set credential is a misconfig (e.g. SES with the password forgotten):
    # require both or neither.
    with pytest.raises(ValueError, match="SMTP_USERNAME and SMTP_PASSWORD"):
        _prod_settings(smtp_username=missing)  # password still set in the base → XOR
    with pytest.raises(ValueError, match="SMTP_USERNAME and SMTP_PASSWORD"):
        _prod_settings(smtp_password=missing)  # username still set in the base → XOR


def test_production_refuses_dev_database():
    with pytest.raises(ValueError, match="DATABASE_URL"):
        _prod_settings(
            database_url="postgresql+asyncpg://alkera:alkera@localhost:5432/alkera",
        )


def test_production_refuses_default_cors_origins():
    with pytest.raises(ValueError, match="API_CORS_ORIGINS"):
        _prod_settings(api_cors_origins="http://localhost:5173")


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        pytest.param("http://localhost:5173", "must use https", id="local-default"),
        pytest.param("http://app.alkera.example", "must use https", id="plain-http"),
        pytest.param("https://localhost", "not a localhost", id="https-localhost"),
        pytest.param("https://files.localhost:8443", "not a localhost", id="dot-localhost"),
        pytest.param("https://127.0.0.1", "not a localhost", id="ipv4-loopback"),
        pytest.param("https://[::1]:8443", "not a localhost", id="ipv6-loopback"),
        pytest.param("https://0.0.0.0", "not a localhost", id="unspecified"),
        pytest.param("", "must use https", id="blank"),
        pytest.param("app.alkera.example", "must use https", id="no-scheme"),
        pytest.param("https://", "must name the public portal host", id="no-host"),
        pytest.param("https://app.alkera.example?x=1", "query string", id="query"),
        pytest.param("https://app.alkera.example/#top", "query string", id="fragment"),
        pytest.param("https://app.alkera.example?", "query string", id="empty-query"),
        pytest.param("https://user:pw@app.alkera.example", "credentials", id="userinfo"),
        pytest.param("https://app.alkera.example ", "no whitespace", id="trailing-space"),
        pytest.param("https://app.alkera.example\n", "no whitespace", id="trailing-newline"),
        pytest.param("https://app.alkera.example/\x00", "control characters", id="nul"),
        pytest.param("https://[::1", "valid https URL", id="unparseable"),
        pytest.param("https://app.alkera.example:abc", "valid https URL", id="port-not-a-number"),
        pytest.param("https://app.alkera.example:99999", "valid https URL", id="port-out-of-range"),
        pytest.param("https://app.alkera.example:0", "valid https URL", id="port-zero"),
    ],
)
def test_production_refuses_a_frontend_base_url_that_is_not_the_public_portal(
    url: str, reason: str
) -> None:
    with pytest.raises(ValueError, match="FRONTEND_BASE_URL") as exc:
        _prod_settings(frontend_base_url=url)
    assert reason in str(exc.value)


@pytest.mark.parametrize(
    "url",
    [
        pytest.param("https://app.alkera.example", id="bare-origin"),
        pytest.param("https://app.alkera.example/", id="trailing-slash"),
        pytest.param("https://portal.customer.example:8443", id="custom-port"),
        pytest.param("https://intranet.customer.example/alkera", id="sub-path"),
        pytest.param("https://10.0.4.12", id="private-ip"),
    ],
)
def test_production_accepts_a_public_https_frontend_base_url(url: str) -> None:
    assert _prod_settings(frontend_base_url=url).frontend_base_url == url


def test_production_refuses_the_frontend_base_url_default() -> None:
    # The field's own default, not a value the test spells: an operator who never
    # set the variable at all is the case this check exists for.
    default = Settings.model_fields["frontend_base_url"].default
    with pytest.raises(ValueError, match="FRONTEND_BASE_URL must use https"):
        _prod_settings(frontend_base_url=default)


def test_production_refuses_the_fast_password_hash_profile():
    # The test suites export PASSWORD_HASH_PROFILE=fast, so the value an operator
    # is most likely to inherit by accident is exactly the one that would leave a
    # real password column hashed at argon2's floor.
    with pytest.raises(ValueError, match="PASSWORD_HASH_PROFILE"):
        _prod_settings(password_hash_profile="fast")


def test_production_accepts_the_real_password_hash_profile():
    assert _prod_settings(password_hash_profile="production").password_hash_profile == "production"


def test_the_password_hash_profile_defaults_to_production_and_reads_the_environment(
    monkeypatch: pytest.MonkeyPatch,
):
    """A deployment that never heard of this setting keeps the real argon2 cost;
    only an explicit `fast` lowers it, and a name that is neither is refused at
    load rather than quietly resolved to one of them."""
    assert Settings(_env_file=None).password_hash_profile == "production"  # type: ignore[call-arg]
    monkeypatch.setenv("PASSWORD_HASH_PROFILE", "fast")
    assert Settings(_env_file=None).password_hash_profile == "fast"  # type: ignore[call-arg]
    monkeypatch.setenv("PASSWORD_HASH_PROFILE", "cheap")
    with pytest.raises(ValidationError, match="password_hash_profile"):
        Settings(_env_file=None)  # type: ignore[call-arg]


@pytest.mark.parametrize(
    ("cap", "accepted"),
    [
        pytest.param(0, False, id="zero-413s-the-whole-surface"),
        pytest.param(-1, False, id="negative-413s-the-whole-surface"),
        pytest.param(33554431, False, id="one-below-the-32-MiB-ceiling"),
        pytest.param(33554432, True, id="at-the-32-MiB-artifact-ceiling"),
        pytest.param(34603008, True, id="the-shipped-33-MiB-default"),
        pytest.param(35651584, True, id="at-ceiling-plus-2-MiB-envelope"),
        pytest.param(35651585, False, id="one-past-the-envelope"),
    ],
)
def test_production_bounds_the_gate_ingest_cap_both_ways(cap: int, accepted: bool):
    """The gate-ingest cap is the pre-auth memory bound on the WAF-exempt upload
    paths, so production refuses it BOTH ways: below the 32 MiB artifact ceiling
    every valid upload 413s before the service's own 422 can answer, and above
    ceiling + 2 MiB an inflated override multiplies what one unauthenticated POST
    can buffer. A zero or negative cap 413s the whole ingest surface."""
    if accepted:
        assert _prod_settings(gate_ingest_max_body_bytes=cap).gate_ingest_max_body_bytes == cap
    else:
        with pytest.raises(ValueError, match="GATE_INGEST_MAX_BODY_BYTES"):
            _prod_settings(gate_ingest_max_body_bytes=cap)


@pytest.mark.parametrize(
    ("ceiling", "accepted"),
    [
        pytest.param(0, False, id="zero-ends-every-listing-on-page-one"),
        pytest.param(-1, False, id="negative-ends-every-listing-on-page-one"),
        pytest.param(1, True, id="a-narrow-but-usable-ceiling"),
        pytest.param(50_000, True, id="the-shipped-default"),
        pytest.param(1_000_000, True, id="at-the-upper-bound"),
        pytest.param(1_000_001, False, id="one-past-the-upper-bound"),
    ],
)
def test_the_listing_scan_ceiling_is_bounded_both_ways(ceiling: int, accepted: bool) -> None:
    """The ceiling is how far a listing reads past rows the caller may not see
    before it gives up. Zero or less ends every listing on its first page; a
    ceiling far past the page sizes lets one hidden-heavy listing scan a table
    per request. Bounded in every environment, not only production."""
    if accepted:
        assert (
            _prod_settings(objects_list_scan_ceiling=ceiling).objects_list_scan_ceiling == ceiling
        )
    else:
        with pytest.raises(ValueError, match="objects_list_scan_ceiling"):
            _prod_settings(objects_list_scan_ceiling=ceiling)


@pytest.mark.parametrize(
    ("budget", "accepted"),
    [
        pytest.param(0, False, id="zero-closes-every-socket"),
        pytest.param(-1, False, id="negative-closes-every-socket"),
        pytest.param(1, True, id="a-narrow-but-usable-budget"),
        pytest.param(4 * 1024 * 1024, True, id="the-shipped-default"),
    ],
)
def test_production_refuses_a_socket_byte_budget_that_admits_nothing(budget: int, accepted: bool):
    """The byte window is what bounds how much JSON one websocket can make a
    worker parse, so its VALUE is the defense. At zero or below the first frame
    of every socket is over budget and the connection is closed — an outage, not
    a limit."""
    if accepted:
        assert (
            _prod_settings(realtime_ws_max_bytes_per_window=budget).realtime_ws_max_bytes_per_window
            == budget
        )
    else:
        with pytest.raises(ValueError, match="REALTIME_WS_MAX_BYTES_PER_WINDOW"):
            _prod_settings(realtime_ws_max_bytes_per_window=budget)


def test_production_refuses_a_publisher_budget_below_the_readers():
    """A workspace machine publishing a chat streams a whole turn through its
    socket, so it is the lane that legitimately sends the most. Configured below
    a person's socket it is the first thing closed under load, mid-turn."""
    with pytest.raises(ValueError, match="REALTIME_WS_PUBLISHER_MAX_BYTES_PER_WINDOW"):
        _prod_settings(
            realtime_ws_max_bytes_per_window=8 * 1024 * 1024,
            realtime_ws_publisher_max_bytes_per_window=4 * 1024 * 1024,
        )


def test_production_accepts_a_publisher_budget_at_or_above_the_readers():
    accepted = _prod_settings(
        realtime_ws_max_bytes_per_window=8 * 1024 * 1024,
        realtime_ws_publisher_max_bytes_per_window=8 * 1024 * 1024,
    )
    assert accepted.realtime_ws_publisher_max_bytes_per_window == 8 * 1024 * 1024


@pytest.mark.parametrize("frames", [0, -1], ids=["zero", "negative"])
def test_production_refuses_a_publisher_frame_window_that_admits_nothing(frames: int):
    """At or below zero every workspace machine is closed on its first frame,
    taking the chat it was publishing with it."""
    with pytest.raises(ValueError, match="REALTIME_WS_PUBLISHER_MAX_FRAMES_PER_WINDOW"):
        _prod_settings(realtime_ws_publisher_max_frames_per_window=frames)


def test_production_accepts_a_raised_publisher_frame_window():
    accepted = _prod_settings(realtime_ws_publisher_max_frames_per_window=50_000)
    assert accepted.realtime_ws_publisher_max_frames_per_window == 50_000


@pytest.mark.parametrize("missing", [None, ""], ids=["none", "empty"])
def test_production_requires_turnstile_secret(missing: str | None):
    # Bot protection on the public auth endpoints is mandatory in production
    # (login / signup / password-reset) WHEN turnstile_required is left at its
    # SaaS default of True. Empty == unset (the alkera/<env> secret seeds it as
    # ""), so both None and "" must trip the validator.
    with pytest.raises(ValueError, match="TURNSTILE_SECRET_KEY"):
        _prod_settings(turnstile_secret_key=missing)


@pytest.mark.parametrize("missing", [None, ""], ids=["none", "empty"])
def test_production_allows_no_turnstile_when_not_required(missing: str | None):
    # Self-hosted / VPC / air-gapped opt-out: with TURNSTILE_REQUIRED=false a
    # production app boots with NO secret key, and the captcha is fully off (so no
    # call ever reaches challenges.cloudflare.com).
    s = _prod_settings(turnstile_required=False, turnstile_secret_key=missing)
    assert s.turnstile_enabled is False


def test_turnstile_enabled_requires_key_and_prod_or_dev_flag():
    # Production: the key alone enforces — the dev flag is irrelevant there.
    assert _prod_settings().turnstile_enabled is True
    assert _prod_settings(turnstile_dev_enabled=False).turnstile_enabled is True

    # Opt-out master switch: turnstile_required=False forces it OFF in production
    # even with a valid key set (the self-hosted / air-gapped path).
    assert _prod_settings(turnstile_required=False).turnstile_enabled is False

    # No key → never enforced, in any env.
    assert Settings(_env_file=None).turnstile_enabled is False  # type: ignore[call-arg]

    # Local with a key but NO dev flag → OFF, so a stray `.env.local` test key can't
    # break every local login; the explicit opt-in flag turns it back on.
    local_off = Settings(_env_file=None, app_env="local", turnstile_secret_key="x")  # type: ignore[call-arg]
    assert local_off.turnstile_enabled is False
    local_on = Settings(  # type: ignore[call-arg]
        _env_file=None, app_env="local", turnstile_secret_key="x", turnstile_dev_enabled=True
    )
    assert local_on.turnstile_enabled is True

    # Staging is non-production too: off with a key unless the flag is set.
    assert _staging_settings(turnstile_secret_key="x").turnstile_enabled is False
    assert (
        _staging_settings(turnstile_secret_key="x", turnstile_dev_enabled=True).turnstile_enabled
        is True
    )


def test_production_still_requires_jwt_secret():
    with pytest.raises(ValueError, match="AUTH_JWT_SECRET"):
        _prod_settings(auth_jwt_secret=None)


@pytest.mark.parametrize("missing", [None, ""], ids=["none", "empty"])
def test_production_requires_token_hash_pepper(missing: str | None):
    # The HMAC pepper for hashing reset/verification/invitation tokens at rest
    # must be set in non-local envs (mirrors AUTH_JWT_SECRET).
    with pytest.raises(ValueError, match="TOKEN_HASH_PEPPER"):
        _prod_settings(token_hash_pepper=missing)


#: The local-dev fallbacks published in this repository (``alkera_core.config``),
#: spelled out so the test pins the values an operator could actually paste.
PUBLISHED_DEV_SECRETS = [
    pytest.param("alkera-local-dev-secret-do-not-use-in-prod", id="dev-jwt-fallback"),
    pytest.param("alkera-local-dev-token-pepper-do-not-use-in-prod", id="dev-token-pepper"),
    pytest.param("alkera-local-dev-files-content-key-do-not-use-in-prod", id="dev-files-key"),
]

#: Values an operator could plausibly paste that are below the 32-byte floor.
SHORT_SECRETS = [
    pytest.param("changeme", id="changeme"),
    pytest.param("CHANGE-ME", id="change-me-placeholder"),
    pytest.param("<48+ char random string>", id="env-example-placeholder"),
    pytest.param("x" * 31, id="one-byte-under-the-floor"),
    # Padding adds bytes but no secret: the runtime signs with the whole value,
    # so the floor is measured on what is left once surrounding whitespace goes.
    pytest.param("changeme" + " " * 24, id="changeme-space-padded-to-32"),
    pytest.param("changeme" + "\t\n" * 12, id="changeme-tab-newline-padded-to-32"),
]

#: All-whitespace values. For the single slots these are refused; a
#: ``*_PREVIOUS`` list drops blank entries when it is parsed, so there they mean
#: "no previous value", which is what an operator clearing the list types.
WHITESPACE_ONLY = [
    pytest.param(" " * 32, id="thirty-two-spaces"),
    pytest.param("\t" * 40, id="all-tabs"),
]


@pytest.mark.parametrize("value", WHITESPACE_ONLY)
@pytest.mark.parametrize("field", ["auth_jwt_secret", "token_hash_pepper"])
def test_production_refuses_an_all_whitespace_secret(field: str, value: str) -> None:
    with pytest.raises(ValueError, match=rf"{field.upper()} "):
        _prod_settings(**{field: value})


#: Strong values boot: a generated token and a long password-manager passphrase
#: with no symbols (the floor is a length, not an entropy heuristic).
STRONG_SECRET = "kQ3v_9Zx1LmN-tRs7Yb2Wc4Ee6Gg8Ii0Kk2Mm4Oo6Qq8S"
PASSPHRASE_SECRET = "correct horse battery staple orbit lantern"


@pytest.mark.parametrize("value", PUBLISHED_DEV_SECRETS)
@pytest.mark.parametrize(
    "field",
    [
        "auth_jwt_secret",
        "token_hash_pepper",
        "auth_jwt_secret_previous",
        "token_hash_pepper_previous",
    ],
)
def test_production_refuses_a_published_dev_secret(field: str, value: str) -> None:
    label = field.upper().replace("_PREVIOUS", "_PREVIOUS[0]")
    with pytest.raises(ValueError, match=r"published in the Alkera repository") as exc:
        _prod_settings(**{field: value})
    assert label in str(exc.value)


@pytest.mark.parametrize("value", SHORT_SECRETS)
@pytest.mark.parametrize(
    "field",
    [
        "auth_jwt_secret",
        "token_hash_pepper",
        "auth_jwt_secret_previous",
        "token_hash_pepper_previous",
    ],
)
def test_production_refuses_a_secret_under_32_bytes(field: str, value: str) -> None:
    with pytest.raises(ValueError, match=r"must be at least 32 bytes"):
        _prod_settings(**{field: value})


def test_production_names_every_weak_secret_at_once() -> None:
    with pytest.raises(ValueError) as exc:
        _prod_settings(auth_jwt_secret="changeme", token_hash_pepper="short-pepper")
    assert "AUTH_JWT_SECRET must be at least 32 bytes" in str(exc.value)
    assert "(got 8 bytes)" in str(exc.value)
    assert "TOKEN_HASH_PEPPER must be at least 32 bytes" in str(exc.value)
    assert "(got 12 bytes)" in str(exc.value)
    assert "secrets.token_urlsafe(48)" in str(exc.value)


@pytest.mark.parametrize("value", [STRONG_SECRET, PASSPHRASE_SECRET], ids=["token", "passphrase"])
def test_production_boots_with_strong_secrets_in_every_slot(value: str) -> None:
    s = _prod_settings(
        auth_jwt_secret=value,
        token_hash_pepper=value[::-1],
        auth_jwt_secret_previous=f"{value}-old",
        token_hash_pepper_previous=f"{value[::-1]}-old",
    )
    assert s.effective_jwt_secret == value


@pytest.mark.parametrize("value", PUBLISHED_DEV_SECRETS)
def test_staging_keeps_its_lighter_secret_rules(value: str) -> None:
    # Staging previews run the local-style stack; the floor is production's.
    assert _staging_settings(auth_jwt_secret=value).auth_jwt_secret == value


def test_production_refuses_the_published_dev_files_signing_key() -> None:
    # 53 bytes, so the width check alone let it through.
    with pytest.raises(ValueError, match=r"FILES_CONTENT_SIGNING_KEY is a local-dev value"):
        _files_prod_settings(
            files_content_signing_key="alkera-local-dev-files-content-key-do-not-use-in-prod"
        )


SECRET_SLOTS = [
    "auth_jwt_secret",
    "token_hash_pepper",
    "auth_jwt_secret_previous",
    "token_hash_pepper_previous",
]


@pytest.mark.parametrize("field", SECRET_SLOTS)
def test_production_boots_a_secret_of_exactly_32_bytes(field: str) -> None:
    assert _prod_settings(**{field: "a1b2c3d4" * 4}).is_production


@pytest.mark.parametrize("field", SECRET_SLOTS)
def test_production_refuses_a_secret_of_31_bytes(field: str) -> None:
    with pytest.raises(ValueError, match=r"\(got 31 bytes\)"):
        _prod_settings(**{field: ("a1b2c3d4" * 4)[:31]})


@pytest.mark.parametrize(
    "padded",
    [
        pytest.param("alkera-local-dev-secret-do-not-use-in-prod  ", id="trailing-spaces"),
        pytest.param("alkera-local-dev-secret-do-not-use-in-prod\n", id="trailing-newline"),
        pytest.param("\talkera-local-dev-secret-do-not-use-in-prod", id="leading-tab"),
    ],
)
def test_production_refuses_the_dev_value_with_padding(padded: str) -> None:
    with pytest.raises(ValueError, match=r"AUTH_JWT_SECRET is a local-dev value"):
        _prod_settings(auth_jwt_secret=padded)


def test_the_floor_is_measured_in_bytes_not_characters() -> None:
    # 16 characters, 32 bytes in UTF-8: boots under a byte floor, would be
    # refused under a character floor.
    assert _prod_settings(auth_jwt_secret="\u00e9" * 16).is_production
    # 15 characters, 30 bytes: refused, and the message states the unit.
    with pytest.raises(
        ValueError, match=r"AUTH_JWT_SECRET must be at least 32 bytes.*\(got 30 bytes\)"
    ):
        _prod_settings(auth_jwt_secret="\u00e9" * 15)


@pytest.mark.parametrize(
    ("field", "label"),
    [
        ("auth_jwt_secret_previous", "AUTH_JWT_SECRET_PREVIOUS"),
        ("token_hash_pepper_previous", "TOKEN_HASH_PEPPER_PREVIOUS"),
    ],
)
def test_one_weak_previous_entry_among_strong_ones_is_named_by_position(
    field: str, label: str
) -> None:
    strong_a, strong_b = STRONG_SECRET, STRONG_SECRET[::-1]
    with pytest.raises(ValueError) as exc:
        _prod_settings(**{field: f"{strong_a},changeme,{strong_b}"})
    assert f"{label}[1] must be at least 32 bytes" in str(exc.value)
    assert f"{label}[0]" not in str(exc.value)
    assert f"{label}[2]" not in str(exc.value)


def test_a_blank_files_key_with_files_off_is_not_a_weak_secret() -> None:
    # The self-hosted compose file passes the key as `${FILES_CONTENT_SIGNING_KEY:-}`,
    # so a Files-off install hands the app an empty string.
    s = _prod_settings(files_enabled=False, files_content_signing_key="")
    assert s.files_enabled is False


@pytest.mark.parametrize("blank", ["", "   "], ids=["empty", "whitespace"])
def test_a_blank_files_key_with_files_on_is_refused(blank: str) -> None:
    with pytest.raises(ValueError, match=r"FILES_CONTENT_SIGNING_KEY must be set"):
        _files_prod_settings(files_content_signing_key=blank)


def test_a_refusal_never_echoes_a_secret_value() -> None:
    """pydantic appends the input it was given to a validation error; the
    refusal is logged at boot, so it must carry the setting names only."""
    weak = "weak-but-unique-marker-7Qx"
    live = "sk_live_UNIQUEMARKER9f3kP2nR8sT"
    with pytest.raises(ValueError) as exc:
        Settings(  # type: ignore[call-arg]
            _env_file=None,
            app_env="production",
            token_hash_pepper=STRONG_SECRET,
            auth_jwt_secret=weak,
            stripe_secret_key=live,
        )
    text = str(exc.value)
    assert "AUTH_JWT_SECRET must be at least 32 bytes" in text
    assert "input_value" not in text
    assert weak not in text
    assert live not in text


# --- the self-hosted compose reference, booted as shipped -------------------

_COMPOSE_PROD = (
    Path(__file__).resolve().parents[3] / "deploy" / "docker" / "compose.prod.example.yml"
)
_COMPOSE_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?:(:?[-?])([^}]*))?\}")

#: What an operator supplies: every variable the file's header lists as required
#: (pinned against the header below) plus the bootstrap profile's two `:?` names.
_COMPOSE_OPERATOR_ENV = {
    "SMTP_HOST": "email-smtp.us-east-1.amazonaws.com",
    "SMTP_USERNAME": "smtp-user-from-the-operator",
    "SMTP_PASSWORD": "smtp-password-from-the-operator",
    "DB_PASSWORD": "db-password-from-the-operator",
    "TEMPORAL_DB_PASSWORD": "temporal-db-password-from-the-operator",
    "PUBLIC_BASE_URL": "https://alkera.acme.example",
    "FILES_CONTENT_BASE_URL": "https://files.acme-content.example",
    "ADMIN_BOOTSTRAP_EMAIL": "admin@acme.example",
    "ADMIN_BOOTSTRAP_ORG_NAME": "Acme",
}


def _compose_interpolate(value: str, operator: dict[str, str]) -> str:
    """Docker Compose's variable rules for the forms the reference file uses."""

    def one(match: re.Match[str]) -> str:
        name, op, arg = match.group(1), match.group(2), match.group(3)
        given = operator.get(name)
        if op in (":-", ":?"):
            if given:
                return given
            if op == ":?":
                raise AssertionError(f"compose requires {name}: {arg}")
            return arg or ""
        if op in ("-", "?"):
            if given is not None:
                return given
            if op == "?":
                raise AssertionError(f"compose requires {name}: {arg}")
            return arg or ""
        return given or ""

    return _COMPOSE_VAR.sub(one, value)


def _compose_service_env(service: str, operator: dict[str, str]) -> dict[str, str]:
    import yaml

    compose = yaml.safe_load(_COMPOSE_PROD.read_text(encoding="utf-8"))
    raw = compose["services"][service]["environment"]
    return {k: _compose_interpolate(str(v), operator) for k, v in raw.items()}


def _python_services() -> list[str]:
    import yaml

    compose = yaml.safe_load(_COMPOSE_PROD.read_text(encoding="utf-8"))
    return sorted(
        name
        for name, svc in compose["services"].items()
        if isinstance(svc.get("environment"), dict) and "APP_ENV" in svc["environment"]
    )


def _compose_header_required() -> set[str]:
    """The names under the header's "Required env vars" heading."""
    text = _COMPOSE_PROD.read_text(encoding="utf-8")
    block = text.split("# Required env vars", 1)[1].split("# Optional:", 1)[0]
    return set(re.findall(r"^#   ([A-Z][A-Z0-9_]+)\s{2,}\S", block, flags=re.MULTILINE))


@pytest.mark.parametrize("name", ["AUTH_JWT_SECRET", "TOKEN_HASH_PEPPER"])
def test_the_compose_reference_runs_production_and_generates_each_server_secret(
    name: str,
) -> None:
    """The reference deployment never falls back to a published dev secret: it
    pins APP_ENV=production, and a signing secret the operator leaves unset is
    generated into a volume every Python service mounts instead of required."""
    import yaml

    text = _COMPOSE_PROD.read_text(encoding="utf-8")
    assert re.search(r"^\s+APP_ENV: production$", text, re.MULTILINE)
    required = {m.group(1) for m in _COMPOSE_VAR.finditer(text) if m.group(2) == ":?"}
    assert name not in required
    compose = yaml.safe_load(text)
    for service in _python_services():
        env = compose["services"][service]["environment"]
        directory = env["GENERATED_SECRETS_DIR"]
        assert any(
            str(volume).endswith(f":{directory}")
            for volume in compose["services"][service].get("volumes", [])
        ), service


def test_the_compose_reference_has_python_services_to_boot() -> None:
    assert {"backend", "worker", "gateway"} <= set(_python_services())


def test_the_compose_boot_supplies_exactly_what_the_header_requires() -> None:
    required = _compose_header_required()
    assert {"DB_PASSWORD", "TEMPORAL_DB_PASSWORD", "PUBLIC_BASE_URL"} <= required
    assert not {"AUTH_JWT_SECRET", "TOKEN_HASH_PEPPER"} & required
    assert required <= set(_COMPOSE_OPERATOR_ENV)


@pytest.mark.parametrize("service", _python_services())
def test_every_compose_python_service_boots_in_production_as_shipped(
    service: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The compose file itself, interpolated with only what its `:?` markers
    demand, must pass every production check: a new variable there that the
    validator refuses at its default breaks every self-hosted upgrade. The
    secrets volume is stood in for by a directory of this test's own."""
    for key, value in _compose_service_env(service, _COMPOSE_OPERATOR_ENV).items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("GENERATED_SECRETS_DIR", str(tmp_path))
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.is_production
    assert s.files_enabled is True
    assert s.auth_jwt_secret == (tmp_path / "AUTH_JWT_SECRET").read_text().strip()
    assert s.token_hash_pepper == (tmp_path / "TOKEN_HASH_PEPPER").read_text().strip()


def test_the_compose_boot_harness_does_run_the_production_checks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    for key, value in _compose_service_env(
        "backend", {**_COMPOSE_OPERATOR_ENV, "AUTH_JWT_SECRET": "changeme"}
    ).items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("GENERATED_SECRETS_DIR", str(tmp_path))
    with pytest.raises(ValueError, match=r"AUTH_JWT_SECRET must be at least 32 bytes"):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_staging_requires_token_hash_pepper():
    with pytest.raises(ValueError, match="TOKEN_HASH_PEPPER"):
        _staging_settings(token_hash_pepper=None)


def test_local_token_hash_pepper_falls_back_to_dev_value(monkeypatch: pytest.MonkeyPatch):
    # Local keeps its leniency: a missing pepper falls back to the dev constant.
    # Hermetic: a bootstrapped .env assigns TOKEN_HASH_PEPPER= (an empty string,
    # not "missing"), and make exports it into the environment too.
    monkeypatch.delenv("TOKEN_HASH_PEPPER", raising=False)
    s = Settings(_env_file=None, app_env="local")  # type: ignore[call-arg]
    assert s.is_local
    assert s.token_hash_pepper is None
    assert s.effective_token_hash_pepper  # non-empty dev fallback


_SERVER_SECRETS = (
    ("effective_jwt_secret", "AUTH_JWT_SECRET"),
    ("effective_token_hash_pepper", "TOKEN_HASH_PEPPER"),
    ("effective_files_content_signing_key", "FILES_CONTENT_SIGNING_KEY"),
)


@pytest.mark.parametrize(("prop", "name"), _SERVER_SECRETS)
def test_an_unset_app_env_never_stands_in_a_published_dev_secret(prop: str, name: str) -> None:
    """APP_ENV left unset defaults to local, but only an explicit APP_ENV=local
    may sign with the fallback anyone can read in the open repository."""
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.is_local and not s.app_env_is_explicit
    with pytest.raises(MissingServerSecretError, match=name):
        getattr(s, prop)
    with pytest.raises(MissingServerSecretError):
        s.require_server_secrets()


@pytest.mark.parametrize(("prop", "name"), _SERVER_SECRETS)
def test_an_explicit_local_env_keeps_the_dev_fallback(prop: str, name: str) -> None:
    s = Settings(_env_file=None, app_env="local")  # type: ignore[call-arg]
    assert "local-dev" in getattr(s, prop)
    s.require_server_secrets()


def test_an_unset_app_env_with_every_server_secret_set_serves() -> None:
    s = Settings(  # type: ignore[call-arg]
        _env_file=None,
        auth_jwt_secret="j" * 48,
        token_hash_pepper="p" * 48,
        files_content_signing_key="k" * 48,
    )
    s.require_server_secrets()
    assert s.effective_jwt_secret == "j" * 48


def test_app_env_from_the_environment_counts_as_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "local")
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.app_env_is_explicit
    s.require_server_secrets()


def test_production_refuses_mock_oauth_enabled():
    with pytest.raises(ValueError, match="OAUTH_MOCK_ENABLED"):
        _prod_settings(oauth_mock_enabled=True)


def test_staging_refuses_mock_oauth_enabled():
    # The mock provider is a credential-less bypass — forbidden in staging too,
    # not just production (regression guard for the staging gap).
    #
    # Built through the baseline helper so the config under test is fully
    # spelled here: the helper passes `_env_file=None`, and the autouse fixture
    # above clears the matching process env, so no repo-root `.env` value (a dev
    # `FILES_ENABLED=true`, say) can trip an earlier validator and make this case
    # pass on the wrong message.
    with pytest.raises(ValueError, match="OAUTH_MOCK_ENABLED"):
        _staging_settings(oauth_mock_enabled=True)


def test_local_allows_mock_oauth_enabled():
    s = Settings(app_env="local", oauth_mock_enabled=True)  # type: ignore[arg-type]
    assert s.oauth_mock_enabled is True


def test_mock_oauth_disabled_by_default():
    # Fail-safe: the mock provider is OFF unless explicitly enabled, so a deploy
    # that forgets APP_ENV (falling back to "local") still won't expose it.
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.oauth_mock_enabled is False


def test_production_refuses_half_configured_google():
    with pytest.raises(ValueError, match="OAUTH_GOOGLE"):
        _prod_settings(oauth_google_client_id="id-only", oauth_google_client_secret=None)


def test_production_refuses_half_configured_github():
    with pytest.raises(ValueError, match="OAUTH_GITHUB"):
        _prod_settings(oauth_github_client_id="id-only", oauth_github_client_secret=None)


def test_production_refuses_localhost_callback_when_provider_configured():
    with pytest.raises(ValueError, match="API_PUBLIC_BASE_URL"):
        _prod_settings(
            oauth_google_client_id="id",
            oauth_google_client_secret="secret",
            api_public_base_url="http://localhost:8000",
        )


def test_production_accepts_fully_configured_oauth():
    s = _prod_settings(
        oauth_google_client_id="id",
        oauth_google_client_secret="secret",
        api_public_base_url="https://api.alkera.example",
    )
    assert s.google_oauth_configured is True


def test_local_mode_tolerates_dev_defaults():
    """Local mode must keep its leniency — the validator only fires in prod."""
    s = Settings()  # all defaults; app_env=local
    assert s.is_local
    assert s.smtp_host == "localhost"
    assert s.smtp_from_name is None  # the brand names the sender
    assert s.auth_cookie_secure is False


def test_is_staging_property():
    s = _staging_settings()
    assert s.is_staging
    assert not s.is_local
    assert not s.is_production


def test_staging_baseline_is_accepted():
    """The reference 'good' staging config must not trip the validator."""
    s = _staging_settings()
    assert s.is_staging
    assert s.auth_cookie_secure is True


def test_staging_refuses_insecure_cookie():
    with pytest.raises(ValueError, match="AUTH_COOKIE_SECURE"):
        _staging_settings(auth_cookie_secure=False)


def test_staging_refuses_default_cors_origins():
    with pytest.raises(ValueError, match="API_CORS_ORIGINS"):
        _staging_settings(api_cors_origins="http://localhost:5173")


def test_staging_refuses_default_frontend_url():
    with pytest.raises(ValueError, match="FRONTEND_BASE_URL"):
        _staging_settings(frontend_base_url="http://localhost:5173")


def test_staging_still_requires_jwt_secret():
    with pytest.raises(ValueError, match="AUTH_JWT_SECRET"):
        _staging_settings(auth_jwt_secret=None)


@pytest.mark.parametrize(
    ("overrides", "refused"),
    [
        pytest.param(
            {"stripe_secret_key": "sk_test_x", "stripe_webhook_secret": "whsec_x"},
            None,
            id="test-secret-key-with-its-endpoint-secret-is-accepted",
        ),
        pytest.param(
            {"stripe_secret_key": "rk_test_x", "stripe_webhook_secret": "whsec_x"},
            None,
            id="restricted-test-key-is-accepted",
        ),
        pytest.param(
            {
                "stripe_secret_key": "sk_test_x",
                "stripe_webhook_secret": "whsec_x",
                "stripe_publishable_key": "pk_test_x",
            },
            None,
            id="test-publishable-key-is-accepted",
        ),
        pytest.param(
            {"stripe_secret_key": "sk_live_x", "stripe_webhook_secret": "whsec_x"},
            "STRIPE_SECRET_KEY must be a test-mode key",
            id="a-live-secret-key-is-refused",
        ),
        pytest.param(
            {"stripe_secret_key": "rk_live_x", "stripe_webhook_secret": "whsec_x"},
            "STRIPE_SECRET_KEY must be a test-mode key",
            id="a-live-restricted-key-is-refused",
        ),
        pytest.param(
            {"stripe_secret_key": "sk_test_x"},
            "STRIPE_WEBHOOK_SECRET must be set",
            id="a-test-key-without-the-endpoint-secret-is-refused",
        ),
        pytest.param(
            {
                "stripe_secret_key": "sk_test_x",
                "stripe_webhook_secret": "whsec_x",
                "stripe_publishable_key": "pk_live_x",
            },
            "STRIPE_PUBLISHABLE_KEY must be a test-mode key",
            id="a-live-publishable-key-is-refused",
        ),
        pytest.param(
            {"stripe_publishable_key": "pk_live_x"},
            "STRIPE_PUBLISHABLE_KEY must be a test-mode key",
            id="a-live-publishable-key-is-refused-even-with-no-secret-key",
        ),
        pytest.param(
            {"stripe_secret_key": "SK_TEST_x", "stripe_webhook_secret": "whsec_x"},
            "STRIPE_SECRET_KEY must be a test-mode key",
            id="the-prefix-match-is-exact-not-case-folded",
        ),
    ],
)
def test_staging_admits_only_stripe_test_mode(overrides: dict[str, object], refused: str | None):
    """Staging rehearses billing against Stripe's test mode and nothing else:
    a live key of either kind is refused at boot, the way production refuses
    a test key, and a secret key still needs its endpoint's signing secret."""
    if refused is None:
        assert _staging_settings(**overrides).is_staging
        return
    with pytest.raises(ValueError, match=refused):
        _staging_settings(**overrides)


def test_production_still_refuses_a_test_key_so_the_two_guards_never_agree():
    """The staging guard and the production guard are opposites by design: no
    one Stripe key can boot both environments."""
    with pytest.raises(ValueError, match="must be a live key"):
        _prod_settings(stripe_secret_key="sk_test_x", stripe_webhook_secret="whsec_x")
    assert _staging_settings(stripe_secret_key="sk_test_x", stripe_webhook_secret="whsec_x")


def test_staging_allows_mailpit_and_inbox_postgres():
    # Lighter than prod: a per-PR preview runs the local-style compose stack, so
    # Mailpit (SMTP_HOST=localhost) + the dev-default Postgres are allowed in
    # staging even though production rejects both.
    s = _staging_settings(
        smtp_host="localhost",
        database_url="postgresql+asyncpg://alkera:alkera@localhost:5432/alkera",
    )
    assert s.is_staging  # validators passed


def test_provider_and_aws_credentials_default_to_none(monkeypatch: pytest.MonkeyPatch):
    for var in (
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "AWS_BEARER_TOKEN_BEDROCK",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
    ):
        monkeypatch.delenv(var, raising=False)
    # `_env_file=None` ignores the repo's real `.env`/`.env.local` (which a dev may
    # have filled with live provider keys) so we test the true field defaults.
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.openai_api_key is None
    assert s.anthropic_api_key is None
    assert s.aws_bearer_token_bedrock is None
    assert s.aws_access_key_id is None
    assert s.aws_secret_access_key is None
    assert s.aws_session_token is None


def test_env_local_overrides_env(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """`.env.local` overlays `.env` and wins on conflict — the loading contract
    the model gateway relies on for local downstream provider keys."""
    # Isolate from ambient env so the files are the only source.
    for var in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "LOG_LEVEL"):
        monkeypatch.delenv(var, raising=False)
    env = tmp_path / ".env"
    env.write_text("OPENAI_API_KEY=from-dotenv\nLOG_LEVEL=INFO\n", encoding="utf-8")
    env_local = tmp_path / ".env.local"
    env_local.write_text("OPENAI_API_KEY=from-dotenv-local\n", encoding="utf-8")

    # Same precedence as Settings.model_config: later file wins.
    s = Settings(_env_file=(str(env), str(env_local)))  # type: ignore[call-arg]
    assert s.openai_api_key == "from-dotenv-local"  # .env.local wins
    assert s.log_level == "INFO"  # .env value survives when not overridden


def test_entitlement_fields_default_unset():
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.alkera_entitlements is None
    assert s.alkera_entitlements_public_key is None
    assert s.alkera_entitlements_signing_key is None
    assert s.entitlements_minting_configured is False


def test_prod_accepts_valid_entitlement_signing_key():
    from alkera_core.entitlements import generate_keypair

    seed, _pub = generate_keypair()
    s = _prod_settings(alkera_entitlements_signing_key=seed)
    assert s.entitlements_minting_configured is True


@pytest.mark.parametrize(
    "bad_seed",
    [
        pytest.param("not-base64!!", id="not-b64"),
        pytest.param("c2hvcnQ", id="decodes-short"),  # "short"
        pytest.param("A" * 100, id="decodes-long"),
    ],
)
def test_prod_refuses_malformed_entitlement_signing_key(bad_seed: str):
    with pytest.raises(ValueError, match="ALKERA_ENTITLEMENTS_SIGNING_KEY"):
        _prod_settings(alkera_entitlements_signing_key=bad_seed)


def test_prod_never_validates_the_customer_entitlement_token():
    """A malformed CUSTOMER token must never refuse a boot — fail closed and
    quiet is the contract (the deployment just runs unentitled)."""
    s = _prod_settings(alkera_entitlements="alk1.total.garbage")
    assert s.alkera_entitlements == "alk1.total.garbage"


# --- /metrics scrape credential -------------------------------------------------


def test_metrics_scrape_token_defaults_unset():
    """Metrics stay on by default, but the scrape credential is opt-in — with none
    set, /metrics answers 404 outside local dev (see alkera_core.observability.asgi)."""
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.metrics_enabled is True
    assert s.metrics_auth_token is None


@pytest.mark.parametrize(
    ("token", "accepted"),
    [
        pytest.param(None, True, id="unset-serves-no-metrics-at-all"),
        # Every key in the alkera/<env> Secrets Manager envelope is seeded as ""
        # and injected into all four task defs, so "" is the shape an unset
        # credential ACTUALLY arrives in. Treating it as a weak secret would refuse
        # to boot backend, gateway, worker AND beat — this validator runs at import
        # time in every one of them. It means the same thing the request path
        # already believes it means: no credential, serve no metrics.
        pytest.param("", True, id="empty-is-unset-not-weak"),
        pytest.param("m3trics-scrape-9f4b2c", True, id="high-entropy-token"),
        pytest.param("hunter2", False, id="too-short"),
        pytest.param("aaaaaaaaaaaaaaaaaaaa", False, id="one-distinct-character"),
        pytest.param("abababababababababab", False, id="two-distinct-characters"),
        pytest.param("                    ", False, id="blank-is-a-real-misconfiguration"),
        pytest.param(" ", False, id="one-space-is-not-empty"),
        pytest.param("scrape-token-9f4", True, id="exactly-16-characters"),
        pytest.param("scrape-token-9f", False, id="one-below-16-characters"),
    ],
)
def test_production_requires_a_high_entropy_metrics_token(token: str | None, accepted: bool):
    """The token is the only thing between the internet and /metrics (the ALB
    routes on Host alone), so production refuses a guessable one. Unset — None or
    the empty string Secrets Manager seeds — stays valid: that deployment just
    serves no metrics."""
    if accepted:
        assert _prod_settings(metrics_auth_token=token).metrics_auth_token == token
    else:
        with pytest.raises(ValueError, match="METRICS_AUTH_TOKEN"):
            _prod_settings(metrics_auth_token=token)


def test_local_stays_lenient_on_a_weak_metrics_token():
    """A dev scraping their own loopback stack must not be blocked from booting."""
    assert Settings(_env_file=None, metrics_auth_token="dev").metrics_auth_token == "dev"  # type: ignore[call-arg]


# --- gateway backpressure vs the real connection-pool ceiling -------------------


async def test_the_pool_ceiling_constant_matches_the_client_we_actually_build():
    """`UPSTREAM_POOL_MAX_CONNECTIONS` is only meaningful if it IS the pool the
    gateway's shared client gets. `alkera_core.http.async_client` states the ceiling
    explicitly via `UPSTREAM_POOL_LIMITS`, so the constant and the client are bound
    by a value WE own instead of by httpx's default — a factory that stopped
    applying it fails here rather than silently re-opening the gap between the shed
    threshold and real capacity. (The end-to-end proof that a built client's pool
    really carries the ceiling lives in `test_http.py`.)"""
    from alkera_core.http import UPSTREAM_POOL_LIMITS

    assert UPSTREAM_POOL_LIMITS.max_connections == UPSTREAM_POOL_MAX_CONNECTIONS


def test_the_shipped_stream_cap_sheds_before_the_pool_saturates():
    """The shed threshold must be BELOW the connection ceiling: above it the gate
    never fires, and saturation surfaces as a 10s pool timeout that the pipeline
    retries and fails over as if the PROVIDER had failed — holding a gate slot, a
    credit reservation and a committed request row for far longer."""
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert 0 < s.gateway_max_concurrent_streams <= UPSTREAM_POOL_MAX_CONNECTIONS


@pytest.mark.parametrize(
    ("cap", "accepted"),
    [
        pytest.param(0, False, id="zero-sheds-everything"),
        pytest.param(-1, False, id="negative"),
        pytest.param(1, True, id="minimum"),
        pytest.param(80, True, id="the-shipped-default"),
        pytest.param(UPSTREAM_POOL_MAX_CONNECTIONS, True, id="exactly-at-the-pool-ceiling"),
        pytest.param(UPSTREAM_POOL_MAX_CONNECTIONS + 1, False, id="one-past-the-pool-ceiling"),
        pytest.param(10000, False, id="the-old-100x-default"),
    ],
)
@pytest.mark.parametrize(
    "app_env", [pytest.param("local", id="local"), pytest.param("production", id="production")]
)
def test_stream_cap_above_the_pool_ceiling_refuses_to_boot(cap: int, accepted: bool, app_env: str):
    """Enforced in EVERY environment, not just production: the inversion is a
    capacity bug wherever the gateway runs."""

    def _local_settings(**overrides: object) -> Settings:
        return Settings(_env_file=None, app_env="local", **overrides)  # type: ignore[arg-type, call-arg]

    build = _prod_settings if app_env == "production" else _local_settings
    if accepted:
        assert build(gateway_max_concurrent_streams=cap).gateway_max_concurrent_streams == cap
    else:
        with pytest.raises(ValidationError, match="GATEWAY_MAX_CONCURRENT_STREAMS"):
            build(gateway_max_concurrent_streams=cap)


def test_env_example_values_are_unquoted():
    """Quoted values in `.env.example` fork the config: dotenv readers strip the
    quotes, but the Makefile's `include .env` + `export` forwards them verbatim
    into every child process (pytest, uvicorn), where the OS env outranks the
    dotenv files. `SMTP_FROM_NAME="Alkera AI"` once shipped literal quotes into
    the From header under `make` and failed the defaults assertions above."""
    example = Path(__file__).parents[3] / ".env.example"
    quoted = []
    for line in example.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if value.startswith(('"', "'")):
            quoted.append(key)
    assert quoted == []


# --- DB pool geometry -----------------------------------------------------------


def test_pool_geometry_defaults_match_the_historical_constants():
    """The settings defaults must equal the previously hard-coded 20 + 10 so that
    every deployment that sets nothing keeps its exact pool behavior."""
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.database_pool_size == 20
    assert s.database_pool_max_overflow == 10


def test_pool_geometry_reads_the_environment(monkeypatch: pytest.MonkeyPatch):
    """A service pins its pool per-task-environment (the gateway runs 12+6 so the
    fleet's resident connections stay inside the RDS budget)."""
    monkeypatch.setenv("DATABASE_POOL_SIZE", "12")
    monkeypatch.setenv("DATABASE_POOL_MAX_OVERFLOW", "6")
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.database_pool_size == 12
    assert s.database_pool_max_overflow == 6


def test_the_engine_is_built_from_the_settings_geometry():
    """`alkera_core.db.session` must derive its pool from the settings, not a
    constant — otherwise the terraform env knobs are silently inert. Asserting
    `session.POOL_SIZE == settings.database_pool_size` in-process can't fail
    (alias vs the same singleton, and the default equals the historical
    constant), so build the engine in a SUBPROCESS under a non-default env and
    read the LIVE pool geometry back — a session.py hard-coding regression
    fails this while the in-process comparison would stay green."""
    import subprocess
    import sys

    code = (
        "from alkera_core.db import session\n"
        "p = session.engine.sync_engine.pool\n"
        "print(p.size(), p._max_overflow)\n"
    )
    env = {**os.environ, "DATABASE_POOL_SIZE": "7", "DATABASE_POOL_MAX_OVERFLOW": "3"}
    out = subprocess.run(
        [sys.executable, "-c", code], env=env, capture_output=True, text=True, check=True
    )
    assert out.stdout.split() == ["7", "3"]


@pytest.mark.parametrize(
    ("size", "accepted"),
    [
        pytest.param(0, False, id="zero-cannot-serve"),
        pytest.param(-1, False, id="negative"),
        pytest.param(1, True, id="minimum"),
        pytest.param(12, True, id="the-gateway-override"),
        pytest.param(20, True, id="the-shipped-default"),
        pytest.param(100, True, id="upper-bound"),
        pytest.param(101, False, id="past-the-upper-bound"),
    ],
)
def test_pool_size_bounds(size: int, accepted: bool):
    if accepted:
        assert Settings(_env_file=None, database_pool_size=size).database_pool_size == size  # type: ignore[call-arg]
    else:
        with pytest.raises(ValidationError, match="DATABASE_POOL_SIZE"):
            Settings(_env_file=None, database_pool_size=size)  # type: ignore[call-arg]


@pytest.mark.parametrize(
    ("overflow", "accepted"),
    [
        pytest.param(-1, False, id="negative"),
        pytest.param(0, True, id="zero-is-a-fixed-pool"),
        pytest.param(6, True, id="the-gateway-override"),
        pytest.param(10, True, id="the-shipped-default"),
        pytest.param(100, True, id="upper-bound"),
        pytest.param(101, False, id="past-the-upper-bound"),
    ],
)
def test_pool_overflow_bounds(overflow: int, accepted: bool):
    if accepted:
        s = Settings(_env_file=None, database_pool_max_overflow=overflow)  # type: ignore[call-arg]
        assert s.database_pool_max_overflow == overflow
    else:
        with pytest.raises(ValidationError, match="DATABASE_POOL_MAX_OVERFLOW"):
            Settings(_env_file=None, database_pool_max_overflow=overflow)  # type: ignore[call-arg]


# --- Temporal -----------------------------------------------------------------


def test_temporal_defaults_point_at_the_local_dev_server():
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.temporal_address == "localhost:7233"
    assert s.temporal_namespace == "default"
    assert s.temporal_api_key is None
    assert s.temporal_tls is None
    assert s.temporal_tls_client_cert is None
    assert s.temporal_tls_client_key is None
    assert s.alkera_temporal_task_queues == "money,email,sync,default"
    assert s.temporal_task_queue_list == ["money", "email", "sync", "default"]
    assert s.alkera_worker_health_host == "127.0.0.1"
    assert s.alkera_worker_health_port == 9000
    assert s.alkera_worker_max_concurrent_activities == 3
    assert s.alkera_worker_connect_timeout_seconds == 120


@pytest.mark.parametrize(
    ("api_key", "tls", "expected"),
    [
        pytest.param(None, None, False, id="no-key-auto-off"),
        pytest.param("", None, False, id="blank-key-auto-off"),
        pytest.param("tmprl_key", None, True, id="key-auto-on"),
        pytest.param(None, True, True, id="explicit-on-without-key"),
        pytest.param("tmprl_key", False, False, id="explicit-off-beats-inference"),
        pytest.param(None, False, False, id="explicit-off-without-key"),
    ],
)
def test_temporal_tls_enabled_inference(api_key: str | None, tls: bool | None, expected: bool):
    s = Settings(_env_file=None, temporal_api_key=api_key, temporal_tls=tls)  # type: ignore[call-arg]
    assert s.temporal_tls_enabled is expected


@pytest.mark.parametrize("blank", ["", "  "], ids=["empty", "spaces"])
def test_a_blank_temporal_tls_env_value_means_auto(monkeypatch: pytest.MonkeyPatch, blank: str):
    """`TEMPORAL_TLS=` in a seeded env file or task definition must not fail boot
    with a boolean parse error; blank is the same as unset."""
    monkeypatch.setenv("TEMPORAL_TLS", blank)
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.temporal_tls is None
    assert s.temporal_tls_enabled is False
    monkeypatch.setenv("TEMPORAL_API_KEY", "tmprl_key")
    assert Settings(_env_file=None).temporal_tls_enabled is True  # type: ignore[call-arg]


def test_a_non_boolean_temporal_tls_value_is_still_refused():
    with pytest.raises(ValidationError, match="temporal_tls"):
        Settings(_env_file=None, temporal_tls="maybe")  # type: ignore[call-arg]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param("money", ["money"], id="single"),
        pytest.param("money,email,sync,default", ["money", "email", "sync", "default"], id="all"),
        pytest.param(" sync , money ", ["sync", "money"], id="trimmed-order-kept"),
        pytest.param("money,money,sync", ["money", "sync"], id="deduped"),
        pytest.param("default,,sync,", ["default", "sync"], id="empty-items-dropped"),
    ],
)
def test_temporal_task_queue_list_parses(raw: str, expected: list[str]):
    s = Settings(_env_file=None, alkera_temporal_task_queues=raw)  # type: ignore[call-arg]
    assert s.temporal_task_queue_list == expected


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param("", id="empty"),
        pytest.param(" , ", id="only-separators"),
        pytest.param("money,payments", id="unknown-name"),
        pytest.param("Money", id="wrong-case"),
        pytest.param("money;sync", id="wrong-separator"),
    ],
)
def test_temporal_task_queues_reject_unknown_or_empty_names(raw: str):
    with pytest.raises(ValidationError, match="ALKERA_TEMPORAL_TASK_QUEUES"):
        Settings(_env_file=None, alkera_temporal_task_queues=raw)  # type: ignore[call-arg]


@pytest.mark.parametrize(
    ("field", "value", "accepted"),
    [
        pytest.param("alkera_worker_health_port", 0, False, id="port-0"),
        pytest.param("alkera_worker_health_port", 1, True, id="port-1"),
        pytest.param("alkera_worker_health_port", 65535, True, id="port-65535"),
        pytest.param("alkera_worker_health_port", 65536, False, id="port-65536"),
        pytest.param("alkera_worker_max_concurrent_activities", 0, False, id="concurrency-0"),
        pytest.param("alkera_worker_max_concurrent_activities", 1, True, id="concurrency-1"),
        pytest.param("alkera_worker_max_concurrent_activities", 100, True, id="concurrency-100"),
        pytest.param("alkera_worker_max_concurrent_activities", 101, False, id="concurrency-101"),
        pytest.param("alkera_worker_connect_timeout_seconds", 0, False, id="connect-timeout-0"),
        pytest.param("alkera_worker_connect_timeout_seconds", 1, True, id="connect-timeout-1"),
        pytest.param("alkera_worker_connect_timeout_seconds", 3600, True, id="connect-timeout-1h"),
        pytest.param(
            "alkera_worker_connect_timeout_seconds", 3601, False, id="connect-timeout-over"
        ),
    ],
)
def test_worker_numeric_settings_are_bounded(field: str, value: int, accepted: bool):
    if accepted:
        assert getattr(Settings(_env_file=None, **{field: value}), field) == value  # type: ignore[call-arg]
    else:
        with pytest.raises(ValidationError, match=field.upper()):
            Settings(_env_file=None, **{field: value})  # type: ignore[call-arg]


def test_local_accepts_the_dev_temporal_defaults():
    """Only production refuses the dev server; local boots on the defaults."""
    assert Settings(_env_file=None).temporal_address == "localhost:7233"  # type: ignore[call-arg]


@pytest.mark.parametrize(
    "address",
    [
        pytest.param("localhost:7233", id="localhost"),
        pytest.param("LOCALHOST:7233", id="localhost-uppercase"),
        pytest.param("127.0.0.1:7233", id="loopback-v4"),
        pytest.param("[::1]:7233", id="loopback-v6"),
        pytest.param("  localhost:7233 ", id="padded"),
    ],
)
def test_production_refuses_a_local_temporal_address(address: str):
    with pytest.raises(ValueError, match="TEMPORAL_ADDRESS must point at the production"):
        _prod_settings(temporal_address=address)


@pytest.mark.parametrize(
    "address",
    [
        pytest.param("", id="empty"),
        pytest.param("   ", id="spaces"),
        pytest.param("\t", id="tab"),
        pytest.param(" \n ", id="newline"),
    ],
)
def test_production_refuses_a_blank_temporal_address(address: str):
    """A deploy that seeds every variable but leaves this one empty must not
    boot: a blank address is not the dev default, so the loopback rule alone
    would let a worker start and then fail on its first connect."""
    with pytest.raises(ValueError, match="TEMPORAL_ADDRESS must be set") as excinfo:
        _prod_settings(temporal_address=address)
    # One problem, one line: the blank address is not also reported as a local one.
    assert "must point at the production" not in str(excinfo.value)


@pytest.mark.parametrize(
    "address",
    [
        pytest.param("temporal.example.internal:7233", id="private-dns"),
        pytest.param("us-east-1.aws.api.temporal.io:7233", id="cloud-regional"),
        pytest.param("10.0.12.7:7233", id="private-ip"),
    ],
)
def test_production_accepts_a_real_temporal_address(address: str):
    assert _prod_settings(temporal_address=address).temporal_address == address


def test_production_refuses_an_api_key_over_cleartext():
    with pytest.raises(ValueError, match="TEMPORAL_TLS=false with TEMPORAL_API_KEY"):
        _prod_settings(
            temporal_api_key="tmprl_key", temporal_tls=False, temporal_namespace="prod.acct"
        )


def test_production_refuses_an_api_key_with_a_dotless_namespace():
    with pytest.raises(ValueError, match="TEMPORAL_NAMESPACE must be the hosted form"):
        _prod_settings(temporal_api_key="tmprl_key", temporal_namespace="default")


def test_production_accepts_a_hosted_namespace_with_a_key_and_inferred_tls():
    s = _prod_settings(
        temporal_address="us-east-1.aws.api.temporal.io:7233",
        temporal_api_key="tmprl_key",
        temporal_namespace="example-prod.a1b2c",
    )
    assert s.temporal_tls_enabled is True


def test_production_accepts_the_self_hosted_shape_without_key_or_tls():
    """The default deployment: auto-setup on private DNS, plain gRPC, no key."""
    s = _prod_settings()
    assert s.temporal_api_key is None
    assert s.temporal_tls_enabled is False
    assert s.temporal_namespace == "default"


@pytest.mark.parametrize(
    "half",
    [
        pytest.param({"temporal_tls_client_cert": _TEMPORAL_CERT_PEM}, id="cert-only"),
        pytest.param({"temporal_tls_client_key": _PRIVATE_KEY_PEM}, id="key-only"),
    ],
)
def test_production_refuses_half_an_mtls_pair(half: dict[str, str]):
    with pytest.raises(ValueError, match="TEMPORAL_TLS_CLIENT_CERT and TEMPORAL_TLS_CLIENT_KEY"):
        _prod_settings(**half)


def test_production_accepts_an_inline_mtls_pair():
    s = _prod_settings(
        temporal_tls_client_cert=_TEMPORAL_CERT_PEM, temporal_tls_client_key=_PRIVATE_KEY_PEM
    )
    assert s.temporal_tls_client_cert is not None


def test_production_accepts_an_mtls_pair_from_files(tmp_path):
    cert = tmp_path / "client.crt"
    key = tmp_path / "client.key"
    cert.write_text(_TEMPORAL_CERT_PEM)
    key.write_text(_PRIVATE_KEY_PEM)
    s = _prod_settings(temporal_tls_client_cert=str(cert), temporal_tls_client_key=str(key))
    assert s.temporal_tls_client_cert == str(cert)


def test_production_refuses_an_unreadable_mtls_cert_path(tmp_path):
    key = tmp_path / "client.key"
    key.write_text(_PRIVATE_KEY_PEM)
    with pytest.raises(ValueError, match="TEMPORAL_TLS_CLIENT_CERT must be inline PEM"):
        _prod_settings(
            temporal_tls_client_cert=str(tmp_path / "missing.crt"),
            temporal_tls_client_key=str(key),
        )


def test_production_refuses_an_unreadable_mtls_key_path(tmp_path):
    cert = tmp_path / "client.crt"
    cert.write_text(_TEMPORAL_CERT_PEM)
    with pytest.raises(ValueError, match="TEMPORAL_TLS_CLIENT_KEY must be inline PEM"):
        _prod_settings(
            temporal_tls_client_cert=str(cert),
            temporal_tls_client_key=str(tmp_path / "missing.key"),
        )


def test_production_refuses_an_mtls_cert_file_the_process_cannot_read(tmp_path):
    cert = tmp_path / "client.crt"
    key = tmp_path / "client.key"
    cert.write_text(_TEMPORAL_CERT_PEM)
    key.write_text(_PRIVATE_KEY_PEM)
    cert.chmod(0o000)
    try:
        if os.access(cert, os.R_OK):
            pytest.skip("running as a user that can read mode-000 files (root)")
        with pytest.raises(ValueError, match="TEMPORAL_TLS_CLIENT_CERT must be inline PEM"):
            _prod_settings(temporal_tls_client_cert=str(cert), temporal_tls_client_key=str(key))
    finally:
        cert.chmod(0o600)


def test_production_reports_every_temporal_error_at_once():
    """The operator sees the full list on one failed boot, not one error per restart."""
    with pytest.raises(ValueError) as excinfo:
        _prod_settings(
            temporal_address="localhost:7233",
            temporal_api_key="tmprl_key",
            temporal_tls=False,
            temporal_namespace="default",
            temporal_tls_client_cert=_TEMPORAL_CERT_PEM,
        )
    message = str(excinfo.value)
    for fragment in (
        "TEMPORAL_ADDRESS must point",
        "TEMPORAL_TLS=false with TEMPORAL_API_KEY",
        "TEMPORAL_NAMESPACE must be the hosted form",
        "must be set together",
    ):
        assert fragment in message


# --- Realtime -----------------------------------------------------------------


def test_realtime_defaults():
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.realtime_listener_enabled is True
    assert s.realtime_poll_interval_seconds == 5.0
    assert s.realtime_sse_keepalive_seconds == 15
    assert s.realtime_sse_max_stream_seconds == 3000
    assert s.realtime_sse_max_streams == 500
    assert s.realtime_sse_max_streams_per_user == 8
    assert s.realtime_ws_max_connections == 500
    assert s.realtime_ws_max_connections_per_user == 8
    assert s.realtime_ws_max_session_seconds == 3000
    assert s.realtime_ws_ticket_ttl_seconds == 30
    assert s.realtime_presence_ttl_seconds == 45
    assert s.realtime_ephemeral_max_bytes == 4096
    assert s.realtime_doc_max_bytes == 4 * 1024 * 1024
    assert s.realtime_ws_frame_budget_bytes == 500 * REALTIME_WS_MAX_FRAME_BYTES


def test_the_websocket_frame_ceiling_is_the_event_log_frame_ceiling():
    """The number every launch line passes as `--ws-max-size` and the number the
    socket layer refuses a larger frame with are the same ceiling, restated in
    config only because the module that owns it imports settings. They must move
    together: a config copy that drifted low would refuse a boot the transport
    would have served, and one that drifted high would under-state the memory
    the budget check exists to bound."""
    from alkera_core.events.outbox import MAX_FRAME_BYTES

    assert REALTIME_WS_MAX_FRAME_BYTES == MAX_FRAME_BYTES


def test_the_default_websocket_frame_budget_covers_the_default_connection_cap():
    """The defaults have to be a bootable pair — this validator runs in every
    environment, so a default that refused its own defaults would refuse every
    local dev server."""
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    peak = REALTIME_WS_MAX_FRAME_BYTES * s.realtime_ws_max_connections
    assert peak <= s.realtime_ws_frame_budget_bytes
    # ...and with no slack, so one more connection than the default cap is
    # refused rather than quietly absorbed.
    assert peak + REALTIME_WS_MAX_FRAME_BYTES > s.realtime_ws_frame_budget_bytes


def test_a_smaller_pod_may_trade_connections_for_frame_budget():
    """The point of stating the peak once: a deployment on a pod that cannot hold
    1 GiB of receive buffers lowers BOTH numbers and boots."""
    s = Settings(  # type: ignore[call-arg]
        _env_file=None,
        realtime_ws_frame_budget_bytes=8 * REALTIME_WS_MAX_FRAME_BYTES,
        realtime_ws_max_connections=8,
        realtime_ws_max_connections_per_user=4,
    )
    assert s.realtime_ws_max_connections == 8


@pytest.mark.parametrize(
    ("overrides", "fragment"),
    [
        pytest.param(
            {"realtime_poll_interval_seconds": 0},
            "REALTIME_POLL_INTERVAL_SECONDS",
            id="poll-zero",
        ),
        pytest.param(
            {"realtime_poll_interval_seconds": 0.01},
            "REALTIME_POLL_INTERVAL_SECONDS",
            id="poll-too-fast",
        ),
        pytest.param(
            {"realtime_poll_interval_seconds": 301},
            "REALTIME_POLL_INTERVAL_SECONDS",
            id="poll-too-slow",
        ),
        pytest.param(
            {"realtime_sse_keepalive_seconds": 0},
            "REALTIME_SSE_KEEPALIVE_SECONDS must be between",
            id="keepalive-zero",
        ),
        pytest.param(
            {"realtime_sse_keepalive_seconds": 301},
            "REALTIME_SSE_KEEPALIVE_SECONDS must be between",
            id="keepalive-too-slow",
        ),
        pytest.param(
            {"realtime_sse_max_stream_seconds": 15},
            "must exceed REALTIME_SSE_KEEPALIVE_SECONDS",
            id="deadline-equals-keepalive",
        ),
        pytest.param(
            {"realtime_sse_max_stream_seconds": 10},
            "must exceed REALTIME_SSE_KEEPALIVE_SECONDS",
            id="deadline-below-keepalive",
        ),
        pytest.param(
            {"realtime_ws_max_session_seconds": 0},
            "REALTIME_WS_MAX_SESSION_SECONDS",
            id="ws-session-zero",
        ),
        pytest.param(
            {"realtime_sse_max_streams": 0},
            "REALTIME_SSE_MAX_STREAMS must be at least 1",
            id="sse-cap-zero",
        ),
        pytest.param(
            {"realtime_sse_max_streams_per_user": 0},
            "REALTIME_SSE_MAX_STREAMS_PER_USER must be at least 1",
            id="sse-per-user-zero",
        ),
        pytest.param(
            {"realtime_sse_max_streams": 4},
            "REALTIME_SSE_MAX_STREAMS_PER_USER must not exceed REALTIME_SSE_MAX_STREAMS",
            id="sse-per-user-above-total",
        ),
        pytest.param(
            {"realtime_ws_max_connections": 0},
            "REALTIME_WS_MAX_CONNECTIONS must be at least 1",
            id="ws-cap-zero",
        ),
        pytest.param(
            {"realtime_ws_max_connections_per_user": 0},
            "REALTIME_WS_MAX_CONNECTIONS_PER_USER must be at least 1",
            id="ws-per-user-zero",
        ),
        pytest.param(
            {"realtime_ws_max_connections_per_user": 501},
            "REALTIME_WS_MAX_CONNECTIONS_PER_USER must not exceed REALTIME_WS_MAX_CONNECTIONS",
            id="ws-per-user-above-total",
        ),
        pytest.param(
            {"realtime_sse_max_streams_per_principal": 0},
            "REALTIME_SSE_MAX_STREAMS_PER_PRINCIPAL must be at least 1",
            id="sse-per-principal-zero",
        ),
        pytest.param(
            {"realtime_ws_max_connections_per_principal": 0},
            "REALTIME_WS_MAX_CONNECTIONS_PER_PRINCIPAL must be at least 1",
            id="ws-per-principal-zero",
        ),
        pytest.param(
            {"realtime_ws_frame_budget_bytes": 1024},
            "REALTIME_WS_FRAME_BUDGET_BYTES must be at least",
            id="ws-budget-under-one-frame",
        ),
        pytest.param(
            {"realtime_ws_max_connections": 501},
            "peak receive buffer, over REALTIME_WS_FRAME_BUDGET_BYTES",
            id="ws-cap-over-frame-budget",
        ),
        pytest.param(
            {
                "realtime_ws_frame_budget_bytes": 8 * REALTIME_WS_MAX_FRAME_BYTES,
                "realtime_ws_max_connections": 9,
                "realtime_ws_max_connections_per_user": 9,
            },
            "lower the cap to 8 or raise the budget",
            id="ws-small-pod-cap-over-its-own-budget",
        ),
        pytest.param(
            {"realtime_ws_ticket_ttl_seconds": 0},
            "REALTIME_WS_TICKET_TTL_SECONDS",
            id="ticket-zero",
        ),
        pytest.param(
            {"realtime_ws_ticket_ttl_seconds": 301},
            "REALTIME_WS_TICKET_TTL_SECONDS",
            id="ticket-too-long",
        ),
        pytest.param(
            {"realtime_presence_ttl_seconds": 30},
            "must exceed twice REALTIME_SSE_KEEPALIVE_SECONDS",
            id="presence-equals-two-keepalives",
        ),
        pytest.param(
            {"realtime_presence_ttl_seconds": 29},
            "must exceed twice REALTIME_SSE_KEEPALIVE_SECONDS",
            id="presence-below-two-keepalives",
        ),
        pytest.param(
            {"realtime_ephemeral_max_bytes": 0},
            "REALTIME_EPHEMERAL_MAX_BYTES",
            id="ephemeral-zero",
        ),
        pytest.param(
            {"realtime_ephemeral_max_bytes": 7901},
            "REALTIME_EPHEMERAL_MAX_BYTES",
            id="ephemeral-over-notify-limit",
        ),
        pytest.param(
            {"realtime_ephemeral_max_bytes": 8000},
            "REALTIME_EPHEMERAL_MAX_BYTES",
            id="ephemeral-at-postgres-cap",
        ),
        pytest.param(
            {"realtime_doc_max_bytes": 1023},
            "REALTIME_DOC_MAX_BYTES",
            id="doc-too-small",
        ),
        pytest.param({"realtime_crdt_workers": 0}, "REALTIME_CRDT_WORKERS", id="crdt-no-workers"),
        pytest.param(
            {"realtime_crdt_workers": 17}, "REALTIME_CRDT_WORKERS", id="crdt-too-many-workers"
        ),
        pytest.param(
            {"realtime_crdt_worker_memory_mb": 255},
            "REALTIME_CRDT_WORKER_MEMORY_MB",
            id="crdt-worker-memory-too-small",
        ),
        pytest.param(
            {"realtime_crdt_cache_docs": 0}, "REALTIME_CRDT_CACHE_DOCS", id="crdt-cache-no-docs"
        ),
        pytest.param(
            {"realtime_crdt_cache_bytes": 1024 * 1024 - 1},
            "REALTIME_CRDT_CACHE_BYTES must be at least",
            id="crdt-cache-too-small",
        ),
        pytest.param(
            {"realtime_crdt_worker_memory_mb": 256, "realtime_crdt_cache_bytes": 256 * 1024 * 1024},
            "REALTIME_CRDT_CACHE_BYTES must stay below",
            id="crdt-cache-fills-the-worker",
        ),
        pytest.param(
            {"realtime_crdt_validate_timeout_ms": 99},
            "REALTIME_CRDT_VALIDATE_TIMEOUT_MS",
            id="crdt-validate-too-short",
        ),
        pytest.param(
            {"realtime_crdt_validate_timeout_ms": 5000, "realtime_crdt_load_timeout_ms": 4999},
            "REALTIME_CRDT_LOAD_TIMEOUT_MS",
            id="crdt-load-shorter-than-validate",
        ),
        pytest.param(
            {"realtime_crdt_load_timeout_ms": 300_001},
            "REALTIME_CRDT_LOAD_TIMEOUT_MS",
            id="crdt-load-too-long",
        ),
    ],
)
def test_realtime_settings_refuse_an_inconsistent_value(
    overrides: dict[str, object], fragment: str
):
    with pytest.raises(ValidationError, match=fragment):
        Settings(_env_file=None, **overrides)  # type: ignore[call-arg]


@pytest.mark.parametrize(
    "name",
    [
        # Zero is not "no timeout" here: Postgres reads lock_timeout = 0 as
        # unbounded, a zero wait for the runner lock never waits, and a zero
        # batch walks nothing for ever.
        pytest.param("migration_lock_timeout_ms", id="lock-timeout"),
        pytest.param("migration_runner_wait_seconds", id="runner-wait"),
        pytest.param("migration_batch_rows", id="batch-rows"),
    ],
)
@pytest.mark.parametrize("value", [pytest.param(0, id="zero"), pytest.param(-1, id="negative")])
def test_migration_settings_refuse_zero_and_below(name: str, value: int):
    with pytest.raises(ValidationError, match=f"{name.upper()} must be at least 1"):
        Settings(_env_file=None, **{name: value})  # type: ignore[arg-type]
    assert getattr(Settings(_env_file=None, **{name: 1}), name) == 1  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"realtime_sse_max_stream_seconds": 16}, id="deadline-just-above-keepalive"),
        pytest.param(
            {"realtime_presence_ttl_seconds": 31}, id="presence-just-above-two-keepalives"
        ),
        pytest.param({"realtime_ephemeral_max_bytes": 7900}, id="ephemeral-at-the-limit"),
        pytest.param({"realtime_poll_interval_seconds": 0.05}, id="poll-fastest"),
        pytest.param({"realtime_sse_max_streams_per_user": 500}, id="sse-per-user-equals-total"),
        pytest.param(
            {"realtime_sse_keepalive_seconds": 5, "realtime_presence_ttl_seconds": 11},
            id="fast-keepalive-with-matching-presence",
        ),
        pytest.param({"realtime_listener_enabled": False}, id="listener-off"),
        pytest.param(
            {
                "realtime_crdt_workers": 1,
                "realtime_crdt_worker_memory_mb": 256,
                "realtime_crdt_cache_bytes": 1024 * 1024,
                "realtime_crdt_validate_timeout_ms": 100,
                "realtime_crdt_load_timeout_ms": 100,
            },
            id="crdt-smallest",
        ),
    ],
)
def test_realtime_settings_accept_a_consistent_override(overrides: dict[str, object]):
    s = Settings(_env_file=None, **overrides)  # type: ignore[call-arg]
    for name, value in overrides.items():
        assert getattr(s, name) == value


def test_realtime_settings_report_every_error_at_once():
    with pytest.raises(ValidationError) as excinfo:
        Settings(  # type: ignore[call-arg]
            _env_file=None,
            realtime_sse_keepalive_seconds=100,
            realtime_sse_max_stream_seconds=50,
            realtime_presence_ttl_seconds=45,
            realtime_ephemeral_max_bytes=9000,
        )
    message = str(excinfo.value)
    for fragment in (
        "REALTIME_SSE_MAX_STREAM_SECONDS must exceed",
        "REALTIME_PRESENCE_TTL_SECONDS must exceed twice",
        "REALTIME_EPHEMERAL_MAX_BYTES must be between",
    ):
        assert fragment in message


def test_realtime_settings_are_checked_in_production_too():
    with pytest.raises(ValueError, match="REALTIME_SSE_MAX_STREAM_SECONDS must exceed"):
        _prod_settings(realtime_sse_max_stream_seconds=1)


# --- Files (the object storage layer) -----------------------------------------


def _files_prod_settings(**overrides: object) -> Settings:
    """A production SaaS config with Files ON and every Files knob coherent.

    The default store is `aws` (SaaS): a separate content origin, a bucket, the
    vending role, and no static credentials (the task role signs). Tests break
    exactly one knob at a time.
    """
    base: dict[str, object] = {
        "self_hosted": False,
        "files_enabled": True,
        "files_store_provider": "aws",
        "files_store_bucket": "alkera-files-prod",
        "files_vend_role_arn": "arn:aws:iam::123456789012:role/alkera-files-vend",
        "files_content_base_url": "https://c.alkerausercontent.com",
        # Files on outside local means a real HMAC key: the validator refuses the
        # published dev fallback, so every Files-on case here carries one.
        "files_content_signing_key": "8Zq2p6Hn4vLxRt7wKcYs3BdFgJmNaQeUyTiOpXzV",
        "api_public_base_url": "https://api.alkera.example",
        "frontend_base_url": "https://app.alkera.example",
    }
    base.update(overrides)
    return _prod_settings(**base)


def test_files_ships_dark_with_a_filesystem_store():
    """Day-one defaults: nothing mounts, nothing is configured, nothing refuses."""
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.files_enabled is False
    assert s.files_store_provider == "filesystem"
    assert s.files_transfer_mode == "proxied"
    assert s.files_content_base_url is None
    assert s.files_content_host is None
    assert s.files_inline_max_bytes == 65536
    assert s.files_part_max_bytes == 128 * 1024**2
    assert s.files_single_put_max_bytes == 67108864
    # The published upload ceilings: 1000 GB for one file — decimal, the unit
    # the product states — and 10,000 parts a session, which is S3's own
    # multipart limit rather than a choice. Both are enforced at open, so the
    # default is a promise; and 1000 GB of 128 MiB parts is 7,451 of them, so
    # the promise is one the part count can keep.
    assert s.files_max_file_bytes == 1000 * 1000**3
    assert s.files_max_file_bytes == 1_000_000_000_000
    assert s.files_max_upload_parts == 10_000
    assert s.files_part_max_bytes * s.files_max_upload_parts >= s.files_max_file_bytes
    # 100 GB, in the units storage is sold in: what an operator reads back on
    # the admin page is the figure they set, not 7.4% more of it.
    assert s.files_quota_default_bytes == 100 * 1000**3
    assert s.files_quota_default_bytes == 100_000_000_000
    assert s.files_quota_default_nodes == 1_000_000
    # The cadence a holder is handed is the lower of this figure and a quarter
    # of the TTL, so on the defaults the figure is the live one: a holder beats
    # every 10s inside a 60s window.
    assert s.files_lease_heartbeat_seconds < s.files_lease_ttl_seconds / 4


def test_the_resident_upload_budget_admits_fifty_parts_by_default():
    """The shipped budget is fifty concurrent streaming parts at the measured
    per-part cost -- the arithmetic the constant documents, checked rather than
    restated: a budget that drifted from the constant would admit a number
    nobody chose."""
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert FILES_UPLOAD_PART_RESIDENT_BYTES == 24 * 1024**2
    assert s.files_upload_resident_budget_bytes == 50 * FILES_UPLOAD_PART_RESIDENT_BYTES
    assert s.files_upload_resident_budget_bytes == 1200 * 1024**2
    # Under thirty per cent of the 4 GiB SaaS task, single process.
    assert s.files_upload_resident_budget_bytes <= 4096 * 1024**2 * 0.30


@pytest.mark.parametrize(
    ("budget", "accepted"),
    [
        pytest.param(0, False, id="zero-sheds-every-part"),
        pytest.param(FILES_UPLOAD_PART_RESIDENT_BYTES - 1, False, id="one-byte-under-a-part"),
        pytest.param(FILES_UPLOAD_PART_RESIDENT_BYTES, True, id="exactly-one-part"),
        pytest.param(8 * FILES_UPLOAD_PART_RESIDENT_BYTES, True, id="a-512Mi-pod-worth"),
    ],
)
def test_production_refuses_a_resident_upload_budget_that_admits_no_part(
    budget: int, accepted: bool
):
    """The resident budget decides how many parts a process streams at once, so
    its VALUE is the surface: under one part's cost every part PUT is a 503
    before a byte is read -- an upload path that refuses everything, dressed as
    a limit. One part is the floor; a deployment sizes upward from there."""
    if accepted:
        prod = _prod_settings(files_upload_resident_budget_bytes=budget)
        assert prod.files_upload_resident_budget_bytes == budget
    else:
        with pytest.raises(ValueError, match="FILES_UPLOAD_RESIDENT_BUDGET_BYTES"):
            _prod_settings(files_upload_resident_budget_bytes=budget)


def test_a_part_size_that_cannot_carry_the_file_ceiling_refuses_to_load():
    """The published file ceiling must be the one an upload reaches first.

    A session is cut into `files_part_max_bytes` parts and refused above
    `files_max_upload_parts` of them, so a deployment whose parts cannot carry
    `files_max_file_bytes` publishes a number it will never honour: the caller
    reads the file limit, sends a file under it, and is refused for a part
    count no answer ever named. Set together, refused together.
    """
    with pytest.raises(ValidationError) as raised:
        Settings(
            _env_file=None,  # type: ignore[call-arg]
            files_part_max_bytes=32 * 1024**2,
            files_max_upload_parts=10_000,
            files_max_file_bytes=1_000_000_000_000,
        )
    message = str(raised.value)
    assert "FILES_PART_MAX_BYTES" in message
    assert "FILES_MAX_UPLOAD_PARTS" in message
    assert "FILES_MAX_FILE_BYTES" in message


@pytest.mark.parametrize(
    ("part_bytes", "parts", "file_bytes"),
    [
        pytest.param(128 * 1024**2, 10_000, 1_000_000_000_000, id="the-shipped-pair"),
        pytest.param(5 * 1024**3, 10_000, 1_000_000_000_000, id="parts-to-spare"),
        pytest.param(100, 10, 1000, id="exactly-reachable"),
    ],
)
def test_a_part_size_that_carries_the_file_ceiling_loads(part_bytes, parts, file_bytes):
    """Reachable is reachable, including exactly: the pair is refused only when
    the parts fall SHORT, never when they land on the ceiling."""
    s = Settings(
        _env_file=None,  # type: ignore[call-arg]
        files_part_max_bytes=part_bytes,
        files_max_upload_parts=parts,
        files_max_file_bytes=file_bytes,
    )
    assert s.files_max_file_bytes == file_bytes


@pytest.mark.parametrize(
    ("base_url", "expected"),
    [
        pytest.param("http://files.localhost:27220", "files.localhost:27220", id="local-with-port"),
        pytest.param("https://c.alkerausercontent.com", "c.alkerausercontent.com", id="https"),
        pytest.param("https://c.example.com:8443/", "c.example.com:8443", id="explicit-port"),
        pytest.param("https://C.EXAMPLE.COM", "c.example.com", id="lowercased"),
        pytest.param("  https://c.example.com  ", "c.example.com", id="padded"),
        pytest.param("files.example.com:9000", "files.example.com:9000", id="schemeless"),
        pytest.param("", None, id="empty"),
        pytest.param("   ", None, id="blank"),
        pytest.param(None, None, id="unset"),
    ],
)
def test_files_content_host_is_the_hostname_and_port(base_url: str | None, expected: str | None):
    """The content mount refuses any other Host, so the parse must survive a
    scheme, a trailing slash, padding, case and a missing scheme alike."""
    s = Settings(_env_file=None, files_content_base_url=base_url)  # type: ignore[call-arg]
    assert s.files_content_host == expected


@pytest.mark.parametrize(
    ("provider", "can_presign"),
    [
        pytest.param("filesystem", False, id="filesystem"),
        pytest.param("s3_compatible", True, id="s3-compatible"),
        pytest.param("aws", True, id="aws"),
    ],
)
def test_files_store_capabilities_hint_tracks_the_provider(provider: str, can_presign: bool):
    s = Settings(_env_file=None, files_store_provider=provider)  # type: ignore[call-arg]
    assert ("presigned_urls" in s.files_store_capabilities_hint) is can_presign
    # Content addressing needs a conditional write on every driver.
    assert "conditional_write" in s.files_store_capabilities_hint
    # Only real S3 vends session-tag-scoped credentials.
    assert ("scoped_credentials" in s.files_store_capabilities_hint) is (provider == "aws")


def test_files_store_root_defaults_under_the_project_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Never `Path.home()`: the root follows the deployment's project root, and
    an explicit setting wins.

    The home-independence is proved by MOVING home rather than by asserting the
    root is not a descendant of it: on Windows the scratch tree pytest hands out
    already lives under the invoking user's profile, so that shape would fail on
    a correct implementation.
    """
    project = tmp_path / "project"
    project.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    assert Path.home() == home

    s = Settings(_env_file=None, alkera_project_root=project)  # type: ignore[call-arg]
    assert s.files_store_root_path == project / ".alkera-files"
    explicit = Settings(  # type: ignore[call-arg]
        _env_file=None, alkera_project_root=project, files_store_root=tmp_path / "elsewhere"
    )
    assert explicit.files_store_root_path == tmp_path / "elsewhere"

    # The unset branch is the one that could reach for the user's home; it
    # follows the process's working project instead.
    monkeypatch.chdir(project)
    unset = Settings(_env_file=None)  # type: ignore[call-arg]
    assert unset.files_store_root_path.resolve() == (project / ".alkera-files").resolve()
    assert unset.files_store_root_path.resolve() != (home / ".alkera-files").resolve()


def test_files_content_signing_key_falls_back_only_when_unset():
    unset = Settings(_env_file=None, app_env="local")  # type: ignore[call-arg]
    assert "local-dev" in unset.effective_files_content_signing_key
    blank = Settings(_env_file=None, app_env="local", files_content_signing_key="")  # type: ignore[call-arg]
    assert blank.effective_files_content_signing_key == unset.effective_files_content_signing_key
    real = Settings(_env_file=None, files_content_signing_key="a-real-key")  # type: ignore[call-arg]
    assert real.effective_files_content_signing_key == "a-real-key"
    # The value never leaks through repr/str — it is a SecretStr.
    assert "a-real-key" not in repr(real.files_content_signing_key)


def test_files_disabled_never_reaches_the_files_validator():
    """A deployment that does not serve Files must boot with every Files knob at
    its (invalid-for-Files) default — the whole block is behind FILES_ENABLED."""
    s = _prod_settings(
        self_hosted=False,
        files_enabled=False,
        files_store_provider="filesystem",
        files_transfer_mode="direct",
        files_content_base_url=None,
        files_store_secret_key="orphan-secret",
    )
    assert s.files_enabled is False


def test_files_production_baseline_is_accepted():
    s = _files_prod_settings()
    assert s.files_content_host == "c.alkerausercontent.com"
    assert s.files_store_capabilities_hint >= {"presigned_urls", "scoped_credentials"}


@pytest.mark.parametrize("missing", [None, "", "   "], ids=["none", "empty", "blank"])
def test_files_production_refuses_no_content_host(missing: str | None):
    with pytest.raises(ValueError, match="FILES_CONTENT_BASE_URL must be the separate origin"):
        _files_prod_settings(files_content_base_url=missing)


@pytest.mark.parametrize(
    ("content_url", "clashing"),
    [
        pytest.param("https://api.alkera.example", "API_PUBLIC_BASE_URL", id="api-host"),
        pytest.param("https://app.alkera.example", "FRONTEND_BASE_URL", id="frontend-host"),
        pytest.param("https://API.alkera.example", "API_PUBLIC_BASE_URL", id="api-host-cased"),
        # Cookies ignore the port, so a different port is still the same jar.
        pytest.param("https://app.alkera.example:8443", "FRONTEND_BASE_URL", id="frontend-port"),
    ],
)
def test_files_production_refuses_a_content_host_shared_with_the_app(
    content_url: str, clashing: str
):
    with pytest.raises(ValueError, match="FILES_CONTENT_BASE_URL host") as excinfo:
        _files_prod_settings(files_content_base_url=content_url)
    assert clashing in str(excinfo.value)


def test_files_production_refuses_proxied_filesystem_on_saas():
    with pytest.raises(ValueError, match="FILES_TRANSFER_MODE=proxied with FILES_STORE_PROVIDER"):
        _files_prod_settings(
            files_store_provider="filesystem",
            files_transfer_mode="proxied",
            files_store_bucket=None,
            files_vend_role_arn=None,
        )


def test_files_production_allows_proxied_filesystem_self_hosted():
    """The single-volume install is the default self-hosted shape."""
    s = _files_prod_settings(
        self_hosted=True,
        files_store_provider="filesystem",
        files_transfer_mode="proxied",
        files_store_bucket=None,
        files_vend_role_arn=None,
        files_store_root=Path("/srv/alkera/files"),
    )
    assert s.is_self_hosted is True
    assert s.files_store_root_path == Path("/srv/alkera/files")


def test_files_production_refuses_direct_transfer_without_presigning():
    with pytest.raises(ValueError, match="FILES_TRANSFER_MODE=direct needs a store that can"):
        _files_prod_settings(
            self_hosted=True,
            files_store_provider="filesystem",
            files_transfer_mode="direct",
            files_store_bucket=None,
            files_vend_role_arn=None,
        )


@pytest.mark.parametrize(
    "provider", [pytest.param("s3_compatible", id="s3-compatible"), pytest.param("aws", id="aws")]
)
def test_files_production_accepts_direct_transfer_on_a_signing_store(provider: str):
    s = _files_prod_settings(
        files_store_provider=provider,
        files_transfer_mode="direct",
        files_store_endpoint="https://s3.us-east-1.amazonaws.com",
    )
    assert s.files_transfer_mode == "direct"


@pytest.mark.parametrize("missing", [None, ""], ids=["none", "empty"])
def test_files_production_refuses_aws_without_a_vend_role(missing: str | None):
    with pytest.raises(ValueError, match="FILES_VEND_ROLE_ARN must be set"):
        _files_prod_settings(files_vend_role_arn=missing)


@pytest.mark.parametrize(
    "provider", [pytest.param("s3_compatible", id="s3-compatible"), pytest.param("aws", id="aws")]
)
def test_files_production_refuses_an_object_store_without_a_bucket(provider: str):
    with pytest.raises(ValueError, match="FILES_STORE_BUCKET must be set"):
        _files_prod_settings(files_store_provider=provider, files_store_bucket=None)


@pytest.mark.parametrize(
    ("access_key", "secret_key"),
    [
        pytest.param("AKIAEXAMPLE", None, id="access-key-only"),
        pytest.param(None, "s3cr3t", id="secret-key-only"),
        pytest.param("AKIAEXAMPLE", "", id="blank-secret"),
        pytest.param("", "s3cr3t", id="blank-access-key"),
    ],
)
def test_files_production_refuses_half_a_store_credential(
    access_key: str | None, secret_key: str | None
):
    with pytest.raises(ValueError, match="FILES_STORE_ACCESS_KEY and FILES_STORE_SECRET_KEY"):
        _files_prod_settings(files_store_access_key=access_key, files_store_secret_key=secret_key)


@pytest.mark.parametrize(
    ("access_key", "secret_key"),
    [
        pytest.param(None, None, id="task-role"),
        pytest.param("AKIAEXAMPLE", "s3cr3t", id="both-set"),
    ],
)
def test_files_production_accepts_both_or_neither_store_credentials(
    access_key: str | None, secret_key: str | None
):
    s = _files_prod_settings(files_store_access_key=access_key, files_store_secret_key=secret_key)
    assert s.files_store_access_key == access_key


@pytest.mark.parametrize(
    "root",
    [
        pytest.param("/tmp", id="tmp-itself"),
        pytest.param("/tmp/alkera-files", id="under-tmp"),
        pytest.param("/var/tmp/alkera/files", id="under-var-tmp"),
        pytest.param("/private/tmp/alkera-files", id="under-macos-tmp"),
        pytest.param("/srv/alkera/../../tmp/files", id="traversal-into-tmp"),
    ],
)
def test_files_production_refuses_an_ephemeral_store_root(root: str):
    with pytest.raises(ValueError, match="under an OS scratch directory"):
        _files_prod_settings(files_store_root=Path(root))


@pytest.mark.parametrize(
    "root",
    [
        pytest.param("/srv/alkera/files", id="srv"),
        pytest.param("/mnt/data/tmpfiles", id="tmp-lookalike"),
        pytest.param("/var/lib/alkera/files", id="var-lib"),
    ],
)
def test_files_production_accepts_a_durable_store_root(root: str):
    s = _files_prod_settings(files_store_root=Path(root))
    assert s.files_store_root_path == Path(root)


def test_files_local_dev_content_host_needs_no_production_shape():
    """`files.localhost:<port>` is the local content origin: a distinct hostname
    (cookies ignore ports) on a filesystem store, and the validator never fires
    because the env is not production."""
    s = Settings(  # type: ignore[call-arg]
        _env_file=None,
        app_env="local",
        files_enabled=True,
        files_store_provider="filesystem",
        files_transfer_mode="proxied",
        files_content_base_url="http://files.localhost:27220",
        api_public_base_url="http://localhost:27220",
        frontend_base_url="http://localhost:27221",
    )
    assert s.files_content_host == "files.localhost:27220"
    assert s.files_content_host != "localhost:27220"


def test_files_production_reports_every_refusal_at_once():
    """One boot, one message: an operator fixes the whole block in one pass."""
    with pytest.raises(ValidationError) as excinfo:
        _files_prod_settings(
            files_store_provider="aws",
            files_transfer_mode="proxied",
            files_content_base_url=None,
            files_store_bucket=None,
            files_vend_role_arn=None,
            files_store_access_key="AKIAEXAMPLE",
            files_store_root=Path("/tmp/alkera-files"),
        )
    message = str(excinfo.value)
    for fragment in (
        "FILES_CONTENT_BASE_URL must be the separate origin",
        "FILES_STORE_BUCKET must be set",
        "FILES_VEND_ROLE_ARN must be set",
        "FILES_STORE_ACCESS_KEY and FILES_STORE_SECRET_KEY",
        "under an OS scratch directory",
    ):
        assert fragment in message


# ---------------------------------------------------------------------------
# FILES_INLINE_OPERATIONS — where a queued Files operation actually runs.
# ---------------------------------------------------------------------------


def test_files_inline_operations_is_off_by_default():
    """The default hands queued work to the worker, which is what SaaS runs."""
    assert Settings(_env_file=None).files_inline_operations is False  # type: ignore[call-arg]


def test_production_refuses_inline_operations_on_saas():
    """A replica must not run a 40,000-node copy inside the request that asked."""
    with pytest.raises(ValueError, match="FILES_INLINE_OPERATIONS must be 'false' on SaaS"):
        _files_prod_settings(files_inline_operations=True)


def test_production_allows_inline_operations_on_a_self_hosted_install():
    """A single-process install has no Temporal worker to hand the work to."""
    s = _files_prod_settings(
        self_hosted=True,
        files_inline_operations=True,
        files_store_provider="filesystem",
        files_transfer_mode="proxied",
        files_store_bucket=None,
        files_vend_role_arn=None,
        files_store_root=Path("/srv/alkera/files"),
    )
    assert s.files_inline_operations is True


def test_production_refuses_inline_operations_even_with_files_dark():
    """The flag names WHERE work runs, not what Files serves, so it is refused
    ahead of the FILES_ENABLED gate: a SaaS replica with the flag set would run
    inline the moment someone turned Files on."""
    with pytest.raises(ValueError, match="FILES_INLINE_OPERATIONS must be 'false' on SaaS"):
        _prod_settings(self_hosted=False, files_enabled=False, files_inline_operations=True)


# A content URL is a bearer capability: the HMAC over it is the only check
# between a forged URL and the bytes, and an unset key falls back to a constant
# published in this repository.
_A_REAL_FILES_SIGNING_KEY = "8Zq2p6Hn4vLxRt7wKcYs3BdFgJmNaQeUyTiOpXzV"  # 40 bytes


@pytest.mark.parametrize("missing", [None, "", "   "], ids=["unset", "empty", "whitespace"])
def test_files_production_refuses_an_unset_content_signing_key(missing: str | None):
    """Booting production with Files on and no key would sign every content URL
    with the repo's dev constant — refuse instead of falling back."""
    with pytest.raises(ValidationError, match="FILES_CONTENT_SIGNING_KEY must be set") as excinfo:
        _files_prod_settings(files_content_signing_key=missing)
    assert "published in the Alkera repository" in str(excinfo.value)


def test_files_staging_refuses_an_unset_content_signing_key():
    """Staging is internet-facing too, so the refusal is 'not local', not
    'production only' — it fires before the staging validator's own checks."""
    with pytest.raises(ValidationError, match="FILES_CONTENT_SIGNING_KEY must be set"):
        Settings(  # type: ignore[call-arg]
            _env_file=None,
            app_env="staging",
            auth_jwt_secret="real-staging-secret",
            token_hash_pepper="real-staging-pepper",
            files_enabled=True,
        )


@pytest.mark.parametrize(
    ("key", "refused"),
    [
        pytest.param("a" * 31, True, id="31-bytes"),
        pytest.param("a" * 32, False, id="32-bytes"),
        pytest.param("é" * 15, True, id="15-two-byte-chars-is-30-bytes"),
        pytest.param("é" * 16, False, id="16-two-byte-chars-is-32-bytes"),
        pytest.param(_A_REAL_FILES_SIGNING_KEY, False, id="40-bytes"),
    ],
)
def test_files_production_refuses_a_content_signing_key_under_32_bytes(key: str, refused: bool):
    """The floor is BYTES of key material, not characters — a 16-character
    two-byte-per-char key is 32 bytes and passes; 31 bytes does not."""
    if refused:
        with pytest.raises(ValidationError, match="must be at least 32 bytes") as excinfo:
            _files_prod_settings(files_content_signing_key=key)
        assert f"(got {len(key.encode())})" in str(excinfo.value)
    else:
        s = _files_prod_settings(files_content_signing_key=key)
        assert s.effective_files_content_signing_key == key


def test_files_production_without_files_never_asks_for_a_signing_key():
    """The refusal is behind FILES_ENABLED: a deployment that serves no Files
    boots with no key, and never signs anything."""
    s = _prod_settings(files_enabled=False)
    assert s.files_content_signing_key is None


def test_files_local_keeps_the_dev_fallback_without_a_key():
    """Local dev still boots keyless and still gets the published constant —
    that leniency is exactly what the non-local refusal above fences off."""
    s = Settings(  # type: ignore[call-arg]
        _env_file=None,
        app_env="local",
        files_enabled=True,
        files_store_provider="filesystem",
        files_content_base_url="http://files.localhost:27220",
    )
    assert s.files_content_signing_key is None
    assert s.effective_files_content_signing_key == (
        "alkera-local-dev-files-content-key-do-not-use-in-prod"
    )


@pytest.mark.parametrize(
    ("provider", "driver"),
    [
        pytest.param("filesystem", "filesystem", id="filesystem"),
        pytest.param("s3_compatible", "s3", id="s3-compatible"),
        pytest.param("aws", "s3", id="aws"),
    ],
)
def test_files_store_driver_is_the_persisted_spelling(provider: str, driver: str):
    """The provider a deployment configures is not the value the store table
    stores: `aws` and `s3_compatible` are both addressed as the `s3` driver,
    and every one of them is a member of the model's catalogue."""
    s = Settings(_env_file=None, files_store_provider=provider)  # type: ignore[call-arg]
    assert s.files_store_driver == driver
    assert s.files_store_driver in STORE_DRIVERS


def test_every_provider_maps_onto_the_model_catalogue():
    """The mapping is total in both directions that matter: no provider spells a
    driver the CHECK constraint would refuse, and none is left unmapped."""
    assert set(FILES_STORE_DRIVERS.values()) <= set(STORE_DRIVERS)
    for provider in ("filesystem", "s3_compatible", "aws"):
        assert provider in FILES_STORE_DRIVERS


def test_a_provider_outside_the_catalogue_refuses_at_boot():
    """A store row is written mid-request, on the first drive of the first org,
    so an unknown provider has to be refused before the process serves."""
    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None, files_store_provider="gcs")  # type: ignore[call-arg]

    assert "files_store_provider" in str(excinfo.value)


def test_a_mapped_driver_outside_the_catalogue_refuses_at_boot(monkeypatch: pytest.MonkeyPatch):
    """And the guard is the mapping, not just the literal: drop the row for a
    provider the settings still admit and boot fails with the fix in the
    message, rather than the first drive request failing with a 500."""
    monkeypatch.delitem(FILES_STORE_DRIVERS, "s3_compatible")

    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None, files_store_provider="s3_compatible")  # type: ignore[call-arg]

    assert "FILES_STORE_DRIVERS" in str(excinfo.value)


@pytest.mark.parametrize("budget", [0, -1])
def test_a_janitor_org_budget_below_one_refuses_at_boot(budget: int):
    """A pass that sweeps no org returns the cursor it was handed, so the
    workflow's page loop would spin and the fleet would never be swept — and
    no test of a single pass would notice."""
    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None, files_janitor_org_budget=budget)  # type: ignore[call-arg]
    assert "FILES_JANITOR_ORG_BUDGET must be >= 1" in str(excinfo.value)


@pytest.mark.parametrize("budget", [0, -1])
def test_a_queued_recovery_budget_below_one_refuses_at_boot(budget: int):
    """A net that catches nothing does it silently: every tick would find no
    abandoned row, report a clean pass, and a stranded bulk trash would look
    exactly as it did before the recovery existed."""
    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None, files_queued_recovery_budget=budget)  # type: ignore[call-arg]
    assert "FILES_QUEUED_RECOVERY_BUDGET must be >= 1" in str(excinfo.value)


@pytest.mark.parametrize("attempts", [0, -1])
def test_a_queued_recovery_allowance_below_one_refuses_at_boot(attempts: int):
    """Zero offers is the opposite failure: every abandoned operation failed on
    the first look, before a runner was ever given the chance to take it."""
    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None, files_queued_recovery_attempts=attempts)  # type: ignore[call-arg]
    assert "FILES_QUEUED_RECOVERY_ATTEMPTS must be >= 1" in str(excinfo.value)


def test_the_queued_recovery_budget_is_one_bound_on_the_whole_tick():
    """One number is the tick's size, not a factor of it: a per-tenant cap read
    together with an org cap is a pass whose real size is their product."""
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.files_queued_recovery_budget == 200
    assert s.files_queued_recovery_attempts == 3


def test_the_janitor_sweeps_a_bounded_page_of_orgs_by_default():
    """The default is a page, not the fleet: an install's tenant count must not
    decide how long one janitor activity runs."""
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.files_janitor_org_budget == 50


@pytest.mark.parametrize("fraction", [-0.01, 1.01, 2.0])
def test_a_gc_breaker_threshold_outside_the_unit_interval_refuses_at_boot(fraction: float):
    """The breaker compares a share of a page's domains; a threshold that is not
    a share would either trip on nothing or never trip."""
    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None, files_gc_max_orphan_fraction=fraction)  # type: ignore[call-arg]
    assert "FILES_GC_MAX_ORPHAN_FRACTION must be between 0 and 1" in str(excinfo.value)


def test_a_blank_gc_flag_is_unset_not_a_boot_failure(monkeypatch: pytest.MonkeyPatch):
    """`FILES_GC_ENABLED=` in a seeded `.env` means "decide from APP_ENV"; a
    boolean parse error there would stop every service from starting."""
    monkeypatch.setenv("FILES_GC_ENABLED", "")
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.files_gc_enabled is None
    monkeypatch.setenv("FILES_GC_ENABLED", "false")
    assert Settings(_env_file=None).files_gc_enabled is False  # type: ignore[call-arg]


def _example_pairs() -> dict[str, str]:
    example = Path(__file__).parents[3] / ".env.example"
    pairs: dict[str, str] = {}
    for line in example.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        pairs[key.strip()] = value.strip()
    return pairs


def test_env_example_never_leaves_a_boolean_blank():
    """`make bootstrap` copies `.env.example` to `.env` verbatim, so a blank
    boolean there is a `bool_parsing` error on every service's first boot.
    Every bool-typed setting the example names must carry a value."""
    blank_bools = []
    for key, value in _example_pairs().items():
        field = Settings.model_fields.get(key.lower())
        if field is None or value:
            continue
        if "bool" in str(field.annotation):
            blank_bools.append(key)
    assert blank_bools == []


def test_a_settings_object_boots_from_a_copy_of_env_example(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The fresh-clone path itself: `.env` is `.env.example`, byte for byte,
    and every service has to construct `Settings` from it."""
    copied = tmp_path / ".env"
    copied.write_bytes((Path(__file__).parents[3] / ".env.example").read_bytes())
    for key in _example_pairs():
        monkeypatch.delenv(key, raising=False)
    s = Settings(_env_file=copied)  # type: ignore[call-arg]
    assert s.files_gc_enabled is False


@pytest.mark.parametrize("fraction", [0.0, 0.1, 1.0])
def test_a_gc_breaker_threshold_on_the_unit_interval_is_accepted(fraction: float):
    s = Settings(_env_file=None, files_gc_max_orphan_fraction=fraction)  # type: ignore[call-arg]
    assert s.files_gc_max_orphan_fraction == fraction


@pytest.mark.parametrize(
    ("app_env", "files_enabled", "gc_enabled", "active"),
    [
        pytest.param("local", True, None, True, id="unset-is-on-locally"),
        pytest.param("staging", True, None, False, id="unset-is-off-on-staging"),
        pytest.param("production", True, None, False, id="unset-is-off-in-production"),
        pytest.param("production", True, True, True, id="an-operator-turns-it-on"),
        pytest.param("local", True, False, False, id="an-operator-turns-it-off-locally"),
        pytest.param("local", False, True, False, id="never-without-files"),
    ],
)
def test_the_gc_pass_is_opt_in_outside_a_developers_machine(
    app_env: str, files_enabled: bool, gc_enabled: bool | None, active: bool
):
    """The collector erases bytes on the strength of what one database can see,
    so nothing but an operator's explicit setting turns it on in a deployment."""
    # ``model_copy`` sidesteps the production boot validator, which is about the
    # rest of the deployment; the property under test reads these three alone.
    s = Settings(_env_file=None).model_copy(  # type: ignore[call-arg]
        update={"app_env": app_env, "files_enabled": files_enabled, "files_gc_enabled": gc_enabled}
    )
    assert s.files_gc_active is active


# --- The per-worktree gateway URL ------------------------------------------
#
# `ops/scripts/workspace-env.sh` is the one place a worktree's ports are
# decided. The backend reaches the gateway through `gateway_base_url`, so the
# script has to emit THAT key: emitting only the CLI's `ALKERA_GATEWAY_URL`
# left the backend dialing the `main` default :8081 on every other worktree,
# which is a connection-refused on the chat-model catalog hop and an empty
# model picker in the browser.

REPO_ROOT = Path(__file__).resolve().parents[3]


#: `ops/scripts/workspace-env.sh` is a POSIX shell script, and the tests below run
#: the real one. The Windows runner's `bash` is the WSL launcher (every call
#: answers with an install prompt) and there is no `env -i`; a worktree's ports
#: are only ever allocated from a developer's Mac or a Linux runner.
_posix_generator_only = pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX shell tooling: ops/scripts/workspace-env.sh runs on macOS/Linux only",
)

#: The port window these tests hand the real generator. Two ranges have to be
#: cleared, and the second one is what made this suite flaky on the shared Linux
#: runner: the window used to be three blocks at 41500, which sits inside the
#: kernel's ephemeral range (`net.ipv4.ip_local_port_range`, 32768-60999 by
#: default). Any unrelated outbound socket on the host therefore landed on one of
#: those sixty ports, `lsof` called the block busy, and with all three blocks busy
#: and no reclaimable sibling the generator exits 1 — which surfaced as the
#: helper's CalledProcessError, nothing to do with the setting under test.
#: Below 32768 neither Linux nor macOS hands a port out ephemerally, and
#: 14000-15279 is above the well-known ports and clear of the real fleet's
#: 20000-38999; the slot count is generous so a stray *listener* inside the
#: window only makes the picker advance to the next block.
_PROBE_PORT_ENV = {
    "WORKSPACE_ENV_PORT_BASE_MIN": "14000",
    "WORKSPACE_ENV_PORT_BLOCK_SLOTS": "64",
    "WORKSPACE_ENV_PORT_BLOCK_STRIDE": "20",
}


def _generate_workspace_env(root: Path) -> dict[str, str]:
    """Run the real script against a throwaway tree and read back what it wrote."""
    # `check=True` would raise a CalledProcessError carrying only the exit code,
    # and the script says WHY it gave up on stderr — the CI log for this helper's
    # one failure named the command and nothing else. Assert instead, so the
    # reason travels with the failure.
    done = subprocess.run(
        ["bash", str(REPO_ROOT / "ops" / "scripts" / "workspace-env.sh"), "generate"],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            # Otherwise the slug is forced to main's classic ports: by CI, and
            # by the checkout's own branch when the gate runs on `main`.
            "CI": "",
            "WORKSPACE_ENV_BRANCH": "throwaway/settings-probe",
            "WORKSPACE_ENV_ROOT": str(root),
            **_PROBE_PORT_ENV,
        },
    )
    assert done.returncode == 0, f"rc={done.returncode} stderr={done.stderr!r}"
    written: dict[str, str] = {}
    for line in (root / ".env.workspace").read_text().splitlines():
        if line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        written[key] = value
    return written


@_posix_generator_only
def test_the_workspace_env_emits_the_gateway_url_the_backend_reads(tmp_path: Path):
    """Both gateway URLs derive from the one allocated port, so they cannot split."""
    written = _generate_workspace_env(tmp_path)
    port = written["GATEWAY_PORT"]
    assert written["GATEWAY_BASE_URL"] == f"http://localhost:{port}"
    assert written["ALKERA_GATEWAY_URL"] == written["GATEWAY_BASE_URL"]


@_posix_generator_only
def test_settings_reach_this_worktrees_gateway_not_mains(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The far end of the same wire: load what the script wrote and the backend
    is pointed at THIS worktree's gateway. Before the script emitted the key the
    settings stayed on :8081 — a port nothing serves off `main`."""
    written = _generate_workspace_env(tmp_path)
    port = written["GATEWAY_PORT"]
    assert port != "8081", "the throwaway tree should have been allocated its own block"
    for key, value in written.items():
        monkeypatch.setenv(key, value)

    assert Settings(_env_file=None).gateway_base_url == f"http://localhost:{port}"  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# The seal itself — the module's hermeticity is a mechanism, so it is tested.
# ---------------------------------------------------------------------------


def test_the_seal_hides_a_setting_the_ambient_process_exported() -> None:
    """A shell that exported the dev config must not reach `_env_file=None`.

    The first assertion is the leak: with the var set, the "default" a hermetic
    case would assert is whatever the environment says. The seal is what makes
    the second assertion true.
    """
    with pytest.MonkeyPatch.context() as ambient:
        ambient.setenv("FILES_INLINE_OPERATIONS", "true")
        assert Settings(_env_file=None).files_inline_operations is True  # type: ignore[call-arg]

        seal_settings_env(ambient)
        assert Settings(_env_file=None).files_inline_operations is False  # type: ignore[call-arg]


def test_the_seal_covers_every_setting_because_it_walks_the_model() -> None:
    """A hand-kept list falls behind the day a setting is added — and did.

    `FILES_INLINE_OPERATIONS` shipped without being added to it, and an
    environment exporting it turned all fourteen "production baseline is
    accepted" cases in this module into refusals about inline operations.
    """
    with pytest.MonkeyPatch.context() as ambient:
        for field in Settings.model_fields:
            ambient.setenv(field.upper(), "ambient")

        seal_settings_env(ambient)

        assert [f for f in Settings.model_fields if f.upper() in os.environ] == []


# --- The generated per-worktree Files block (ops/scripts/workspace-env.sh) ---

_REPO_ROOT = Path(__file__).resolve().parents[3]
_WORKSPACE_ENV = _REPO_ROOT / "ops" / "scripts" / "workspace-env.sh"


def _generated_workspace_env(tmp_path: Path, branch: str) -> dict[str, str]:
    """Run the real generator into a throwaway root and read back what it wrote.

    The values are parsed from the script's own output rather than restated here,
    so a change to the generated block that the app would refuse fails this test
    instead of being discovered by the next developer's `make dev-all`.
    """
    subprocess.run(
        ["bash", str(_WORKSPACE_ENV), "generate"],
        check=True,
        cwd=_REPO_ROOT,
        env={
            **os.environ,
            "CI": "",
            "WORKSPACE_ENV_ROOT": str(tmp_path),
            "WORKSPACE_ENV_BRANCH": branch,
        },
    )
    written: dict[str, str] = {}
    for line in (tmp_path / ".env.workspace").read_text().splitlines():
        if line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        written[key] = value
    return written


def _settings_from_generated(written: dict[str, str], **overrides: object) -> Settings:
    """Settings built from the generated block, one key per setting."""
    kwargs: dict[str, object] = {
        "files_enabled": written["FILES_ENABLED"],
        "files_store_provider": written["FILES_STORE_PROVIDER"],
        "files_store_endpoint": written["FILES_STORE_ENDPOINT"],
        "files_store_bucket": written["FILES_STORE_BUCKET"],
        "files_store_region": written["FILES_STORE_REGION"],
        "files_store_access_key": written["FILES_STORE_ACCESS_KEY"],
        "files_store_secret_key": written["FILES_STORE_SECRET_KEY"],
        "files_inline_operations": written["FILES_INLINE_OPERATIONS"],
        "files_content_base_url": written["FILES_CONTENT_BASE_URL"],
        "api_public_base_url": written["API_PUBLIC_BASE_URL"],
        "frontend_base_url": written["FRONTEND_BASE_URL"],
    }
    kwargs.update(overrides)
    return Settings(_env_file=None, **kwargs)  # type: ignore[arg-type,call-arg]


def _prod_base_for_generated_files() -> dict[str, object]:
    """Everything a production boot needs BESIDES the Files store block, so the
    only refusals left are the ones the store block is responsible for."""
    return {
        "app_env": "production",
        "self_hosted": False,
        "auth_jwt_secret": "real-prod-secret-not-the-fallback",
        "token_hash_pepper": "real-prod-token-pepper-at-least-32-bytes",
        "auth_cookie_secure": True,
        "smtp_host": "smtp.example.com",
        "smtp_username": "smtp-user",
        "smtp_password": "smtp-pass",
        "database_url": "postgresql+asyncpg://prod:prod@db.internal:5432/alkera",
        "api_cors_origins": "https://app.alkera.example",
        "oauth_mock_enabled": False,
        "turnstile_secret_key": "real-prod-turnstile-secret",
        "temporal_address": "temporal.example.internal:7233",
        "api_public_base_url": "https://api.alkera.example",
        "frontend_base_url": "https://app.alkera.example",
        "files_content_base_url": "https://c.alkerausercontent.com",
        "files_content_signing_key": "8Zq2p6Hn4vLxRt7wKcYs3BdFgJmNaQeUyTiOpXzV",
    }


@_posix_generator_only
def test_the_generated_workspace_block_is_a_store_the_app_accepts(tmp_path: Path) -> None:
    """A fresh worktree must reach a working Files store with no `.env.local`.

    Every value comes from the generator, so this fails if the block stops being
    coherent: a provider the driver catalogue does not admit, a bucket the S3 API
    would refuse, or a content origin sharing the SPA's hostname — which would put
    the session cookie on user-controlled bytes.
    """
    written = _generated_workspace_env(tmp_path, "robin/Files-Lane")
    s = _settings_from_generated(written, app_env="local")

    assert s.files_enabled is True
    assert s.files_store_provider == "s3_compatible"
    assert s.files_store_driver in STORE_DRIVERS
    assert FILES_STORE_DRIVERS[s.files_store_provider] == s.files_store_driver
    # The store is this worktree's SeaweedFS and the content origin its own API
    # port — the two numbers a developer would otherwise copy by hand.
    assert s.files_store_endpoint == f"http://localhost:{written['SEAWEEDFS_S3_PORT']}"
    assert s.files_content_host == f"files.localhost:{written['API_PORT']}"
    assert s.files_content_host not in (
        f"localhost:{written['API_PORT']}",
        f"localhost:{written['WEB_PORT']}",
    )
    # DNS-shaped, per-worktree, and inside S3's length bound.
    assert s.files_store_bucket is not None
    assert s.files_store_bucket != "alkera-files"
    assert 3 <= len(s.files_store_bucket) <= 63
    assert s.files_store_bucket == s.files_store_bucket.lower()
    assert "_" not in s.files_store_bucket


@_posix_generator_only
def test_production_refuses_the_generated_local_files_block(tmp_path: Path) -> None:
    """The same generated block, booted as production, is refused by name.

    Its credentials are committed in the repo, so an env inherited from a
    developer's machine must never reach a real bucket — and the inline runner
    that exists because local has no worker must not run a tenant's copy inside
    a SaaS request.
    """
    written = _generated_workspace_env(tmp_path, "robin/Files-Lane")
    with pytest.raises(ValidationError) as excinfo:
        _settings_from_generated(written, **_prod_base_for_generated_files())
    message = str(excinfo.value)
    assert "local-dev SeaweedFS credentials" in message
    assert "FILES_INLINE_OPERATIONS must be 'false' on SaaS" in message


@_posix_generator_only
def test_production_accepts_the_same_shape_with_its_own_credentials(tmp_path: Path) -> None:
    """The refusal is about the committed dev pair, not about static credentials.

    A validator that refused every static key would pass the test above while
    breaking every self-hosted install pointed at a real bucket, so the accepting
    case is pinned beside the refusing one.
    """
    written = _generated_workspace_env(tmp_path, "robin/Files-Lane")
    s = _settings_from_generated(
        written,
        **_prod_base_for_generated_files(),
        files_store_access_key="AKIAREALKEYEXAMPLE",
        files_store_secret_key="a-real-secret-from-the-secret-store",
        files_inline_operations=False,
    )
    assert s.files_store_provider == "s3_compatible"
    assert s.files_store_bucket == written["FILES_STORE_BUCKET"]


# --- The generator itself must survive a Linux runner's environment ---
#
# The port picker scans SIBLING worktrees for their reserved blocks, and reads each
# sibling's mtime through `stat`. `stat` speaks two incompatible dialects: BSD wants
# `-f %m`, GNU wants `-c %Y`. Probing BSD-first on GNU does not merely fail over —
# GNU's `-f` is --file-system and prints a filesystem report (`ID: …`) to stdout
# before erroring, so the "mtime" captured was that report, and the very next
# arithmetic evaluated the word `ID` as a shell variable. Under `set -u` that exits 1
# and every caller of the generator dies with it. Both tests below run the real
# script; the first pins the dialect handling, the second the bare-environment shape.


def _gnu_stat_shim(bin_dir: Path) -> None:
    """A `stat` that behaves like GNU coreutils' on the PATH.

    Mimics the two behaviours that broke the runner: `-c %Y` works, and `-f` is
    --file-system, which takes no format, so it reports on the files it was handed
    (emitting an `ID:` line to STDOUT) and then fails on the bogus `%m` operand.
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    shim = bin_dir / "stat"
    shim.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "-c" ]; then\n'
        "  shift 2\n"
        '  exec python3 -c "import os,sys;print(int(os.stat(sys.argv[1]).st_mtime))" "$1"\n'
        "fi\n"
        'if [ "$1" = "-f" ]; then\n'
        "  shift\n"
        "  rc=0\n"
        '  for arg in "$@"; do\n'
        '    if [ -e "$arg" ]; then\n'
        '      printf \'  File: \\"%s\\"\\n    ID: 2bf2a0a3ee5d3ba6 Namelen: 255\\n\' "$arg"\n'
        "    else\n"
        '      echo "stat: cannot read file system information for $arg" >&2\n'
        "      rc=1\n"
        "    fi\n"
        "  done\n"
        "  exit $rc\n"
        "fi\n"
        "exit 2\n"
    )
    shim.chmod(0o755)


def _run_generator(root: Path, *, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(_WORKSPACE_ENV), "generate"],
        cwd=_REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )


def _api_port(root: Path) -> str:
    for line in (root / ".env.workspace").read_text().splitlines():
        if line.startswith("API_PORT="):
            return line.partition("=")[2]
    raise AssertionError("the generator wrote no API_PORT")


def _lsof_tripwire(bin_dir: Path, calls: Path) -> None:
    """An `lsof` on the PATH that records every call it gets.

    The picker's own seam answers for every port, so nothing here may reach lsof
    at all: a real lsof is one process per probed port on a box the run shares,
    and its answer is whatever the host has bound. The shim reports "nothing
    listening" so the walk goes on, and the record it leaves is the failure.
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    shim = bin_dir / "lsof"
    shim.write_text(f'#!/bin/sh\nprintf \'%s\\n\' "$*" >> "{calls}"\nexit 1\n')
    shim.chmod(0o755)


def _next_block(base: int) -> int:
    """The block the picker steps to when ``base`` is taken: one stride up,
    wrapping to the window's start past its last slot."""
    base_min = int(_PROBE_PORT_ENV["WORKSPACE_ENV_PORT_BASE_MIN"])
    slots = int(_PROBE_PORT_ENV["WORKSPACE_ENV_PORT_BLOCK_SLOTS"])
    stride = int(_PROBE_PORT_ENV["WORKSPACE_ENV_PORT_BLOCK_STRIDE"])
    nxt = base + stride
    return nxt if nxt <= base_min + (slots - 1) * stride else base_min


@_posix_generator_only
@pytest.mark.parametrize(
    "gnu_stat",
    [pytest.param(True, id="gnu-stat"), pytest.param(False, id="native-stat")],
)
def test_the_generator_allocates_around_a_sibling_in_either_stat_dialect(
    tmp_path: Path, gnu_stat: bool
) -> None:
    """A worktree whose slug lands on a sibling's block is moved one stride past
    it, on GNU stat as on BSD.

    Both roots carry the same branch — a lane re-created beside the directory
    its predecessor left behind — so both hash to the same starting block, and
    the sibling's recorded block is the only thing between the second run and
    a collision. The second run is the one that matters: only then is there a
    sibling to scan, so only then does the mtime probe run and its output reach
    the picker's arithmetic. With the BSD-first probe and a GNU `stat`, this
    run exits 1 with `ID: unbound variable` instead of allocating.

    Every port is declared free through the picker's own seam, so what the walk
    steps over is the sibling and nothing else. Probing real ports made the
    answer the host's: on a shared runner the ephemeral range is full of other
    workers' sockets, so the window filled, the walk exhausted it, and the
    least-recently-used reclaim handed the sibling's own block back — the
    collision this test exists to refuse, reported as a failure of the claim.
    """
    fleet = tmp_path / "fleet"
    first, second = fleet / "lane-one", fleet / "lane-two"
    first.mkdir(parents=True)
    second.mkdir(parents=True)

    bin_dir = tmp_path / "bin"
    lsof_calls = tmp_path / "lsof-calls"
    _lsof_tripwire(bin_dir, lsof_calls)
    if gnu_stat:
        _gnu_stat_shim(bin_dir)
    env = {
        **os.environ,
        "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
        "CI": "",
        "WORKSPACE_ENV_BRANCH": "robin/Lane-One",
        # Set and empty: no port is busy, and lsof is not asked.
        "WORKSPACE_ENV_BUSY_PORTS": "",
        **_PROBE_PORT_ENV,
    }

    ports: list[int] = []
    for root in (first, second):
        done = _run_generator(root, env={**env, "WORKSPACE_ENV_ROOT": str(root)})
        assert done.returncode == 0, f"{root.name}: rc={done.returncode} stderr={done.stderr!r}"
        assert "unbound variable" not in done.stderr
        ports.append(int(_api_port(root)))

    assert ports[1] == _next_block(ports[0]), (
        "a sibling's block must be treated as reserved: the walk steps one stride past it"
    )
    assert not lsof_calls.exists(), (
        f"the walk consulted lsof despite the seam: {lsof_calls.read_text()!r}"
    )


@_posix_generator_only
def test_the_generator_runs_with_nothing_but_a_path(tmp_path: Path) -> None:
    """The runner hands the script a bare environment; it must still allocate.

    Pins that nothing the generator reads is smuggled in from an interactive
    shell — no HOME, no LANG, no git config, no inherited WORKSPACE_ENV_* seam.
    """
    root = tmp_path / "solo"
    root.mkdir()
    done = subprocess.run(
        [
            "env",
            "-i",
            f"PATH={os.environ['PATH']}",
            f"HOME={tmp_path}",
            f"WORKSPACE_ENV_ROOT={root}",
            "WORKSPACE_ENV_BRANCH=robin/Bare-Env",
            "bash",
            str(_WORKSPACE_ENV),
            "generate",
        ],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert done.returncode == 0, f"rc={done.returncode} stderr={done.stderr!r}"
    assert done.stderr == ""
    assert _api_port(root).isdigit()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        pytest.param("gateway_upstream_read_timeout_seconds", 59.0, id="http-just-under"),
        pytest.param("gateway_upstream_read_timeout_seconds", 5.0, id="http-tiny"),
        pytest.param("gateway_upstream_read_timeout_seconds", 0.0, id="http-zero"),
        pytest.param("gateway_bedrock_read_timeout_seconds", 59, id="bedrock-just-under"),
        pytest.param("gateway_bedrock_read_timeout_seconds", 1, id="bedrock-tiny"),
    ],
)
def test_prod_rejects_an_upstream_silence_bound_under_a_minute(field: str, value: float):
    """A bound this short cuts a provider that is only thinking, and the cut reads
    as a provider outage. A crashed peer is caught by the transport's TCP
    keepalive probes instead, so nothing is gained by lowering it."""
    with pytest.raises(ValueError, match="must be at least 60s"):
        _prod_settings(**{field: value})


# --------------------------------------------------------------------------
# The device-grant poll throttle can never sit below the cadence the server issues
# --------------------------------------------------------------------------


def test_the_default_poll_throttle_clears_the_default_poll_interval():
    s = Settings(_env_file=None, app_env="local")  # type: ignore[call-arg]
    issued_per_minute = 60 / s.auth_device_poll_interval_seconds
    assert s.rate_limit_device_poll_per_minute >= issued_per_minute
    assert s.rate_limit_device_poll_ip_per_minute >= issued_per_minute


@pytest.mark.parametrize(
    ("overrides", "named"),
    [
        pytest.param(
            {"rate_limit_device_poll_per_minute": 59},
            "RATE_LIMIT_DEVICE_POLL_PER_MINUTE",
            id="per-code-below-a-1s-poll",
        ),
        pytest.param(
            {"rate_limit_device_poll_ip_per_minute": 59},
            "RATE_LIMIT_DEVICE_POLL_IP_PER_MINUTE",
            id="per-address-below-a-1s-poll",
        ),
        pytest.param(
            {"auth_device_poll_interval_seconds": 5, "rate_limit_device_poll_per_minute": 11},
            "RATE_LIMIT_DEVICE_POLL_PER_MINUTE",
            id="per-code-below-a-5s-poll",
        ),
    ],
)
def test_a_poll_throttle_below_the_issued_cadence_is_refused(
    overrides: dict[str, object], named: str
):
    with pytest.raises(ValueError, match=named):
        Settings(_env_file=None, app_env="local", **overrides)  # type: ignore[call-arg]


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({}, id="defaults"),
        pytest.param({"gateway_upstream_read_timeout_seconds": 60.0}, id="http-exactly-a-minute"),
        pytest.param({"gateway_bedrock_read_timeout_seconds": 60}, id="bedrock-exactly-a-minute"),
        pytest.param({"gateway_bedrock_read_timeout_seconds": 172800}, id="bedrock-two-days"),
    ],
)
def test_prod_accepts_a_silence_bound_of_a_minute_or_more(overrides):
    s = _prod_settings(**overrides)
    assert s.gateway_upstream_read_timeout_seconds >= 60
    assert s.gateway_bedrock_read_timeout_seconds >= 60


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        pytest.param(
            {"gateway_client_keepalive_seconds": 0.0},
            "must be greater than 0",
            id="disabled",
        ),
        pytest.param(
            {"gateway_client_keepalive_seconds": -1.0},
            "must be greater than 0",
            id="negative",
        ),
        pytest.param(
            {"gateway_client_keepalive_seconds": 4000.0},
            "must be under",
            id="equal-to-the-edge-window",
        ),
        pytest.param(
            {"gateway_client_keepalive_seconds": 5000.0},
            "must be under",
            id="above-the-edge-window",
        ),
        pytest.param(
            {
                "gateway_client_keepalive_seconds": 120.0,
                "gateway_edge_idle_timeout_seconds": 60.0,
            },
            "must be under",
            id="above-a-tightened-edge-window",
        ),
        pytest.param(
            {"gateway_client_keepalive_seconds": 75.1},
            "must be at most 75s",
            id="too-rare-for-the-client-silence-bound",
        ),
        pytest.param(
            {"gateway_client_keepalive_seconds": 120.0},
            "a client ends a step it hears nothing on for 300s",
            id="half-the-client-silence-bound",
        ),
    ],
)
def test_prod_rejects_a_keepalive_that_cannot_hold_the_edge_open(overrides, match: str):
    """The keepalive exists so the load balancer does not close a silent step. A
    cadence at or above the edge's own idle window never fires in time, and a
    disabled one never fires at all — both leave the step cut by the edge."""
    with pytest.raises(ValueError, match=match):
        _prod_settings(**overrides)


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({}, id="defaults"),
        pytest.param(
            {"gateway_client_keepalive_seconds": 75.0}, id="a-quarter-of-the-client-bound"
        ),
        pytest.param(
            {"gateway_client_keepalive_seconds": 30.0, "gateway_edge_idle_timeout_seconds": 60.0},
            id="under-a-tightened-edge-window",
        ),
    ],
)
def test_prod_accepts_a_keepalive_under_the_edge_idle_timeout(overrides):
    s = _prod_settings(**overrides)
    assert 0 < s.gateway_client_keepalive_seconds < s.gateway_edge_idle_timeout_seconds


def test_the_shipped_per_step_bounds_let_one_step_run_for_a_day():
    """A single model step is entitled to a day; only the credit-reservation
    sweeper's need for an outer bound keeps these finite at all. Read off the
    model so a local .env cannot make this pass with small shipped defaults."""
    assert Settings.model_fields["gateway_upstream_read_timeout_seconds"].default == 7200.0
    assert Settings.model_fields["gateway_bedrock_read_timeout_seconds"].default == 86400
    assert Settings.model_fields["gateway_max_stream_seconds"].default == 86400
    # And the keepalive cadence stays far under the SaaS ALB's 4000s idle window.
    assert Settings.model_fields["gateway_client_keepalive_seconds"].default == 15.0
    assert Settings.model_fields["gateway_edge_idle_timeout_seconds"].default == 4000.0


# --------------------------------------------------------------------------- #
# The postpaid reservation guardrail.
# --------------------------------------------------------------------------- #


def test_the_postpaid_reserve_buffer_ships_as_a_setting_not_a_constant():
    """It was a hard-coded $50, which is a spend-cap-sized hole in a guardrail
    that only has to clear one request's estimate. Read off the model so a local
    .env cannot make this pass."""
    field = Settings.model_fields["billing_postpaid_reserve_buffer_usd"]
    assert field.default == Decimal("10")


@pytest.mark.parametrize(
    "value",
    [pytest.param(0, id="zero"), pytest.param(-1, id="negative")],
)
def test_a_postpaid_buffer_of_nothing_is_refused_outright(value: int):
    """A zero buffer grants no headroom, so a postpaid pool can never reserve and
    every request 402s with no org-visible reason."""
    with pytest.raises(ValidationError):
        Settings(_env_file=None, billing_postpaid_reserve_buffer_usd=value)


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param(
            {
                "billing_postpaid_reserve_buffer_usd": Decimal("0.10"),
                "billing_max_overdraft_nanos": 500_000_000,
            },
            id="buffer-under-the-overdraft",
        ),
        pytest.param(
            {
                "billing_postpaid_reserve_buffer_usd": Decimal("10"),
                "billing_max_overdraft_nanos": 20_000_000_000,
            },
            id="overdraft-raised-past-the-buffer",
        ),
    ],
)
def test_prod_rejects_a_postpaid_buffer_under_the_overdraft_it_must_absorb(overrides):
    """A settle may already push the balance ``billing_max_overdraft_nanos``
    negative. A guardrail smaller than that leaves the pool unable to admit the
    very next request it just overspent on."""
    with pytest.raises(ValueError, match="BILLING_POSTPAID_RESERVE_BUFFER_USD"):
        _prod_settings(**overrides)


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({}, id="defaults"),
        pytest.param(
            {"billing_postpaid_reserve_buffer_usd": Decimal("0.50")}, id="exactly-the-overdraft"
        ),
        pytest.param({"billing_postpaid_reserve_buffer_usd": Decimal("250")}, id="generous"),
    ],
)
def test_prod_accepts_a_postpaid_buffer_that_covers_the_overdraft(overrides):
    s = _prod_settings(**overrides)
    overdraft = Decimal(s.billing_max_overdraft_nanos) / Decimal(1_000_000_000)
    assert overdraft <= s.billing_postpaid_reserve_buffer_usd


# --- Gateway tokenizer + agent kill switches ----------------------------------


def test_tokenizer_and_web_fetch_defaults():
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.gateway_tokenizer_warm_timeout_seconds == 10.0
    assert s.gateway_tokenizer_cache_dir == ""
    # The kill switch is a subtractive lever, so its default must be "on" —
    # shipping it off would silently remove a tool from every install.
    assert s.agent_web_fetch_enabled is True


@pytest.mark.parametrize(
    "seconds",
    [
        pytest.param(0, id="zero-never-waits"),
        pytest.param(-1.0, id="negative"),
        pytest.param(121.0, id="longer-than-any-boot-budget"),
    ],
)
def test_an_unbounded_tokenizer_warm_timeout_is_refused(seconds: float):
    """The warm-up exists to keep a blackholed rank fetch off the request path.
    A timeout of zero or below would never wait (so the tokenizer would never be
    adopted), and an unbounded one would move the same stall from the first
    request to the boot."""
    with pytest.raises(ValidationError) as err:
        Settings(_env_file=None, gateway_tokenizer_warm_timeout_seconds=seconds)  # type: ignore[call-arg]
    assert "GATEWAY_TOKENIZER_WARM_TIMEOUT_SECONDS" in str(err.value)


def test_a_bounded_tokenizer_warm_timeout_is_accepted():
    s = Settings(_env_file=None, gateway_tokenizer_warm_timeout_seconds=0.25)  # type: ignore[call-arg]
    assert s.gateway_tokenizer_warm_timeout_seconds == 0.25


#: A settle margin well under every stream cap these cases use. The sibling rule
#: `_settle_margin_within_the_stream_cap` refuses a margin at or above the cap and
#: names GATEWAY_MAX_STREAM_SECONDS while doing it, so at the default 60s margin a
#: short cap is refused by THAT rule and the keepalive relation is never reached.
#: Clearing it is what makes these cases evidence for the rule they are about.
_MARGIN_CLEAR: dict[str, object] = {"gateway_stream_settle_margin_seconds": 30.0}


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param(
            {"gateway_max_stream_seconds": 60, "gateway_client_keepalive_seconds": 60.0},
            id="cap-equals-keepalive",
        ),
        pytest.param(
            {"gateway_max_stream_seconds": 60, "gateway_client_keepalive_seconds": 120.0},
            id="cap-under-keepalive",
        ),
        pytest.param(
            {"gateway_max_stream_seconds": 3600, "gateway_client_keepalive_seconds": 3600.0},
            id="cap-equals-an-hour-long-keepalive",
        ),
    ],
)
def test_prod_refuses_a_stream_cap_its_keepalive_never_reaches(overrides):
    """The keepalive is what keeps a quiet step alive across the edge. A stream
    cut at or before the first tick never gets one, so the mechanism is dead
    configuration rather than a shorter stream.

    Matched on the clause only this refusal writes, so a neighbouring rule that
    also names the stream cap cannot stand in for it."""
    with pytest.raises(ValidationError, match="must exceed GATEWAY_CLIENT_KEEPALIVE_SECONDS"):
        _prod_settings(**_MARGIN_CLEAR, **overrides)


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param(
            {"gateway_max_stream_seconds": 61, "gateway_client_keepalive_seconds": 60.0},
            id="one-second-above-the-refused-pair",
        ),
        pytest.param(
            {"gateway_max_stream_seconds": 86400, "gateway_client_keepalive_seconds": 15.0},
            id="the-pair-the-saas-task-ships",
        ),
    ],
)
def test_prod_accepts_a_stream_cap_its_keepalive_reaches(overrides):
    """The first case is the refused pair with one second added and nothing else
    moved, so the boundary belongs to this rule and not to its neighbour."""
    s = _prod_settings(**_MARGIN_CLEAR, **overrides)
    assert s.gateway_client_keepalive_seconds < s.gateway_max_stream_seconds


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(0.0, id="zero"),
        pytest.param(-1.0, id="negative"),
        pytest.param(30.0, id="exactly-the-widest-probe-timeout"),
        pytest.param(120.0, id="far-over"),
    ],
)
def test_prod_refuses_a_health_ready_budget_no_probe_waits_for(value):
    """`/health/ready` exists to answer a fast 503 while a slow database is still
    serving. A budget at or above the probe's own timeout means the probe gives
    up first, so the answer never arrives and every task drains at once; at or
    below zero the endpoint 503s before asking the database anything."""
    with pytest.raises(ValidationError, match="HEALTH_READY_TIMEOUT_SECONDS"):
        _prod_settings(health_ready_timeout_seconds=value)


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(0.5, id="aggressive"),
        pytest.param(4.0, id="the-value-the-chart-and-compose-ship"),
        pytest.param(29.9, id="just-inside"),
    ],
)
def test_prod_accepts_a_health_ready_budget_a_probe_outlasts(value):
    assert _prod_settings(health_ready_timeout_seconds=value).health_ready_timeout_seconds == value


def test_prod_refuses_a_negative_readiness_grace():
    with pytest.raises(ValidationError, match="HEALTH_READY_GRACE_SECONDS"):
        _prod_settings(health_ready_grace_seconds=-1.0)


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(0.0, id="zero-is-the-strict-probe"),
        pytest.param(900.0, id="the-default"),
    ],
)
def test_prod_accepts_a_readiness_grace_of_zero_or_more(value):
    assert _prod_settings(health_ready_grace_seconds=value).health_ready_grace_seconds == value


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({}, id="defaults"),
        pytest.param({"rate_limit_device_poll_per_minute": 60}, id="exactly-the-1s-cadence"),
        pytest.param(
            {
                "auth_device_poll_interval_seconds": 5,
                "rate_limit_device_poll_per_minute": 12,
                "rate_limit_device_poll_ip_per_minute": 12,
            },
            id="a-slower-poll-admits-a-lower-throttle",
        ),
        pytest.param(
            {"auth_device_poll_interval_seconds": 7, "rate_limit_device_poll_per_minute": 9},
            id="a-fractional-cadence-rounds-up",
        ),
    ],
)
def test_a_poll_throttle_at_or_above_the_issued_cadence_is_accepted(overrides: dict[str, object]):
    Settings(_env_file=None, app_env="local", **overrides)  # type: ignore[call-arg]


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(_prod_settings, id="production"),
        pytest.param(_staging_settings, id="staging"),
    ],
)
def test_multi_org_is_off_by_default_and_a_deployment_may_turn_it_on(
    build: Callable[..., Settings],
):
    """Multi-org is a rollout flag, off unless a deployment sets it. The tenancy
    ratchets hold at zero findings, so neither validator refuses it on."""
    assert build().multi_org_enabled is False
    assert build(multi_org_enabled=True).multi_org_enabled is True


def test_multi_org_is_not_refused_outside_production():
    assert Settings(_env_file=None, app_env="local", multi_org_enabled=True).multi_org_enabled
