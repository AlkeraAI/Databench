"""Password hashing via argon2-cffi (modern default).

`hash_password` returns a self-contained PHC string that includes algorithm,
parameters, salt, and hash — `verify_password` doesn't need those side-band.
Because the cost rides in the string, a hash written at one work factor still
verifies under another: changing `PASSWORD_HASH_PROFILE` re-costs new hashes and
leaves every stored credential readable.
"""

from __future__ import annotations

from functools import lru_cache
from typing import NamedTuple

from alkera_core.config import PasswordHashProfile, settings
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError


class _Argon2Cost(NamedTuple):
    """The three knobs that decide what one hash costs."""

    time_cost: int
    memory_cost: int
    parallelism: int


# `production` is argon2-cffi 25.x's own default (RFC 9106 low-memory), written
# out rather than inherited so a library upgrade cannot quietly change what a
# live login spends. `fast` is the floor argon2 accepts — 8 KiB, one pass, one
# lane — for test suites that hash and verify several passwords per test and
# measure nothing about the cost; production refuses to boot with it.
_COSTS: dict[PasswordHashProfile, _Argon2Cost] = {
    "production": _Argon2Cost(time_cost=3, memory_cost=65536, parallelism=4),
    "fast": _Argon2Cost(time_cost=1, memory_cost=8, parallelism=1),
}


@lru_cache(maxsize=len(_COSTS))
def _hasher(profile: PasswordHashProfile) -> PasswordHasher:
    cost = _COSTS[profile]
    return PasswordHasher(
        time_cost=cost.time_cost,
        memory_cost=cost.memory_cost,
        parallelism=cost.parallelism,
    )


def hash_password(plain: str) -> str:
    return _hasher(settings.password_hash_profile).hash(plain)


def verify_password(plain: str, hashed: str | None) -> bool:
    """Returns True iff `plain` matches `hashed`. Treats a None hash (no
    local password set) as a non-match — OAuth-only users can't log in via
    email/password until a password is assigned."""
    if hashed is None:
        return False
    try:
        _hasher(settings.password_hash_profile).verify(hashed, plain)
    except VerifyMismatchError:
        return False
    return True
