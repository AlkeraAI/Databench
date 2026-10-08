"""Wire shapes for adding a machine the org runs by its SSH details.

In-flight HTTP shapes only. The credential fields are ``SecretStr``: they never
appear in a ``repr``, a log line or a validation error, and no response shape
carries them (:class:`~alkera_core.schemas.org_machines.SshEndpointRead` is
all a reader gets back).
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field, SecretStr, StringConstraints, model_validator

from alkera_core.schemas.org_machines import AudienceGrant, MachineName, UseModeLiteral

SSH_HOST_MAX = 253
SSH_USERNAME_MAX = 64
#: The longest private key accepted, in characters (an RSA 16384-bit PEM fits).
SSH_PRIVATE_KEY_MAX = 16_384
SSH_PASSWORD_MAX = 1_024

SshHost = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=SSH_HOST_MAX)
]
SshUsername = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=SSH_USERNAME_MAX,
        pattern=r"^[A-Za-z0-9._][A-Za-z0-9._-]*$",
    ),
]


class SshMachineTarget(BaseModel):
    """A host and how to sign in to it."""

    host: SshHost
    port: int = Field(default=22, ge=1, le=65535)
    username: SshUsername
    auth_kind: Literal["password", "private_key"]
    password: SecretStr | None = Field(default=None, max_length=SSH_PASSWORD_MAX)
    private_key: SecretStr | None = Field(default=None, max_length=SSH_PRIVATE_KEY_MAX)
    passphrase: SecretStr | None = Field(default=None, max_length=SSH_PASSWORD_MAX)

    @model_validator(mode="after")
    def _credential_matches_kind(self) -> SshMachineTarget:
        secret = self.password if self.auth_kind == "password" else self.private_key
        if secret is None or not secret.get_secret_value():
            raise ValueError(
                "a password is required"
                if self.auth_kind == "password"
                else "a private key is required"
            )
        return self


class SshMachineAdd(SshMachineTarget):
    name: MachineName
    #: The fingerprint the admin confirmed in the test step. The add is refused
    #: when the host presents another key.
    host_key_fingerprint: str = Field(min_length=1, max_length=128)
    use_mode: UseModeLiteral = "assigned"
    audience: list[AudienceGrant] = Field(default_factory=list)
    idle_stop_minutes: int | None = Field(default=None, ge=5)


class SshMachineTestRead(BaseModel):
    """What a connection test found. ``reachable`` false carries the reason in
    ``error_code`` and ``message``; the facts are then empty."""

    reachable: bool
    host_key_fingerprint: str | None = None
    #: The OpenSSH name of the fingerprinted key's type (``ED25519``,
    #: ``ECDSA``, ``RSA``), shown beside the fingerprint so it can be checked
    #: against ``ssh-keygen -lf`` on the right key file.
    host_key_type: str | None = None
    os: str = ""
    arch: str = ""
    vcpu: int = 0
    memory_gb: int = 0
    disk_gb: int = 0
    gpu_count: int = 0
    prerequisites_met: bool = False
    #: What the host lacks to run a node, in words for the admin.
    missing: list[str] = Field(default_factory=list)
    error_code: str | None = None
    message: str | None = None


__all__ = [
    "SSH_HOST_MAX",
    "SSH_PASSWORD_MAX",
    "SSH_PRIVATE_KEY_MAX",
    "SSH_USERNAME_MAX",
    "SshMachineAdd",
    "SshMachineTarget",
    "SshMachineTestRead",
]
