"""The AWS variant: the S3 dialect plus the one thing only AWS gives us.

`AwsStore` differs from a generic endpoint in exactly one behaviour —
credential vending. STS `AssumeRole` with a **session tag** carrying the domain
id lets the role's trust policy scope a handed-out credential to a single
domain's prefix, so a client uploading directly to the store cannot read another
tenant's bytes even with a valid credential in hand. Every other endpoint
reports ``scoped_credentials=False`` and falls back to `proxied`.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import replace
from datetime import UTC, timedelta
from typing import Any

import aioboto3

from alkera_core.files.clock import Clock
from alkera_core.files.store.errors import InvalidKey
from alkera_core.files.store.keys import DOMAIN_PREFIX
from alkera_core.files.store.protocol import ScopedCredentials, StoreCapabilities
from alkera_core.files.store.s3_compatible import S3CompatibleStore, S3Config, s3_capabilities

StsFactory = Callable[[], AbstractAsyncContextManager[Any]]

__all__ = ["AwsStore", "aws_capabilities", "domain_of"]


def aws_capabilities(config: S3Config) -> StoreCapabilities:
    """S3-on-AWS: everything the generic driver has, plus the AWS-only features."""
    return replace(
        s3_capabilities(config),
        versioning=True,
        object_lock=True,
        lifecycle=True,
        scoped_credentials=True,
        storage_classes=True,
        kms=config.kms_key_id is not None,
    )


def domain_of(prefix: str) -> str:
    """The domain id a store prefix belongs to, for the session tag."""
    if not prefix.startswith(DOMAIN_PREFIX):
        msg = f"a scoped credential is vended per domain prefix (got {prefix!r})"
        raise InvalidKey(msg)
    domain = prefix[len(DOMAIN_PREFIX) :].split("/", 1)[0]
    if not domain:
        msg = f"prefix {prefix!r} names no domain"
        raise InvalidKey(msg)
    return domain


class AwsStore(S3CompatibleStore):
    """The driver selected when ``provider == "aws"``."""

    def __init__(
        self,
        config: S3Config,
        *,
        clock: Clock,
        sts_factory: StsFactory | None = None,
        **kwargs: Any,
    ) -> None:
        # This driver IS the bucket-wide admin handle: the keys it takes are
        # bucket-absolute (`domains/<id>/...`), exactly like the vended handles
        # it mints in ``client_for``. The base class defaults to the domain
        # layout a request-path handle speaks, and under that default the
        # readiness probe's absolute key was refused as "relative key may not
        # carry the domains/ prefix" — so no instance with Files on ever became
        # ready on AWS.
        kwargs.setdefault("layout", "bucket")
        super().__init__(config, clock=clock, **kwargs)
        self.capabilities = aws_capabilities(config)
        self._sts_factory = sts_factory or self._default_sts_factory

    def _default_sts_factory(self) -> AbstractAsyncContextManager[Any]:
        session = aioboto3.Session()
        client: AbstractAsyncContextManager[Any] = session.client(
            service_name="sts",
            region_name=self._config.region,
            aws_access_key_id=self._config.access_key,
            aws_secret_access_key=self._config.secret_key,
        )
        return client

    def client_for(self, credentials: ScopedCredentials) -> S3CompatibleStore:
        """A driver that speaks to the same bucket as ``credentials`` allows.

        Same endpoint, same region, same timeouts, same retry seams — only the
        credential differs, so the handle a request path gets behaves exactly
        like the bucket-wide one except that the store itself refuses every
        key outside the domain the session tag names. The keys it takes are
        bucket-absolute, because the vended prefix is a bucket prefix.
        """
        scoped_config = replace(
            self._config,
            access_key=credentials.access_key_id,
            secret_key=credentials.secret_access_key,
        )
        store = S3CompatibleStore(
            scoped_config,
            clock=self._clock,
            layout="bucket",
            # A test that injected a client factory injected it for the whole
            # driver, vended sessions included; production has none and gets a
            # real client carrying the session token.
            client_factory=self._injected_client_factory,
            sleep=self._sleep,
            policy=self._policy,
            budget=self._budget,
        )
        if self._injected_client_factory is None:
            store._client_factory = self._session_client_factory(credentials)
        store.capabilities = replace(self.capabilities, scoped_credentials=False)
        return store

    def _session_client_factory(
        self, credentials: ScopedCredentials
    ) -> Callable[[], AbstractAsyncContextManager[Any]]:
        """A client factory identical to this driver's but for the credential."""
        kwargs = {
            **self._client_kwargs(),
            "aws_access_key_id": credentials.access_key_id,
            "aws_secret_access_key": credentials.secret_access_key,
            "aws_session_token": credentials.session_token,
        }

        def factory() -> AbstractAsyncContextManager[Any]:
            session = aioboto3.Session()
            client: AbstractAsyncContextManager[Any] = session.client(**kwargs)
            return client

        return factory

    async def vend_scoped_credentials(
        self, prefix: str, *, ttl: timedelta, read_only: bool
    ) -> ScopedCredentials:
        role_arn = self._config.vend_role_arn
        if role_arn is None:
            msg = "vending scoped credentials needs files_vend_role_arn"
            raise NotImplementedError(msg)
        domain = domain_of(prefix)
        async with self._sts_factory() as sts:
            response = await sts.assume_role(
                RoleArn=role_arn,
                RoleSessionName=f"files-{domain}",
                DurationSeconds=int(ttl.total_seconds()),
                # The trust policy reads this tag; it is what scopes the
                # credential to one domain's prefix rather than the bucket.
                Tags=[{"Key": "domainId", "Value": domain}],
            )
        credentials = response["Credentials"]
        return ScopedCredentials(
            access_key_id=str(credentials["AccessKeyId"]),
            secret_access_key=str(credentials["SecretAccessKey"]),
            session_token=str(credentials["SessionToken"]),
            expires_at=credentials["Expiration"].astimezone(UTC),
            prefix=prefix,
            read_only=read_only,
        )
