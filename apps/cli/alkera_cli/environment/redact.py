"""Credentials never reach an environment spec.

Index URLs, VCS URLs and conda channels routinely carry a token (``https://
__token__:pypi-AgE...@host/simple``, ``https://x-access-token:ghp_...@github.com``,
``https://conda.anaconda.org/t/<token>/channel``). The capture records the URL
with the credential removed and says so; an environment variable reference
(``${PIP_TOKEN}``) is a pointer, not a secret, and is kept so the target can
supply it.

:func:`find_secrets` is the last check before a spec is written: a spec that
still holds anything credential-shaped is refused, never written.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from alkera_core.observability import REDACTED, scrub_text

#: The query and fragment parameters a package or index URL keeps: what pip
#: reads to find and check the artifact (``#egg=``, ``#subdirectory=``, a
#: ``#sha256=`` pin). Every other parameter is dropped, since nothing tells a
#: signed URL's ``X-Goog-Signature`` or a GitLab ``private_token`` apart from a
#: harmless ``?v=1`` reliably enough to keep one.
_KEPT_PARAMS = frozenset(
    {"egg", "subdirectory", "md5", "sha1", "sha224", "sha256", "sha384", "sha512"}
)
#: Parameter names that are a credential whole (after ``-`` becomes ``_``).
_SECRET_NAMES = frozenset({"sig", "code", "pass", "pwd"})
#: Parameter names that are a credential when they hold one of these words.
_SECRET_WORDS = ("token", "signature", "credential", "secret", "password", "passwd", "auth")
#: Parameter name prefixes of the cloud vendors' signed URLs.
_SECRET_PREFIXES = ("x_goog_", "x_amz_", "x_ms_", "key")


def is_secret_param(name: str) -> bool:
    """Whether a query or fragment parameter called ``name`` carries a
    credential: ``-`` and ``_`` are one, case is ignored, and a name holding
    ``token``, ``signature``, ``credential`` (and the like), a name ending in
    ``key`` or starting ``key``, and the cloud vendors' ``x-goog-`` /
    ``x-amz-`` signing parameters all count."""
    key = name.strip().lower().replace("-", "_")
    if not key:
        return False
    if key in _SECRET_NAMES or key.endswith("key") or key.startswith(_SECRET_PREFIXES):
        return True
    return any(word in key for word in _SECRET_WORDS)


#: An environment variable reference pip and uv expand on the target.
_ENV_REF_RE = re.compile(r"^\$\{[A-Za-z_][A-Za-z0-9_]*\}$")
#: The conda token path segment: ``/t/<token>/``.
_CONDA_TOKEN_RE = re.compile(r"/t/[^/]+(?=/)")
#: A URL inside free text (a requirements line, a pip.conf value).
_URL_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s'\"]+")
#: Token shapes from the hosts an environment talks to.
_TOKEN_RE = re.compile(
    r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|glpat-[A-Za-z0-9_-]{16,}"
    r"|pypi-[A-Za-z0-9_-]{32,}|xox[abprs]-[A-Za-z0-9-]{10,}|npm_[A-Za-z0-9]{30,}"
    r"|AKIA[0-9A-Z]{16}|ASIA[0-9A-Z]{16})"
)


class SpecSecretError(ValueError):
    """A spec still holds something credential-shaped; it is not written."""

    def __init__(self, locations: list[str]) -> None:
        self.locations = locations
        super().__init__(
            "refusing to write the environment spec: it holds what looks like a credential at "
            + ", ".join(locations)
        )


def _is_env_ref(text: str) -> bool:
    return bool(_ENV_REF_RE.match(text))


#: A login name, not a credential: the user in ``ssh://user@host/repo``.
_PLAIN_USER_RE = re.compile(r"^[A-Za-z][A-Za-z0-9._-]{0,31}$")


def _is_plain_user(user: str) -> bool:
    """A bare user with no password that is a short login name and no token
    shape (a provider prefix, a long string with digits in it)."""
    if not _PLAIN_USER_RE.match(user) or _TOKEN_RE.search(user):
        return False
    return not (len(user) >= 20 and any(c.isdigit() for c in user))


def _userinfo_kept(netloc: str) -> tuple[str, bool]:
    """``netloc`` without a credential-bearing ``user:password@`` part, and
    whether one was removed."""
    if "@" not in netloc:
        return netloc, False
    userinfo, _, hostport = netloc.rpartition("@")
    user, _, password = userinfo.partition(":")
    keep = (_is_env_ref(user) and (not password or _is_env_ref(password))) or (
        not password and _is_plain_user(user)
    )
    return (netloc, False) if keep else (hostport, True)


def _params(text: str) -> list[tuple[str, str]]:
    return parse_qsl(text, keep_blank_values=True)


def redact_url(url: str) -> tuple[str, bool]:
    """``url`` with every credential removed, and whether anything was.

    A ``user:password@`` part goes unless each half is an environment variable
    reference; a user with no password stays when it is a reference or a plain
    login name rather than something token-shaped; a conda ``/t/<token>/``
    segment goes. Of the query and the fragment only what pip reads to find
    and check the artifact (``egg``, ``subdirectory``, a hash pin) and
    parameters whose value is an environment variable reference stay; any
    other parameter goes, whatever it is called. A URL this cannot parse is
    returned with :func:`scrub_text` applied."""
    try:
        parts = urlsplit(url)
    except ValueError:
        cleaned = scrub_text(url)
        return cleaned, cleaned != url
    netloc, removed = _userinfo_kept(parts.netloc)
    path = parts.path
    if _CONDA_TOKEN_RE.search(path):
        path = _CONDA_TOKEN_RE.sub("", path)
        removed = True
    kept_parts: list[str] = []
    for text in (parts.query, parts.fragment):
        if not text:
            kept_parts.append("")
            continue
        pairs = _params(text)
        kept = [(k, v) for k, v in pairs if k.lower() in _KEPT_PARAMS or _is_env_ref(v)]
        if len(kept) != len(pairs) or not pairs:
            removed = True
            kept_parts.append(urlencode(kept, safe="${}"))
        else:
            kept_parts.append(text)
    query, fragment = kept_parts
    cleaned = urlunsplit((parts.scheme, netloc, path, query, fragment))
    return cleaned, removed


def url_holds_credential(url: str) -> bool:
    """Whether ``url`` carries something credential-shaped: a ``user:password``
    that is not a pair of references, a token-shaped user, a conda token
    segment, or a query or fragment parameter named like a credential
    (:func:`is_secret_param`) whose value is not a reference."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return scrub_text(url) != url
    if _userinfo_kept(parts.netloc)[1] or _CONDA_TOKEN_RE.search(parts.path):
        return True
    for text in (parts.query, parts.fragment):
        for key, value in _params(text):
            if is_secret_param(key) and not _is_env_ref(value):
                return True
    return False


