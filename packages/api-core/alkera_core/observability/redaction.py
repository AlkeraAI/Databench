"""Central redaction — one scrubber reused by structlog, Sentry, and crash reports.

Policy (aggressive): never emit secrets (tokens, passwords, API keys,
cookies, JWTs); never emit prompt/chat/file *content*; user email + org id are
allowed in an authed context; home-directory paths are reduced to ``~`` so crash
reports from a user's machine don't leak their username.

Everything here is pure (no I/O, no logging) so it can run inside a logging
processor and Sentry's ``before_send`` without recursion or side effects.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from alkera_core.token_prefixes import TOKEN_PREFIXES

REDACTED = "[redacted]"

# Exact field / header names (normalized: lowercased, dashes -> underscores)
# whose VALUE is always removed. Exact-match — NOT substring — on purpose:
# substring "token" would wrongly redact billing's ``input_tokens`` /
# ``output_tokens`` counts that the growth/analytics pipeline needs.
#
# Every name the deployment injects as a secret must be covered here or by
# `_SENSITIVE_KEY_SUFFIXES` below, so the day someone logs a settings mapping the
# values are already gone. The suffix rule carries the family names
# (``*_secret``, ``*_password``, ``*_private_key``, …); the entries below carry
# the ones whose spelling no suffix predicts.
SENSITIVE_KEYS: frozenset[str] = frozenset(
    {
        "authorization",
        "proxy_authorization",
        "x_auth_token",
        "x_api_key",
        "cookie",
        "set_cookie",
        "api_key",
        "apikey",
        "token",
        "access_token",
        "refresh_token",
        "id_token",
        "session_token",
        "auth_token",
        "cli_token",
        "bearer",
        "jwt",
        "secret",
        "client_secret",
        "private_key",
        "password",
        "passwd",
        "pwd",
        "dsn",
        "sentry_dsn",
        "auth_jwt_secret",
        # The key that seals stored connection secrets; nothing in its spelling
        # is a family suffix, and it is injected into every production task.
        "secret_box_key",
        "aws_access_key_id",
        "aws_secret_access_key",
        "aws_session_token",
        "aws_bearer_token_bedrock",
        "anthropic_api_key",
        "openai_api_key",
        "credential",
        "credentials",
        "alkera_session",
        # Single-use / long-lived bearer credentials whose names end in a plain
        # "_token" — spelled out one by one rather than by suffix, because a
        # "*_token" suffix rule would also swallow pagination cursors and the
        # boolean has-a-token flags that operations genuinely needs in a log.
        "metrics_auth_token",
        "proxy_token",
        "ci_token",
        "scim_token",
        "task_token",
        "invite_token",
        "invitation_token",
        "verification_token",
        "reset_token",
        "slack_bot_token",
        # Key material and derived material that no suffix predicts.
        "token_hash_pepper",
        "password_hash",
        # A DSN carries the database user and password in the URL itself.
        "database_url",
        "database_url_sync",
        # Deployment-injected identifiers. Not secrets on their own, but they
        # ride in the same secrets bundle and name the tenant an attacker would
        # target, so they are never worth a log line.
        "smtp_username",
        "github_app_id",
        "github_app_client_id",
        "oauth_google_client_id",
        "oauth_github_client_id",
        "slack_client_id",
    }
)

# Suffixes that make a key sensitive by construction, so a secret introduced
# later is covered without anyone remembering to extend the list above. None of
# these can collide with the billing counters: ``input_tokens`` / ``max_tokens``
# end in "_tokens", not "_token", and no counter ends in "_secret" or "_key".
_SENSITIVE_KEY_SUFFIXES: tuple[str, ...] = (
    "_secret",
    "_password",
    "_passwd",
    "_api_key",
    "_secret_key",
    "_private_key",
    "_signing_key",
    "_access_key",
    "_credential",
    "_credentials",
    "_pepper",
)

# Value-level patterns scrubbed from free text (messages, tracebacks, log args).
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\b")
_AUTH_SCHEME_RE = re.compile(r"\b(Bearer|Basic)\s+([A-Za-z0-9._\-+/=]{8,})", re.IGNORECASE)
# A credential looks token-like if it has a digit or a base64/url-safe symbol, or
# is long. Avoids redacting ordinary prose like "Basic understanding".
_TOKENISH_RE = re.compile(r"[0-9._\-+/=]")
_OPENAI_KEY_RE = re.compile(r"\bsk-(?:proj-|ant-)?[A-Za-z0-9_-]{16,}\b")
_BEDROCK_KEY_RE = re.compile(r"\bABSK[A-Za-z0-9+/=]{16,}\b")
_AWS_AKID_RE = re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")
# Every bearer credential the platform mints, recognised by its prefix. Built
# from the one tuple the minting modules read, so a new kind of token is
# redacted the day its prefix is added there.
_PLATFORM_TOKEN_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(prefix) for prefix in TOKEN_PREFIXES) + r")[A-Za-z0-9_-]{8,}"
)
# A password carried in a URL's userinfo (``scheme://user:password@host``), as
# a DSN or a clone URL prints it. The user name stays; the password does not.
# A ``${NAME}`` there is a reference to an environment variable, which names
# where the password is and is not one, so it is left as written.
_URL_USERINFO_RE = re.compile(
    r"(\b[A-Za-z][A-Za-z0-9+.-]*://[^\s/:@\"'<>]*)"
    r":(?!\$\{[A-Za-z_][A-Za-z0-9_]*\}@)[^\s/@\"'<>]+@"
)

# Emailed entry links carry a LIVE single-use account-takeover credential in the
# URL itself — the password-reset, email-verification and invitation-preview
# tokens as the last path segment, the invitation token (`invite` on the SPA,
# `invite_token` on the OAuth start) / OAuth register ticket (`oauth_ticket` on
# the SPA, `ticket` on the register-context read) / device user code as a query
# param. Those URLs reach the logs by several routes (the per-request
# access log's path, the client-error reporter's `url` and `stack`, a persisted
# crash report), and the key-name rules above can't help: the credential is a
# *value fragment*, not a field of its own. The length floor keeps the sibling
# routes that share the prefix — `/password-reset/request`, `/verify-email/resend`
# — readable in the access log.
_CREDENTIAL_PATH_RE = re.compile(
    r"\b((?:reset-password|password-reset|verify-email|invitations/by-token)/)[A-Za-z0-9_.~-]{16,}"
)
_CREDENTIAL_PARAM_RE = re.compile(
    r"\b(invite|invite_token|oauth_ticket|ticket|user_code)=[^&#\s\"'<>]+", re.IGNORECASE
)

# Home-directory paths -> ~ (drop the username; keep the rest of the path).
_HOME_POSIX_RE = re.compile(r"/(?:Users|home)/[^/\s\"']+")
_HOME_WIN_RE = re.compile(r"[A-Za-z]:\\Users\\[^\\\s\"']+", re.IGNORECASE)

# Defensive cap so a pathological / cyclic structure can't blow the stack when a
# scrub runs inside a logging processor.
_MAX_DEPTH = 8


def is_sensitive_key(key: str) -> bool:
    """True if `key` names a field whose value must always be redacted."""
    normalized = key.strip().lower().replace("-", "_")
    return normalized in SENSITIVE_KEYS or normalized.endswith(_SENSITIVE_KEY_SUFFIXES)


def _redact_auth_scheme(match: re.Match[str]) -> str:
    scheme, cred = match.group(1), match.group(2)
    if _TOKENISH_RE.search(cred) or len(cred) >= 24:
        return f"{scheme} {REDACTED}"
    return match.group(0)


def scrub_url_credentials(value: str) -> str:
    """Strip live credentials carried inside a URL or path.

    Applies to any free text, so a bare request path, a full href and a
    traceback line that happens to quote one are all covered by the same rule.
    """
    if not value:
        return value
    out = _CREDENTIAL_PATH_RE.sub(rf"\g<1>{REDACTED}", value)
    return _CREDENTIAL_PARAM_RE.sub(rf"\g<1>={REDACTED}", out)


def scrub_text(value: str) -> str:
    """Redact secret-looking substrings (JWTs, bearer/basic creds, provider keys,
    and credentials riding inside a URL)."""
    if not value:
        return value
    out = scrub_url_credentials(value)
    out = _URL_USERINFO_RE.sub(rf"\g<1>:{REDACTED}@", out)
    out = _PLATFORM_TOKEN_RE.sub(REDACTED, out)
    out = _JWT_RE.sub(REDACTED, out)
    out = _AUTH_SCHEME_RE.sub(_redact_auth_scheme, out)
    out = _OPENAI_KEY_RE.sub(REDACTED, out)
    out = _BEDROCK_KEY_RE.sub(REDACTED, out)
    return _AWS_AKID_RE.sub(REDACTED, out)


def scrub_path(value: str) -> str:
    """Reduce absolute home-dir paths to ``~`` so usernames don't leak."""
    if not value:
        return value
    out = _HOME_POSIX_RE.sub("~", value)
    return _HOME_WIN_RE.sub("~", out)


def scrub_value(value: Any, _depth: int = 0) -> Any:
    """Recursively scrub a value: strings get text+path scrubbing, containers
    are walked, everything else passes through untouched."""
    if _depth > _MAX_DEPTH:
        return value
    if isinstance(value, str):
        return scrub_path(scrub_text(value))
    if isinstance(value, Mapping):
        return _scrub_mapping(value, _depth + 1)
    if isinstance(value, (list, tuple)):
        return [scrub_value(item, _depth + 1) for item in value]
    return value


def _scrub_mapping(data: Mapping[str, Any], depth: int) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in data.items():
        if isinstance(key, str) and is_sensitive_key(key):
            result[key] = REDACTED
        else:
            result[key] = scrub_value(value, depth)
    return result


def scrub_mapping(data: Mapping[str, Any]) -> dict[str, Any]:
    """Return a scrubbed shallow-or-deep copy of a mapping (sensitive keys
    redacted, string values text/path-scrubbed, nested containers walked)."""
    return _scrub_mapping(data, 0)
