"""Where the API may send a browser back to: a path under the app's own origin.

Every redirect the API answers to a browser resolves through here, so the one
open-redirect guard covers the OAuth landings and the edge-gate return alike.
"""

from __future__ import annotations

import re
from urllib.parse import urlencode

from alkera_core.config import settings

#: Rejects backslashes (browsers normalize ``\`` to ``/``, so ``/\evil`` becomes
#: the protocol-relative ``//evil``) and control / whitespace characters (CRLF,
#: tabs).
_UNSAFE_RETURN_PATH = re.compile(r"[\x00-\x20\x7f\\]")

#: Where a browser lands when the path it asked for cannot be honoured.
DEFAULT_RETURN_PATH = "/dashboard"


def safe_return_path(candidate: str | None) -> str:
    """Only a same-site relative path is honoured.

    Everything else (an absolute URL, the protocol-relative ``//evil``, the
    backslash and CRLF tricks) falls back to the dashboard. The final redirect
    resolves the result against ``frontend_base_url``.
    """
    if (
        candidate
        and candidate.startswith("/")
        and not candidate.startswith("//")
        and not _UNSAFE_RETURN_PATH.search(candidate)
    ):
        return candidate
    return DEFAULT_RETURN_PATH


def frontend_url(path: str) -> str:
    """``path`` under the app's origin."""
    return f"{settings.frontend_base_url.rstrip('/')}{path}"


def sso_login_with_return(login_url: str, *, return_to: str) -> str:
    """An org's SSO sign-in URL (``sign_in_policy.sso_login_url``) that brings
    the browser back to ``return_to`` afterwards. The SSO routes run the value
    through :func:`safe_return_path` again on the way back, so this only ever
    names a path under the app's origin."""
    return f"{login_url}?{urlencode({'return_to': safe_return_path(return_to)})}"
