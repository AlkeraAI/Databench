"""Personal/free email-provider classification — backend-only.

The vendored free-email-domains snapshot lives HERE, not in ``api-core``, so the
shipped CLI binary never bundles it: ``alkera_core`` is compiled into
``alkera.exe`` (and a stray ~54KB data file + its package READMEs were leaking
into the onefile), whereas the backend is a server that's never Nuitka-compiled.
Self-hosted deployments extend the set via the ``PERSONAL_EMAIL_DOMAINS``
setting (comma-separated).

``email_domain`` (pure, dependency-free) stays in ``alkera_core.utils.email``.
"""

from __future__ import annotations

from collections.abc import Iterable
from importlib.resources import files

from alkera_core.config import settings
from alkera_core.utils.email import email_domain

# Modern free/personal providers the vendored upstream snapshot predates.
_EXTRA_PERSONAL_DOMAINS: frozenset[str] = frozenset({"proton.me", "pm.me"})


def _load_personal_domains() -> frozenset[str]:
    """Read the vendored free-email-provider snapshot into a frozenset.

    One lowercased domain per line; ``#`` comment + blank lines ignored. Bundled
    as backend package data (``backend/auth/data/free_email_domains.txt``,
    refreshed via ``make gen-free-email-domains``), so it resolves inside the
    backend wheel / Docker image as well as an editable checkout. Modern
    providers the source predates are folded in via ``_EXTRA_PERSONAL_DOMAINS``.
    """
    text = (files("backend.auth") / "data" / "free_email_domains.txt").read_text(encoding="utf-8")
    domains = {
        stripped.lower()
        for line in text.splitlines()
        if (stripped := line.strip()) and not stripped.startswith("#")
    }
    return frozenset(domains | _EXTRA_PERSONAL_DOMAINS)


# Loaded once at import — the snapshot is static package data.
DEFAULT_PERSONAL_EMAIL_DOMAINS: frozenset[str] = _load_personal_domains()


def _configured_extra_domains() -> frozenset[str]:
    """Self-hosted ``PERSONAL_EMAIL_DOMAINS`` extras (comma-separated), if any."""
    return frozenset(
        d.strip().lower() for d in settings.personal_email_domains.split(",") if d.strip()
    )


def is_personal_email(email: str, *, personal_domains: Iterable[str] | None = None) -> bool:
    """True when ``email``'s domain is a known free/personal provider.

    Used to discourage business signups with consumer webmail (gmail, yahoo, …).
    By default the set is the vendored snapshot unioned with any configured
    ``PERSONAL_EMAIL_DOMAINS`` extras; pass ``personal_domains`` to override it
    entirely (tests). Exact-domain match only (no subdomain wildcarding).
    """
    domain = email_domain(email)
    if not domain:
        return False
    domains = (
        DEFAULT_PERSONAL_EMAIL_DOMAINS | _configured_extra_domains()
        if personal_domains is None
        else set(personal_domains)
    )
    return domain in domains


__all__ = ["DEFAULT_PERSONAL_EMAIL_DOMAINS", "is_personal_email"]
