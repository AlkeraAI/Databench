"""Server secrets a self-hosted install lets the platform generate.

When ``GENERATED_SECRETS_DIR`` names a directory the install keeps (a volume
every process mounts), a server secret the operator left unset is generated
once, written there with mode 0600, and read back by every later boot and
every other process. The first writer wins: concurrent processes race on an
exclusive link, and the losers read the winner's value. Values are never
logged.

A deployment that sets no directory gets no generation, so a fleet without
shared storage cannot end up with one secret per replica: its production
validator still refuses the missing secret.
"""

from __future__ import annotations

import base64
import contextlib
import os
import secrets
from collections.abc import Callable
from pathlib import Path


def token() -> str:
    """A random URL-safe value of 64 bytes of entropy (86 characters)."""
    return secrets.token_urlsafe(64)


def fernet_key() -> str:
    """A random Fernet key: 32 bytes, URL-safe base64."""
    return base64.urlsafe_b64encode(os.urandom(32)).decode()


def read(directory: Path, name: str) -> str | None:
    """The value already persisted for ``name``, or None."""
    try:
        value = (directory / name).read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    return value or None


def load_or_create(directory: Path, name: str, make: Callable[[], str] = token) -> str:
    """The persisted value for ``name``, generating and persisting it first if absent."""
    existing = read(directory, name)
    if existing is not None:
        return existing
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    final = directory / name
    scratch = directory / f".{name}.{os.getpid()}.{secrets.token_hex(4)}"
    descriptor = os.open(scratch, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(make() + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        with contextlib.suppress(FileExistsError):
            os.link(scratch, final)
    finally:
        scratch.unlink(missing_ok=True)
    value = read(directory, name)
    if value is None:
        raise RuntimeError(f"{final} is empty; remove it so the secret can be generated again")
    return value
