"""Per-org LLM provider credentials (BYOK): an org admin on a self-hosted
install stores their own Anthropic / OpenAI / Bedrock credentials, and the
gateway calls the provider directly with them.

One row per (org, provider). Secrets are Fernet-encrypted via
``alkera_core.auth.secret_box`` (same box as the SSO client secret) and are
WRITE-ONLY through the API — reads expose only ``secret_hint`` (the last 4
characters, computed at write time so display never needs a decrypt).

``bedrock_auth_mode`` is ``"iam"`` (ambient credential chain / instance
profile — the recommended posture; only ``bedrock_region`` is stored) or
``"access_key"`` (a static IAM key pair, both halves encrypted). Bedrock
*bearer tokens* are deliberately NOT supported per-org: the gateway injects
them via a process-global env var, which cannot be scoped to one org.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, UniqueConstraint, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from alkera_core.db.base import Base


class ModelProviderConfig(Base):
    __tablename__ = "model_provider_configs"
    __table_args__ = (
        UniqueConstraint("org_team_id", "provider", name="uq_model_provider_configs_org_provider"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_team_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("teams.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # A Provider enum VALUE ("anthropic" | "openai" | "bedrock") — VARCHAR-backed
    # like every enum in this schema.
    provider: Mapped[str] = mapped_column(String(16), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")

    # --- Anthropic / OpenAI ---
    api_key_encrypted: Mapped[str | None] = mapped_column(String(4096), nullable=True)
    # None -> the provider's public default endpoint. Changing this on an
    # existing config requires re-entering the key (anti-exfiltration).
    base_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    openai_organization_id: Mapped[str | None] = mapped_column(String(256), nullable=True)

    # --- Bedrock ---
    bedrock_region: Mapped[str | None] = mapped_column(String(64), nullable=True)
    bedrock_auth_mode: Mapped[str | None] = mapped_column(String(16), nullable=True)
    aws_access_key_id_encrypted: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    aws_secret_access_key_encrypted: Mapped[str | None] = mapped_column(String(2048), nullable=True)

    # Display-only masked hint ("…abcd" — last 4 of the api key / access key id).
    # Never any secret material.
    secret_hint: Mapped[str | None] = mapped_column(String(16), nullable=True)

    # Outcome of the last admin-clicked connection test. Cleared whenever the
    # credential / endpoint / auth mode changes (a stale proof is no proof).
    last_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_verified_status: Mapped[str | None] = mapped_column(String(16), nullable=True)

    updated_by_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
