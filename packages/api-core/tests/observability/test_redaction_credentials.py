"""Two gaps the central redactor used to have, pinned shut.

1. A live single-use credential riding *inside* a URL — the password-reset and
   email-verification tokens are a path segment, the invitation token / OAuth
   register ticket / device user code are query params. No key-name rule can
   catch those: the credential is a fragment of a value, not a field.
2. The deployment's own secret NAMES. The key list matches exactly (so billing's
   ``input_tokens`` counters survive), which used to mean most of the names the
   prod task definition actually injects were not covered at all.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from alkera_core.observability.redaction import (
    REDACTED,
    is_sensitive_key,
    scrub_mapping,
    scrub_text,
    scrub_url_credentials,
)

# A real `secrets.token_urlsafe(48)` shape: 64 url-safe characters.
TOKEN = "kQ3v_9Zx1LmN-tRs7Yb2Wc4Ee6Gg8Ii0Kk2Mm4Oo6Qq8Ss0Uu2Ww4Yy6Aa8Cc0Ee2G"

#: The names the prod task definition pulls out of the app secret. Kept as a
#: literal so the assertion runs everywhere, and cross-checked against the real
#: terraform below so the copy can't quietly rot.
PROD_SECRET_KEYS = [
    "AUTH_JWT_SECRET",
    "TOKEN_HASH_PEPPER",
    "DATABASE_URL",
    "DATABASE_URL_SYNC",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "SMTP_USERNAME",
    "SMTP_PASSWORD",
    "OAUTH_GOOGLE_CLIENT_ID",
    "OAUTH_GOOGLE_CLIENT_SECRET",
    "OAUTH_GITHUB_CLIENT_ID",
    "OAUTH_GITHUB_CLIENT_SECRET",
    "AWS_BEARER_TOKEN_BEDROCK",
    "SENTRY_DSN",
    "TURNSTILE_SECRET_KEY",
    "ALKERA_ENTITLEMENTS_SIGNING_KEY",
    "STRIPE_SECRET_KEY",
    "STRIPE_WEBHOOK_SECRET",
    "GITHUB_APP_ID",
    "GITHUB_APP_PRIVATE_KEY",
    "GITHUB_WEBHOOK_SECRET",
    "GITHUB_APP_CLIENT_ID",
    "GITHUB_APP_CLIENT_SECRET",
    "METRICS_AUTH_TOKEN",
    "SLACK_SIGNING_SECRET",
    "SLACK_BOT_TOKEN",
    "SLACK_CLIENT_ID",
    "SLACK_CLIENT_SECRET",
    "FILES_CONTENT_SIGNING_KEY",
    "SECRET_BOX_KEY",
]


def _repo_root() -> Path | None:
    for candidate in Path(__file__).resolve().parents:
        if (candidate / "ops" / "terraform" / "envs" / "prod" / "app" / "main.tf").is_file():
            return candidate
    return None


# --- credentials carried inside a URL -------------------------------------


@pytest.mark.parametrize(
    ("value", "secret"),
    [
        pytest.param(f"/api/v1/auth/password-reset/{TOKEN}", TOKEN, id="backend-reset-path"),
        pytest.param(f"/api/v1/auth/verify-email/{TOKEN}", TOKEN, id="backend-verify-path"),
        pytest.param(f"https://app.example.com/reset-password/{TOKEN}", TOKEN, id="spa-reset-href"),
        pytest.param(
            f"https://app.example.com/verify-email/{TOKEN}?src=email", TOKEN, id="spa-verify-href"
        ),
        pytest.param(f"  at Page (/reset-password/{TOKEN}:1:9)", TOKEN, id="stack-frame"),
        pytest.param(f"https://app.example.com/signup?invite={TOKEN}", TOKEN, id="invite-param"),
        pytest.param(
            f"https://app.example.com/signup?oauth_ticket={TOKEN}&x=1", TOKEN, id="ticket-param"
        ),
        pytest.param(
            "https://app.example.com/device?user_code=BDXR-QLPZ", "BDXR-QLPZ", id="device-user-code"
        ),
        pytest.param(
            f"/api/v1/invitations/by-token/{TOKEN}", TOKEN, id="backend-invitation-preview-path"
        ),
        pytest.param(
            f"/api/v1/auth/oauth/google/start?intent=signup&invite_token={TOKEN}",
            TOKEN,
            id="oauth-start-invite-token-param",
        ),
        pytest.param(
            f"/api/v1/auth/oauth/register/context?ticket={TOKEN}",
            TOKEN,
            id="oauth-register-context-ticket-param",
        ),
        pytest.param(
            f'INFO:     10.0.1.7:0 - "GET /api/v1/invitations/by-token/{TOKEN} HTTP/1.1" 200 OK',
            TOKEN,
            id="uvicorn-style-access-line",
        ),
    ],
)
def test_a_credential_in_a_url_never_survives_scrubbing(value: str, secret: str) -> None:
    scrubbed = scrub_text(value)
    assert secret not in scrubbed
    assert REDACTED in scrubbed


def test_the_route_around_the_credential_stays_readable() -> None:
    # An operator still needs to see WHICH endpoint was hit; only the last
    # segment goes.
    assert (
        scrub_url_credentials(f"/api/v1/auth/password-reset/{TOKEN}")
        == f"/api/v1/auth/password-reset/{REDACTED}"
    )
    assert (
        scrub_url_credentials(f"https://app.example.com/verify-email/{TOKEN}?src=email")
        == f"https://app.example.com/verify-email/{REDACTED}?src=email"
    )


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("/api/v1/auth/password-reset/request", id="reset-request-sibling"),
        pytest.param("/api/v1/auth/verify-email/resend", id="verify-resend-sibling"),
        pytest.param("/api/v1/invitations/me", id="invitations-sibling"),
        pytest.param("/reset-password", id="expired-link-page"),
        pytest.param("/api/v1/auth/login", id="unrelated-route"),
        pytest.param("we invited them and the invite was accepted", id="prose-mentioning-invite"),
    ],
)
def test_non_credential_paths_are_left_alone(value: str) -> None:
    assert scrub_url_credentials(value) == value


@pytest.mark.parametrize("field", ["path", "url", "stack", "message"])
def test_a_credential_url_is_scrubbed_whatever_field_carries_it(field: str) -> None:
    """The access log calls it `path`, the client-error endpoint calls it `url`
    and `stack`, a crash report calls it `message`. None of those names is (or
    should be) in the sensitive-key list, so the value-level rule is what has to
    carry all four."""
    out = scrub_mapping({field: f"https://app.example.com/reset-password/{TOKEN}"})
    assert TOKEN not in out[field]


def test_a_credential_url_nested_in_a_context_payload_is_scrubbed() -> None:
    out = scrub_mapping(
        {"context": {"breadcrumbs": [{"href": f"https://app.example.com/reset-password/{TOKEN}"}]}}
    )
    assert TOKEN not in repr(out)


# --- the deployment's own secret names ------------------------------------


@pytest.mark.parametrize("name", PROD_SECRET_KEYS, ids=PROD_SECRET_KEYS)
def test_every_injected_prod_secret_name_is_redacted(name: str) -> None:
    assert is_sensitive_key(name.lower()), name
    assert scrub_mapping({name.lower(): "the-live-value"})[name.lower()] == REDACTED


@pytest.mark.parametrize(
    "name",
    [
        # Names no suffix predicts, or that a suffix rule must not be widened to
        # reach (a "*_token" suffix would swallow pagination cursors).
        "proxy_token",
        "ci_token",
        "scim_token",
        "task_token",
        "invite_token",
        "invitation_token",
        "verification_token",
        "reset_token",
        "password_hash",
        "totp_secret",
        # Reached by the suffix rule, so a newly-added sibling is covered too.
        "some_future_service_password",
        "some_future_provider_api_key",
        "some_future_webhook_secret",
        "some_future_signing_key",
    ],
)
def test_other_credential_key_names_are_redacted(name: str) -> None:
    assert is_sensitive_key(name), name


@pytest.mark.parametrize(
    "name",
    [
        # The whole reason the key match is exact: billing and analytics read
        # these, and the suffix rule must not reach them.
        "input_tokens",
        "output_tokens",
        "max_tokens",
        "cached_tokens",
        "reasoning_tokens",
        "token_count",
        # Ordinary identifiers that merely contain a sensitive-looking word.
        "page_token_count",
        "idempotency_key",
        "cache_key",
        "partition_key",
        "user_id",
        "org_id",
        "email",
        "has_scim_token",
    ],
)
def test_benign_keys_survive_the_suffix_rule(name: str) -> None:
    assert not is_sensitive_key(name), name


def test_the_pinned_prod_secret_names_match_the_terraform_that_injects_them() -> None:
    """The list above is a copy; this is what stops it rotting. A secret added to
    the prod task definition fails here until the redactor covers it."""
    root = _repo_root()
    if root is None:  # pragma: no cover - only when api-core is used standalone
        pytest.skip("terraform stack not present next to this package")
    source = (root / "ops" / "terraform" / "envs" / "prod" / "app" / "main.tf").read_text()
    match = re.search(r"secret_keys\s*=\s*\[(.*?)\]", source, re.DOTALL)
    assert match, "could not find the secret_keys list in the prod app stack"
    declared = re.findall(r'"([A-Z0-9_]+)"', match.group(1))
    assert declared, "the secret_keys list parsed empty"
    assert set(declared) == set(PROD_SECRET_KEYS), (
        "the prod secret bundle changed; extend the redactor and this list together"
    )
