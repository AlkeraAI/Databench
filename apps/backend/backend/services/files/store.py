"""The process-wide store factory the request path borrows domain handles from.

A request never builds a driver: it asks :func:`store_factory` for the one
factory this process owns, and that factory for the caller's domain. Keeping it
process-wide is what makes the AWS session cache and the S3-compatible
credential lease worth having — a per-request factory would vend a fresh STS
session on every call — and keeping it behind :func:`set_store_factory` is what
lets a test point the whole backend at a filesystem store under ``tmp_path``
without a route knowing.
"""

from __future__ import annotations

from alkera_core.config import Settings
from alkera_core.config import settings as default_settings
from alkera_core.files.clock import Clock, SystemClock
from alkera_core.files.store.aws import AwsStore
from alkera_core.files.store.protocol import ObjectStore
from alkera_core.files.store.s3_compatible import S3CompatibleStore, S3Config
from alkera_core.files.store.scoped import (
    AwsScoped,
    FilesystemScoped,
    S3CompatConfig,
    S3CompatScoped,
    ScopedStoreFactory,
)

_factory: ScopedStoreFactory | None = None


def set_store_factory(factory: ScopedStoreFactory | None) -> None:
    """Install (or, with ``None``, drop) the factory this process vends from.

    The test seam. Dropping it on teardown is what stops the next test
    inheriting a handle rooted at a ``tmp_path`` that no longer exists.
    """
    global _factory
    _factory = factory


def store_factory(
    settings: Settings | None = None, *, clock: Clock | None = None
) -> ScopedStoreFactory:
    """The factory for this process, built once from settings on first use."""
    global _factory
    if _factory is None:
        _factory = build_store_factory(settings or default_settings, clock=clock or SystemClock())
    return _factory


def admin_store() -> ObjectStore:
    """This process's bucket-wide admin handle, which the health probes read."""
    return store_factory().admin()


def build_store_factory(settings: Settings, *, clock: Clock) -> ScopedStoreFactory:
    """A fresh factory for ``settings.files_store_provider``, nothing cached."""
    provider = settings.files_store_provider
    if provider == "filesystem":
        return FilesystemScoped(settings.files_store_root_path, clock=clock)
    config = S3Config(
        endpoint_url=settings.files_store_endpoint,
        region=settings.files_store_region,
        bucket=_required(settings.files_store_bucket, "files_store_bucket"),
        access_key=settings.files_store_access_key,
        secret_key=(
            settings.files_store_secret_key.get_secret_value()
            if settings.files_store_secret_key is not None
            else None
        ),
        addressing=settings.files_store_addressing,
        vend_role_arn=settings.files_vend_role_arn,
    )
    if provider == "aws":
        return AwsScoped(AwsStore(config, clock=clock), clock=clock)
    inner = S3CompatibleStore(config, clock=clock, layout="bucket")
    return S3CompatScoped(S3CompatConfig(store=inner), clock=clock)


def _required(value: str | None, name: str) -> str:
    if not value:
        raise RuntimeError(f"{name} must be set for this files_store_provider")
    return value


__all__ = ["build_store_factory", "set_store_factory", "store_factory"]
