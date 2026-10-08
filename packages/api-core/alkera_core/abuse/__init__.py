"""Abuse-prevention primitives shared by the backend and admin surfaces.

Currently: the disposable-email-domain blocklist. The list is VENDORED
(``disposable_domains.txt``, from the community-maintained
disposable-email-domains project) rather than pulled from PyPI — a data file
needs no dependency resolution and refreshes with a plain file replacement.
Loaded once per process into a frozenset; ~8k entries, sub-millisecond lookups.
"""

from __future__ import annotations

from functools import lru_cache
from importlib import resources


@lru_cache(maxsize=1)
def _blocklist() -> frozenset[str]:
    text = (
        resources.files("alkera_core.abuse")
        .joinpath("disposable_domains.txt")
        .read_text(encoding="utf-8")
    )
    return frozenset(
        line.strip().lower() for line in text.splitlines() if line.strip() and "." in line
    )


def is_disposable_domain(domain: str | None) -> bool:
    """True when ``domain`` (an email's domain part, any case) is a known
    disposable/throwaway mail provider. Unknown/empty domains are False —
    the blocklist refuses known-bad, it never vouches."""
    if not domain:
        return False
    candidate = domain.strip().lower().rstrip(".")
    if not candidate:
        return False
    # Match the registered domain AND any subdomain of a listed domain
    # (mail.tempsite.example is as disposable as tempsite.example).
    blocked = _blocklist()
    if candidate in blocked:
        return True
    parts = candidate.split(".")
    return any(".".join(parts[i:]) in blocked for i in range(1, len(parts) - 1))


def is_disposable_email(email: str | None) -> bool:
    """True when the address's domain part is a known disposable provider."""
    if not email or "@" not in email:
        return False
    return is_disposable_domain(email.rsplit("@", 1)[1])


__all__ = ["is_disposable_domain", "is_disposable_email"]
