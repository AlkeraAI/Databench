"""Small shared, dependency-free helpers used across apps."""

from __future__ import annotations

from alkera_core.utils.clock import make_monotonic_clock
from alkera_core.utils.email import email_domain

__all__ = ["email_domain", "make_monotonic_clock"]
