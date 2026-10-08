"""Shared input-safety policies for values that leave the product.

``display_name`` is the one policy for human-typed names (org, team, person)
that get embedded in outbound email; ``password`` holds the pure half of the
credential-strength policy (the half with no vendored wordlist, so it stays out
of the Nuitka-compiled CLI binary -- see ``backend.auth.password_policy``).
"""

from __future__ import annotations

from alkera_core.validation.display_name import (
    MAX_DISPLAY_NAME_LENGTH,
    DisplayNameError,
    normalize_display_name,
    scrub_display_name,
    validate_display_name,
)
from alkera_core.validation.password import (
    PasswordPolicyError,
    check_password_shape,
)

__all__ = [
    "MAX_DISPLAY_NAME_LENGTH",
    "DisplayNameError",
    "PasswordPolicyError",
    "check_password_shape",
    "normalize_display_name",
    "scrub_display_name",
    "validate_display_name",
]
