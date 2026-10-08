"""The key namespace the production store factory hands its driver.

An S3-compatible deployment is scoped in process: ``S3CompatScoped`` keeps one
bucket-wide driver and wraps it per domain in a ``PrefixGuard`` that prepends
``domains/<uuid>/``. Every key that reaches the driver — the admin handle's and
a domain handle's alike — is therefore bucket-absolute, and a driver opened on
the relative namespace refuses all of them before any request goes out. The
cases below address the factory the way production does and assert the key that
lands on the wire, so the layout cannot silently go back to the relative one.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta

import pytest
from alkera_core.config import Settings
from alkera_core.config import settings as process_settings
from alkera_core.files.clock import SystemClock
from alkera_core.files.ids import DomainId
from alkera_core.files.store import keys
from alkera_core.files.store.protocol import ObjectStore
from backend.services.files.store import build_store_factory
from blake3 import blake3
from pydantic import SecretStr

TTL = timedelta(minutes=5)
RELATIVE = "objects/aa/bb/aabbccdd"


def _s3_settings(**overrides: object) -> Settings:
    """Process settings with the whole store block spelled by the case."""
    return process_settings.model_copy(
        update={
            "files_enabled": True,
            "files_store_provider": "s3_compatible",
            "files_store_endpoint": "http://127.0.0.1:1/",
            "files_store_region": "us-east-1",
            "files_store_bucket": "alkera-files",
            "files_store_access_key": "alkera",
            "files_store_secret_key": SecretStr("alkera-dev-secret"),
            "files_store_addressing": "path",
            "files_vend_role_arn": None,
            **overrides,
        }
    )


def _signed_key(url: str) -> str:
    """The object key a presigned URL addresses, path-addressing bucket dropped."""
    without_query = url.split("?", 1)[0]
    after_scheme = without_query.split("://", 1)[1]
    return after_scheme.split("/", 2)[2]


def test_the_admin_handle_addresses_bucket_absolute_keys() -> None:
    """The janitor, scrub and fsck name ``domains/<uuid>/…``; a driver on the
    relative namespace refuses every one of those keys before any I/O."""
    factory = build_store_factory(_s3_settings(), clock=SystemClock())
    domain = DomainId(uuid.uuid4())

    signed = factory.admin().presign_get(keys.absolute(domain, RELATIVE), range=None, ttl=TTL)

    assert _signed_key(signed) == f"domains/{domain}/{RELATIVE}"


async def test_a_domain_handle_signs_the_prefixed_key_it_guards() -> None:
    """The per-domain handle prepends the domain prefix, so what reaches the
    driver is absolute there too — the request path breaks with a relative
    driver exactly as the janitor does."""
    factory = build_store_factory(_s3_settings(), clock=SystemClock())
    domain = DomainId(uuid.uuid4())

    handle = await factory.for_domain(domain)
    signed = handle.presign_get(RELATIVE, range=None, ttl=TTL)

    assert _signed_key(signed) == f"domains/{domain}/{RELATIVE}"


# -- live -------------------------------------------------------------------

live = pytest.mark.live

ENDPOINTS_ENV = "FILES_LIVE_ENDPOINTS"


def _live_settings() -> Settings:
    """The first S3-compatible row of ``FILES_LIVE_ENDPOINTS`` as store settings."""
    raw = os.environ.get(ENDPOINTS_ENV)
    if not raw:
        pytest.skip(f"{ENDPOINTS_ENV} is unset")
    try:
        parsed = json.loads(raw)
    except ValueError as exc:  # pragma: no cover - a malformed env var
        pytest.skip(f"{ENDPOINTS_ENV} is not JSON: {exc}")
    rows = [
        row
        for row in parsed
        if isinstance(row, dict) and row.get("provider", "s3_compatible") == "s3_compatible"
    ]
    if not rows:
        pytest.skip(f"{ENDPOINTS_ENV} names no s3_compatible endpoint")
    row = rows[0]
    return _s3_settings(
        files_store_endpoint=row.get("endpoint"),
        files_store_region=row.get("region", "us-east-1"),
        files_store_bucket=row["bucket"],
        files_store_access_key=row.get("access_key"),
        files_store_secret_key=(
            None if row.get("secret_key") is None else SecretStr(str(row["secret_key"]))
        ),
        files_store_addressing=row.get("addressing", "path"),
    )


@live
async def test_the_live_factory_puts_and_heads_through_a_domain_handle() -> None:
    """The blocker in one case: on a real endpoint the production factory must
    be able to write a domain's object and read it back through the admin
    handle. A driver on the wrong namespace refuses both before the wire."""
    factory = build_store_factory(_live_settings(), clock=SystemClock())
    domain = DomainId(uuid.uuid4())
    relative = f"incoming/{uuid.uuid4()}/probe"
    body = b"probe body"
    digest = blake3(body).digest()

    async def stream() -> AsyncIterator[bytes]:
        yield body

    handle = await factory.for_domain(domain)
    admin: ObjectStore = factory.admin()
    try:
        put = await handle.put(relative, stream(), size=len(body), checksum=digest)
        assert put.size == len(body)
        assert put.checksum == digest

        through_domain = await handle.head(relative)
        through_admin = await admin.head(keys.absolute(domain, relative))
        assert through_domain.size == len(body)
        # The admin handle reaches the very object the domain handle wrote, so
        # the janitor and the request path are addressing one namespace.
        assert through_admin.size == through_domain.size
        assert through_admin.etag == through_domain.etag
    finally:
        await admin.delete(keys.absolute(domain, relative))
