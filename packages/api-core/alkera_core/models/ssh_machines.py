"""Where an added org machine is reached: one row per host an org attached by
its SSH details.

The credential (a password, or a private key and its passphrase) is held only
as ``secret_box`` ciphertext in ``secret_sealed`` and is cleared when the
machine is removed. ``host_key`` is the public key the admin confirmed when
they added the host; every later connection accepts that key and no other.

A tenant table: it carries ``org_team_id``, is listed in
:data:`alkera_core.db.row_security.CONTENT_TABLES`, and has row security
enabled and forced with the ``tenant_isolation`` policy. The pointer at its
org machine is a composite foreign key onto ``org_machines(id, org_team_id)``,
so an endpoint can never name another org's machine.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from alkera_core.db.base import Base

SSH_HOST_MAX = 253
SSH_USERNAME_MAX = 64
SSH_AUTH_KIND_VALUES: tuple[str, ...] = ("password", "private_key")


class SshMachineEndpoint(Base):
    __tablename__ = "ssh_machine_endpoints"
    __table_args__ = (
        CheckConstraint(
            f"auth_kind IN {SSH_AUTH_KIND_VALUES}", name="ck_ssh_machine_endpoints_auth_kind"
        ),
        CheckConstraint("port BETWEEN 1 AND 65535", name="ck_ssh_machine_endpoints_port"),
        ForeignKeyConstraint(
            ["org_machine_id", "org_team_id"],
            ["org_machines.id", "org_machines.org_team_id"],
            name="fk_ssh_machine_endpoints_org_machine_tenant",
            ondelete="CASCADE",
        ),
        Index("ux_ssh_machine_endpoints_org_machine_id", "org_machine_id", unique=True),
        Index("ix_ssh_machine_endpoints_org_team_id", "org_team_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_team_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("teams.id", ondelete="CASCADE", name="fk_ssh_machine_endpoints_org_team_id"),
        nullable=False,
    )
    org_machine_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    host: Mapped[str] = mapped_column(String(SSH_HOST_MAX), nullable=False)
    port: Mapped[int] = mapped_column(Integer, nullable=False)
    username: Mapped[str] = mapped_column(String(SSH_USERNAME_MAX), nullable=False)
    auth_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    # secret_box ciphertext of the credential document; '' once forgotten.
    secret_sealed: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    # The host's public key as OpenSSH writes it, and its SHA-256 fingerprint.
    host_key: Mapped[str] = mapped_column(Text, nullable=False)
    host_key_fingerprint: Mapped[str] = mapped_column(String(128), nullable=False)
    os: Mapped[str] = mapped_column(String(64), nullable=False, server_default="")
    arch: Mapped[str] = mapped_column(String(32), nullable=False, server_default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


__all__ = ["SSH_AUTH_KIND_VALUES", "SSH_HOST_MAX", "SSH_USERNAME_MAX", "SshMachineEndpoint"]