def redact_text(text: str) -> tuple[str, bool]:
    """``text`` with every URL in it redacted and token shapes scrubbed."""
    removed = False

    def _one(match: re.Match[str]) -> str:
        nonlocal removed
        cleaned, hit = redact_url(match.group(0))
        removed = removed or hit
        return cleaned

    out = _URL_RE.sub(_one, text)
    scrubbed = _TOKEN_RE.sub(REDACTED, scrub_text(out))
    return scrubbed, removed or scrubbed != out


def _strings(value: Any, where: str) -> Iterator[tuple[str, str]]:
    if isinstance(value, str):
        yield where, value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _strings(item, f"{where}.{key}" if where else str(key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _strings(item, f"{where}[{index}]")


def find_secrets(data: Any) -> list[str]:
    """The locations (``packages[3].url``) of every credential-shaped string
    in ``data``; empty when there are none."""
    hits: list[str] = []
    for where, text in _strings(data, ""):
        if _TOKEN_RE.search(text) or scrub_text(text) != text:
            hits.append(where)
            continue
        if any(url_holds_credential(m.group(0)) for m in _URL_RE.finditer(text)):
            hits.append(where)
    return hits


__all__ = [
    "SpecSecretError",
    "find_secrets",
    "is_secret_param",
    "redact_text",
    "redact_url",
    "url_holds_credential",
]
